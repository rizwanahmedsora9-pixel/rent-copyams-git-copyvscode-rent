"""GRN stock-consumption indicator.

The indicator answers one question per GRN lot: *how much of what this GRN
received has actually left the warehouse?*  These tests pin the four things that
can silently make that answer wrong:

1. the FIFO replay must agree, lot for lot, with the FIFO cost the Profit & Loss
   report recognizes (one stock truth, two views);
2. consumption must come from recorded ``GRNAllocation`` rows first and only then
   from the movement replay, and it must say which one it used;
3. zero / missing quantities must not divide by zero and no percentage may leave
   0–100;
4. filtering or paginating the GRN screen must not change a lot's percentage —
   the replay always covers the whole ledger, only the *reported* rows narrow.

Everything here runs against a throw-away SQLite database built by the shared
``app_factory`` fixture; no production database is opened.
"""
from __future__ import annotations

import re
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services.grn_consumption import (
    build_grn_consumption,
    consumption_level,
    consumption_percent,
    grn_consumption_for_page,
)
from app.services.profit_loss import build_profit_loss_report
from models import (
    Client,
    DirectSale,
    DirectSaleItem,
    GRN,
    GRNAllocation,
    GRNItem,
    Material,
    MaterialReturn,
    MaterialReturnItem,
    Supplier,
    db,
)
from tests.conftest import make_csrf_client


@pytest.fixture()
def consumption_app(app_factory, tmp_path):
    app = app_factory(tmp_path / "grn_consumption.db")
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


def seed(material_total=4000.0):
    material = Material(name="CEMENT", code="MAT-1", unit="Bags", unit_price=1200, total=material_total)
    client_row = Client(name="CUSTOMER ONE", code="CL-1", category="General", is_active=True)
    supplier = Supplier(name="SUPPLIER ONE", is_active=True)
    db.session.add_all([material, client_row, supplier])
    db.session.flush()
    return material, client_row, supplier


def add_grn(supplier, material, qty, rate, posted, bill):
    grn = GRN(supplier_id=supplier.id, supplier=supplier.name, manual_bill_no=bill, date_posted=posted, is_void=False)
    db.session.add(grn)
    db.session.flush()
    item = GRNItem(grn_id=grn.id, mat_name=material.name, qty=qty, price_at_time=rate, is_void=False)
    db.session.add(item)
    db.session.flush()
    return grn, item


def post_sale(client, client_row, posted, lines, bill, category="Credit Customer"):
    """Post through the real route so GRNAllocation rows are written by the app."""
    response = client.post("/add_direct_sale", data={
        "client_name": client_row.name,
        "driver_name": "Driver",
        "category": category,
        "product_name[]": [line[0] for line in lines],
        "qty[]": [str(line[1]) for line in lines],
        "unit_rate[]": [str(line[2]) for line in lines],
        "payment_method": "Credit",
        "paid_amount": "0",
        "manual_bill_no": bill,
        "sale_date": posted,
        "allow_negative_stock": "1",
    }, follow_redirects=False)
    assert response.status_code in (302, 303), response.get_data(as_text=True)[:500]
    return DirectSale.query.filter(DirectSale.manual_bill_no.like(f"%{bill}%")).one()


def add_legacy_sale(client_row, posted, qty, rate, bill):
    """Imported dispatch: no GRNAllocation rows at all."""
    sale = DirectSale(
        client_name=client_row.name, client_code=client_row.code, category="Credit Customer",
        amount=qty * rate, paid_amount=0, manual_bill_no=bill, date_posted=posted, is_void=False,
    )
    db.session.add(sale)
    db.session.flush()
    db.session.add(DirectSaleItem(sale_id=sale.id, product_name="CEMENT", qty=qty, price_at_time=rate))
    db.session.flush()
    return sale


def report(**kwargs):
    kwargs.setdefault("start_date", "2026-01-01")
    kwargs.setdefault("end_date", "2026-12-31")
    return SimpleNamespace(**build_profit_loss_report(**kwargs))


