"""The sequential acceptance cycle, protected by the test suite.

``tools/audit_sequence_smoke.py`` is the tool the owner runs by hand:
data 0 → category → materials → client → supplier → rent service → several
GRNs of the same cement at different price groups (plus a two-material GRN)
→ sales that cross lot boundaries, one bill with two lines of the same
material, a sand bill, a rent-service bill, a different cement, and a
deliberately over-sold bill — verifying GRN consumption and Profit & Loss
after every single step against an independent raw-SQL FIFO replay.

This module keeps that whole cycle in CI.  It also pins the two defects the
first run of the audit found (see the assertions at the bottom).
"""
from __future__ import annotations

from datetime import datetime

import pytest

from models import (
    Client,
    DirectSale,
    DirectSaleItem,
    Material,
    Service,
    Supplier,
    db,
)
from tools.audit_sequence_smoke import Reporter, run_sequence
from tests.conftest import make_csrf_client


@pytest.fixture()
def sequence_app(app_factory, tmp_path):
    app = app_factory(tmp_path / "sequence_smoke.db")
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        from app.services.schema import _ensure_default_admin
        _ensure_default_admin()
        db.session.commit()
    return app


def test_sequence_smoke_end_to_end(sequence_app):
    # The tool raises on nothing; it records every check and prints the log.
    report = run_sequence(sequence_app, Reporter())
    failures = report.failures
    assert not failures, "\n".join(f"{name}: {detail}" for name, _ok, detail in failures)


# ---------------------------------------------------------------------------
# Defect 1 — a registered Service is income, not "unknown delivery cost"
# ---------------------------------------------------------------------------
def _service_sale(app, *, register_service: bool, name: str = "CEMENT RENT"):
    """One credit sale with one service charge line (2 trips @ 2,000)."""
    with app.app_context():
        client = Client(name="RENT CUSTOMER", code="CL-RENT", category="Credit Customer", is_active=True)
        db.session.add(client)
        if register_service:
            db.session.add(Service(name=name, code="SVC-RENT", unit="Trips"))
        db.session.flush()
        sale = DirectSale(
            client_name=client.name, client_code=client.code,
            category="Credit Customer", amount=4000.0, paid_amount=0.0,
            manual_bill_no="MB NO.RENT-SALE", date_posted=datetime(2026, 9, 16),
            is_void=False,
        )
        db.session.add(sale)
        db.session.flush()
        db.session.add(DirectSaleItem(
            sale_id=sale.id, product_name=name, qty=2.0, price_at_time=2000.0,
            grn_item_id=None, cost_rate_at_sale=None,
        ))
        db.session.commit()
        return sale.id


def _report(app):
    from app.services.profit_loss import build_profit_loss_report

    with app.app_context():
        return build_profit_loss_report(
            start_date="2026-09-01", end_date="2026-09-30", previous=False
        )


def test_registered_service_income_is_recognised_with_zero_cost_of_goods(sequence_app):
    _service_sale(sequence_app, register_service=True)
    report = _report(sequence_app)
    service_rows = [
        row for row in report["rows"]
        if row["entry_type"] == "delivery_service"
        and row["material"] == "CEMENT RENT"
    ]
    assert len(service_rows) == 1
    row = service_rows[0]
    assert row["delivery_revenue"] == pytest.approx(4000)
    assert row["delivery_cost"] == pytest.approx(0)
    assert row["include_in_net"] is True
    assert row["cost_status"] == "not_applicable"
    assert row["cost_sources"] == []
    # A service is not an unknown purchase: it must not raise the
    # "some sales have no recorded purchase cost" alarm.
    assert report["summary"]["unknown_cost_rows"] == 0
    assert report["summary"]["total_revenue"] == pytest.approx(4000)
    assert report["summary"]["total_profit"] == pytest.approx(4000)


