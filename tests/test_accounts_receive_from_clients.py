"""New Transaction → Receive: clients, parties, payable amounts and discounts.

The receive form is the money-in door for the whole business, so it has to
answer three questions before it saves anything: who is paying, what do they
already owe, and how much of that is being settled in cash versus waived as a
discount.  These tests pin that behaviour down end to end — picker data, the
payable lookup, the client-ledger posting, discount-only waive-offs, reusable
parties (loan givers), and the refusals that keep the books honest.
"""
from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from werkzeug.security import generate_password_hash

from models import Account, AccountTransaction, CashFlowParty, Client, Payment, User, db
from app.services.cash_flow_svc import collect_cash_flow_rows
from app.services.finance_clients import _client_balance_as_of
from app.services.time_money import pk_now

NEW_TRANSACTION = "/accounts/transactions/new"
DUES = "/accounts/transactions/client-dues"


def login(client, username="Admin", password="Admin@fbm12345"):
    response = client.post("/login", data={"username": username, "password": password})
    assert response.status_code in (302, 303)


def raw_login(app, username, password):
    """A bare test client that still satisfies the session-bound CSRF gate."""
    raw = app.test_client()
    raw.get("/login")
    with raw.session_transaction() as session:
        token = session["_csrf_token"]
    response = raw.post("/login", data={"username": username, "password": password, "_csrf_token": token})
    assert response.status_code in (302, 303)
    return raw


def account(name="Till", kind="cash", opening="1000.00", number=None):
    from app.services import account_setup as setup
    numbers = {"bank": "PK00 MEZN 0001", "wallet": "03001234567"}
    row, _ = setup.save_account({
        "name": name, "account_type": kind,
        "account_number": number if number is not None else numbers.get(kind, ""),
        "opening_balance": opening,
    }, actor=None)
    db.session.flush()
    return row


def client_row(code="C-001", name="Ali Traders", opening=0, active=True, phone=""):
    row = Client(code=code, name=name, phone=phone, opening_balance=opening,
                 opening_balance_date=pk_now(), is_active=active)
    db.session.add(row)
    db.session.flush()
    return row


def payload(mode="receive", source="client", **overrides):
    data = {
        "entry_source": "accounts_workspace", "tx_mode": mode, "receive_source": source,
        "idempotency_key": uuid4().hex, "amount": "1000",
        "date_posted": pk_now().strftime("%Y-%m-%dT%H:%M"),
    }
    data.update(overrides)
    return data


def post(client, data):
    return client.post(NEW_TRANSACTION, data=data)


def dues(client, code):
    return client.get(f"{DUES}?client_code={code}").get_json()


# ---------------------------------------------------------------------------
# The picker is data driven, and the payable under it is the ledger's answer
# ---------------------------------------------------------------------------

def test_receive_form_offers_clients_parties_and_other_sources(app, client):
    with app.app_context():
        account()
        client_row(code="C-001", name="Ali Traders", phone="03001112222")
        client_row(code="C-002", name="Bilal Steel Works")
        client_row(code="C-999", name="Suspended Client", active=False)
        db.session.add(CashFlowParty(name="Haji Abdul", party_type="loan", is_active=True))
        db.session.add(CashFlowParty(name="Retired Party", party_type="other", is_active=False))
        db.session.commit()
    login(client)
    text = client.get(NEW_TRANSACTION).get_data(as_text=True)
    # All three receive sources are offered, with the client list first.
    for source in ("client", "party", "other"):
        assert f'data-tx-source-panel="{source}"' in text
    assert 'id="tx-receive-group"' in text
    assert "Client Ledger" in text
    # Active clients and parties are searchable; suspended rows are not offered.
    assert "Ali Traders" in text and "Bilal Steel Works" in text and "Haji Abdul" in text
    assert "Suspended Client" not in text and "Retired Party" not in text
    assert 'id="tx-client-search"' in text and 'name="client_code"' in text
    assert 'id="tx-party-search"' in text and 'name="party_id"' in text and 'name="party_name"' in text
    assert 'id="tx-discount"' in text and 'name="discount_reason"' in text
    # The picker data and the dues endpoint are handed to the page script.
    assert '"clientDuesUrl": "/accounts/transactions/client-dues"' in text
    assert '"receiveSources"' in text and '"C-001"' in text
    # Client is the default source: the common case needs no extra click.
    assert 'id="tx-receive-source" value="client"' in text


