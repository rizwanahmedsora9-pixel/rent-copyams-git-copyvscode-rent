"""Post the simple Receive / Pay / Transfer forms in the Accounts workspace.

This is an entry adapter, not a second ledger: movements use AccountTransaction
and the same balance applier, period guard, journal autoposting and audit trail
as the existing Accounts entry points. Party-ledger payments keep their existing
specialised services; this form deliberately moves money only between real
business accounts and external counterparties.
"""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal, InvalidOperation

from sqlalchemy.exc import IntegrityError

from models import Account, AccountTransaction, db
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
_MAX_AMOUNT_MINOR = 99_999_999_999_99


def _text(form, name, limit, label, *, required=False):
    value = str(form.get(name) or "").strip()
    if required and not value:
        raise ValueError(f"{label} is required.")
    if len(value) > limit or any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ValueError(f"{label} must be up to {limit} characters and cannot contain control characters.")
    return value


def _amount_minor(form):
    raw = (form.get("amount") or "").strip().replace(",", "")
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Enter a valid amount.") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Amount must be greater than zero.")
    if amount.as_tuple().exponent < -2:
        raise ValueError("Amount can have at most two decimal places.")
    if amount > Decimal(_MAX_AMOUNT_MINOR) / 100:
        raise ValueError("Amount is too large.")
    minor = int(amount * 100)
    return minor


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


def save_workspace_transaction(form, *, actor):
    """Validate and stage one movement. The caller owns the final commit.

    Returns (transaction, created). A retry of the same form returns the
    original row, including when it has subsequently been voided; it never
    posts a second movement or silently restores a voided transaction.
    """
    mode = (form.get("tx_mode") or "receive").strip().lower()
    if mode not in MODES:
        raise ValueError("Choose Receive, Pay, or Transfer.")
    key = _idempotency_key(form, actor)
    minor = _amount_minor(form)
    posted = _posted_at(form)
    note = _text(form, "note", 500, "Note")
    reference = _text(form, "reference_no", 80, "Reference") or None
    party = None
    category = None
    subcategory = None
    source = destination = None

    if mode == "receive":
        destination = _account(form, "receive_account_id", "destination")
        party = _text(form, "party", 160, "Received from", required=True)
        category, subcategory = _category(form, mode)
        description = f"Money received from {party}"
    elif mode == "pay":
        source = _account(form, "pay_from_account_id", "source")
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

    if source:
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
        reason=note or "New Transaction workspace",
    )
    db.session.flush()
    return tx, True
