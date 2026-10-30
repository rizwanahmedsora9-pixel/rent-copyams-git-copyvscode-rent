"""From-scratch Accounts workspace: empty setup, CRUD, suspension, and posting."""
from __future__ import annotations

from werkzeug.security import generate_password_hash

from models import (
    Account, AccountCategory, AccountTransaction, CashFlowCategory,
    CashFlowSubcategory, User, db,
)
from blueprints.accounts.transaction_tree import build_unified_category_catalog


def login(client, username="Admin", password="Admin@fbm12345"):
    response = client.post("/login", data={"username": username, "password": password})
    assert response.status_code in (302, 303)


def create_account(client, **overrides):
    data = {"name": "Main cash", "account_type": "cash", "account_number": "", "opening_balance": "100.00"}
    data.update(overrides)
    return client.post("/accounts/setup/accounts", data=data)


def account_by_name(name):
    return Account.query.filter_by(name=name).one()


def create_category(client, **overrides):
    data = {"name": "Rent income", "direction": "in", "notes": ""}
    data.update(overrides)
    return client.post("/accounts/setup/categories", data=data)


def test_fresh_workspace_has_only_three_tabs_and_no_seeded_setup(app, client):
    login(client)
    page = client.get("/accounts/")
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    assert "Cash Flow" in text and "Manage Categories" in text and "Manage Accounts" in text
    assert "Start with your accounts" in text
    assert "Add company account" not in text and "class_category" not in text
    with app.app_context():
        assert Account.query.count() == 0
        assert CashFlowCategory.query.count() == 0
        assert AccountCategory.query.count() == 0
    assert client.get("/accounts/accounts").status_code == 200
    assert client.get("/accounts/categories").status_code == 200


def test_account_create_edit_suspend_resume_delete(app, client):
    login(client)
    response = create_account(client, name="Main till", account_number="00123", opening_balance="123.45")
    assert response.status_code == 302
    with app.app_context():
        account = account_by_name("Main till")
        assert account.account_type == "company" and account.type == "company"
        assert account.category == "cash" and account.channel == "cash"
        assert account.account_number == "00123"
        assert account.opening_balance_minor == 12345 and account.balance_minor == 12345
        assert account.source_category is None and account.class_category is None
        assert AccountCategory.query.count() == 0 and CashFlowCategory.query.count() == 0
        account_id, revision = account.id, account.revision

    response = create_account(client, name="main TILL")
    assert response.status_code == 422
    with app.app_context():
        assert Account.query.filter_by(name="Main till").count() == 1

    response = client.post(f"/accounts/setup/accounts", data={
        "account_id": account_id, "expected_revision": revision, "name": "Front desk cash",
        "account_type": "bank", "account_number": "PK00 TEST 0001", "opening_balance": "-50.25",
    })
    assert response.status_code == 302
    with app.app_context():
        account = db.session.get(Account, account_id)
        assert account.name == "Front desk cash" and account.channel == "bank"
        assert account.opening_balance_minor == -5025 and account.balance_minor == -5025
        revision = account.revision

    response = client.post(f"/accounts/setup/accounts/{account_id}/status", data={"active": "0", "expected_revision": revision})
    assert response.status_code == 302
    with app.app_context():
        account = db.session.get(Account, account_id)
        assert account.is_active is False and account.account_status == "inactive"
        assert account.balance_minor == -5025
        revision = account.revision
    page = client.get("/accounts/accounts").get_data(as_text=True)
    assert "Suspended" in page
    with app.app_context():
        from app.services.payments_crud import active_cash_bank_accounts
        assert all(a.id != account_id for a in active_cash_bank_accounts())

    client.post(f"/accounts/setup/accounts/{account_id}/status", data={"active": "1", "expected_revision": revision})
    with app.app_context():
        assert db.session.get(Account, account_id).is_active is True
        revision = db.session.get(Account, account_id).revision
    response = client.post(f"/accounts/setup/accounts/{account_id}/delete", data={"expected_revision": revision})
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Account, account_id) is None


def test_account_validation_and_wallet(app, client):
    login(client)
    assert create_account(client, name="Bad", opening_balance="NaN").status_code == 422
    assert create_account(client, name="Bad", opening_balance="1.234").status_code == 422
    assert create_account(client, name="Bad", opening_balance="").status_code == 422
    assert create_account(client, name="Bad", account_type="cash", opening_balance="-1").status_code == 422
    assert create_account(client, name="Bad", account_type="bank", account_number="").status_code == 422
    assert create_account(client, name="Bad", account_type="wallet", account_number="").status_code == 422
    assert create_account(client, name="Bad", account_type="invented").status_code == 422
    with app.app_context():
        assert Account.query.count() == 0
    assert create_account(client, name="JazzCash", account_type="wallet", account_number="03001234567", opening_balance="0").status_code == 302
    with app.app_context():
        wallet = account_by_name("JazzCash")
        assert wallet.channel == "digital_wallet" and wallet.category == "bank"
        assert wallet.wallet_number == "03001234567" and wallet.account_number == "03001234567"
    assert create_account(client, name="Duplicate number", account_type="bank", account_number="0300 1234567").status_code == 422