def test_dues_endpoint_reports_the_ledger_payable(app, client):
    with app.app_context():
        account()
        client_row(code="C-001", name="Ali Traders", opening=18500.50)
        client_row(code="C-002", name="Bilal Steel Works", opening=0)
        client_row(code="C-003", name="Chaudhry Builders", opening=-2500)
        db.session.commit()
    login(client)
    owing_payload = dues(client, "C-001")
    assert owing_payload["found"] is True and owing_payload["kind"] == "client"
    assert owing_payload["name"] == "Ali Traders"
    assert owing_payload["balance"] == 18500.50
    assert owing_payload["payable"] == 18500.50 and owing_payload["advance"] == 0.0
    assert owing_payload["status"] == "payable"
    settled_payload = dues(client, "C-002")
    assert settled_payload["payable"] == 0.0 and settled_payload["status"] == "settled"
    advance_payload = dues(client, "C-003")
    assert advance_payload["payable"] == 0.0 and advance_payload["advance"] == 2500.0
    assert advance_payload["status"] == "advance"
    # An unknown code is a clean miss, never a 500 or a guessed zero.
    missing = dues(client, "NOPE")
    assert missing == {"found": False}
    response = client.get(f"{DUES}?client_code=C-001")
    assert response.headers["Cache-Control"].startswith("no-store")
    # The endpoint reports the same number the client ledger itself projects.
    with app.app_context():
        owing = Client.query.filter_by(code="C-001").one()
        assert _client_balance_as_of(owing) == 18500.50


def test_dues_endpoint_needs_a_login_and_workspace_access(app):
    with app.app_context():
        account()
        client_row(code="C-001", name="Ali Traders", opening=500)
        outsider = User(username="outsider", password_hash=generate_password_hash("Outsider@12345"),
                        role="user", can_manage_accounts=False, can_manage_payments=False,
                        can_manage_account_masters=False, can_view_cash_flow=False,
                        can_view_client_ledger=False)
        db.session.add(outsider)
        db.session.commit()
    anonymous = app.test_client().get(f"{DUES}?client_code=C-001")
    assert anonymous.status_code in (302, 401)
    assert "/login" in anonymous.headers.get("Location", "")
    raw = raw_login(app, "outsider", "Outsider@12345")
    denied = raw.get(f"{DUES}?client_code=C-001")
    # The Accounts permission hook answers with its usual redirect home; either
    # way no balance is leaked to a user without Accounts or ledger access.
    assert denied.status_code in (302, 403)
    assert "Ali Traders" not in denied.get_data(as_text=True)
    assert "18500" not in denied.get_data(as_text=True).replace(",", "")


# ---------------------------------------------------------------------------
# Receiving from a client settles the client ledger, not a side table
# ---------------------------------------------------------------------------