def indicator_rows(html):
    """Every rendered per-lot indicator: (grn_item_id, received, consumed, pct)."""
    pattern = re.compile(
        r'data-grn-consume data-grn-item-id="(\d+)"\s+data-received="([-\d.]+)"\s+'
        r'data-consumed="([-\d.]+)"\s+data-percent="([-\d.]+)"'
    )
    return [(int(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4))) for m in pattern.finditer(html)]


# --------------------------------------------------------------------------- #
# 1. The scenario from the specification, end to end
# --------------------------------------------------------------------------- #
def test_01_spec_fifo_purchase_and_dispatch_numbers(consumption_app):
    """2,000 @ 1,200 + 2,000 @ 1,300; dispatch 2,100 @ 1,500."""
    client = make_csrf_client(consumption_app)
    login(client)
    with consumption_app.app_context():
        material, client_row, supplier = seed()
        first_grn, first_lot = add_grn(supplier, material, 2000, 1200, datetime(2026, 1, 1, 9, 0), "GRN-A")
        second_grn, second_lot = add_grn(supplier, material, 2000, 1300, datetime(2026, 1, 2, 9, 0), "GRN-B")
        db.session.commit()

        post_sale(client, client_row, "2026-01-05", [("CEMENT", 2100, 1500)], "SALE-1")
        db.session.commit()

        # --- profit & loss: FIFO cost of the lots that actually left ---------
        pl = report()
        assert pl.summary["total_material_revenue"] == pytest.approx(3_150_000)
        assert pl.summary["total_cogs"] == pytest.approx(2_530_000)   # 2000*1200 + 100*1300
        assert pl.summary["total_profit"] == pytest.approx(620_000)

        # --- the allocations the posting path wrote -------------------------
        allocations = GRNAllocation.query.order_by(GRNAllocation.id).all()
        assert [(a.grn_item_id, a.qty, a.cost_rate) for a in allocations] == [
            (first_lot.id, 2000.0, 1200.0),
            (second_lot.id, 100.0, 1300.0),
        ]

        # --- the consumption indicator --------------------------------------
        data = build_grn_consumption()
        first = data["items"][first_lot.id]
        second = data["items"][second_lot.id]

        assert (first["received_qty"], first["consumed_qty"], first["remaining_qty"]) == (2000.0, 2000.0, 0.0)
        assert first["consumed_pct"] == pytest.approx(100.0)
        assert first["level"] == "complete"
        assert first["fully_consumed"] is True
        assert first["status"] == "recorded"

        assert (second["received_qty"], second["consumed_qty"], second["remaining_qty"]) == (2000.0, 100.0, 1900.0)
        assert second["consumed_pct"] == pytest.approx(5.0)
        assert second["level"] == "low"
        assert second["fully_consumed"] is False
        assert second["status"] == "recorded"

        # Remaining stock is 1,900 bags at the surviving lot rate = 2,470,000.
        assert data["inventory"]["total_qty"] == pytest.approx(1900.0)
        assert data["inventory"]["total_value"] == pytest.approx(2_470_000)

        # The GRN-level roll-up must be the sum of its lots.
        grn_row = data["grns"][second_grn.id]
        assert grn_row["received_qty"] == pytest.approx(2000.0)
        assert grn_row["consumed_qty"] == pytest.approx(100.0)
        assert grn_row["consumed_pct"] == pytest.approx(5.0)
        assert data["grns"][first_grn.id]["consumed_pct"] == pytest.approx(100.0)

        # The P&L replay ends on exactly the same lot balances.
        balances = pl.context.grn_lot_balances
        for grn_item_id, lot in data["items"].items():
            assert balances[grn_item_id]["remaining"] == pytest.approx(lot["remaining_qty"])
            assert balances[grn_item_id]["received"] == pytest.approx(lot["received_qty"])


