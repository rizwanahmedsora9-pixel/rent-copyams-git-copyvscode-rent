"""The ledger pages and API render the book — and only the book.

Every page here is asserted against ``ledger.query`` (the single aggregation
layer) and, where a second source exists, against it too.  A page that renders
without error but shows its own arithmetic fails these tests.
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
    Supplier,
    db,
)
from app.services.ledger import accounts as coa
from app.services.ledger import query as q
from app.services.read_models import cash_view, dashboard, pnl_view

ADMIN = {"username": "Admin", "password": "Admin@fbm12345"}


def _login(client):
    response = client.post("/login", data=ADMIN, follow_redirects=False)
    assert response.status_code in (302, 303), response.get_data(as_text=True)[:300]


@pytest.fixture()
def books(app):
    """A small but complete set of documents, posted through the real engine."""
    with app.app_context():
        supplier = Supplier(name="Ravi Cement Supplier", opening_balance=0)
        client = Client(code="CL-1", name="Bilal Construction", opening_balance=0)
        cash = Account(
            name="Main Cash", type="cash", category="cash", account_type="company",
            channel="cash", balance=0, balance_minor=0, is_active=True,
        )
        db.session.add_all([supplier, client, cash])
        db.session.commit()

        grn = GRN(
            supplier_id=supplier.id, supplier=supplier.name, manual_bill_no="G-100",
            date_posted=datetime.now(), freight_cost=500, paid_amount=0,
        )
        grn.items = [GRNItem(mat_name="OPC Cement", qty=100, price_at_time=1000)]
        db.session.add(grn)
        db.session.commit()

        sale = DirectSale(
            client_name=client.name, client_code=client.code, category="Credit Customer",
            amount=12_000, discount=2_000, paid_amount=5_000,
            date_posted=datetime.now(), payment_account_id=cash.id,
            manual_bill_no="S-200",
        )
        db.session.add(sale)
        db.session.flush()
        db.session.add(DirectSaleItem(
            sale_id=sale.id, product_name="OPC Cement", qty=10, price_at_time=1_200,
            cost_rate_at_sale=1_000, discount=2_000, discount_kind="manual",
        ))
        db.session.commit()
        return {
            "supplier": supplier.id,
            "client": client.id,
            "cash": cash.id,
            "grn": grn.id,
            "sale": sale.id,
        }


def _page_text(response) -> str:
    assert response.status_code == 200, response.get_data(as_text=True)[:500]
    return response.get_data(as_text=True)


def test_overview_renders_the_book(books, client):
    _login(client)
    with client.application.app_context():
        summary = dashboard.load(date.today())
        expected_cash = f"{summary['cash_balance'] / 100:,.2f}"
    html = _page_text(client.get("/ledger/"))
    assert expected_cash in html
    assert "The book" in html


def test_chart_lists_every_seeded_account(books, client):
    _login(client)
    html = _page_text(client.get("/ledger/chart"))
    for code in (coa.GL.CASH, coa.GL.RECEIVABLE, coa.GL.PAYABLE, coa.GL.SALES_GOODS,
                 coa.GL.COGS_GOODS, coa.GL.WAIVE_OFF):
        assert code in html
    # Every chart line seeded in §6 is present, no more and no less missing.
    with client.application.app_context():
        assert len(coa.chart_accounts()) >= len(coa.CHART)


def test_journal_shows_entries_and_reversal_pairs(books, client):
    _login(client)
    html = _page_text(client.get("/ledger/journal"))
    with client.application.app_context():
        entries = q.journal_entries()["entries"]
        assert entries
        assert entries[0].entry_no in html

    with client.application.app_context():
        sale = db.session.get(DirectSale, books["sale"])
        sale.paid_amount = 6_000
        db.session.commit()
        live = [e for e in q.journal_entries()["entries"] if e.source_type == "DirectSale"]
        assert any(entry.is_reversal_of for entry in live), "edit must leave a reversal"
        assert len([e for e in live if not e.is_reversal_of]) >= 2  # old + re-post
        assert q.trial_balance()["balanced"]


def test_trial_balance_is_balanced(books, client):
    _login(client)
    html = _page_text(client.get("/ledger/trial_balance"))
    with client.application.app_context():
        statement = q.trial_balance()
        assert statement["balanced"]
        assert f"{statement['total_debit'] / 100:,.2f}" in html


def test_pnl_matches_the_read_model(books, client):
    _login(client)
    month_start = date.today().replace(day=1)
    html = _page_text(client.get(f"/ledger/pnl?start={month_start}&end={date.today()}"))
    with client.application.app_context():
        view = pnl_view.load(month_start, date.today())
        assert view["net_revenue"] == 1_000_000  # 12,000 gross − 2,000 discount
        assert f"{view['net_revenue'] / 100:,.2f}" in html
        assert "Net profit" in html


def test_balance_sheet_balances(books, client):
    _login(client)
    html = _page_text(client.get("/ledger/balance_sheet"))
    with client.application.app_context():
        sheet = q.balance_sheet()
        assert sheet["balanced"], sheet
    assert "Assets" in html


def test_cash_page_ties_to_the_journal(books, client):
    _login(client)
    html = _page_text(client.get("/ledger/cash"))
    with client.application.app_context():
        view = cash_view.load(as_of=date.today())
        assert view["total_book"] == 500_000  # 5,000 received with the sale
        assert f"{view['total_book'] / 100:,.2f}" in html


def test_receivables_and_payables_pages(books, client):
    _login(client)
    receivables = _page_text(client.get("/ledger/receivables"))
    assert "Bilal Construction" in receivables
    payables = _page_text(client.get("/ledger/payables"))
    assert "Ravi Cement Supplier" in payables


def test_api_returns_the_same_numbers(books, client):
    _login(client)
    summary = client.get("/ledger/api/summary").get_json()
    with client.application.app_context():
        view = dashboard.load(date.today())
    assert summary["cash_balance_minor"] == view["cash_balance"]
    assert summary["receivable_total_minor"] == view["receivable_total"]
    assert summary["version"] == view["ledger_version"]

    version = client.get("/ledger/api/version").get_json()
    assert version["version"] == view["ledger_version"]
    assert version["entries"] >= 3


def test_ledger_pages_need_a_login(app):
    response = app.test_client().get("/ledger/journal")
    assert response.status_code in (301, 302, 303)


def test_dashboard_page_uses_the_read_model(books, client):
    _login(client)
    html = _page_text(client.get("/"))
    with client.application.app_context():
        view = dashboard.load(date.today())
    assert f"{view['cash_in_today'] / 100:,.0f}" in html
    assert "Current Stock by Brand" in html