def test_client_receipt_with_discount_settles_dues_and_credits_the_account(app, client):
    with app.app_context():
        cash_id = account().id
        row = client_row(code="C-001", name="Ali Traders", opening=5000)
        client_id = row.id
        db.session.commit()
    login(client)
    response = post(client, payload(receive_account_id=cash_id, client_code="C-001",
                                    amount="1000.50", discount="500.25",
                                    discount_reason="Loyal customer", reference_no="RCPT-9"))
    assert response.status_code == 302
    assert response.headers["Location"].endswith(NEW_TRANSACTION)
    with app.app_context():
        payment = Payment.query.one()
        assert payment.client_id == client_id
        assert payment.amount_minor == 100050 and payment.discount_minor == 50025
        assert payment.payment_type == "Receipt" and payment.method == "Cash"
        assert payment.payment_account_id == cash_id
        assert payment.discount_reason == "Loyal customer"
        assert payment.manual_bill_no == "MB NO.RCPT-9"
        assert len(payment.manual_bill_no) <= 50
        assert db.session.get(Account, cash_id).balance_minor == 200050
        # The receivable fell by cash + discount, exactly as the picker promised.
        # 5,000.00 opening - 1,000.50 cash - 500.25 waived = 3,499.25 still due.
        assert round(_client_balance_as_of(db.session.get(Client, client_id)), 2) == 3499.25
    assert dues(client, "C-001")["payable"] == 3499.25
    with app.app_context():
        today = pk_now().strftime("%Y-%m-%d")
        rows = [row for row in collect_cash_flow_rows(today, today) if row["type"] == "in"]
    assert [(row["party_name"], row["cash_in"]) for row in rows] == [("Ali Traders", 1000.5)]


def test_discount_only_receive_allows_zero_amount_and_no_account(app, client):
    with app.app_context():
        cash_id = account().id
        row = client_row(code="C-001", name="Ali Traders", opening=5000)
        client_id = row.id
        db.session.commit()
    login(client)
    response = post(client, payload(receive_account_id="", client_code="C-001", amount="0",
                                    discount="5000", discount_reason="Goodwill settlement"))
    assert response.status_code == 302
    with app.app_context():
        payment = Payment.query.one()
        assert payment.payment_type == "Waive-Off"
        assert payment.amount_minor == 0 and payment.discount_minor == 500000
        # No cash arrived, so no account is attached and no balance moved.
        assert payment.payment_account_id is None
        assert db.session.get(Account, cash_id).balance_minor == 100000
        assert _client_balance_as_of(db.session.get(Client, client_id)) == 0
        # The waive-off is still projected as a loss row, never as a receipt.
        assert not AccountTransaction.query.filter_by(transaction_type="Receipt").all()
        assert [tx.description for tx in AccountTransaction.query.all() if not tx.from_account_id and not tx.to_account_id] \
            == ["Waive-off loss for Ali Traders (SB-CP-1000)"]
    assert dues(client, "C-001")["status"] == "settled"


def test_a_bank_or_wallet_receive_uses_a_method_the_account_accepts(app, client):
    with app.app_context():
        bank_id = account("Meezan Bank", "bank", "0").id
        wallet_id = account("JazzCash", "wallet", "0").id
        client_row(code="C-001", name="Ali Traders", opening=5000)
        db.session.commit()
    login(client)
    assert post(client, payload(receive_account_id=bank_id, client_code="C-001", amount="700")).status_code == 302
    assert post(client, payload(receive_account_id=wallet_id, client_code="C-001", amount="300")).status_code == 302
    with app.app_context():
        methods = {p.method for p in Payment.query.all()}
        assert methods == {"Bank", "Online"}
        assert db.session.get(Account, bank_id).balance_minor == 70000
        assert db.session.get(Account, wallet_id).balance_minor == 30000


def test_client_receipt_is_idempotent_and_rejects_a_changed_replay(app, client):
    with app.app_context():
        cash_id = account().id
        client_row(code="C-001", name="Ali Traders", opening=5000)
        db.session.commit()
    login(client)
    data = payload(receive_account_id=cash_id, client_code="C-001", amount="250",
                   discount="50", discount_reason="Rounded off")
    assert post(client, data).status_code == 302
    replayed = post(client, data)
    assert replayed.status_code == 302
    with app.app_context():
        assert Payment.query.count() == 1
        assert db.session.get(Account, cash_id).balance_minor == 125000
    # Same token, different money: refused rather than silently overwritten.
    changed = post(client, {**data, "amount": "999"})
    assert changed.status_code == 422
    assert b"different payment" in changed.data
    with app.app_context():
        assert Payment.query.count() == 1
        assert db.session.get(Account, cash_id).balance_minor == 125000


