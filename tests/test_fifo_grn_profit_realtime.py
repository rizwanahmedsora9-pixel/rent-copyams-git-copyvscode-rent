"""One FIFO truth for GRN consumption, Profit & Loss and the live screens.

The regression that motivated this suite: an issue had an older GRN on its
date, but the balance of its quantity was physically covered by a later GRN.
The P&L consumed the later lot; the GRN page's independent replay left it at
100% in stock.  Real snapshot: 19 lots / 6,438 units disagreed.  These tests
pin the shared replay, exact per-lot rates, the API and the live change token.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services.grn_consumption import build_grn_consumption
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
def live_app(app_factory, tmp_path):
    app = app_factory(tmp_path / "fifo_live.db")
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
    material = Material(name="CEMENT", code="MAT-1", unit="Bags", unit_price=999, total=0)
    client = Client(name="CUSTOMER ONE", code="CL-1", category="General", is_active=True)
    supplier = Supplier(name="SUPPLIER ONE", is_active=True)
    db.session.add_all([material, client, supplier])
    db.session.flush()
    return material, client, supplier


def add_grn(supplier, qty, rate, posted, bill):
    grn = GRN(
        supplier_id=supplier.id, supplier=supplier.name, manual_bill_no=bill,
        date_posted=posted, is_void=False,
    )
    db.session.add(grn)
    db.session.flush()
    item = GRNItem(grn_id=grn.id, mat_name="CEMENT", qty=qty, price_at_time=rate, is_void=False)
    db.session.add(item)
    db.session.flush()
    return grn, item


def add_legacy_issue(client_row, posted, qty, rate, bill):
    """An imported issue: no posting-time GRNAllocation and no frozen rate."""
    sale = DirectSale(
        client_name=client_row.name, client_code=client_row.code,
        category="Credit Customer", amount=qty * rate, paid_amount=0,
        manual_bill_no=bill, date_posted=posted, is_void=False,
    )
    db.session.add(sale)
    db.session.flush()
    db.session.add(DirectSaleItem(
        sale_id=sale.id, product_name="CEMENT", qty=qty,
        price_at_time=rate, cost_rate_at_sale=None, grn_item_id=None,
    ))
    db.session.flush()
    return sale


def report(**kwargs):
    kwargs.setdefault("start_date", "2026-01-01")
    kwargs.setdefault("end_date", "2026-12-31")
    kwargs.setdefault("previous", False)
    return SimpleNamespace(**build_profit_loss_report(**kwargs))


def goods_rows(rep):
    return [
        row for row in rep.rows
        if row["entry_type"] == "material_sale" and row["material"] == "CEMENT"
    ]


def test_exhausted_dated_lot_and_future_lot_match_in_report_grn_and_api(live_app):
    client = make_csrf_client(live_app)
    login(client)
    with live_app.app_context():
        _material, client_row, supplier = seed()
        _old_grn, old_lot = add_grn(supplier, 10, 1000, datetime(2026, 1, 1, 9, 0), "GRN-OLD")
        _future_grn, future_lot = add_grn(supplier, 30, 1200, datetime(2026, 3, 1, 9, 0), "GRN-FUTURE")
        add_legacy_issue(client_row, datetime(2026, 1, 10, 10, 0), 40, 1300, "SALE-FUTURE")
        db.session.commit()

        rep = report()
        rows = goods_rows(rep)
        assert len(rows) == 1
        row = rows[0]
        # One physical sale, two exact FIFO cost layers.  No blended 1,150 rate
        # is used as a purchase cost and no source line merges the two rates.
        assert row["material_cost"] == pytest.approx(46_000)
        assert rep.summary["total_cogs"] == pytest.approx(46_000)
        assert [(s["qty"], s["rate"], s["lot_rate"]) for s in row["cost_sources"]] == [
            (10.0, 1000.0, 1000.0), (30.0, 1200.0, 1200.0),
        ]
        assert {s["grn_item_id"] for s in row["cost_sources"]} == {old_lot.id, future_lot.id}
        assert sum(s["amount"] for s in row["cost_sources"]) == pytest.approx(row["material_cost"])

        # The report's inventory panel and a direct GRN query use the very same
        # lot balances; the API served to the browser does too.
        for view in (rep.inventory_view, build_grn_consumption()):
            assert view["items"][old_lot.id]["remaining_qty"] == pytest.approx(0)
            assert view["items"][future_lot.id]["remaining_qty"] == pytest.approx(0)
            assert view["inventory"]["total_qty"] == pytest.approx(0)
            for lot_id, lot in view["items"].items():
                assert rep.context.grn_lot_balances[lot_id]["remaining"] == pytest.approx(lot["remaining_qty"])

        api = client.get("/api/grn_consumption")
        assert api.status_code == 200
        payload = api.get_json()
        assert payload["items"][str(old_lot.id)]["consumed_qty"] == pytest.approx(10)
        assert payload["items"][str(future_lot.id)]["consumed_qty"] == pytest.approx(30)
        assert payload["inventory"]["total_qty"] == pytest.approx(0)

        detail = client.get(
            f"/profit_reports/transaction/DirectSale/{row['document_id']}"
            "?start_date=2026-01-01&end_date=2026-12-31"
        )
        assert detail.status_code == 200
        assert f"Lot #{old_lot.id}".encode() in detail.data
        assert f"Lot #{future_lot.id}".encode() in detail.data
        assert b"10.00 \xc3\x97 Rs. 1,000.00" in detail.data
        assert b"30.00 \xc3\x97 Rs. 1,200.00" in detail.data


def test_sale_posting_reserves_the_same_fifo_lots_used_by_report_and_grn(live_app):
    client = make_csrf_client(live_app)
    login(client)
    with live_app.app_context():
        _material, client_row, supplier = seed()
        _old_grn, old_lot = add_grn(supplier, 10, 1000, datetime(2026, 1, 1, 9, 0), "GRN-OLD")
        _new_grn, new_lot = add_grn(supplier, 30, 1200, datetime(2026, 1, 2, 9, 0), "GRN-NEW")
        # An imported dispatch has no GRNAllocation, but it physically consumed
        # the old lot.  Posting must not promise that lot to a second sale.
        add_legacy_issue(client_row, datetime(2026, 1, 5, 10, 0), 10, 1300, "LEGACY-ISSUE")
        db.session.commit()

        response = client.post("/add_direct_sale", data={
            "client_name": client_row.name,
            "driver_name": "Driver",
            "category": "Credit Customer",
            "product_name[]": ["CEMENT"],
            "qty[]": ["5"],
            "unit_rate[]": ["1500"],
            "payment_method": "Credit",
            "paid_amount": "0",
            "manual_bill_no": "NEW-FIFO-SALE",
            "sale_date": "2026-01-06",
            "allow_negative_stock": "1",
        }, follow_redirects=False)
        assert response.status_code in (302, 303), response.get_data(as_text=True)[:400]

        sale = DirectSale.query.filter(DirectSale.manual_bill_no.like("%NEW-FIFO-SALE%")).one()
        item = DirectSaleItem.query.filter_by(sale_id=sale.id).one()
        assert item.grn_item_id == new_lot.id
        assert item.cost_rate_at_sale == pytest.approx(1200)
        allocation = GRNAllocation.query.filter_by(sale_id=sale.id).one()
        assert allocation.grn_item_id == new_lot.id
        assert allocation.cost_rate == pytest.approx(1200)

        consumption = build_grn_consumption()
        assert consumption["items"][old_lot.id]["consumed_qty"] == pytest.approx(10)
        assert consumption["items"][new_lot.id]["consumed_qty"] == pytest.approx(5)


def test_ledger_revision_changes_after_sale_and_api_shows_consumption(live_app):
    client = make_csrf_client(live_app)
    login(client)
    with live_app.app_context():
        _material, client_row, supplier = seed()
        _grn, lot = add_grn(supplier, 10, 1000, datetime(2026, 1, 1, 9, 0), "GRN-LIVE")
        db.session.commit()

        first = client.get("/api/ledger_revision")
        assert first.status_code == 200
        revision_before = first.get_json()["revision"]

        sale = client.post("/add_direct_sale", data={
            "client_name": client_row.name,
            "driver_name": "Driver",
            "category": "Credit Customer",
            "product_name[]": ["CEMENT"],
            "qty[]": ["10"],
            "unit_rate[]": ["1300"],
            "payment_method": "Credit",
            "paid_amount": "0",
            "manual_bill_no": "SALE-LIVE",
            "sale_date": "2026-01-05",
            "allow_negative_stock": "1",
        }, follow_redirects=False)
        assert sale.status_code in (302, 303), sale.get_data(as_text=True)[:400]

        second = client.get("/api/ledger_revision")
        assert second.status_code == 200
        revision_after = second.get_json()["revision"]
        assert revision_after != revision_before

        payload = client.get("/api/grn_consumption").get_json()
        assert payload["items"][str(lot.id)]["consumed_qty"] == pytest.approx(10)
        assert payload["items"][str(lot.id)]["fully_consumed"] is True
        assert GRNAllocation.query.filter_by(grn_item_id=lot.id).count() == 1


def test_both_screens_are_wired_to_the_live_ledger_watcher(live_app):
    client = make_csrf_client(live_app)
    login(client)
    with live_app.app_context():
        seed()
        db.session.commit()

        grn_page = client.get("/grn")
        assert grn_page.status_code == 200
        assert b"ledger_live.js" in grn_page.data
        assert b"data-ledger-live-status" in grn_page.data
        assert b"FBMLedgerLive.init" in grn_page.data

        pl_page = client.get("/profit_reports?start_date=2026-01-01&end_date=2026-12-31")
        assert pl_page.status_code == 200
        assert b"ledger_live.js" in pl_page.data
        assert b"data-ledger-live-status" in pl_page.data
        assert b"FBMLedgerLive.init" in pl_page.data
        assert b"profit_report.js" in pl_page.data
