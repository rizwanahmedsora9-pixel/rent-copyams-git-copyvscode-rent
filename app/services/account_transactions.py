"""Post the simple Receive / Pay / Transfer forms in the Accounts workspace.

This is an entry adapter, not a second ledger. Pay and Transfer post an
AccountTransaction through the same balance applier, period guard, journal
autoposting, idempotency index and audit trail as the existing Accounts entry
points. Receive additionally asks *who* paid:

* a **client** settles a real receivable, so it is delegated to
  ``save_client_payment`` — the client ledger, FIFO bill allocation, waive-off
  loss row and account receipt projection stay authoritative, and a
  discount-only entry (amount 0) posts a Waive-Off that moves no cash;
* a **party** (loan giver, lender, other outside party) is resolved against
  ``CashFlowParty``, registering a newly typed name for reuse;
* **other** keeps the original free-text counterparty.
"""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal, InvalidOperation

from sqlalchemy.exc import IntegrityError

from models import Account, AccountTransaction, CashFlowParty, db
from app.services.account_setup import ACCOUNT_TYPES, account_type_key, setup_accounts_query
from app.services.accounting import _account_minor, _apply_account_tx_effect
from app.services.payments_crud import _assert_period_open
from app.services.time_money import pk_now
from utils.accounting_audit import record_accounting_audit
from utils.money import from_minor, to_minor


MODES = {
    "receive": {"label": "Receive", "transaction_type": "Receipt", "message": "Receive transaction recorded successfully."},
    "pay": {"label": "Pay", "transaction_type": "Expense", "message": "Outgoing payment recorded successfully."},
    "transfer": {"label": "Transfer", "transaction_type": "Transfer", "message": "Transfer transaction recorded successfully."},
}

#: Who the money came from.  ``client`` settles a real receivable through the
#: client payment ledger (so dues, waive-offs and FIFO bill allocation stay
#: authoritative); ``party`` covers loan givers, lenders and other outside
#: parties; ``other`` is the plain free-text counterparty.
RECEIVE_SOURCES = {
    "client": {
        "label": "Client",
        "hint": "Search your client list. The amount settles their outstanding dues.",
        "message": "Client receipt recorded successfully.",
    },
    "party": {
        "label": "Party",
        "hint": "Loan givers, lenders, partners and other outside parties.",
        "message": "Party receipt recorded successfully.",
    },
    "other": {
        "label": "Other",
        "hint": "Anyone else - type the name you want on the record.",
        "message": "Receive transaction recorded successfully.",
    },
}
DEFAULT_RECEIVE_SOURCE = "other"
_DISCOUNT_NOT_ALLOWED = "A discount reduces what a client owes. Choose Client to record a discount."
# ``Payment.manual_bill_no`` is 50 characters wide and the normaliser prefixes
# "MB NO.", which leaves this much room for the typed reference.
_MAX_CLIENT_REFERENCE = 43
_MAX_AMOUNT_MINOR = 99_999_999_999_99


def _text(form, name, limit, label, *, required=False):
    value = str(form.get(name) or "").strip()
    if required and not value:
        raise ValueError(f"{label} is required.")
    if len(value) > limit or any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ValueError(f"{label} must be up to {limit} characters and cannot contain control characters.")
    return value


def _money_minor(raw, label, *, allow_zero=False):
    """Parse one money input into exact paisa, rejecting noise and overflow."""
    raw = (raw or "").strip().replace(",", "")
    if not raw and allow_zero:
        return 0
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Enter a valid {label.lower()}.") from exc
    if not amount.is_finite():
        raise ValueError(f"Enter a valid {label.lower()}.")
    if amount < 0:
        raise ValueError(f"{label} cannot be negative.")
    if amount == 0 and not allow_zero:
        raise ValueError(f"{label} must be greater than zero.")
    if amount.as_tuple().exponent < -2:
        raise ValueError(f"{label} can have at most two decimal places.")
    if amount > Decimal(_MAX_AMOUNT_MINOR) / 100:
        raise ValueError(f"{label} is too large.")
    return int(amount * 100)