# ---------------------------------------------------------------------------
# Receiving from parties: loan givers and other outside counterparties
# ---------------------------------------------------------------------------

def test_party_receive_from_a_loan_giver_registers_a_reusable_party(app, client):
    with app.app_context():
        cash_id = account().id
        db.session.commit()
    login(client)
    response = post(client, payload(source="party", receive_account_id=cash_id,
                                    party_name="Haji Abdul", party_type="loan",
                                    amount="250000", note="Qarz from family"))
    assert response.status_code == 302
    with app.app_context():
        party = CashFlowParty.query.one()
        assert party.name == "Haji Abdul" and party.party_type == "loan" and party.is_active
        tx = AccountTransaction.query.filter_by(transaction_type="Receipt").one()
        assert tx.payee == "Haji Abdul"
        assert tx.description == "Loan received from Haji Abdul"
        assert tx.amount_minor == 25000000 and tx.to_account_id == cash_id
        assert tx.note == "Qarz from family"
        assert db.session.get(Account, cash_id).balance_minor == 25100000
    # The saved party is offered next time, so the list really is data driven.
    text = client.get(NEW_TRANSACTION).get_data(as_text=True)
    assert "Haji Abdul" in text and '"party_type": "loan"' in text


def test_picking_a_saved_party_reuses_it_and_ignores_a_typed_type(app, client):
    with app.app_context():
        cash_id = account().id
        party = CashFlowParty(name="Haji Abdul", party_type="loan", is_active=True)
        db.session.add(party)
        db.session.commit()
        party_id = party.id
    login(client)
    response = post(client, payload(source="party", receive_account_id=cash_id,
                                    party_id=str(party_id), party_name="", party_type="person",
                                    amount="5000"))
    assert response.status_code == 302
    with app.app_context():
        assert CashFlowParty.query.count() == 1
        tx = AccountTransaction.query.filter_by(transaction_type="Receipt").one()
        # The stored type wins over whatever the form still carried.
        assert tx.description == "Loan received from Haji Abdul"
        assert tx.payee == "Haji Abdul"


def test_a_non_loan_party_is_recorded_as_money_received(app, client):
    with app.app_context():
        cash_id = account().id
        db.session.commit()
    login(client)
    assert post(client, payload(source="party", receive_account_id=cash_id,
                                party_name="Usman Outsider", party_type="outsider",
                                amount="1500")).status_code == 302
    with app.app_context():
        assert CashFlowParty.query.one().party_type == "outsider"
        tx = AccountTransaction.query.filter_by(transaction_type="Receipt").one()
        assert tx.description == "Money received from Usman Outsider"
    with app.app_context():
        today = pk_now().strftime("%Y-%m-%d")
        rows = [row for row in collect_cash_flow_rows(today, today) if row["type"] == "in"]
    assert rows[0]["party_name"] == "Usman Outsider"


def test_free_text_other_receive_still_works_without_a_source_field(app, client):
    """The plain counterparty path predates the picker and must keep working."""
    with app.app_context():
        cash_id = account().id
        db.session.commit()
    login(client)
    data = {
        "entry_source": "accounts_workspace", "tx_mode": "receive",
        "idempotency_key": uuid4().hex, "amount": "321.10", "party": "Walk-in customer",
        "date_posted": pk_now().strftime("%Y-%m-%dT%H:%M"), "receive_account_id": str(cash_id),
    }
    assert post(client, data).status_code == 302
    with app.app_context():
        assert AccountTransaction.query.one().payee == "Walk-in customer"
        assert CashFlowParty.query.count() == 0
        assert Payment.query.count() == 0