def test_unregistered_rent_like_label_keeps_the_conservative_rule(sequence_app):
    """A legacy rent/delivery label with no Service row is still unresolved.

    Nothing in the data says we own the thing being charged for, so the report
    refuses to guess a zero cost until a delivery cost exists.
    """
    _service_sale(sequence_app, register_service=False, name="RENT LEGACY TRIP")
    report = _report(sequence_app)
    rows = [row for row in report["rows"] if row["entry_type"] == "delivery_service"]
    assert len(rows) == 1
    assert rows[0]["include_in_net"] is False
    assert rows[0]["cost_status"] == "missing"
    assert report["summary"]["unknown_cost_rows"] == 1
    assert report["summary"]["total_profit"] == pytest.approx(0)


def test_registered_service_with_a_recorded_delivery_cost_is_netted(sequence_app):
    """When a real delivery cost is recorded, behaviour is unchanged."""
    with sequence_app.app_context():
        client = Client(name="RENT CUSTOMER", code="CL-RENT", category="Credit Customer", is_active=True)
        db.session.add(client)
        db.session.add(Service(name="CEMENT RENT", code="SVC-RENT", unit="Trips"))
        db.session.flush()
        sale = DirectSale(
            client_name=client.name, client_code=client.code,
            category="Credit Customer", amount=4000.0, paid_amount=0.0,
            manual_bill_no="MB NO.RENT-SALE-COST", date_posted=datetime(2026, 9, 17),
            delivery_rent_cost=1500.0, is_void=False,
        )
        db.session.add(sale)
        db.session.flush()
        db.session.add(DirectSaleItem(
            sale_id=sale.id, product_name="CEMENT RENT", qty=2.0, price_at_time=2000.0,
        ))
        db.session.commit()

    report = _report(sequence_app)
    row = next(r for r in report["rows"] if r["entry_type"] == "delivery_service")
    assert row["delivery_cost"] == pytest.approx(1500)
    assert row["cost_status"] == "actual"
    assert row["include_in_net"] is True
    assert report["summary"]["total_profit"] == pytest.approx(2500)


# ---------------------------------------------------------------------------
# Defect 2 — "data 0" must also clear the Services master
# ---------------------------------------------------------------------------
def test_full_wipe_clears_the_services_master(sequence_app):
    client = make_csrf_client(sequence_app)
    assert client.post("/login", data={"username": "Admin", "password": "Admin@fbm12345"}).status_code in (302, 303)
    with sequence_app.app_context():
        db.session.add(Service(name="CEMENT RENT", code="SVC-RENT", unit="Trips"))
        db.session.add(Service(name="STEEL RENT", code="SVC-STEEL", unit="Trips"))
        db.session.add(Material(name="DG CEMENT", code="MAT-1", unit="Bags", total=10))
        db.session.add(Supplier(name="SUPPLIER ONE", is_active=True))
        db.session.commit()

    from tools.audit_full_cycle import FULL_WIPE_TARGETS

    assert "services" in FULL_WIPE_TARGETS
    resp = client.post("/delete_selected_data", data={
        "confirm_text": "DELETE ALL DATA",
        "hard_delete_override": "1",
        "delete_targets": FULL_WIPE_TARGETS,
    })
    assert resp.status_code in (302, 303)
    with sequence_app.app_context():
        assert Service.query.count() == 0
        assert Material.query.count() == 0
        assert Supplier.query.count() == 0


def test_services_wipe_target_can_run_on_its_own(sequence_app):
    client = make_csrf_client(sequence_app)
    assert client.post("/login", data={"username": "Admin", "password": "Admin@fbm12345"}).status_code in (302, 303)
    with sequence_app.app_context():
        db.session.add(Service(name="CEMENT RENT", code="SVC-RENT", unit="Trips"))
        material = Material(name="DG CEMENT", code="MAT-1", unit="Bags", total=10)
        db.session.add(material)
        db.session.commit()

    resp = client.post("/delete_selected_data", data={
        "confirm_text": "DELETE ALL DATA",
        "hard_delete_override": "1",
        "delete_targets": ["services"],
    })
    assert resp.status_code in (302, 303)
    with sequence_app.app_context():
        assert Service.query.count() == 0
        # A granular service wipe must not touch the physical masters.
        assert Material.query.count() == 1