def _amount_minor(form, *, allow_zero=False):
    return _money_minor(form.get("amount"), "Amount", allow_zero=allow_zero)


def _discount_minor(form):
    """General discount given while receiving.  Blank means no discount."""
    return _money_minor(form.get("discount"), "Discount", allow_zero=True)


def _receive_source(form):
    source = (form.get("receive_source") or "").strip().lower()
    if not source:
        return DEFAULT_RECEIVE_SOURCE
    if source == "account":
        return source
    if source not in RECEIVE_SOURCES:
        raise ValueError("Choose who the money was received from: a client, a party, or other.")
    return source


def _posted_at(form):
    from datetime import datetime

    raw = (form.get("date_posted") or "").strip()
    if not raw:
        return pk_now().replace(second=0, microsecond=0)
    try:
        posted = datetime.strptime(raw, "%Y-%m-%dT%H:%M" if len(raw) == 16 else "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise ValueError("Enter a valid transaction date and time.") from exc
    if posted > pk_now():
        raise ValueError("The transaction date cannot be in the future.")
    return posted


def _account(form, field, label):
    raw = str(form.get(field) or "").strip()
    try:
        account_id = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Select a {label} account.") from exc
    account = setup_accounts_query().filter(Account.id == account_id).first()
    if not account or account.is_active is False:
        raise ValueError(f"Select an active {label} account. Suspended accounts cannot be used for new transactions.")
    return account


def _category(form, mode):
    # Import lazily: the category catalog belongs to the Accounts blueprint,
    # while this service is also usable by tests and maintenance tools.
    from blueprints.accounts.transaction_tree import build_unified_category_catalog

    name = _text(form, "category", 120, "Category")
    child = _text(form, "subcategory", 120, "Subcategory")
    if not name:
        if child:
            raise ValueError("Select a category before a subcategory.")
        return None, None
    direction = "in" if mode == "receive" else "out"
    catalog = build_unified_category_catalog()["by_name"]
    match = next((item for key, item in catalog.items() if key.strip().lower() == name.lower()), None)
    if not match or match["direction"] not in (direction, "both"):
        raise ValueError("Select an active category for this transaction direction.")
    if child:
        canonical_child = next((item for item in match["subcategories"] if item.strip().lower() == child.lower()), None)
        if not canonical_child:
            raise ValueError("Select an active subcategory belonging to the selected category.")
        child = canonical_child
    return match["name"], child or None


def _receive_client(form):
    """The picked client, resolved by code with an exact-name fallback."""
    from app.services.payments_crud import _resolve_client

    picked = _text(form, "client_code", 160, "Client", required=True)
    client = _resolve_client(picked, picked)
    if not client:
        raise ValueError("Select a client from the search list.")
    if client.is_active is False:
        raise ValueError(f"{client.name} is suspended and cannot be used for a new transaction.")
    return client


def _client_payment_method(account):
    """Client payments speak Cash/Bank/Online; the workspace speaks account types."""
    if account is None:
        return "Cash"
    return {"cash": "Cash", "bank": "Bank", "wallet": "Online"}[account_type_key(account)]


def _party_type_labels():
    from app.services.cash_flow_svc import CF_PARTY_TYPES

    return dict(CF_PARTY_TYPES)


def _receive_party(form):
    """Resolve the outside party, registering a newly typed name for reuse.

    Loan givers, lenders and other one-off counterparties live in
    ``CashFlowParty`` so the picker stays data driven: picking a row reuses it,
    typing a new name adds it to the list for next time.
    """
    from app.services.cash_flow_svc import save_cf_party

    labels = _party_type_labels()
    party_type = (form.get("party_type") or "").strip().lower()
    if party_type and party_type not in labels:
        raise ValueError("Choose a valid party type.")
    raw_id = (form.get("party_id") or "").strip()
    if raw_id:
        try:
            party = db.session.get(CashFlowParty, int(raw_id))
        except (TypeError, ValueError) as exc:
            raise ValueError("Select a party from the search list.") from exc
        if not party or party.is_active is False:
            raise ValueError("Select an active party from the search list.")
        return party.name, (party.party_type or "other").strip().lower() or "other"
    name = _text(form, "party_name", 160, "Party name", required=True)
    party, _created = save_cf_party(name, party_type or "other")
    return party.name, (party.party_type or "other").strip().lower() or "other"


def _idempotency_key(form, actor):
    token = (form.get("idempotency_key") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", token):
        raise ValueError("This form has expired. Reload New Transaction and try again.")
    actor_id = str(getattr(actor, "id", "system"))
    return hashlib.sha256(f"workspace:{actor_id}:{token}".encode()).hexdigest()


def _same_payload(tx, values):
    stored = {
        "transaction_type": tx.transaction_type,
        "from_account_id": tx.from_account_id,
        "to_account_id": tx.to_account_id,
        "amount_minor": int(tx.amount_minor if tx.amount_minor is not None else to_minor(tx.amount)),
        "date_posted": tx.date_posted.replace(microsecond=0),
        "category": tx.category,
        "subcategory": tx.subcategory,
        "payee": tx.payee,
        "reference_no": tx.reference_no,
        "note": tx.note or "",
    }
    expected = {key: values.get(key) for key in stored}
    expected["date_posted"] = expected["date_posted"].replace(microsecond=0)
    return stored == expected


def _replay(key, values):
    tx = AccountTransaction.query.filter_by(idempotency_key=key).first()
    if tx and not _same_payload(tx, values):
        raise ValueError("This form was already used for a different transaction. Reload New Transaction before entering another.")
    return tx


def _post_account_transaction(values, *, actor, key):
    """Insert one AccountTransaction exactly once for this submission.

    Returns ``(transaction, created)``.  A retry of the same form returns the
    original row, including when it has subsequently been voided; it never
    posts a second movement or silently restores a voided transaction.
    """
    replay = _replay(key, values)
    if replay:
        return replay, False

    tx = AccountTransaction(**values)
    try:
        # The existing partial unique index arbitrates concurrent submissions.
        # A savepoint lets the loser read the winner's row without poisoning
        # the request session or applying the balance effect twice.
        with db.session.begin_nested():
            db.session.add(tx)
            db.session.flush()
    except IntegrityError:
        # after_flush may have queued the rolled-back row for journal posting.
        # Remove that queue item before returning the concurrent winner.
        from app.services.ledger.autopost import _pending
        _pending(db.session).pop(("AccountTransaction", tx.id), None)
        replay = _replay(key, values)
        if replay:
            return replay, False
        raise
    _apply_account_tx_effect(tx)
    record_accounting_audit(
        actor, action="Create", entity_type="AccountTransaction", entity_id=tx.id,
        after={**values, "id": tx.id, "entry_source": "accounts_workspace"},
        amount_after=tx.amount,
        account_before_id=tx.from_account_id, account_after_id=tx.to_account_id,
        reason=values.get("note") or "New Transaction workspace",
    )
    db.session.flush()
    return tx, True


def _save_client_receive(form, *, actor, key, posted, note, reference,
                         destination, amount_minor, discount_minor):
    """Settle a client's dues through the client payment ledger.

    Deliberately not a second receivable path: ``save_client_payment`` owns the
    client balance, FIFO bill allocation, waive-off loss row, account receipt
    projection, period guard and audit trail.  A discount-only entry posts a
    Waive-Off, so no cash moves and no destination account is needed.
    """
    from app.services.payments_crud import save_client_payment

    client = _receive_client(form)
    reason = _text(form, "discount_reason", 200, "Discount reason")
    if discount_minor > 0 and not reason:
        raise ValueError("Add a reason for the discount.")
    if amount_minor > 0 and destination is None:
        raise ValueError("Select a destination account.")
    # The reference becomes the receipt's bill/voucher number. That column holds
    # 50 characters and the normaliser adds a "MB NO." prefix, so refuse an
    # over-long one instead of quietly truncating what the user typed.
    if reference and len(reference) > _MAX_CLIENT_REFERENCE:
        raise ValueError(f"For a client receipt the reference must be up to {_MAX_CLIENT_REFERENCE} characters.")
    payment, created = save_client_payment(
        client_code=client.code, client_name=client.name,
        amount=float(from_minor(amount_minor)), discount=float(from_minor(discount_minor)),
        discount_reason=reason if discount_minor > 0 else "",
        payment_type="Receipt" if amount_minor > 0 else "Waive-Off",
        method=_client_payment_method(destination),
        payment_account_id=destination.id if destination else None,
        manual_bill_no=reference or "",
        date_posted=posted, note=note, actor=actor, idempotency_key=key,
    )
    return payment, created


def _save_receive(form, *, actor, key, posted, note, reference):
    """Money in: from a client (settles dues), a party, or a free-text name."""
    receive_source = _receive_source(form)
    discount_minor = _discount_minor(form)
    from_client = receive_source == "client"
    if discount_minor and not from_client:
        raise ValueError(_DISCOUNT_NOT_ALLOWED)
    # Only a client discount can stand on its own: it reduces a receivable
    # without any cash arriving, so both the amount and the account may be 0.
    minor = _amount_minor(form, allow_zero=from_client)
    if minor == 0 and not (from_client and discount_minor > 0):
        raise ValueError("Enter an amount to receive, or a discount." if from_client
                         else "Amount must be greater than zero.")
    destination = None if minor == 0 else _account(form, "receive_account_id", "destination")

    if receive_source == "client":
        return _save_client_receive(
            form, actor=actor, key=key, posted=posted, note=note, reference=reference,
            destination=destination, amount_minor=minor, discount_minor=discount_minor,
        )

    if receive_source == "account":
        group = _text(form, "receive_group", 100, "Transaction group", required=True)
        try:
            source_id = int(form.get("receive_from_account_id") or "")
        except (ValueError, TypeError):
            raise ValueError("Select a source entry from the chosen transaction group.")
        source = db.session.get(Account, source_id)
        if not source or source.is_active is False or (source.source_category or "").strip() != group:
            raise ValueError("Selected source entry does not belong to the chosen transaction group.")
        if source.id == destination.id:
            raise ValueError("Source and destination accounts must be different.")
        if group.lower() != "loan" and _account_minor(source) < minor:
            raise ValueError("Insufficient balance in selected source account.")
        for account in (source, destination):
            _assert_period_open(account.id, posted, operation="posted")
        return _post_account_transaction({
            "transaction_type": "Transfer", "from_account_id": source.id,
            "to_account_id": destination.id, "amount": float(from_minor(minor)),
            "amount_minor": minor, "description": f"Funds received from account {source.name}",
            "note": note, "category": group, "subcategory": None,
            "payee": source.name, "reference_no": reference,
            "payment_method": ACCOUNT_TYPES[account_type_key(destination)]["label"],
            "date_posted": posted, "created_by": getattr(actor, "username", None),
            "idempotency_key": key, "is_void": False,
        }, actor=actor, key=key)

    if receive_source == "party":
        party, party_type = _receive_party(form)
        description = f"Loan received from {party}" if party_type == "loan" else f"Money received from {party}"
    else:
        party = _text(form, "party", 160, "Received from", required=True)
        description = f"Money received from {party}"
    category, subcategory = _category(form, "receive")
    if destination:
        _assert_period_open(destination.id, posted, operation="posted")
    values = {
        "transaction_type": MODES["receive"]["transaction_type"],
        "from_account_id": None,
        "to_account_id": destination.id,
        "amount": float(from_minor(minor)),
        "amount_minor": minor,
        "description": description,
        "note": note,
        "category": category,
        "subcategory": subcategory,
        "payee": party,
        "reference_no": reference,
        "payment_method": ACCOUNT_TYPES[account_type_key(destination)]["label"],
        "date_posted": posted,
        "created_by": getattr(actor, "username", None),
        "idempotency_key": key,
        "is_void": False,
    }
    return _post_account_transaction(values, actor=actor, key=key)


def save_workspace_transaction(form, *, actor):
    """Validate and stage one movement. The caller owns the final commit.

    Returns ``(record, created)`` where ``record`` is the posted
    ``AccountTransaction``, or the ``Payment`` a client receive settles through.
    """
    mode = (form.get("tx_mode") or "receive").strip().lower()
    if mode not in MODES:
        raise ValueError("Choose Receive, Pay, or Transfer.")
    key = _idempotency_key(form, actor)
    posted = _posted_at(form)
    note = _text(form, "note", 500, "Note")
    reference = _text(form, "reference_no", 80, "Reference") or None

    if mode == "receive":
        return _save_receive(form, actor=actor, key=key, posted=posted, note=note, reference=reference)

    # Money out and transfers have no receivable to reduce, so a discount here
    # could only be dropped silently. Say so instead.
    if _discount_minor(form):
        raise ValueError(_DISCOUNT_NOT_ALLOWED)
    minor = _amount_minor(form)
    party = None
    if mode == "pay":
        source = _account(form, "pay_from_account_id", "source")
        destination = None
        target = (form.get("pay_target") or "expense").strip()
        if target not in ("supplier", "client_refund", "expense", "other"):
            raise ValueError("Select a valid payment type.")
        if target in ("supplier", "client_refund"):
            _assert_period_open(source.id, posted, operation="posted")
            if reference and len(reference) > _MAX_CLIENT_REFERENCE:
                raise ValueError("Bill / voucher reference must be up to 43 characters.")
            common = dict(amount=float(from_minor(minor)), method=_client_payment_method(source),
                payment_account_id=source.id, date_posted=posted, note=note,
                manual_bill_no=reference or "", actor=actor, idempotency_key=key)
            if target == "supplier":
                from models import Supplier
                from app.services.payments_crud import save_supplier_payment
                try:
                    supplier = db.session.get(Supplier, int(form.get("supplier_id") or ""))
                except (ValueError, TypeError):
                    raise ValueError("Select a supplier from the search list.")
                if not supplier or supplier.is_active is False:
                    raise ValueError("Select an active supplier from the search list.")
                return save_supplier_payment(supplier_id=supplier.id, **common)
            from app.services.payments_crud import save_client_payment
            client = _receive_client({"client_code": form.get("refund_client_code")})
            return save_client_payment(client_code=client.code, client_name=client.name,
                payment_type="Refund", discount=0, **common)
        party = _text(form, "party", 160, "Paid to", required=True)
        category, subcategory = _category(form, mode)
        description = f"Payment to {party}"
    else:
        source = _account(form, "transfer_from_account_id", "source")
        destination = _account(form, "transfer_to_account_id", "destination")
        if source.id == destination.id:
            raise ValueError("Source and destination accounts must be different.")
        category, subcategory = "Internal Transfer", None
        description = "Intra-company transfer"

    if _account_minor(source) < minor:
        raise ValueError(f"Insufficient balance in {source.name}. Available: {float(from_minor(_account_minor(source))):,.2f}.")
    for account in (source, destination):
        if account:
            _assert_period_open(account.id, posted, operation="posted")

    account = source or destination
    values = {
        "transaction_type": MODES[mode]["transaction_type"],
        "from_account_id": source.id if source else None,
        "to_account_id": destination.id if destination else None,
        "amount": float(from_minor(minor)),
        "amount_minor": minor,
        "description": description,
        "note": note,
        "category": category,
        "subcategory": subcategory,
        "payee": party,
        "reference_no": reference,
        "payment_method": ACCOUNT_TYPES[account_type_key(account)]["label"],
        "date_posted": posted,
        "created_by": getattr(actor, "username", None),
        "idempotency_key": key,
        "is_void": False,
    }
    return _post_account_transaction(values, actor=actor, key=key)


def success_message(form, *, created):
    """Flash wording for one saved submission, per mode and receive source."""
    if not created:
        return "This transaction was already saved. No duplicate was recorded."
    mode = (form.get("tx_mode") or "receive").strip().lower()
    if mode != "receive":
        if mode == "pay":
            target = form.get("pay_target")
            if target == "supplier":
                return "Supplier payment recorded successfully."
            if target == "client_refund":
                return "Client refund recorded successfully."
        return MODES.get(mode, MODES["receive"])["message"]
    try:
        source = _receive_source(form)
    except ValueError:
        source = DEFAULT_RECEIVE_SOURCE
    return RECEIVE_SOURCES.get(source, RECEIVE_SOURCES["other"])["message"]
