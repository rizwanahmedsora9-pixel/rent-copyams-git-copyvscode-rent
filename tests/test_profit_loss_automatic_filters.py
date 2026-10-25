"""Automatic filters and the results-region refresh for the P&L Control Center.

The filters are transport only: they decide *which* rows are included, never how
a row is valued.  These tests therefore check both halves of that promise:

* the filter selects the expected rows (payment status, booking status,
  transaction type, customer / client category, item, sorting);
* the numbers behind the selected rows still reconcile to the ledger, a credit
  sale is never reduced by the money still owed, and the "estimated" block is
  never mixed into net profit.

Route tests assert that the automatic filter bar works without JavaScript
(``noscript`` submit), that ``?partial=1`` returns only the results region, and
that drill-down links never leak the transport flag.
"""
from __future__ import annotations

import re
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services.profit_loss import build_profit_loss_report
from models import (
    Account,
    AccountTransaction,
    Booking,
    BookingAllocation,
    BookingItem,
    Client,
    DirectSale,
    DirectSaleItem,
    GRN,
    GRNAllocation,
    GRNItem,
    Material,
    Service,
    Supplier,
    db,
)
from tests.conftest import make_csrf_client

PERIOD = {"start_date": "2026-03-01", "end_date": "2026-03-31"}


@pytest.fixture()
def filter_app(app_factory, tmp_path):
    app = app_factory(tmp_path / "profit_filters.db")
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        from app.services.schema import _ensure_default_admin
        _ensure_default_admin()
        db.session.commit()
    return app


def login(client):
    response = client.post("/login", data={"username": "Admin", "password": "Admin@fbm12345"})
    assert response.status_code in (302, 303)


def seed():
    material = Material(name="CEMENT", code="MAT-1", unit="Bags", unit_price=100, total=10000)
    client = Client(name="CUSTOMER ONE", code="CL-1", category="General", is_active=True)
    other = Client(name="OPEN KHATA CO", code="CL-2", category="Open Khata", is_active=True)
    supplier = Supplier(name="SUPPLIER ONE", is_active=True)
    account = Account(name="MAIN CASH", account_type="Cash", category="Cash", type="Cash", is_active=True)
    service = Service(name="RENT-CEMENT", code="SVC-RENT", unit="Bags")
    db.session.add_all([material, client, other, supplier, account, service])
    db.session.flush()
    grn = GRN(supplier_id=supplier.id, supplier=supplier.name, manual_bill_no="GRN-1",
              date_posted=datetime(2026, 2, 20), is_void=False)
    db.session.add(grn)
    db.session.flush()
    lot = GRNItem(grn_id=grn.id, mat_name="CEMENT", qty=1000, price_at_time=100, is_void=False)
    db.session.add(lot)
    db.session.flush()
    return material, client, other, supplier, account, lot


def add_sale(client_row, posted, items, *, category="Credit Customer", paid=0.0, bill=None, delivery_cost=0.0):
    sale = DirectSale(
        client_name=client_row.name, client_code=client_row.code, category=category,
        amount=sum(qty * rate for _name, qty, rate in items), paid_amount=paid,
        manual_bill_no=bill, date_posted=posted, delivery_rent_cost=delivery_cost, is_void=False,
    )
    db.session.add(sale)
    db.session.flush()
    rows = []
    for name, qty, rate in items:
        item = DirectSaleItem(sale_id=sale.id, product_name=name, qty=qty, price_at_time=rate)
        db.session.add(item)
        db.session.flush()
        rows.append(item)
    return sale, rows


def allocate(sale, item, lot, qty, rate=100.0):
    db.session.add(GRNAllocation(
        sale_id=sale.id, sale_item_id=item.id, grn_item_id=lot.id, qty=qty, cost_rate=rate, is_void=False,
    ))