# --------------------------------------------------------------------------- #
# 2. Percentages and colour bands are guarded
# --------------------------------------------------------------------------- #
def test_02_percentage_and_level_guards():
    assert consumption_percent(0, 0) == 0.0          # divide-by-zero guard
    assert consumption_percent(0, 50) == 0.0         # no received quantity
    assert consumption_percent(None, None) == 0.0
    assert consumption_percent(-10, 5) == 0.0
    assert consumption_percent(200, 1) == pytest.approx(0.5)
    assert consumption_percent(100, 140) == 100.0    # clamped, never > 100
    assert consumption_percent(100, -20) == 0.0      # clamped, never < 0

    bands = {
        0.0: "low", 24.99: "low",
        25.0: "moderate", 49.99: "moderate",
        50.0: "high", 74.99: "high",
        75.0: "critical", 99.99: "critical",
        100.0: "complete", 250.0: "complete",
    }
    for pct, expected in bands.items():
        assert consumption_level(pct) == expected, pct


# --------------------------------------------------------------------------- #
# 3. Recorded allocations vs the movement replay
# --------------------------------------------------------------------------- #
def test_03_recorded_and_reconstructed_lots_are_labelled(consumption_app):
    client = make_csrf_client(consumption_app)
    login(client)
    with consumption_app.app_context():
        material, client_row, supplier = seed(material_total=1000)
        _grn_a, lot_a = add_grn(supplier, material, 100, 80, datetime(2026, 2, 1), "GRN-RECORDED")
        _grn_b, lot_b = add_grn(supplier, material, 100, 90, datetime(2026, 2, 2), "GRN-LEGACY")
        db.session.commit()

        # Posted sale -> recorded allocation.
        post_sale(client, client_row, "2026-02-05", [("CEMENT", 40, 150)], "SALE-REC")
        # Imported dispatch -> no allocation, reconstructed by the replay.
        add_legacy_sale(client_row, datetime(2026, 2, 6), 30, 150, "LEGACY-1")
        db.session.commit()

        data = build_grn_consumption()
        recorded = data["items"][lot_a.id]
        reconstructed = data["items"][lot_b.id]

        assert recorded["consumed_qty"] == pytest.approx(70.0)   # 40 allocated + 30 replayed
        assert recorded["recorded_consumed_qty"] == pytest.approx(40.0)
        assert recorded["reconstructed_consumed_qty"] == pytest.approx(30.0)
        assert recorded["status"] == "reconstructed"             # not fully evidenced

        assert reconstructed["consumed_qty"] == 0.0
        assert reconstructed["status"] == "untouched"
        assert reconstructed["consumed_pct"] == 0.0
        assert reconstructed["level"] == "low"

        assert data["reconstructed_lot_count"] == 1
        assert data["recorded_lot_count"] == 0
        assert data["fully_consumed_lot_count"] == 0


