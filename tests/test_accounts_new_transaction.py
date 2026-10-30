"""The fourth Accounts tab: usable money entry, not a navigation-only mockup."""
from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from werkzeug.security import generate_password_hash

from models import Account, AccountTransaction, CashFlowCategory, CashFlowSubcategory, User, db
from app.services.account_transactions import save_workspace_transaction
from app.services.cash_flow_svc import collect_cash_flow_rows
from app.services.time_money import pk_now


def login(client, username="Admin", password="Admin@fbm12345"):
    response = client.post("/login", data={"username": username, "password": password})
    assert response.status_code in (302, 303)


def account(name="Till", kind="cash", opening="1000.00", active=True):
    from app.services import account_setup as setup
    row, _ = setup.save_account({
        "name": name, "account_type": kind, "account_number": "03001234567" if kind != "cash" else "",
        "opening_balance": opening,
    }, actor=None)
    row.is_active = active
    db.session.flush()
    return row


def payload(mode="receive", **overrides):
    data = {
        "entry_source": "accounts_workspace", "tx_mode": mode,
        "idempotency_key": uuid4().hex, "amount": "125.25",
        "date_posted": pk_now().strftime("%Y-%m-%dT%H:%M"),
        "party": "Counterparty", "note": "A useful note", "reference_no": "REF-123",
    }
    data.update(overrides)
    return data


def post(client, data):
    return client.post("/accounts/transactions/new", data=data)


def test_fourth_tab_is_last_and_get_form_is_available(app, client):
    login(client)
    for path in ("/accounts/", "/accounts/accounts", "/accounts/categories", "/accounts/transactions/new"):
        page = client.get(path)
        assert page.status_code == 200
        text = page.get_data(as_text=True)
        assert "New Transaction" in text
        assert text.index("aw-tab-new") < text.index("aw-panel")
    text = client.get("/accounts/transactions/new").get_data(as_text=True)
    assert "Add an account to get started" in text
    assert 'name="_csrf_token"' in text
    with app.app_context():
        assert Account.query.count() == 0
        assert CashFlowCategory.query.count() == 0
        assert AccountTransaction.query.count() == 0


def test_receive_pay_transfer_update_balances_and_cash_flow_details(app, client):
    with app.app_context():
        cash_id = account().id
        bank_id = account("Bank", "bank", "500").id
        income = CashFlowCategory(name="Hire income", direction="in", is_active=True)
        expense = CashFlowCategory(name="Repairs", direction="out", is_active=True)
        db.session.add_all([income, expense]); db.session.flush()
        db.session.add(CashFlowSubcategory(category_id=expense.id, name="Equipment", is_active=True))
        db.session.commit()
    login(client)
    response = post(client, payload(receive_account_id=cash_id, category="Hire income"))
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/accounts/transactions/new")
    response = post(client, payload("pay", pay_from_account_id=cash_id, category="Repairs", subcategory="Equipment"))
    assert response.status_code == 302
    response = post(client, payload("transfer", transfer_from_account_id=cash_id, transfer_to_account_id=bank_id, amount="200"))
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Account, cash_id).balance_minor == 80000
        assert db.session.get(Account, bank_id).balance_minor == 70000
        assert db.session.get(Account, cash_id).balance == 800.00
        rows = collect_cash_flow_rows(pk_now().strftime("%Y-%m-%d"), pk_now().strftime("%Y-%m-%d"))
        assert len(rows) == 3
        by_type = {row["type"]: row for row in rows}
        assert by_type["in"]["category"] == "Hire income"
        assert by_type["out"]["subcategory"] == "Equipment"
        assert by_type["in"]["party_name"] == "Counterparty"
        assert by_type["out"]["reference"] == "REF-123"
        assert by_type["transfer"]["account_display"] == "Till → Bank"
        assert by_type["transfer"]["cash_in"] == by_type["transfer"]["cash_out"] == 0
    page = client.get("/accounts/").get_data(as_text=True)
    assert "Hire income" in page and "Repairs" in page and "Counterparty" in page


def test_wallet_can_receive_and_pay_without_a_cash_method_mismatch(app, client):
    with app.app_context():
        wallet_id = account("JazzCash", "wallet", "100").id
        db.session.commit()
    login(client)
    assert post(client, payload(receive_account_id=wallet_id, amount="50")).status_code == 302
    assert post(client, payload("pay", pay_from_account_id=wallet_id, amount="25")).status_code == 302
    with app.app_context():
        assert db.session.get(Account, wallet_id).balance_minor == 12500
        assert all(tx.payment_method == "Mobile wallet" for tx in AccountTransaction.query.all())


