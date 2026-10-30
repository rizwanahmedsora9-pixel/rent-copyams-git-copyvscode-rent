"""Account create/edit after the from-scratch rebuild.

The old classification wizard is no longer the account setup path. These tests
pin the simple owner-defined form, exact opening balances, history locks, and
the legacy compatibility URLs. The old registry remains covered as a library
because historical rows and other modules still import it.
"""
from __future__ import annotations

import pytest

from models import db, Account, AccountTransaction


def login(client, username="Admin", password="Admin@fbm12345"):
    resp = client.post("/login", data={"username": username, "password": password})
    assert resp.status_code in (302, 303)


def create_account(client, **overrides):
    base = {"name": "Test Account", "account_type": "cash", "account_number": "", "opening_balance": "0"}
    base.update(overrides)
    return client.post("/accounts/setup/accounts", data=base)


def test_registry_blocks_invalid_combination():
    from blueprints.accounts import classification as cls

    assert cls.is_valid_triple("Assets", "Cash", "Main Cash")
    assert not cls.is_valid_triple("Expense", "Bank", "Operating Bank")
    assert not cls.is_valid_triple("Assets", "Bank", "Client Ledger")
    assert not cls.is_valid_triple("Liabilities", "Supplier Payables", "Main Cash")


def test_registry_forces_channel_and_entity():
    from blueprints.accounts import classification as cls

    assert cls.allowed_channels("Assets", "Cash", "Main Cash") == ["cash"]
    assert cls.required_entity("Assets", "Cash", "Main Cash") == "none"
    assert cls.allowed_channels("Assets", "Client Receivables", "Client Ledger") == ["ledger_only"]
    assert cls.required_entity("Assets", "Client Receivables", "Client Ledger") == "client"
    assert cls.required_entity("Liabilities", "Supplier Payables", "Supplier Ledger") == "supplier"


def test_validate_account_form_rejects_invalid_triple(app):
    from blueprints.accounts.account_form import validate_account_form

    class Form:
        def __init__(self, d):
            self._d = d

        def get(self, k, default=""):
            return self._d.get(k, default)

    bad = Form({"name": "X", "class_category": "Expenses", "class_subcategory": "Cash",
                "class_account_type": "Main Cash", "account_status": "active"})
    with app.app_context():
        with pytest.raises(ValueError):
            validate_account_form(bad, is_edit=False)


def test_simple_create_and_legacy_add_url(client, app):
    login(client)
    assert create_account(client, name="Main cash", opening_balance="50000.50").status_code == 302
    with app.app_context():
        account = Account.query.filter_by(name="Main cash").one()
        assert account.opening_balance_minor == 5000050
        assert account.balance_minor == 5000050
        assert account.account_type == "company" and account.channel == "cash"
        assert account.class_category is None
    # The previous create URL still posts to the same simple service.
    assert client.post("/accounts/accounts/add", data={
        "name": "Bank account", "account_type": "bank", "account_number": "001", "opening_balance": "100",
    }).status_code == 302
    with app.app_context():
        bank = Account.query.filter_by(name="Bank account").one()
        assert bank.channel == "bank" and bank.account_number == "001"


def test_edit_unused_account_can_change_opening_and_type(client, app):
    login(client)
    create_account(client, name="Editable", opening_balance="100")
    with app.app_context():
        account = Account.query.filter_by(name="Editable").one()
        aid, revision = account.id, account.revision
    response = client.post(f"/accounts/{aid}/edit", data={
        "expected_revision": revision, "name": "Editable", "account_type": "bank",
        "account_number": "PK123", "opening_balance": "-250.75",
    })
    assert response.status_code == 302
    with app.app_context():
        account = db.session.get(Account, aid)
        assert account.channel == "bank" and account.account_number == "PK123"
        assert account.opening_balance_minor == -25075 and account.balance_minor == -25075


def test_stale_revision_is_rejected(client, app):
    login(client)
    create_account(client, name="Stale", opening_balance="10")
    with app.app_context():
        aid = Account.query.filter_by(name="Stale").one().id
    response = client.post("/accounts/setup/accounts", data={
        "account_id": aid, "expected_revision": 999, "name": "Stale changed",
        "account_type": "cash", "opening_balance": "10",
    })
    assert response.status_code == 422
    with app.app_context():
        assert db.session.get(Account, aid).name == "Stale"


def test_opening_balance_is_locked_after_ledger_activity(client, app):
    login(client)
    create_account(client, name="Posted", opening_balance="100")
    with app.app_context():
        account = Account.query.filter_by(name="Posted").one()
        aid = account.id
        tx = AccountTransaction(to_account_id=aid, amount=10, amount_minor=1000,
                                transaction_type="Receipt", description="Test receipt")
        db.session.add(tx)
        account.balance_minor = 11000
        account.balance = 110.0
        db.session.commit()
        revision = account.revision
    response = client.post("/accounts/setup/accounts", data={
        "account_id": aid, "expected_revision": revision, "name": "Posted",
        "account_type": "cash", "opening_balance": "200",
    })
    assert response.status_code == 422
    with app.app_context():
        account = db.session.get(Account, aid)
        assert account.opening_balance_minor == 10000 and account.balance_minor == 11000


def test_bootstrap_preserves_existing_accounts(app_factory, tmp_path):
    db_file = tmp_path / "integ.db"
    app = app_factory(db_file=db_file)
    with app.app_context():
        acc = Account(name="Legacy Cash", category="cash", source_category="Company",
                      account_type="company", type="company", balance=1000.0, balance_minor=100000,
                      opening_balance=1000.0, opening_balance_minor=100000, is_active=True, revision=1)
        db.session.add(acc)
        db.session.commit()
        seeded_id = acc.id
        before_count = Account.query.count()
    app2 = app_factory(db_file=db_file)
    with app2.app_context():
        assert Account.query.count() == before_count
        acc = db.session.get(Account, seeded_id)
        assert acc is not None and acc.name == "Legacy Cash" and acc.balance == 1000.0
