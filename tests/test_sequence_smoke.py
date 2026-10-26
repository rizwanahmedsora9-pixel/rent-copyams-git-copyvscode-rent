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