# ---------------------------------------------------------------------------
# Refusals: the form must not be able to post a nonsense receive
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("overrides, expected", [
    ({"client_code": ""}, b"Client is required"),
    ({"client_code": "NOPE"}, b"Select a client from the search list"),
    ({"client_code": "C-001", "amount": "0", "discount": "0"}, b"Enter an amount to receive, or a discount"),
    ({"client_code": "C-001", "amount": "100", "discount": ""}, b""),
    ({"client_code": "C-001", "amount": "100", "discount": "10"}, b"reason"),
    ({"client_code": "C-001", "amount": "100", "discount": "-5"}, b"cannot be negative"),
    ({"client_code": "C-001", "amount": "100", "discount": "1.234"}, b"two decimal places"),
    ({"client_code": "C-001", "amount": "100", "receive_account_id": ""}, b"Select a destination account"),
    ({"client_code": "C-001", "amount": "100", "receive_account_id": 999999}, b"Select an active destination account"),
    # The reference becomes the receipt's bill number, which has room for 43.
    ({"client_code": "C-001", "amount": "100", "reference_no": "R" * 44}, b"up to 43 characters"),
])
def test_client_receive_refusals(app, client, overrides, expected):
    with app.app_context():
        cash_id = account().id
        client_row(code="C-001", name="Ali Traders", opening=5000)
        db.session.commit()
    login(client)
    data = {**payload(receive_account_id=cash_id, discount_reason=""), **overrides}
    response = post(client, data)
    if expected:
        assert response.status_code == 422, data
        assert expected in response.data
    else:
        assert response.status_code == 302, data
    with app.app_context():
        assert Payment.query.count() == (0 if expected else 1)
        assert db.session.get(Account, cash_id).balance_minor == (100000 if expected else 110000)


def test_suspended_client_cannot_receive(app, client):
    with app.app_context():
        cash_id = account().id
        client_row(code="C-001", name="Ali Traders", opening=5000, active=False)
        db.session.commit()
    login(client)
    response = post(client, payload(receive_account_id=cash_id, client_code="C-001", amount="100"))
    assert response.status_code == 422
    assert b"suspended" in response.data
    with app.app_context():
        assert Payment.query.count() == 0


@pytest.mark.parametrize("overrides, expected", [
    ({"party_name": "", "party_id": ""}, b"Party name is required"),
    ({"party_id": "999999"}, b"Select an active party"),
    ({"party_name": "Lender", "party_type": "wizard"}, b"Choose a valid party type"),
    ({"party_name": "Lender", "amount": "0"}, b"Amount must be greater than zero"),
    ({"party_name": "Lender", "amount": "100", "discount": "10"}, b"Choose Client to record a discount"),
    ({"party_name": "Lender", "amount": "100", "receive_account_id": ""}, b"Select a destination account"),
])
def test_party_receive_refusals(app, client, overrides, expected):
    with app.app_context():
        cash_id = account().id
        db.session.commit()
    login(client)
    data = {**payload(source="party", receive_account_id=cash_id, party_type="loan"), **overrides}
    response = post(client, data)
    assert response.status_code == 422, overrides
    assert expected in response.data
    with app.app_context():
        assert AccountTransaction.query.count() == 0
        assert CashFlowParty.query.count() == 0
        assert db.session.get(Account, cash_id).balance_minor == 100000


def test_discount_is_not_available_outside_a_client_receive(app, client):
    with app.app_context():
        cash_id = account().id
        db.session.commit()
    login(client)
    free_text = post(client, payload(source="other", receive_account_id=cash_id,
                                     party="Walk-in", amount="100", discount="10"))
    assert free_text.status_code == 422
    assert b"Choose Client to record a discount" in free_text.data
    outgoing = post(client, payload("pay", source="other", pay_from_account_id=cash_id,
                                    party="Supplier", amount="100", discount="10"))
    assert outgoing.status_code == 422
    with app.app_context():
        assert AccountTransaction.query.count() == 0
        assert db.session.get(Account, cash_id).balance_minor == 100000


# ---------------------------------------------------------------------------
# A refused submit must hand the picker state back to the form
# ---------------------------------------------------------------------------