# --------------------------------------------------------------------------- #
# 4. The GRN screen and the JSON endpoint show the same numbers
# --------------------------------------------------------------------------- #
def test_04_grn_page_and_api_render_the_same_numbers(consumption_app):
    client = make_csrf_client(consumption_app)
    login(client)
    with consumption_app.app_context():
        material, client_row, supplier = seed()
        add_grn(supplier, material, 2000, 1200, datetime(2026, 1, 1), "GRN-A")
        add_grn(supplier, material, 2000, 1300, datetime(2026, 1, 2), "GRN-B")
        db.session.commit()
        post_sale(client, client_row, "2026-01-05", [("CEMENT", 2100, 1500)], "SALE-1")
        db.session.commit()

        service = build_grn_consumption()

        response = client.get("/grn")
        assert response.status_code == 200
        html = response.get_data(as_text=True)

    rows = indicator_rows(html)
    assert rows, "no consumption indicators were rendered"
    assert {row[0] for row in rows} == set(service["items"])
    for grn_item_id, received, consumed, pct in rows:
        expected = service["items"][grn_item_id]
        assert received == pytest.approx(expected["received_qty"])
        assert consumed == pytest.approx(expected["consumed_qty"])
        assert pct == pytest.approx(expected["consumed_pct"])
        assert 0.0 <= pct <= 100.0

    # One bar per lot, inside the collapsible details row.  The old per-GRN
    # roll-up (data-grn-summary) is deliberately not rendered: each lot reports
    # its own consumption when the row is expanded.  No duplicated element ids.
    assert html.count('data-grn-consume data-grn-item-id=') == len(service["items"])
    assert "data-grn-summary" not in html
    assert html.count('<tr class="grn-row"') == html.count('<tr class="grn-details-row"')
    assert html.count('<tr class="grn-details-row"') == len(service["grns"])
    ids = re.findall(r'\sid="([^"]+)"', html)
    assert len(ids) == len(set(ids)), "duplicate element ids on the GRN page"
    assert "grn-consume-pct" in html and "grn_consumption.js" in html
    assert "grnConsumptionRefresh" in html

    # Colour bands came from the server and match the percentages.
    assert 'class="grn-consume level-complete"' in html
    assert 'class="grn-consume level-low"' in html

    with consumption_app.app_context():
        api = client.get("/api/grn_consumption")
        assert api.status_code == 200
        payload = api.get_json()
    assert payload["ok"] is True
    assert len(payload["items"]) == len(service["items"])
    for grn_item_id, row in service["items"].items():
        served = payload["items"][str(grn_item_id)]
        assert served["received_qty"] == pytest.approx(row["received_qty"])
        assert served["consumed_qty"] == pytest.approx(row["consumed_qty"])
        assert served["consumed_pct"] == pytest.approx(row["consumed_pct"])
        assert served["level"] == row["level"]
    # The per-GRN payload omits the nested item list but keeps the totals.
    assert all("items" not in row for row in payload["grns"].values())
    assert payload["inventory"]["total_qty"] == pytest.approx(service["inventory"]["total_qty"])

    with consumption_app.app_context():
        subset = client.get("/api/grn_consumption", query_string={"grn_ids": str(next(iter(service["grns"])))})
        subset_payload = subset.get_json()
    assert len(subset_payload["grns"]) == 1
    assert len(subset_payload["items"]) <= len(service["items"])


# --------------------------------------------------------------------------- #
# 5. Filtering the screen must not change a lot's percentage
# --------------------------------------------------------------------------- #
def test_05_filtered_screen_keeps_the_same_percentages(consumption_app):
    client = make_csrf_client(consumption_app)
    login(client)
    with consumption_app.app_context():
        material, client_row, supplier = seed()
        add_grn(supplier, material, 2000, 1200, datetime(2026, 1, 1), "GRN-OLD")
        add_grn(supplier, material, 2000, 1300, datetime(2026, 1, 2), "GRN-NEW")
        db.session.commit()
        post_sale(client, client_row, "2026-01-05", [("CEMENT", 2100, 1500)], "SALE-1")
        db.session.commit()
        service = build_grn_consumption()
        full_page = client.get("/grn").get_data(as_text=True)

    filtered = client.get("/grn", query_string={"search": "GRN-OLD"}).get_data(as_text=True)
    full_rows = {row[0]: row for row in indicator_rows(full_page)}
    filtered_rows = {row[0]: row for row in indicator_rows(filtered)}
    assert filtered_rows, "the filtered screen rendered no indicators"
    assert len(filtered_rows) < len(full_rows)
    for grn_item_id, row in filtered_rows.items():
        assert row[2] == pytest.approx(full_rows[grn_item_id][2]), "filtering changed a lot's consumption"
        assert row[3] == pytest.approx(full_rows[grn_item_id][3])

    # A date filter must behave the same way.
    by_date = client.get("/grn", query_string={"start_date": "2026-01-02", "end_date": "2026-01-31"})
    by_date_rows = {row[0]: row for row in indicator_rows(by_date.get_data(as_text=True))}
    assert by_date_rows
    for grn_item_id, row in by_date_rows.items():
        assert row[2] == pytest.approx(full_rows[grn_item_id][2])

    # The service itself is what both pages came from.
    for grn_item_id in filtered_rows:
        assert service["items"][grn_item_id]["consumed_qty"] == pytest.approx(filtered_rows[grn_item_id][2])


