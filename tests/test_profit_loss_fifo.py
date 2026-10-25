"""FIFO regressions for the Profit & Loss Control Center.

The P&L values every dispatch from the GRN lot queue (oldest lot first).  These
tests pin the three places where that queue used to drift away from the FIFO
posted at the counter:

1. several lines of the *same* sale must take lots in sequence, not all from
   the oldest lot;
2. a delivery that predates every purchase must be valued from the nearest
   later lot, never from the newest purchase in the dataset;
3. a GRN posted later on the same calendar day is available to that day's
   deliveries, exactly as posting-time FIFO treats it.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services.profit_loss import build_profit_loss_report
from models import (
    Client,
    DirectSale,
    DirectSaleItem,
    GRN,
    GRNAllocation,
    GRNItem,
    Material,
    Supplier,
    db,
)
from tests.conftest import make_csrf_client


@pytest.fixture()
def fifo_app(app_factory, tmp_path):
    app = app_factory(tmp_path / "profit_fifo.db")
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


def add_grn(supplier, material, qty, rate, posted, bill):
    grn = GRN(
        supplier_id=supplier.id, supplier=supplier.name, manual_bill_no=bill,
        date_posted=posted, is_void=False,
    )
    db.session.add(grn)
    db.session.flush()
    item = GRNItem(grn_id=grn.id, mat_name=material.name, qty=qty, price_at_time=rate)
    db.session.add(item)
    db.session.flush()
    return grn, item


def add_sale(client_row, posted, lines, bill, category="Credit Customer"):
    sale = DirectSale(
        client_name=client_row.name, client_code=client_row.code, category=category,
        date_posted=posted, manual_bill_no=bill, paid_amount=0, is_void=False,
    )
    db.session.add(sale)
    db.session.flush()
    items = []
    for name, qty, rate in lines:
        item = DirectSaleItem(sale_id=sale.id, product_name=name, qty=qty, price_at_time=rate)
        db.session.add(item)
        items.append(item)
    db.session.flush()
    return sale, items


def seed(fifo_app, material_rate=0.0):
    material = Material(name="CEMENT", code="MAT-1", unit="Bags", unit_price=material_rate, total=1000)
    client_row = Client(name="CUSTOMER ONE", code="CL-1", category="General", is_active=True)
    supplier = Supplier(name="SUPPLIER ONE", is_active=True)
    db.session.add_all([material, client_row, supplier])
    db.session.flush()
    return material, client_row, supplier


def report(start="2026-01-01", end="2026-12-31", **kwargs):
    return SimpleNamespace(**build_profit_loss_report(start_date=start, end_date=end, **kwargs))


def material_rows(rep, material="CEMENT"):
    return [row for row in rep.rows if row["material"] == material and row["entry_type"] in ("material_sale", "booking_supply")]


def test_two_lines_of_one_sale_consume_lots_in_sequence(fifo_app):
    """Line 1 takes the oldest lot; line 2 continues where line 1 stopped."""
    client = make_csrf_client(fifo_app)
    login(client)
    with fifo_app.app_context():
        material, client_row, supplier = seed(fifo_app)
        add_grn(supplier, material, 50, 80, datetime(2026, 1, 1, 9, 0), "GRN-80")
        add_grn(supplier, material, 100, 90, datetime(2026, 1, 2, 9, 0), "GRN-90")
        db.session.commit()

        response = client.post("/add_direct_sale", data={
            "client_name": client_row.name,
            "driver_name": "Driver",
            "category": "Credit Customer",
            "product_name[]": ["CEMENT", "CEMENT"],
            "qty[]": ["40", "40"],
            "unit_rate[]": ["150", "160"],
            "payment_method": "Credit",
            "paid_amount": "0",
            "manual_bill_no": "MULTI-1",
            "sale_date": "2026-01-05",
            "allow_negative_stock": "1",
        }, follow_redirects=False)
        assert response.status_code in (302, 303), response.get_data(as_text=True)[:400]

        sale = DirectSale.query.filter(DirectSale.manual_bill_no.like("%MULTI-1%")).one()
        items = DirectSaleItem.query.filter_by(sale_id=sale.id).order_by(DirectSaleItem.id).all()
        # 40 units from the 80-rate lot, then 10 more from it and 30 from 90.
        assert [(item.qty, item.cost_rate_at_sale) for item in items] == [
            (40.0, 80.0), (10.0, 80.0), (30.0, 90.0),
        ]
        allocations = GRNAllocation.query.filter_by(sale_id=sale.id).order_by(GRNAllocation.id).all()
        first_lot = items[0].grn_item_id
        assert [round(a.qty, 2) for a in allocations] == [40.0, 10.0, 30.0]
        assert allocations[0].grn_item_id == allocations[1].grn_item_id == first_lot
        assert allocations[2].grn_item_id != first_lot
        # No lot may be promised more stock than it holds.
        consumed = {}
        for allocation in allocations:
            consumed[allocation.grn_item_id] = consumed.get(allocation.grn_item_id, 0.0) + allocation.qty
        assert consumed[first_lot] == pytest.approx(50.0)

        day = report("2026-01-05", "2026-01-05")
        rows = material_rows(day)
        assert sum(row["qty"] for row in rows) == pytest.approx(80.0)
        # 40*80 + 10*80 + 30*90 = 6,700 -- the old snapshot-per-line plan
        # produced 40*80 + 40*80 = 6,400 and over-consumed the 80-rate lot.
        assert day.summary["total_cogs"] == pytest.approx(6700.0)
        assert day.summary["total_material_revenue"] == pytest.approx(40 * 150 + 40 * 160)
        fifo_sources = [source for row in rows for source in row["cost_sources"]]
        assert sum(source.get("qty", 0) for source in fifo_sources) == pytest.approx(80.0)
        assert sum(source.get("amount", 0) for source in fifo_sources) == pytest.approx(6700.0)
        assert all(source.get("reference") and source.get("date") for source in fifo_sources)
        detail_response = client.get(f"/profit_reports/transaction/DirectSale/{sale.id}")
        assert detail_response.status_code == 200
        assert b"GRN cost layers (qty \xc3\x97 unit cost)" in detail_response.data
        assert b"GRN-80" in detail_response.data
        assert b"40.00 \xc3\x97 Rs. 80.00" in detail_response.data


def test_delivery_before_every_purchase_uses_the_nearest_later_lot(fifo_app):
    """A January delivery is valued from the March lot, not the December one."""
    with fifo_app.app_context():
        material, client_row, supplier = seed(fifo_app)
        add_grn(supplier, material, 100, 70, datetime(2026, 3, 1), "GRN-MAR")
        add_grn(supplier, material, 100, 90, datetime(2026, 6, 1), "GRN-JUN")
        add_grn(supplier, material, 100, 200, datetime(2026, 12, 1), "GRN-DEC")
        add_sale(client_row, datetime(2026, 1, 15, 10, 0), [("CEMENT", 20, 150)], "LEGACY-JAN")
        db.session.commit()

        january = report("2026-01-01", "2026-01-31")
        rows = material_rows(january)
        assert len(rows) == 1
        assert rows[0]["cost_rate"] == pytest.approx(70.0)
        assert rows[0]["cost_status"] == "estimated"
        assert "nearest later GRN" in rows[0]["cost_basis"]
        assert january.summary["total_cogs"] == pytest.approx(1400.0)
        assert january.summary["total_profit"] == pytest.approx(20 * 150 - 1400)

        # Later deliveries keep walking the queue oldest-first.
        add_sale(client_row, datetime(2026, 3, 10), [("CEMENT", 100, 150)], "MAR-SALE")
        db.session.commit()
        march = report("2026-03-01", "2026-03-31")
        assert march.summary["total_cogs"] == pytest.approx(7000.0)

        add_sale(client_row, datetime(2026, 6, 10), [("CEMENT", 100, 150)], "JUN-SALE")
        db.session.commit()
        june = report("2026-06-01", "2026-06-30")
        assert june.summary["total_cogs"] == pytest.approx(9000.0)


def test_lot_posted_later_the_same_day_is_consumed_by_that_days_delivery(fifo_app):
    """Posting-time FIFO compares dates, so the P&L must too."""
    with fifo_app.app_context():
        material, client_row, supplier = seed(fifo_app)
        add_grn(supplier, material, 100, 80, datetime(2026, 1, 5, 8, 0), "GRN-OLD")
        add_grn(supplier, material, 100, 95, datetime(2026, 1, 10, 8, 0), "GRN-MORNING")
        add_grn(supplier, material, 100, 130, datetime(2026, 1, 10, 18, 0), "GRN-EVENING")
        # Imported dispatch with a bare date (00:00) — before both GRNs of its
        # own day are posted, but on that day.
        add_sale(client_row, datetime(2026, 1, 10, 0, 0), [("CEMENT", 120, 150)], "SAME-DAY")
        db.session.commit()

        day = report("2026-01-10", "2026-01-10")
        rows = material_rows(day)
        assert len(rows) == 1
        # 100 * 80 + 20 * 95 = 9,900.  Comparing full datetimes hid the
        # evening lot from the queue and costed the 20 units from the newest
        # GRN of that date (130), i.e. 10,600.
        assert day.summary["total_cogs"] == pytest.approx(9900.0)
        assert rows[0]["cost_status"] == "estimated"