@pytest.fixture()
def scenario(filter_app):
    """March 2026 with goods, a service, a booking supply, a credit balance and an expense."""
    with filter_app.app_context():
        material, client, other, supplier, account, lot = seed()

        credit_sale, credit_items = add_sale(client, datetime(2026, 3, 5), [("CEMENT", 10, 150)], bill="CREDIT-1")
        cash_sale, cash_items = add_sale(client, datetime(2026, 3, 6), [("CEMENT", 1, 200)],
                                         category="Cash", paid=200, bill="CASH-1")
        service_sale, service_items = add_sale(client, datetime(2026, 3, 8), [("RENT-CEMENT", 10, 500)],
                                               bill="SVC-1", delivery_cost=3500)
        other_sale, other_items = add_sale(other, datetime(2026, 3, 10), [("CEMENT", 2, 300)], bill="OTHER-1")

        booking = Booking(client_name=client.name, amount=10000, paid_amount=2000, manual_bill_no="BK-1",
                          date_posted=datetime(2026, 3, 1), is_void=False)
        db.session.add(booking)
        db.session.flush()
        booked = BookingItem(booking_id=booking.id, material_name="CEMENT", qty=100, price_at_time=100)
        db.session.add(booked)
        db.session.flush()
        supply, supply_items = add_sale(client, datetime(2026, 3, 12), [("CEMENT", 40, 0)],
                                        category="Booking Delivery", bill="SUPPLY-40")
        db.session.add(BookingAllocation(
            sale_id=supply.id, sale_item_id=supply_items[0].id, booking_item_id=booked.id, qty=40, is_void=False,
        ))
        db.session.add(AccountTransaction(
            from_account_id=account.id, amount=100, description="Fuel", transaction_type="Expense",
            date_posted=datetime(2026, 3, 9), is_void=False,
        ))
        allocate(credit_sale, credit_items[0], lot, 10)
        allocate(cash_sale, cash_items[0], lot, 1)
        allocate(other_sale, other_items[0], lot, 2)
        db.session.commit()
        return filter_app


def report(**kwargs):
    filters = dict(PERIOD)
    filters.update(kwargs)
    return SimpleNamespace(**build_profit_loss_report(**filters))


# --------------------------------------------------------------------------- #
# Filter semantics
# --------------------------------------------------------------------------- #
def test_01_baseline_totals_and_reconciliation(scenario):
    with scenario.app_context():
        base = report()
        assert base.entries_count == 6
        assert base.summary["total_material_revenue"] == pytest.approx(6300.0)   # 1500 + 200 + 600 + 4000
        assert base.summary["total_material_cost"] == pytest.approx(5300.0)      # 1000 + 100 + 200 + 4000
        assert base.summary["total_delivery_revenue"] == pytest.approx(5000.0)
        assert base.summary["total_delivery_cost"] == pytest.approx(3500.0)
        assert base.summary["total_expenses"] == pytest.approx(100.0)
        assert base.summary["total_profit"] == pytest.approx(2400.0)             # 1000 + 1500 - 100

        breakdown = base.breakdown
        assert breakdown["reconcile"]["matches"] is True
        assert breakdown["reconcile"]["net_profit"] == pytest.approx(base.summary["total_profit"])
        assert breakdown["reconcile"]["gross_profit"] == pytest.approx(1000.0)
        assert breakdown["reconcile"]["service_margin"] == pytest.approx(1500.0)
        assert breakdown["reconcile"]["operating_expenses"] == pytest.approx(100.0)

        cards = breakdown["card_index"]
        assert cards["delivered_goods_sales"]["value"] == pytest.approx(6300.0)
        assert cards["cogs_fifo"]["value"] == pytest.approx(5300.0)
        assert cards["gross_profit"]["value"] == pytest.approx(1000.0)
        assert cards["direct_service_costs"]["value"] == pytest.approx(3500.0)
        assert cards["service_margin"]["value"] == pytest.approx(1500.0)
        assert cards["operating_expenses"]["value"] == pytest.approx(100.0)
        assert cards["net_profit"]["value"] == pytest.approx(2400.0)

        # Unpaid money is a receivable, never a deduction from profit.
        assert cards["credit_outstanding"]["value"] == pytest.approx(11100.0)
        assert cards["credit_outstanding"]["realized"] is False
        assert cards["net_profit"]["realized"] is True

        # Estimated future margin sits outside realized profit.
        assert cards["estimated_undelivered_profit"]["value"] == pytest.approx(
            breakdown["estimated_undelivered"]["total"]
        )
        assert cards["estimated_undelivered_profit"]["realized"] is False


def test_02_payment_status_filter(scenario):
    with scenario.app_context():
        credit = report(payment_status="credit")
        keys = {(row["transaction_group"], row["payment_status_key"]) for row in credit.transactions}
        assert all(row["document_outstanding"] > 0 for row in credit.transactions)
        assert keys == {("goods", "unpaid"), ("goods", "partially_paid"), ("services", "unpaid")}
        assert credit.entries_count == 4
        # Excluding the paid sale removes its profit only; nothing is re-valued.
        assert credit.summary["total_profit"] == pytest.approx(2400.0)

        paid = report(payment_status="paid")
        assert paid.entries_count == 1
        assert paid.summary["total_profit"] == pytest.approx(100.0)
        assert paid.breakdown["reconcile"]["matches"] is True

        unpaid = report(payment_status="unpaid")
        assert unpaid.entries_count == 3
        assert unpaid.summary["total_profit"] == pytest.approx(2400.0)
        assert unpaid.breakdown["reconcile"]["matches"] is True