# ---------------------------------------------------------------------------
# Defect 3 — "rent" was a substring test, so physical materials vanished
# ---------------------------------------------------------------------------
RENT_TRAP_NAMES = (
    "TRANSPARENT SHEET",
    "CURRENT STOCK",
    "PARENT BOARD",
    "DIFFERENT SAND",
    "INHERENT MORTAR",
)


def test_rent_word_never_matches_a_physical_material_name(sequence_app):
    """'TRANSPARENT SHEET' contains r-e-n-t but is not a rent charge.

    The old ``'rent' in name`` rule classified it as a service, so it was
    dropped from stock reports, from the GRN consumption replay and — worst —
    from Profit & Loss cost of goods: its real purchase cost disappeared.
    """
    from app.services.grn_consumption import _is_service, _service_names
    from app.services.lookups import is_rent_like_name, is_service_name
    from app.services.profit_loss import _service_classifier

    with sequence_app.app_context():
        for name in RENT_TRAP_NAMES:
            db.session.add(Material(name=name, code=f"T-{name[:8]}", unit="Pcs"))
        db.session.add(Service(name="CEMENT RENT", code="SVC-1", unit="Trips"))
        db.session.commit()

        classifier = _service_classifier()
        service_keys = _service_names()
        for name in RENT_TRAP_NAMES:
            assert is_rent_like_name(name) is False, name
            assert is_service_name(name) is False, name
            assert classifier(name) is False, name
            assert _is_service(name, service_keys) is False, name
        for name in ("RENT-CEMENT", "RENT STEEL", "RENTAL MIXER", "CEMENT RENT", "rent-cement"):
            assert is_rent_like_name(name) is True, name
            assert is_service_name(name) is True, name
            assert classifier(name) is True, name
            assert _is_service(name, service_keys) is True, name


def test_sheet_material_keeps_its_cost_in_profit_and_loss(sequence_app):
    """A GRN + sale of 'TRANSPARENT SHEET' must carry its real purchase cost."""
    from datetime import datetime

    from app.services.profit_loss import build_profit_loss_report
    from models import GRN, GRNItem

    with sequence_app.app_context():
        client = Client(name="SHEET BUYER", code="CL-SHEET", category="Credit Customer", is_active=True)
        supplier = Supplier(name="SHEET SUPPLIER", is_active=True)
        sheet = Material(name="TRANSPARENT SHEET", code="MAT-SHEET", unit="Sheets", total=40)
        db.session.add_all([client, supplier, sheet])
        db.session.flush()
        grn = GRN(supplier_id=supplier.id, supplier=supplier.name, manual_bill_no="MB NO.SHEET-1",
                  date_posted=datetime(2026, 9, 10), is_void=False)
        db.session.add(grn)
        db.session.flush()
        lot = GRNItem(grn_id=grn.id, mat_name="TRANSPARENT SHEET", qty=40, price_at_time=300, is_void=False)
        db.session.add(lot)
        db.session.flush()
        sale = DirectSale(client_name=client.name, client_code=client.code,
                          category="Credit Customer", amount=4500, paid_amount=0,
                          manual_bill_no="MB NO.SHEET-SALE", date_posted=datetime(2026, 9, 21),
                          is_void=False)
        db.session.add(sale)
        db.session.flush()
        db.session.add(DirectSaleItem(
            sale_id=sale.id, product_name="TRANSPARENT SHEET", qty=10,
            price_at_time=450, grn_item_id=lot.id, cost_rate_at_sale=300,
        ))
        db.session.commit()

        report = build_profit_loss_report(
            start_date="2026-09-01", end_date="2026-09-30", previous=False
        )

    rows = [r for r in report["rows"] if "TRANSPARENT" in (r["material"] or "").upper()]
    assert len(rows) == 1, [r["material"] for r in report["rows"]]
    assert rows[0]["material_revenue"] == pytest.approx(4500)
    assert rows[0]["material_cost"] == pytest.approx(3000)
    assert rows[0]["include_in_net"] is True
    assert report["summary"]["total_profit"] == pytest.approx(1500)