def test_form_shows_only_active_accounts_and_direction_specific_categories(app, client):
    with app.app_context():
        account()
        account("Old cash", active=False)
        db.session.add_all([
            CashFlowCategory(name="Income only", direction="in", is_active=True),
            CashFlowCategory(name="Expense only", direction="out", is_active=True),
            CashFlowCategory(name="Either way", direction="both", is_active=True),
            CashFlowCategory(name="Suspended", direction="both", is_active=False),
        ])
        db.session.commit()
    login(client)
    text = client.get("/accounts/transactions/new").get_data(as_text=True)
    form_text = text[text.index('id="transactionForm"'):text.index('</form>', text.index('id="transactionForm"'))]
    assert "Old cash" not in form_text and "Suspended" not in form_text
    assert "Income only" in form_text and "Expense only" in form_text and "Either way" in form_text
    assert 'data-direction="in"' in text and 'data-direction="out"' in text
    assert 'data-tx-mode="receive"' in text and 'data-tx-mode="pay"' in text and 'data-tx-mode="transfer"' in text


@pytest.mark.parametrize("amount", ["0", "-1", "NaN", "Infinity", "1.234", "1e30", "", "abc"])
def test_invalid_amount_never_posts(app, client, amount):
    with app.app_context():
        account_id = account().id
        db.session.commit()
    login(client)
    response = post(client, payload(receive_account_id=account_id, amount=amount))
    assert response.status_code == 422
    assert b"No changes were applied" not in response.data
    with app.app_context():
        assert AccountTransaction.query.count() == 0
        assert db.session.get(Account, account_id).balance_minor == 100000


def test_validation_retains_input_and_same_form_key(app, client):
    with app.app_context():
        account_id = account().id
        db.session.commit()
    login(client)
    data = payload(receive_account_id=account_id, party="Ali & Sons", note="Keep this note")
    response = post(client, {**data, "amount": "-5"})
    assert response.status_code == 422
    text = response.get_data(as_text=True)
    assert "Ali &amp; Sons" in text and "Keep this note" in text
    assert data["idempotency_key"] in text
    assert post(client, data).status_code == 302
    assert post(client, data).status_code == 302
    with app.app_context():
        assert AccountTransaction.query.count() == 1
        assert db.session.get(Account, account_id).balance_minor == 112525


def test_changed_replay_payload_is_rejected(app, client):
    with app.app_context():
        account_id = account().id
        db.session.commit()
    login(client)
    data = payload(receive_account_id=account_id)
    assert post(client, data).status_code == 302
    response = post(client, {**data, "amount": "200"})
    assert response.status_code == 422
    assert b"different transaction" in response.data
    with app.app_context():
        assert AccountTransaction.query.count() == 1


@pytest.mark.parametrize("overrides", [
    {"receive_account_id": 999999, "tx_mode": "receive"},
    {"pay_from_account_id": 999999, "tx_mode": "pay"},
    {"tx_mode": "receive", "category": "Missing"},
    {"tx_mode": "pay", "category": "Income only"},
    {"tx_mode": "receive", "category": "Income only", "subcategory": "Wrong child"},
    {"tx_mode": "receive", "party": ""},
    {"tx_mode": "pay", "party": ""},
])
def test_invalid_account_party_and_category_are_refused(app, client, overrides):
    with app.app_context():
        account_id = account().id
        db.session.add(CashFlowCategory(name="Income only", direction="in", is_active=True))
        db.session.commit()
    login(client)
    mode = overrides["tx_mode"]
    data = payload(mode, **{"receive_account_id": account_id, "pay_from_account_id": account_id, **overrides})
    assert post(client, data).status_code == 422
    with app.app_context():
        assert AccountTransaction.query.count() == 0
        assert db.session.get(Account, account_id).balance_minor == 100000