def test_used_account_locks_type_and_opening_but_can_be_suspended(app, client):
    login(client)
    create_account(client, name="Till", opening_balance="100")
    create_category(client, name="Custom income", direction="in")
    with app.app_context():
        account_id = account_by_name("Till").id
    response = client.post("/accounts/transactions/new", data={
        "tx_mode": "receive", "receive_account_id": account_id, "receive_from_category": "other_source",
        "receive_source_label": "Someone", "amount": "25", "method": "Cash", "receive_category": "Custom income",
    }, follow_redirects=True)
    assert b"Receive transaction recorded successfully" in response.data
    with app.app_context():
        account = db.session.get(Account, account_id)
        assert account.balance_minor == 12500
        revision = account.revision
    response = client.post("/accounts/setup/accounts", data={
        "account_id": account_id, "expected_revision": revision, "name": "Till", "account_type": "bank",
        "account_number": "123", "opening_balance": "100",
    })
    assert response.status_code == 422
    response = client.post("/accounts/setup/accounts", data={
        "account_id": account_id, "expected_revision": revision, "name": "Till", "account_type": "cash",
        "account_number": "", "opening_balance": "999",
    })
    assert response.status_code == 422
    response = client.post("/accounts/setup/accounts", data={
        "account_id": account_id, "expected_revision": revision, "name": "Renamed till", "account_type": "cash",
        "account_number": "REF-1", "opening_balance": "100",
    })
    assert response.status_code == 302
    with app.app_context():
        account = db.session.get(Account, account_id)
        assert account.name == "Renamed till" and account.balance_minor == 12500
        revision = account.revision
    assert client.post(f"/accounts/setup/accounts/{account_id}/delete", data={"expected_revision": revision}).status_code == 302
    with app.app_context():
        assert db.session.get(Account, account_id) is not None
    client.post(f"/accounts/setup/accounts/{account_id}/status", data={"active": "0", "expected_revision": revision})
    with app.app_context():
        account = db.session.get(Account, account_id)
        assert account.is_active is False and account.balance_minor == 12500
        assert AccountTransaction.query.filter_by(to_account_id=account_id).count() == 1
    response = client.post("/accounts/transactions/new", data={
        "tx_mode": "receive", "receive_account_id": account_id, "receive_from_category": "other_source",
        "receive_source_label": "Someone", "amount": "5", "method": "Cash",
    }, follow_redirects=True)
    assert b"valid destination account" in response.data


def test_category_crud_suspension_and_history_guard(app, client):
    login(client)
    assert create_category(client, name="Rent income", direction="in", notes="Yard rent").status_code == 302
    assert create_category(client, name="rent INCOME").status_code == 422
    assert create_category(client, name="Fuel", direction="out").status_code == 302
    with app.app_context():
        rent = CashFlowCategory.query.filter_by(name="Rent income").one()
        fuel = CashFlowCategory.query.filter_by(name="Fuel").one()
        rent_id, fuel_id = rent.id, fuel.id
    response = client.post("/accounts/setup/categories", data={"category_id": rent_id, "name": "Rental income", "direction": "in", "notes": "Updated"})
    assert response.status_code == 302
    client.post(f"/accounts/setup/categories/{fuel_id}/status", data={"active": "0"})
    with app.app_context():
        assert db.session.get(CashFlowCategory, fuel_id).is_active is False
        assert "Fuel" not in build_unified_category_catalog()["by_name"]
    assert create_category(client, name="Fuel").status_code == 422  # suspended names stay reserved
    client.post(f"/accounts/setup/categories/{fuel_id}/status", data={"active": "1"})
    assert client.post(f"/accounts/setup/categories/{fuel_id}/delete").status_code == 302
    with app.app_context():
        assert db.session.get(CashFlowCategory, fuel_id) is None

    create_account(client, name="Till", opening_balance="0")
    with app.app_context():
        account_id = account_by_name("Till").id
    client.post("/accounts/transactions/new", data={
        "tx_mode": "receive", "receive_account_id": account_id, "receive_from_category": "other_source",
        "receive_source_label": "Tenant", "amount": "10", "method": "Cash", "receive_category": "Rental income",
    })
    with app.app_context():
        rent = db.session.get(CashFlowCategory, rent_id)
        assert rent is not None
    assert client.post(f"/accounts/setup/categories/{rent_id}/delete").status_code == 302
    with app.app_context():
        assert db.session.get(CashFlowCategory, rent_id) is not None
    response = client.post("/accounts/setup/categories", data={"category_id": rent_id, "name": "Rental income", "direction": "out"})
    assert response.status_code == 422
    client.post(f"/accounts/setup/categories/{rent_id}/status", data={"active": "0"})
    with app.app_context():
        assert db.session.get(CashFlowCategory, rent_id).is_active is False
        assert AccountTransaction.query.filter_by(category="Rental income").count() == 1


