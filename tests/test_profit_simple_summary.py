"""Profit section — plain-language summary.

The owner asked for the profit/loss section to be readable by non-accountants:

* real words (Sales / Cost of Goods / Profit / Money Received) instead of
  COGS / margin / markup / GRN;
* two clickable cards — **Total Profit** and **Total Loss** — that drill into
  a list-style page of those entries;
* the detailed transaction list hidden behind a single "View all transactions"
  link instead of being the default view;
* the internal "unknown cost" bookkeeping kept out of the way unless it
  actually explains a gap in the numbers.

This test pins those wording/visibility guarantees so a future change cannot
silently bring the jargon back.
"""
from __future__ import annotations

import re
from datetime import datetime

import pytest

from models import (
    Account,
    Booking,
    BookingItem,
    Client,
    DirectSale,
    DirectSaleItem,
    Material,
    Payment,
    Service,
    db,
)


def login(client, username="Admin", password="Admin@fbm12345"):
    resp = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:400]


@pytest.fixture()
def app(app_factory, tmp_path):
    app = app_factory(tmp_path / "profit.db")
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        from app.services.schema import _ensure_default_admin
        _ensure_default_admin()
        db.session.commit()
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def _seed(app):
    with app.app_context():
        cement = Material(name="OPC CEMENT", code="MAT-001", unit="Bags",
                          unit_price=1200, total=1000)
        db.session.add(cement)
        db.session.flush()

        c1 = Client(name="AL RAHMAN TRADERS", code="CL-001", category="General",
                    is_active=True)
        c2 = Client(name="BILAL BUILDERS", code="CL-002", category="General",
                    is_active=True)
        db.session.add_all([c1, c2])
        db.session.flush()

        acc = Account(name="MAIN CASH", account_type="Cash", category="Cash",
                      is_active=True)
        db.session.add(acc)
        db.session.flush()
        db.session.commit()

        # Profitable booking: 1000 bags @ 1250, cost 1200 => +50,000
        booking = Booking(
            client_name="AL RAHMAN TRADERS", amount=1250000, paid_amount=500000,
            manual_bill_no="MB NO.1", date_posted=datetime(2026, 1, 10, 10, 0, 0),
            is_void=False, discount=0, receive_in_account_id=acc.id,
        )
        db.session.add(booking)
        db.session.flush()
        db.session.add(BookingItem(booking_id=booking.id, material_name="OPC CEMENT",
                                   qty=1000, price_at_time=1250))

        # Profitable direct sale: 100 bags @ 1300, cost 1200 => +10,000
        sale = DirectSale(
            client_name="AL RAHMAN TRADERS", client_code="CL-001",
            category="Credit Customer", amount=130000, paid_amount=0,
            manual_bill_no="MB NO.2", date_posted=datetime(2026, 1, 12, 11, 0, 0),
            is_void=False, discount=0, payment_method="Cash",
            payment_account_id=acc.id,
        )
        db.session.add(sale)
        db.session.flush()
        db.session.add(DirectSaleItem(sale_id=sale.id, product_name="OPC CEMENT",
                                      qty=100, price_at_time=1300))

        # Loss direct sale: 10 bags @ 1000, cost 1200 => -2,000
        loss_sale = DirectSale(
            client_name="BILAL BUILDERS", client_code="CL-002",
            category="Credit Customer", amount=10000, paid_amount=0,
            manual_bill_no="MB NO.3", date_posted=datetime(2026, 1, 14, 12, 0, 0),
            is_void=False, discount=0, payment_method="Cash",
            payment_account_id=acc.id,
        )
        db.session.add(loss_sale)
        db.session.flush()
        db.session.add(DirectSaleItem(sale_id=loss_sale.id, product_name="OPC CEMENT",
                                      qty=10, price_at_time=1000))

        db.session.add(Payment(
            client_id=c1.id, client_name="AL RAHMAN TRADERS", amount=400000,
            method="Cash", payment_type="Receipt", manual_bill_no="MB NO.4",
            date_posted=datetime(2026, 1, 20, 9, 0, 0), is_void=False,
            payment_account_id=acc.id,
        ))
        db.session.commit()


def _get(client, url):
    resp = client.get(url)
    assert resp.status_code == 200, url
    return resp.get_data(as_text=True)