def test_suspended_account_and_insufficient_funds_are_refused(app, client):
    with app.app_context():
        cash_id = account().id
        suspended_id = account("Suspended bank", "bank", "1000", active=False).id
        db.session.commit()
    login(client)
    assert post(client, payload(receive_account_id=suspended_id)).status_code == 422
    assert post(client, payload("pay", pay_from_account_id=cash_id, amount="1000.01")).status_code == 422
    assert post(client, payload("transfer", transfer_from_account_id=cash_id, transfer_to_account_id=cash_id)).status_code == 422
    assert post(client, payload("transfer", transfer_from_account_id=cash_id, transfer_to_account_id=suspended_id)).status_code == 422
    with app.app_context():
        assert AccountTransaction.query.count() == 0


def test_future_and_locked_period_dates_are_refused(app, client):
    from app.services.ledger.periods import get_or_create_period, lock_period
    with app.app_context():
        account_id = account().id
        db.session.commit()
    login(client)
    future = (pk_now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    assert post(client, payload(receive_account_id=account_id, date_posted=future)).status_code == 422
    assert post(client, payload(receive_account_id=account_id, date_posted="bad-date")).status_code == 422
    with app.app_context():
        period = get_or_create_period(pk_now().date())
        lock_period(period, actor="Admin")
        db.session.commit()
    response = post(client, payload(receive_account_id=account_id))
    assert response.status_code == 422
    assert b"locked" in response.data
    with app.app_context():
        assert AccountTransaction.query.count() == 0
        assert db.session.get(Account, account_id).balance_minor == 100000


def test_post_permission_is_required_and_setup_permission_alone_is_not_enough(app, client):
    with app.app_context():
        account_id = account().id
        viewer = User(username="setuponly", password_hash=generate_password_hash("Setup@12345"), role="user",
                      can_manage_accounts=False, can_manage_payments=False, can_manage_account_masters=True)
        poster = User(username="poster", password_hash=generate_password_hash("Poster@12345"), role="user",
                      can_manage_accounts=True, can_manage_account_masters=False)
        db.session.add_all([viewer, poster]); db.session.commit()
    data = payload(receive_account_id=account_id)
    assert post(client, data).status_code in (302, 401, 403)
    assert "/login" in client.get("/accounts/transactions/new").headers["Location"]
    login(client, "setuponly", "Setup@12345")
    denied = post(client, data)
    assert denied.status_code in (302, 403)
    if denied.status_code == 302:
        assert denied.headers["Location"] in ("/", "http://localhost/")
    with app.app_context():
        assert AccountTransaction.query.count() == 0
    client.get("/logout")
    login(client, "poster", "Poster@12345")
    assert client.get("/accounts/transactions/new").status_code == 200
    assert post(client, data).status_code == 302
    with app.app_context():
        assert AccountTransaction.query.count() == 1


def test_workspace_requires_csrf(app):
    raw = app.test_client()
    raw.get("/login")
    with raw.session_transaction() as sess:
        token = sess["_csrf_token"]
    login_response = raw.post("/login", data={"username": "Admin", "password": "Admin@fbm12345", "_csrf_token": token})
    assert login_response.status_code in (302, 303)
    with app.app_context():
        account_id = account().id
        db.session.commit()
    response = raw.post("/accounts/transactions/new", data=payload(receive_account_id=account_id))
    assert response.status_code == 400
    with app.app_context():
        assert AccountTransaction.query.count() == 0


def test_duplicate_racing_past_precheck_has_one_balance_effect_and_audit(app, monkeypatch):
    from app.services import account_transactions as service
    from models import AccountingAuditLog, JournalEntry
    with app.app_context():
        account_id = account().id
        actor = User.query.filter_by(username="Admin").one()
        data = payload(receive_account_id=account_id)
        first, created = save_workspace_transaction(data, actor=actor)
        db.session.commit()
        first_id = first.id
        real_replay = service._replay
        calls = 0

        def racing_replay(key, values):
            nonlocal calls
            calls += 1
            # Simulate a second request whose initial lookup happened before
            # the winner committed. Its INSERT must lose on the unique index.
            return None if calls == 1 else real_replay(key, values)

        monkeypatch.setattr(service, "_replay", racing_replay)
        replay, created = save_workspace_transaction(data, actor=actor)
        db.session.commit()
        assert calls == 2
        assert created is False
        assert replay.id == first_id
        assert AccountTransaction.query.count() == 1
        assert db.session.get(Account, account_id).balance_minor == 112525
        assert AccountingAuditLog.query.filter_by(entity_type="AccountTransaction", entity_id=first_id).count() == 1
        assert JournalEntry.query.filter_by(source_type="AccountTransaction", source_id=first_id).count() == 1