def test_03_transaction_group_filter(scenario):
    with scenario.app_context():
        services = report(transaction_group="services")
        assert services.entries_count == 1
        assert services.summary["total_delivery_revenue"] == pytest.approx(5000.0)
        assert services.summary["total_delivery_cost"] == pytest.approx(3500.0)
        assert services.summary["total_profit"] == pytest.approx(1500.0)
        assert services.breakdown["by_service"][0]["margin"] == pytest.approx(1500.0)

        goods = report(transaction_group="goods")
        assert goods.entries_count == 4
        assert goods.summary["total_material_revenue"] == pytest.approx(6300.0)
        assert goods.summary["total_profit"] == pytest.approx(1000.0)
        assert goods.summary["total_expenses"] == 0

        expenses = report(transaction_group="expenses")
        assert expenses.entries_count == 1
        assert expenses.summary["total_expenses"] == pytest.approx(100.0)
        assert expenses.summary["total_profit"] == pytest.approx(-100.0)
        assert expenses.breakdown["reconcile"]["operating_expenses"] == pytest.approx(100.0)


def test_04_customer_and_client_category_filters(scenario):
    with scenario.app_context():
        by_customer = report(client="CL-2")
        assert by_customer.entries_count == 1
        assert by_customer.summary["total_profit"] == pytest.approx(400.0)

        by_category = report(entity_type="category", client_category="Open Khata")
        assert by_category.entries_count == 1
        assert by_category.summary["total_profit"] == pytest.approx(400.0)
        assert {row["client_category"] for row in by_category.transactions} == {"Open Khata"}

        # General = credit sale, cash sale, service and the booking supply.
        general = report(entity_type="category", client_category="General")
        assert general.entries_count == 4
        assert general.summary["total_profit"] == pytest.approx(2100.0)

        # Selecting a customer switches the filter back to the customer axis;
        # a stray client_category is ignored unless entity_type says otherwise.
        named = report(entity_type="", client_category="Open Khata", client="CUSTOMER ONE")
        assert named.entries_count == 4
        assert {row["client"] for row in named.transactions} == {"CUSTOMER ONE"}


def test_05_booking_status_filter(scenario):
    with scenario.app_context():
        partial = report(booking_status="partially_delivered")
        assert partial.entries_count == 1
        assert partial.summary["total_material_revenue"] == pytest.approx(4000.0)
        assert partial.summary["total_profit"] == pytest.approx(0.0)
        assert partial.transactions[0]["booking_status_key"] == "partially_delivered"

        assert report(booking_status="open").entries_count == 0
        assert report(booking_status="completed").entries_count == 0


def test_06_sorting_never_changes_the_totals(scenario):
    with scenario.app_context():
        base = report()
        ascending = report(sort="profit", order="asc")
        descending = report(sort="profit", order="desc")

        profits = [row["profit"] for row in ascending.transactions]
        assert profits == sorted(profits)
        assert [row["profit"] for row in descending.transactions] == sorted(profits, reverse=True)

        assert ascending.summary == base.summary
        assert descending.summary == base.summary
        assert ascending.entries_count == descending.entries_count == base.entries_count
        assert ascending.breakdown["reconcile"]["net_profit"] == pytest.approx(base.summary["total_profit"])


def test_07b_grn_lot_filter_selects_the_rows_that_consumed_it(scenario):
    """The GRN / batch filter pins the sales whose FIFO cost came from that lot."""
    with scenario.app_context():
        lot = GRNItem.query.one()
        filtered = report(grn_id=lot.id)
        assert filtered.entries_count > 0
        assert all(lot.id in (row.get("grn_ids") or []) for row in filtered.transactions)
        assert filtered.breakdown["reconcile"]["matches"] is True

        unmatched = report(grn_id=999999)
        assert unmatched.entries_count == 0
        assert unmatched.summary["total_profit"] == pytest.approx(0.0)


def test_07_filters_never_leak_into_the_arithmetic(scenario):
    """Every filtered view still reconciles, and only the selected rows are valued."""
    with scenario.app_context():
        base = report()
        for kwargs in (
            {"payment_status": "credit"},
            {"payment_status": "paid"},
            {"transaction_group": "goods"},
            {"transaction_group": "services"},
            {"transaction_group": "expenses"},
            {"entity_type": "category", "client_category": "General"},
            {"booking_status": "partially_delivered"},
            {"sort": "revenue", "order": "asc"},
        ):
            view = report(**kwargs)
            reconcile = view.breakdown["reconcile"]
            assert reconcile["matches"] is True, kwargs
            assert reconcile["net_profit"] == pytest.approx(view.summary["total_profit"]), kwargs
            assert view.entries_count <= base.entries_count
            # No filter may invent profit: a subset can never earn more than all rows.
            assert reconcile["net_profit"] <= base.summary["total_profit"] + 1e-6, kwargs