def test_used_category_rename_keeps_history_grouped_and_delete_blocked(app, client):
    login(client)
    create_account(client, name="Till", opening_balance="0")
    create_category(client, name="Old label", direction="in")
    with app.app_context():
        account_id = account_by_name("Till").id
        category_id = CashFlowCategory.query.filter_by(name="Old label").one().id
    client.post("/accounts/transactions/new", data={
        "tx_mode": "receive", "receive_account_id": account_id, "receive_from_category": "other_source",
        "receive_source_label": "Tenant", "amount": "10", "method": "Cash", "receive_category": "Old label",
    })
    assert client.post("/accounts/setup/categories", data={"category_id": category_id, "name": "New label", "direction": "in"}).status_code == 302
    with app.app_context():
        assert AccountTransaction.query.filter_by(category="New label").count() == 1
        assert AccountTransaction.query.filter_by(category="Old label").count() == 0
    client.post(f"/accounts/setup/categories/{category_id}/delete")
    with app.app_context():
        assert db.session.get(CashFlowCategory, category_id) is not None


def test_editing_used_account_preserves_legacy_bank_details(app, client):
    login(client)
    create_account(client, name="Bank", account_type="bank", account_number="123", opening_balance="10")
    with app.app_context():
        account = account_by_name("Bank")
        account.bank_name = "Our Bank"
        account.account_holder_name = "Business"
        db.session.commit()
        account_id, revision = account.id, account.revision
        tx = AccountTransaction(to_account_id=account_id, amount=1, amount_minor=100, transaction_type="Receipt")
        db.session.add(tx); db.session.commit()
    assert client.post("/accounts/setup/accounts", data={
        "account_id": account_id, "expected_revision": revision, "name": "Bank renamed",
        "account_type": "bank", "account_number": "456", "opening_balance": "10",
    }).status_code == 302
    with app.app_context():
        account = db.session.get(Account, account_id)
        assert account.name == "Bank renamed" and account.account_number == "456"
        assert account.bank_name == "Our Bank" and account.account_holder_name == "Business"


def test_unused_parent_category_delete_removes_unused_children(app, client):
    login(client)
    create_category(client, name="Parent", direction="both")
    with app.app_context():
        parent = CashFlowCategory.query.filter_by(name="Parent").one()
        child = CashFlowSubcategory(category_id=parent.id, name="Child", is_active=True)
        db.session.add(child); db.session.commit()
        parent_id, child_id = parent.id, child.id
    assert client.post(f"/accounts/setup/categories/{parent_id}/delete").status_code == 302
    with app.app_context():
        assert db.session.get(CashFlowCategory, parent_id) is None
        assert db.session.get(CashFlowSubcategory, child_id) is None


def test_setup_permissions_are_separate_from_viewing(app, client):
    with app.app_context():
        viewer = User(username="viewer", password_hash=generate_password_hash("Viewer@12345"), role="user",
                      can_manage_accounts=True, can_manage_account_masters=False)
        master = User(username="master", password_hash=generate_password_hash("Master@12345"), role="user",
                      can_manage_accounts=False, can_manage_account_masters=True)
        db.session.add_all([viewer, master]); db.session.commit()
    login(client, "viewer", "Viewer@12345")
    assert client.get("/accounts/accounts").status_code == 200
    assert create_account(client, name="Nope").status_code in (302, 403)
    with app.app_context():
        assert Account.query.filter_by(name="Nope").count() == 0
    client.get("/logout")
    login(client, "master", "Master@12345")
    assert create_account(client, name="Allowed").status_code == 302


def test_legacy_form_urls_open_the_new_workspace(app, client):
    login(client)
    response = client.get("/accounts/accounts/add")
    assert response.status_code == 302 and "/accounts/accounts" in response.headers["Location"] and "new=1" in response.headers["Location"]
    create_account(client, name="Till")
    with app.app_context():
        account_id = account_by_name("Till").id
    response = client.get(f"/accounts/{account_id}/edit")
    assert response.status_code == 302 and f"edit={account_id}" in response.headers["Location"]
    response = client.get("/accounts/transaction-categories")
    assert response.status_code == 302 and "/accounts/categories" in response.headers["Location"]


def test_cash_flow_tab_uses_accounts_and_excludes_internal_transfers(app, client):
    login(client)
    create_account(client, name="Cash A", opening_balance="100")
    create_account(client, name="Bank A", account_type="bank", account_number="123", opening_balance="50")
    with app.app_context():
        cash_id = account_by_name("Cash A").id
        bank_id = account_by_name("Bank A").id
    client.post("/accounts/transfers/add", data={"from_account": bank_id, "to_account": cash_id, "amount": "30"})
    create_category(client, name="Other income", direction="in")
    client.post("/accounts/transactions/new", data={
        "tx_mode": "receive", "receive_account_id": cash_id, "receive_from_category": "other_source",
        "receive_source_label": "Someone", "amount": "20", "method": "Cash", "receive_category": "Other income",
    })
    page = client.get("/accounts/").get_data(as_text=True)
    assert "Cash Flow" in page and "Other income" in page and "Transfer" in page
    assert "20.00" in page and "30.00" in page
    with app.app_context():
        assert db.session.get(Account, cash_id).balance_minor == 15000
        assert db.session.get(Account, bank_id).balance_minor == 2000