def test_validation_error_keeps_the_source_picker_and_discount(app, client):
    with app.app_context():
        cash_id = account().id
        client_row(code="C-001", name="Ali Traders", opening=5000)
        db.session.commit()
    login(client)
    data = payload(receive_account_id=cash_id, client_code="C-001", amount="100",
                   discount="25", discount_reason="Rounded off")
    response = post(client, {**data, "amount": "-5"})
    assert response.status_code == 422
    text = response.get_data(as_text=True)
    assert 'id="tx-receive-source" value="client"' in text
    assert 'id="tx-client-code" data-picker-value value="C-001"' in text
    assert 'id="tx-discount"' in text and 'value="25"' in text
    assert 'value="Rounded off"' in text
    # The token is unchanged, so correcting the amount cannot double-post.
    assert data["idempotency_key"] in text
    assert post(client, data).status_code == 302
    with app.app_context():
        assert Payment.query.count() == 1


def test_locked_period_blocks_a_client_receive(app, client):
    from app.services.ledger.periods import get_or_create_period, lock_period
    with app.app_context():
        cash_id = account().id
        client_row(code="C-001", name="Ali Traders", opening=5000)
        db.session.commit()
    with app.app_context():
        period = get_or_create_period(pk_now().date())
        lock_period(period, actor="Admin")
        db.session.commit()
    login(client)
    response = post(client, payload(receive_account_id=cash_id, client_code="C-001", amount="100"))
    assert response.status_code == 422
    assert b"locked" in response.data
    with app.app_context():
        assert Payment.query.count() == 0
        assert db.session.get(Account, cash_id).balance_minor == 100000