# --------------------------------------------------------------------------- #
# 6. A material return gives the quantity back
# --------------------------------------------------------------------------- #
def test_06_return_restores_the_lot_quantity(consumption_app):
    client = make_csrf_client(consumption_app)
    login(client)
    with consumption_app.app_context():
        material, client_row, supplier = seed()
        add_grn(supplier, material, 2000, 1200, datetime(2026, 1, 1), "GRN-A")
        add_grn(supplier, material, 2000, 1300, datetime(2026, 1, 2), "GRN-B")
        db.session.commit()
        post_sale(client, client_row, "2026-01-05", [("CEMENT", 2100, 1500)], "SALE-1")
        db.session.commit()

        before = build_grn_consumption()
        before_consumed = sum(row["consumed_qty"] for row in before["items"].values())
        before_received = {key: row["received_qty"] for key, row in before["items"].items()}

        ret = MaterialReturn(
            client_name=client_row.name, return_type="normal", amount=150_000,
            manual_bill_no="RTN-1", date_posted=datetime(2026, 1, 10), is_void=False,
        )
        db.session.add(ret)
        db.session.flush()
        db.session.add(MaterialReturnItem(
            material_return_id=ret.id, material_name="CEMENT", qty=100, unit_rate=1500,
            price_at_time=1500,
        ))
        db.session.commit()

        after = build_grn_consumption()
        after_consumed = sum(row["consumed_qty"] for row in after["items"].values())

        assert before_consumed - after_consumed == pytest.approx(100.0)
        for key, row in after["items"].items():
            assert row["received_qty"] == pytest.approx(before_received[key]), "a return inflated a GRN"
            assert row["remaining_qty"] <= row["received_qty"] + 1e-6
            assert 0.0 <= row["consumed_pct"] <= 100.0

        # The profit report agrees: the returned stock is no longer costed out.
        pl = report()
        assert pl.context.grn_lot_balances
        for key, lot in after["items"].items():
            assert pl.context.grn_lot_balances[key]["remaining"] == pytest.approx(lot["remaining_qty"])


# --------------------------------------------------------------------------- #
# 7. Parities and helpers on the page projection
# --------------------------------------------------------------------------- #
def test_07_page_projection_matches_the_full_projection(consumption_app):
    client = make_csrf_client(consumption_app)
    login(client)
    with consumption_app.app_context():
        material, client_row, supplier = seed()
        add_grn(supplier, material, 100, 80, datetime(2026, 3, 1), "GRN-C1")
        add_grn(supplier, material, 100, 90, datetime(2026, 3, 2), "GRN-C2")
        db.session.commit()
        post_sale(client, client_row, "2026-03-05", [("CEMENT", 150, 200)], "SALE-C")
        db.session.commit()

        grns = GRN.query.order_by(GRN.id).all()
        page_data = grn_consumption_for_page(grns)
        full_data = build_grn_consumption()
        for grn_item_id, row in page_data["items"].items():
            assert row["consumed_qty"] == pytest.approx(full_data["items"][grn_item_id]["consumed_qty"])
            assert row["remaining_qty"] == pytest.approx(full_data["items"][grn_item_id]["remaining_qty"])

        # An empty page is not an error and returns a usable shape.
        empty = grn_consumption_for_page([])
        assert empty["items"] == {} and empty["grns"] == {}
        assert empty["inventory"]["total_qty"] == 0.0
