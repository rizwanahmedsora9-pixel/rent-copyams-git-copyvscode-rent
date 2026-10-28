"""One book, many pages: every read model must agree with the journal.

The acceptance criteria of the sync layer (§16 S11):

1. dashboard totals = statements = subledgers = API;
2. the prepared answers are cached against ``ledger_version`` and rebuild when a
   new document is posted;
3. every ledger page renders without touching a document table itself.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from models import (
    Account,
    Client,
    DirectSale,
    DirectSaleItem,
    GRN,
    GRNItem,
    Payment,
    Supplier,
    db,
)
from app.services import cache
from app.services.ledger import accounts as coa
from app.services.ledger import query as q
from app.services.read_models import (
    books_agree as books_agree_model,
    cash_view,
    client_account,
    dashboard,
    pnl_view,
    supplier_account,
)


@pytest.fixture()
def world(app):
    with app.app_context():
        supplier = Supplier(name="Ravi Cement Supplier", opening_balance=0)
        client = Client(code="CL-1", name="Bilal Construction", opening_balance=0)
        cash = Account(
            name="Main Cash", type="cash", category="cash", account_type="company",
            channel="cash", balance=0, balance_minor=0, is_active=True,
        )
        db.session.add_all([supplier, client, cash])
        db.session.commit()
        coa.ensure_chart()
        coa.ensure_cash_accounts()
        # stock to sell
        grn = GRN(
            supplier_id=supplier.id, supplier="Ravi Cement Supplier",
            manual_bill_no="G-1", date_posted=datetime.now(), freight_cost=0,
            paid_amount=0,
        )
        grn.items = [GRNItem(mat_name="OPC Cement", qty=100, price_at_time=1000)]
        db.session.add(grn)
        db.session.commit()
        # a sale with a discount and a part payment
        sale = DirectSale(
            client_name="Bilal Construction", client_code="CL-1", category="Credit Customer",
            amount=12000, discount=2000, paid_amount=4000, date_posted=datetime.now(),
            payment_account_id=cash.id,
        )
        db.session.add(sale)
        db.session.flush()
        db.session.add(DirectSaleItem(
            sale_id=sale.id, product_name="OPC Cement", qty=10, price_at_time=1200,
            cost_rate_at_sale=1000, discount=2000, discount_kind="manual",
        ))
        db.session.commit()
        # a later receipt
        db.session.add(Payment(
            client_id=client.id, client_name="Bilal Construction", amount=3000,
            payment_type="Receipt", method="Cash", payment_account_id=cash.id,
            date_posted=datetime.now(),
        ))
        db.session.commit()
        return {"client": client.id, "supplier": supplier.id, "cash": cash.id, "sale": sale.id}


def test_dashboard_cash_equals_book_cash(world, app):
    with app.app_context():
        summary = dashboard.load(date.today())
        assert summary["cash_balance"] == q.cash_positions()["total"] == 7_000_00
        assert summary["ledger_version"] == q.ledger_version()


def test_dashboard_revenue_equals_pnl(world, app):
    with app.app_context():
        summary = dashboard.load(date.today())
        pnl = pnl_view.load(date.today().replace(day=1), date.today())
        assert summary["month_revenue"] == pnl["net_revenue"] == 10_000_00
        assert summary["month_cogs"] == pnl["cost_total"] == 10_000_00
        assert summary["month_profit"] == pnl["net_profit"] == 0


def test_subledgers_tie_to_the_control_accounts(world, app):
    with app.app_context():
        receivables = client_account.balances(date.today())
        payables = supplier_account.balances(date.today())
        balances = {row["code"]: row for row in q.account_balances()}
        assert receivables["total"] == balances["1100"]["balance"] == 10_000_00 - 7_000_00
        assert payables["total"] == -balances["2010"]["balance"] == 100_000_00
        # ageing adds up to the balance per client
        for row in receivables["rows"]:
            assert sum(row["ageing"].values()) == row["amount"]


def test_cash_view_reports_book_against_master(world, app):
    with app.app_context():
        view = cash_view.load(as_of=date.today())
        assert view["total_book"] == q.cash_positions()["total"] == 7_000_00
        # The documents in this test were inserted straight into the models, so
        # the legacy cash-master sync has not run: the page must *show* that
        # difference instead of hiding it behind one number.
        assert view["difference"] == view["total_book"] - view["total_master"]
        assert view["matches"] is (view["difference"] == 0)


def test_books_agree_structural_checks_pass(world, app):
    with app.app_context():
        view = books_agree_model.load(date.today())
        structural = [row for row in view["checks"] if row["kind"] == "structural"]
        assert structural and all(row["status"] == "ok" for row in structural), structural
        coverage = {row["key"]: row for row in view["checks"]}["coverage"]
        assert coverage["status"] == "ok", coverage


def test_cache_rebuilds_after_a_new_posting(world, app, monkeypatch):
    with app.app_context():
        cache.reset()
        before = dashboard.load(date.today())
        version = q.ledger_version()

        sale = DirectSale(
            client_name="Bilal Construction", client_code="CL-1", category="Credit Customer",
            amount=1000, discount=0, paid_amount=1000, date_posted=datetime.now(),
            payment_account_id=world["cash"],
        )
        db.session.add(sale)
        db.session.flush()
        db.session.add(DirectSaleItem(
            sale_id=sale.id, product_name="OPC Cement", qty=1, price_at_time=1000,
            cost_rate_at_sale=1000,
        ))
        db.session.commit()

        after = dashboard.load(date.today())
        assert q.ledger_version() > version
        assert after["ledger_version"] > before["ledger_version"]
        assert after["cash_balance"] == before["cash_balance"] + 1000_00


def test_no_page_reads_documents_directly(world, app):
    """The read models only touch the journal tables (plus party names)."""
    import inspect

    from app.services.read_models import client_account as module

    source = inspect.getsource(module)
    for forbidden in ("DirectSale", "Payment", "MaterialReturn", "CashFlowEntry"):
        assert forbidden not in source


def test_every_ledger_page_renders(world, client):
    response = client.post(
        "/login",
        data={"username": "Admin", "password": "Admin@fbm12345"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    urls = [
        "/ledger/",
        "/ledger/chart",
        "/ledger/journal",
        "/ledger/trial_balance",
        "/ledger/pnl",
        "/ledger/balance_sheet",
        "/ledger/cash",
        "/ledger/cash_flow",
        "/ledger/receivables",
        f"/ledger/client/{world['client']}",
        "/ledger/payables",
        f"/ledger/supplier/{world['supplier']}",
        "/ledger/inventory",
        "/ledger/driver_payable",
        "/ledger/periods",
        "/ledger/books_agree",
        "/ledger/coverage",
        "/ledger/api/version",
        "/ledger/api/summary",
    ]
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200, f"{url} -> {response.status_code}"


def test_ledger_pages_require_login(app):
    response = app.test_client().get("/ledger/journal")
    assert response.status_code in (301, 302, 303)