def test_future_dated_client_receive_is_refused(app, client):
    with app.app_context():
        cash_id = account().id
        client_row(code="C-001", name="Ali Traders", opening=5000)
        db.session.commit()
    login(client)
    future = (pk_now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    response = post(client, payload(receive_account_id=cash_id, client_code="C-001",
                                    amount="100", date_posted=future))
    assert response.status_code == 422
    assert b"future" in response.data
    with app.app_context():
        assert Payment.query.count() == 0


# ---------------------------------------------------------------------------
# The client payment form token itself (shared with the Client Payments page)
# ---------------------------------------------------------------------------

def payment_form(cash_id, key, **overrides):
    data = {
        "payment_id": "", "revision": "", "show": "active", "idempotency_key": key,
        "client_code": "C-001", "client_name": "Ali Traders", "amount": "400",
        "discount": "0", "discount_reason": "", "payment_type": "Receipt", "method": "Cash",
        "payment_account_id": str(cash_id), "manual_bill_no": "", "note": "",
        "photo_url": "", "date": pk_now().strftime("%Y-%m-%d"),
    }
    data.update(overrides)
    return data


def test_client_payment_form_token_replays_instead_of_failing(app, client):
    """A create carrying a form token used to die on ``payment.client_code``.

    ``Payment`` has no ``client_code`` column, so fingerprinting the stored row
    raised AttributeError, and a Refund's sign-normalised amount never matched
    the submitted fingerprint either — a legitimate retry was rejected as "a
    different payment". Both now fingerprint what was actually submitted.
    """
    with app.app_context():
        cash_id = account(opening="5000").id
        client_row(code="C-001", name="Ali Traders", opening=5000)
        db.session.commit()
    login(client)
    key = uuid4().hex
    first = client.post("/accounts/payments/clients/save", data=payment_form(cash_id, key))
    assert first.status_code == 302
    replay = client.post("/accounts/payments/clients/save", data=payment_form(cash_id, key),
                         follow_redirects=True)
    assert b"already processed" in replay.data
    with app.app_context():
        assert Payment.query.count() == 1
        assert db.session.get(Account, cash_id).balance_minor == 540000


def test_refund_retry_is_recognised_as_a_replay(app, client):
    """A refund stores a negative amount; the retry submits the positive one."""
    with app.app_context():
        cash_id = account(opening="5000").id
        client_row(code="C-001", name="Ali Traders", opening=0)
        db.session.commit()
    login(client)
    key = uuid4().hex
    data = payment_form(cash_id, key, payment_type="Refund", amount="250")
    assert client.post("/accounts/payments/clients/save", data=data).status_code == 302
    replay = client.post("/accounts/payments/clients/save", data=data, follow_redirects=True)
    assert b"already processed" in replay.data
    assert b"different payment" not in replay.data
    with app.app_context():
        payment = Payment.query.one()
        assert payment.payment_type == "Refund" and payment.amount_minor == -25000
        assert db.session.get(Account, cash_id).balance_minor == 475000
    # A genuinely different payload on the same token is still refused.
    changed = client.post("/accounts/payments/clients/save",
                          data=payment_form(cash_id, key, payment_type="Refund", amount="999"),
                          follow_redirects=True)
    assert b"different payment" in changed.data
    with app.app_context():
        assert Payment.query.count() == 1
        assert db.session.get(Account, cash_id).balance_minor == 475000


def test_receive_group_picker_and_loan_account_posting(app, client):
    with app.app_context():
        destination = account()
        loan = Account(name="Saved loan source", account_type="cash", source_category="Loan", balance=0, is_active=True)
        db.session.add(loan)
        db.session.commit()
        destination_id, loan_id = destination.id, loan.id
    login(client)
    text = client.get(NEW_TRANSACTION).get_data(as_text=True)
    assert "Receive From Transaction Group" in text
    assert 'data-picker-items="sourceAccounts"' in text
    assert "Saved loan source" in text
    response = post(client, payload(source="account", receive_group="Loan",
        receive_from_account_id=str(loan_id), receive_account_id=str(destination_id)))
    assert response.status_code == 302
    with app.app_context():
        row = AccountTransaction.query.filter_by(from_account_id=loan_id, to_account_id=destination_id).one()
        assert row.transaction_type == "Transfer"
        assert row.amount_minor == 100000
    response = post(client, payload(source="account", receive_group="Company",
        receive_from_account_id=str(loan_id), receive_account_id=str(destination_id)))
    assert response.status_code == 422
    assert "does not belong" in response.get_data(as_text=True)


@pytest.mark.parametrize("target", ["supplier", "client_refund", "expense", "other"])
def test_workspace_pay_types_post_to_correct_ledger(app, client, target):
    from models import Supplier, SupplierPayment
    with app.app_context():
        source = account(opening="5000")
        customer = client_row(code="REF-01", name="Refund Customer")
        supplier = Supplier(name="Pay Supplier", is_active=True)
        db.session.add(supplier)
        db.session.commit()
        source_id, supplier_id = source.id, supplier.id
    login(client)
    page = client.get(NEW_TRANSACTION + "?mode=pay")
    assert page.status_code == 200
    assert "Client Refund / Payback" in page.get_data(as_text=True)
    data = payload(mode="pay", pay_target=target, pay_from_account_id=str(source_id),
                   supplier_id=str(supplier_id), refund_client_code="REF-01", party="Spending recipient")
    response = post(client, data)
    assert response.status_code == 302, response.get_data(as_text=True)
    with app.app_context():
        from app.services.payments_crud import ledger_balance
        assert ledger_balance(source_id) == pytest.approx(4000)
        if target == "supplier":
            assert SupplierPayment.query.filter_by(supplier_id=supplier_id).one().amount == 1000
        elif target == "client_refund":
            row = Payment.query.filter_by(client_name="Refund Customer").one()
            assert row.amount == -1000
            assert row.payment_type == "Refund"
        else:
            row = AccountTransaction.query.filter_by(from_account_id=source_id, transaction_type="Expense").one()
            assert row.payee == "Spending recipient"
    # Retrying the same form must not post the spending twice.
    assert post(client, data).status_code == 302
    with app.app_context():
        assert ledger_balance(source_id) == pytest.approx(4000)