def _text(body):
    # Rendered pages wrap labels and values across lines, <br> tags and
    # elements; strip the markup and collapse whitespace so the wording that
    # a browser shows can be asserted on.
    body = re.sub(r"<script.*?</script>", " ", body, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    return " ".join(body.split())


def test_profit_summary_uses_plain_language(app, client):
    login(client)
    _seed(app)
    body = _get(client, "/profit_reports?start_date=2026-01-01&end_date=2026-01-31")

    # Readable headlines.
    for label in ["Profit Summary", "Sales (Money In)", "Cost of Goods (Money Out)",
                  "Money Received", "Sales by Customer", "Sales by Item"]:
        assert label in body, label

    # The owner's own numbers show up.
    assert "AL RAHMAN TRADERS" in body
    assert "OPC CEMENT" in body

    # The drill-down is reachable from the summary page.
    assert "View All Transactions" in body


def test_profit_summary_hides_accounting_jargon(app, client):
    login(client)
    _seed(app)
    body = _get(client, "/profit_reports?start_date=2026-01-01&end_date=2026-01-31")

    for hidden in ["Estimated COGS", "Margin %", "Markup %", "GRN-wise Sales Profit",
                   "Unknown Cost Rows", "Open All Entries Page", "Profit/Loss Entries"]:
        assert hidden not in body, hidden


def test_profit_and_loss_cards_link_to_list_pages(app, client):
    login(client)
    _seed(app)
    body = _get(client, "/profit_reports?start_date=2026-01-01&end_date=2026-01-31")

    assert "Total Profit" in body
    assert "Total Loss" in body
    assert "View profit entries" in body
    assert "View loss entries" in body

    profit_page = _get(
        client,
        "/profit_reports?view=entries&metric=profit"
        "&start_date=2026-01-01&end_date=2026-01-31",
    )
    assert "Profit Entries" in profit_page
    assert "profitable entries" in profit_page.lower()

    loss_page = _get(
        client,
        "/profit_reports?view=entries&metric=loss"
        "&start_date=2026-01-01&end_date=2026-01-31",
    )
    assert "Loss Entries" in loss_page


def test_transactions_page_is_plain(app, client):
    login(client)
    _seed(app)
    body = _get(client,
                "/profit_reports?view=entries&start_date=2026-01-01&end_date=2026-01-31")

    assert "Transactions" in body
    assert "Every Sale in This Period" in body
    for col in ["Money In", "Cost of Item", "Profit"]:
        assert col in body, col

    # Per-row bookkeeping columns are gone.
    assert "Delivery Rent Variance" not in body
    assert "Account Reconciliation" not in body


def _seed_rent(app, revenue=500, delivery_cost=600, discount=0):
    with app.app_context():
        db.session.add(Service(name="RENT-CEMENT", code="SVC-001", unit="Bags"))
        db.session.add(Client(name="AL RAHMAN", code="CL-001",
                              category="General", is_active=True))
        acc = Account(name="MAIN CASH", account_type="Cash", category="Cash",
                      is_active=True)
        db.session.add(acc)
        db.session.commit()
        sale = DirectSale(
            client_name="AL RAHMAN", client_code="CL-001", category="Credit Customer",
            amount=revenue, paid_amount=0, manual_bill_no="MB NO.RENT-1",
            date_posted=datetime(2026, 2, 1, 10, 0, 0), is_void=False, discount=discount,
            payment_method="Cash", payment_account_id=acc.id,
            rent_item_revenue=revenue, delivery_rent_cost=delivery_cost,
        )
        db.session.add(sale)
        db.session.flush()
        db.session.add(DirectSaleItem(sale_id=sale.id, product_name="RENT-CEMENT",
                                      qty=1, price_at_time=revenue))
        db.session.commit()


def test_rent_sale_is_one_entry(app, client):
    # Revenue 500 from the rent service, 600 paid to the delivery person:
    # the page shows ONE entry - a 100 rent loss - not a +500 line plus a
    # separate -100 loss line.
    login(client)
    _seed_rent(app, revenue=500, delivery_cost=600)

    body = _text(_get(client, "/profit_reports?view=entries&start_date=2026-02-01&end_date=2026-02-28"))
    assert "1 entries" in body
    assert "Delivery Rent Difference" not in body
    # The rent line shows the money in, the cost and the net loss, with the
    # transaction reference so the user can trace it.
    assert "Rent" in body
    assert "Bill / Ref: MB NO.RENT-1" in body
    assert "Money In: Rs. 500.00" in body
    assert "Cost of Item: Rs. 600.00" in body
    assert "Rs. -100.00" in body

    summary = _text(_get(client, "/profit_reports?start_date=2026-02-01&end_date=2026-02-28"))
    assert "Sales (Money In) Rs. 500.00" in summary
    assert "Cost of Goods (Money Out) Rs. 600.00" in summary
    assert "Profit (Money Kept) Rs. -100.00" in summary
    assert "Total Loss Rs. 100.00" in summary
    assert "Total Profit Rs. 0.00" in summary


def test_rent_sale_profitable_is_one_profit_entry(app, client):
    # Rent revenue above the delivery charge is shown as one profit entry.
    login(client)
    _seed_rent(app, revenue=600, delivery_cost=500)

    summary = _text(_get(client, "/profit_reports?start_date=2026-02-01&end_date=2026-02-28"))
    assert "Profit (Money Kept) Rs. 100.00" in summary
    assert "Total Profit Rs. 100.00" in summary
    assert "Total Loss Rs. 0.00" in summary

    body = _text(_get(client, "/profit_reports?view=entries&start_date=2026-02-01&end_date=2026-02-28"))
    assert "1 entries" in body
    assert "Rent" in body
    assert "Bill / Ref: MB NO.RENT-1" in body