# ---------------------------------------------------------------------------
# Defect 4 — an over-sold bill was silent once it had a frozen rate
# ---------------------------------------------------------------------------
def test_oversold_bill_raises_a_stock_shortfall_warning(sequence_app):
    from datetime import datetime

    from app.services.profit_loss import build_profit_loss_report
    from models import GRN, GRNItem

    with sequence_app.app_context():
        client = Client(name="SHORT BUYER", code="CL-SHORT", category="Credit Customer", is_active=True)
        supplier = Supplier(name="SHORT SUPPLIER", is_active=True)
        cement = Material(name="DG CEMENT", code="MAT-DG", unit="Bags", total=10)
        db.session.add_all([client, supplier, cement])
        db.session.flush()
        grn = GRN(supplier_id=supplier.id, supplier=supplier.name, manual_bill_no="MB NO.SHORT-1",
                  date_posted=datetime(2026, 9, 1), is_void=False)
        db.session.add(grn)
        db.session.flush()
        lot = GRNItem(grn_id=grn.id, mat_name="DG CEMENT", qty=10, price_at_time=1200, is_void=False)
        db.session.add(lot)
        db.session.flush()
        sale = DirectSale(client_name=client.name, client_code=client.code,
                          category="Credit Customer", amount=30000, paid_amount=0,
                          manual_bill_no="MB NO.SHORT-SALE", date_posted=datetime(2026, 9, 20),
                          is_void=False)
        db.session.add(sale)
        db.session.flush()
        # 10 bags from the lot + 15 bags nothing can cover; posting froze 1,500.
        db.session.add(DirectSaleItem(
            sale_id=sale.id, product_name="DG CEMENT", qty=10,
            price_at_time=1500, grn_item_id=lot.id, cost_rate_at_sale=1200,
        ))
        db.session.add(DirectSaleItem(
            sale_id=sale.id, product_name="DG CEMENT", qty=15,
            price_at_time=1500, grn_item_id=None, cost_rate_at_sale=1500,
        ))
        db.session.commit()

        report = build_profit_loss_report(
            start_date="2026-09-01", end_date="2026-09-30", previous=False
        )

    warnings = [w for w in report["stock_warnings"] if w["code"] == "short_stock_cost"]
    assert len(warnings) == 1
    assert "15.00 sold beyond every GRN lot" in warnings[0]["message"]
    # The honest costing is unchanged: 10 x 1,200 real lot + 15 x 1,500 frozen.
    assert report["summary"]["total_cogs"] == pytest.approx(12000 + 22500)


def test_oversold_warning_is_rendered_on_the_profit_page(sequence_app):
    client = make_csrf_client(sequence_app)
    assert client.post("/login", data={"username": "Admin", "password": "Admin@fbm12345"}).status_code in (302, 303)
    from datetime import datetime

    from models import DirectSale, DirectSaleItem

    with sequence_app.app_context():
        buyer = Client(name="SHORT BUYER", code="CL-SHORT", category="Credit Customer", is_active=True)
        cement = Material(name="GHOST CEMENT", code="MAT-GHOST", unit="Bags", total=0)
        db.session.add_all([buyer, cement])
        db.session.flush()
        sale = DirectSale(client_name=buyer.name, client_code=buyer.code,
                          category="Credit Customer", amount=5000, paid_amount=0,
                          manual_bill_no="MB NO.GHOST-SALE", date_posted=datetime(2026, 9, 20),
                          is_void=False)
        db.session.add(sale)
        db.session.flush()
        db.session.add(DirectSaleItem(sale_id=sale.id, product_name="GHOST CEMENT", qty=5,
                                      price_at_time=1000, cost_rate_at_sale=900))
        db.session.commit()

    page = client.get("/profit_reports?start_date=2026-09-01&end_date=2026-09-30")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "stock shortfall / unresolved-cost warning" in body
    assert "sold beyond every GRN lot" in body