# --------------------------------------------------------------------------- #
# Routes: automatic filter bar, partial refresh, drill-down links
# --------------------------------------------------------------------------- #
def test_08_filter_bar_and_partial_region(scenario):
    client = make_csrf_client(scenario)
    login(client)

    full = client.get("/profit_reports?period=custom&start_date=2026-03-01&end_date=2026-03-31")
    assert full.status_code == 200
    html = full.get_data(as_text=True)

    for needle in (
        'id="plFilterForm"', 'id="plResultsRegion"', 'id="plLoadingBar"', 'id="plFilterState"',
        "profit_report.js", "PL_FILTER_DATA", "FBMProfitFilters.init",
        'name="period"', 'name="start_date"', 'name="end_date"', 'name="entity_type"',
        'name="client"', 'name="client_category"', 'name="material"', 'name="transaction_group"',
        'name="payment_status"', 'name="booking_status"', 'name="grn_id"', 'name="per_page"',
        'data-combo="client"', 'data-combo="material"', 'data-pl-auto', 'data-pl-debounce',
        "data-pl-results", "<noscript>",
    ):
        assert needle in html, needle
    # There is no Apply button in the JavaScript path.
    assert "Apply filters" not in html

    partial = client.get(
        "/profit_reports?period=custom&start_date=2026-03-01&end_date=2026-03-31&partial=1"
    )
    assert partial.status_code == 200
    region = partial.get_data(as_text=True)
    assert "<!DOCTYPE" not in region and "<html" not in region
    assert 'id="plFilterForm"' not in region
    assert 'id="plResults"' in region and "data-pl-results" in region
    assert "Net Profit / Loss" in region
    # Same figures as the full page: the partial is a re-render, not a recount.
    assert "2,400.00" in region
    assert "2,400.00" in html


def test_09_filtered_route_reflects_the_selection(scenario):
    client = make_csrf_client(scenario)
    login(client)
    params = {"period": "custom", "start_date": "2026-03-01", "end_date": "2026-03-31"}

    response = client.get("/profit_reports", query_string={**params, "transaction_group": "services", "partial": "1"})
    assert response.status_code == 200
    region = response.get_data(as_text=True)
    assert "1,500.00" in region            # service margin
    assert "Showing 1–1 of 1" in region or "1 matching entries" in region

    response = client.get("/profit_reports", query_string={**params, "payment_status": "paid", "partial": "1"})
    region = response.get_data(as_text=True)
    assert "Showing 1–1 of 1" in region or "1 matching entries" in region

    # A reversed range is normalised instead of failing.
    response = client.get("/profit_reports", query_string={
        "period": "custom", "start_date": "2026-03-31", "end_date": "2026-03-01", "partial": "1",
    })
    assert response.status_code == 200

    # Entries view keeps working and stays a full page.
    entries = client.get("/profit_reports", query_string={**params, "view": "entries"})
    assert entries.status_code == 200
    assert "<html" in entries.get_data(as_text=True)


def test_10_drill_down_links_never_carry_the_partial_flag(scenario):
    client = make_csrf_client(scenario)
    login(client)
    response = client.get("/profit_reports?period=custom&start_date=2026-03-01&end_date=2026-03-31&partial=1")
    html = response.get_data(as_text=True)
    hrefs = re.findall(r'href="([^"]+)"', html)
    drill = [href for href in hrefs if "profit_reports" in href]
    assert drill, "no drill-down links rendered"
    assert all("partial" not in href for href in drill)

    # The route builds the card links; check them through the rendered markup.
    assert 'href="#pl-undelivered-estimate"' not in html  # estimate card is not a link
    assert 'id="pl-undelivered-estimate"' in html


def test_11_estimated_block_is_visually_separate_from_realized_profit(scenario):
    client = make_csrf_client(scenario)
    login(client)
    html = client.get("/profit_reports?period=custom&start_date=2026-03-01&end_date=2026-03-31").get_data(as_text=True)

    realized = html.split("Realized profit", 1)[1].split("Position &amp; estimates", 1)[0]
    estimates = html.split("Position &amp; estimates", 1)[1].split("Reconciliation", 1)[0]
    assert "Estimated Profit on Undelivered Bookings" in estimates
    assert "Estimated Profit on Undelivered Bookings" not in realized
    # The single headline figure is the realized net, never a blend with the estimate.
    assert "Net Profit / Loss for the selected period" in html
    assert html.index("Net Profit / Loss for the selected period") < html.index("Realized profit")
