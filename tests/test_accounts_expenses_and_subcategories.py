"""Expenses register + category/subcategory management.

Covers the owner-facing flow: create a category, add and manage its
subcategories, record money out under them, and trace that money by date,
category, and subcategory in Expenses, Cash Flow, and the account ledger.
"""
from __future__ import annotations

import uuid

from models import Account, AccountTransaction, CashFlowCategory, CashFlowSubcategory
from blueprints.accounts.transaction_tree import build_unified_category_catalog


def login(client, username="Admin", password="Admin@fbm12345"):
    response = client.post("/login", data={"username": username, "password": password})
    assert response.status_code in (302, 303)


def make_account(client, name="Main cash", opening="5000.00"):
    response = client.post("/accounts/setup/accounts", data={
        "name": name, "account_type": "cash", "account_number": "", "opening_balance": opening,
    })
    assert response.status_code == 302
    return Account.query.filter_by(name=name).one()


def make_category(client, name="Fuel & Transport", direction="out"):
    response = client.post("/accounts/setup/categories", data={"name": name, "direction": direction, "notes": ""})
    assert response.status_code == 302
    return CashFlowCategory.query.filter_by(name=name).one()


def add_subcategory(client, category_id, name, notes=""):
    response = client.post("/accounts/setup/subcategories", data={
        "category_id": category_id, "name": name, "notes": notes,
    })
    assert response.status_code == 302
    return CashFlowSubcategory.query.filter_by(category_id=category_id, name=name).one()


def pay_expense(client, account_id, amount, category, subcategory, party="Fuel Pump", note="expense", posted="2026-01-15T10:00"):
    response = client.post("/accounts/transactions/new", data={
        "_csrf_token": "test-csrf-token",
        "entry_source": "accounts_workspace",
        "idempotency_key": uuid.uuid4().hex,
        "tx_mode": "pay",
        "pay_from_account_id": str(account_id),
        "pay_target": "expense",
        "party": party,
        "amount": amount,
        "category": category,
        "subcategory": subcategory,
        "note": note,
        "reference_no": "V-1",
        "date_posted": posted,
    }, follow_redirects=False)
    return response


def test_subcategory_lifecycle_and_catalog(app, client):
    login(client)
    with app.app_context():
        category = make_category(client)
        category_id = category.id

        sub = add_subcategory(client, category_id, "Diesel & Petrol", "Generator and trucks")
        sub_id = sub.id

        # Shown on the Categories tab and offered for new transactions.
        page = client.get("/accounts/categories").get_data(as_text=True)
        assert "Diesel &amp; Petrol" in page or "Diesel & Petrol" in page
        catalog = build_unified_category_catalog()["by_name"]["Fuel & Transport"]
        assert "Diesel & Petrol" in catalog["subcategories"]

        # Duplicate names inside one category are refused, other categories may reuse them.
        response = client.post("/accounts/setup/subcategories", data={
            "category_id": category_id, "name": "diesel & petrol",
        })
        assert response.status_code == 302
        assert CashFlowSubcategory.query.filter_by(category_id=category_id).count() == 1

        # Suspend / resume / rename / delete.
        client.post(f"/accounts/setup/subcategories/{sub_id}/status", data={"active": "0"})
        assert CashFlowSubcategory.query.get(sub_id).is_active is False
        client.post(f"/accounts/setup/subcategories/{sub_id}/status", data={"active": "1"})
        assert CashFlowSubcategory.query.get(sub_id).is_active is True

        client.post("/accounts/setup/subcategories", data={
            "category_id": category_id, "subcategory_id": sub_id, "name": "Diesel",
        })
        assert CashFlowSubcategory.query.get(sub_id).name == "Diesel"

        client.post(f"/accounts/setup/subcategories/{sub_id}/delete")
        assert CashFlowSubcategory.query.get(sub_id) is None


def test_used_subcategory_cannot_be_deleted(app, client):
    login(client)
    with app.app_context():
        account = make_account(client)
        category = make_category(client)
        sub = add_subcategory(client, category.id, "Diesel")
        response = pay_expense(client, account.id, "150.00", category.name, sub.name)
        assert response.status_code == 302
        client.post(f"/accounts/setup/subcategories/{sub.id}/delete")
        assert CashFlowSubcategory.query.get(sub.id) is not None


def test_expense_appears_in_expenses_cash_flow_and_ledger_with_filters(app, client):
    login(client)
    with app.app_context():
        account = make_account(client)
        fuel = make_category(client, "Fuel & Transport")
        office = make_category(client, "Office & Admin")
        diesel = add_subcategory(client, fuel.id, "Diesel & Petrol")
        stationery = add_subcategory(client, office.id, "Stationery")
        pay_expense(client, account.id, "400.00", fuel.name, diesel.name, party="Fuel Pump")
        pay_expense(client, account.id, "120.00", office.name, stationery.name, party="Book Shop")

        tx = AccountTransaction.query.filter_by(payee="Fuel Pump").one()
        assert tx.category == fuel.name and tx.subcategory == diesel.name
        assert tx.transaction_type == "Expense"
        account_id = account.id

        body = client.get("/accounts/expenses?from_date=2026-01-01&to_date=2026-01-31").get_data(as_text=True)
        assert "Fuel Pump" in body and "Book Shop" in body

        filtered = client.get(
            "/accounts/expenses?from_date=2026-01-01&to_date=2026-01-31"
            "&category=Office%20%26%20Admin&subcategory=Stationery"
        ).get_data(as_text=True)
        assert "Book Shop" in filtered and "Fuel Pump" not in filtered

        outside = client.get(
            "/accounts/expenses?from_date=2026-02-01&to_date=2026-02-28"
        ).get_data(as_text=True)
        assert "Fuel Pump" not in outside

        cash_flow = client.get(
            "/accounts/?from_date=2026-01-01&to_date=2026-01-31&category=Fuel%20%26%20Transport&direction=out"
        ).get_data(as_text=True)
        assert "Fuel Pump" in cash_flow and "Book Shop" not in cash_flow

        ledger = client.get(
            f"/accounts/ledger/{account_id}?from=2026-01-01&to=2026-01-31&category=Fuel%20%26%20Transport"
        ).get_data(as_text=True)
        assert "Fuel Pump" in ledger and "Book Shop" not in ledger

        expenditures = client.get("/accounts/expenditures?from=2026-01-01&to=2026-01-31&subcategory=Diesel%20%26%20Petrol")
        assert expenditures.status_code == 200
        assert "Fuel Pump" in expenditures.get_data(as_text=True)


def test_expenses_tab_is_part_of_the_workspace(app, client):
    login(client)
    page = client.get("/accounts/expenses")
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    assert "Expenses" in text and "Record expense" in text
    tabs = client.get("/accounts/").get_data(as_text=True)
    assert "/accounts/expenses" in tabs
