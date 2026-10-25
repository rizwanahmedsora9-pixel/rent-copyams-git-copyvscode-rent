"""Services master — rent-like charges that are billed but never stocked.

Covers the guarantees in SERVICES_PLAN.md that matter most:

* a service line behaves like a bill line (total, booking pool, ledger);
* a service is never stock — no GRN, no stock total, no stock report row;
* the conversion from Material to Service leaves every historical row intact
  (they are name-keyed) while changing what the name resolves to;
* a Service id can never be mistaken for a Material id.
"""
from __future__ import annotations

import pytest

from models import (
    Booking,
    BookingItem,
    DirectSale,
    DirectSaleItem,
    Entry,
    Material,
    MaterialCategory,
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
    app = app_factory(tmp_path / "services.db")
    # CSRF protection is skipped when TESTING is set (see app/hooks.py), which
    # is how the existing suites drive the test client.
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        # Ensure the default admin exists for login.
        from app.services.schema import _ensure_default_admin
        _ensure_default_admin()
        db.session.commit()
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def _make_rent_material(name="RENT-CEMENT", code="FBMREN-000002", unit="Pcs"):
    cat = MaterialCategory.query.filter_by(name="RENT").first()
    if not cat:
        cat = MaterialCategory(name="RENT", is_active=True)
        db.session.add(cat)
        db.session.flush()
    mat = Material(name=name, code=code, unit=unit, category_id=cat.id, total=0)
    db.session.add(mat)
    db.session.flush()
    return mat


# ---------------------------------------------------------------------------
# is_service_name / resolution
# ---------------------------------------------------------------------------

def test_service_name_resolves_after_add(app):
    with app.app_context():
        from app.services.lookups import is_service_name, invalidate_service_cache
        db.session.add(Service(name="Delivery Rent", code="SVC-DR1", unit="Pcs"))
        db.session.commit()
        invalidate_service_cache()

        assert is_service_name("Delivery Rent") is True
        assert is_service_name("delivery rent") is True
        assert is_service_name("SVC-DR1") is True
        assert is_service_name("12MM STEEL") is False


def test_legacy_rent_name_is_service_while_still_a_material(app):
    """Before conversion the rent row is a Material, but still counts as a charge.

    This is the transition rule: the rent box on the sale form must not go dark
    on the day service support ships.
    """
    with app.app_context():
        from app.services.lookups import is_service_name, invalidate_service_cache
        invalidate_service_cache()
        _make_rent_material()
        db.session.commit()
        invalidate_service_cache()

        assert is_service_name("RENT-CEMENT") is True


def test_service_id_never_resolves_as_material(app):
    """The single most dangerous bug this design prevents.

    Service ids start at 1, and so do Material ids. A Service id must never
    reach the unqualified db.session.get(Material, mid) lookup.
    """
    with app.app_context():
        from app.services.lookups import (
            resolve_transaction_item,
            resolve_transaction_material,
            invalidate_service_cache,
        )
        mat = Material(name="12MM STEEL", code="FBMST-000001", unit="KG")
        db.session.add(mat)
        db.session.flush()
        svc = Service(name="Delivery Rent", code="SVC-DR1", unit="Pcs")
        db.session.add(svc)
        db.session.commit()
        invalidate_service_cache()

        # Resolve by service id -> a service.
        kind, obj = resolve_transaction_item(service_id=svc.id, typed_text="Delivery Rent")
        assert kind == "service" and obj.id == svc.id

        # Resolve by material id -> that material, not the service with the same id.
        kind, obj = resolve_transaction_item(material_id=mat.id, typed_text="12MM STEEL")
        assert kind == "material" and obj.id == mat.id and obj.name == "12MM STEEL"

        # A stale id whose text does not match is rejected, not silently used.
        with pytest.raises(ValueError):
            resolve_transaction_item(material_id=mat.id, typed_text="Delivery Rent")

        # The goods-only resolver refuses a service outright.
        with pytest.raises(ValueError):
            resolve_transaction_material(typed_text="Delivery Rent")


# ---------------------------------------------------------------------------
# Conversion (the /services/migrate button)
# ---------------------------------------------------------------------------

def test_migrate_moves_rent_material_into_services(app, client):
    with app.app_context():
        mat = _make_rent_material()
        db.session.add(Material(name="12MM STEEL", code="FBMST-000001", unit="KG"))
        # A historical movement and a historical bill line, both name-keyed.
        db.session.add(Entry(material="RENT-CEMENT", type="OUT", qty=10,
                             date="2026-01-01", client="ACME"))
        sale = DirectSale(client_name="ACME", amount=300, category="Cash")
        db.session.add(sale)
        db.session.flush()
        db.session.add(DirectSaleItem(sale_id=sale.id, product_name="RENT-CEMENT",
                                      qty=10, price_at_time=30))
        before_material_id = mat.id
        db.session.commit()

    login(client)
    resp = client.post("/services/migrate", data={"confirm": "yes"},
                       follow_redirects=True)
    assert resp.status_code == 200

    with app.app_context():
        # Gone from the Material master...
        assert Material.query.filter_by(name="RENT-CEMENT").first() is None
        # ...present as a service, carrying unit and code across.
        svc = Service.query.filter_by(name="RENT-CEMENT").first()
        assert svc is not None
        assert svc.unit == "Pcs"
        assert svc.code == "FBMREN-000002"
        assert svc.converted_from_material_id == before_material_id

        # Historical rows are byte-identical — only what the name resolves
        # to has changed.
        assert Entry.query.filter_by(material="RENT-CEMENT").count() == 1
        item = DirectSaleItem.query.filter_by(product_name="RENT-CEMENT").first()
        assert item is not None and float(item.qty) == 10
        assert float(item.price_at_time) == 30

        # The non-rent material is untouched.
        assert Material.query.filter_by(name="12MM STEEL").first() is not None

        from app.services.lookups import is_service_name, invalidate_service_cache
        invalidate_service_cache()
        assert is_service_name("RENT-CEMENT") is True
        assert is_service_name("12MM STEEL") is False


def test_migrate_is_idempotent(app, client):
    login(client)
    with app.app_context():
        _make_rent_material()
        db.session.commit()

    client.post("/services/migrate", data={"confirm": "yes"}, follow_redirects=True)
    client.post("/services/migrate", data={"confirm": "yes"}, follow_redirects=True)

    with app.app_context():
        assert Service.query.filter_by(name="RENT-CEMENT").count() == 1


def test_migrate_refuses_without_confirmation(app, client):
    login(client)
    with app.app_context():
        _make_rent_material()
        db.session.commit()

    client.post("/services/migrate", data={}, follow_redirects=True)

    with app.app_context():
        assert Material.query.filter_by(name="RENT-CEMENT").first() is not None
        assert Service.query.filter_by(name="RENT-CEMENT").first() is None


def test_migrate_refuses_when_a_grn_line_exists(app, client):
    """A GRN against a charge is a fake purchase — refuse rather than convert."""
    with app.app_context():
        from models import GRN, GRNItem
        _make_rent_material()
        grn = GRN(supplier="Test Supplier", auto_bill_no="GRN-1")
        db.session.add(grn)
        db.session.flush()
        db.session.add(GRNItem(grn_id=grn.id, mat_name="RENT-CEMENT", qty=5, price_at_time=10))
        db.session.commit()

    login(client)
    client.post("/services/migrate", data={"confirm": "yes"}, follow_redirects=True)

    with app.app_context():
        assert Material.query.filter_by(name="RENT-CEMENT").first() is not None
        assert Service.query.filter_by(name="RENT-CEMENT").first() is None


# ---------------------------------------------------------------------------
# Collision guard
# ---------------------------------------------------------------------------

def test_service_cannot_collide_with_material_name_or_code(app, client):
    login(client)
    with app.app_context():
        db.session.add(Material(name="12MM STEEL", code="FBMST-000001", unit="KG"))
        db.session.commit()

    client.post("/services/add",
                data={"service_name": "12MM STEEL", "service_code": "X1"},
                follow_redirects=True)
    with app.app_context():
        assert Service.query.filter_by(name="12MM STEEL").first() is None

    client.post("/services/add",
                data={"service_name": "Some Charge", "service_code": "FBMST-000001"},
                follow_redirects=True)
    with app.app_context():
        assert Service.query.filter_by(code="FBMST-000001").first() is None


# ---------------------------------------------------------------------------
# Stock exclusion
# ---------------------------------------------------------------------------

def test_service_has_no_stock_and_is_excluded_from_reports(app):
    with app.app_context():
        from app.services.void_rebuild import _rebuild_material_totals
        _make_rent_material()
        db.session.add(Material(name="12MM STEEL", code="FBMST-000001", unit="KG"))
        db.session.add(Entry(material="RENT-CEMENT", type="OUT", qty=10, date="2026-01-01"))
        db.session.add(Entry(material="12MM STEEL", type="IN", qty=100, date="2026-01-01"))
        db.session.commit()

        _rebuild_material_totals()
        db.session.commit()

        # Real goods still total correctly...
        steel = Material.query.filter_by(name="12MM STEEL").first()
        assert float(steel.total) == 100

        # ...and the service, once converted, has no total at all.
        db.session.add(Service(name="RENT-CEMENT", code="FBMREN-000002", unit="Pcs"))
        rent = Material.query.filter_by(name="RENT-CEMENT").first()
        db.session.delete(rent)
        db.session.commit()

        _rebuild_material_totals()
        db.session.commit()
        assert Material.query.filter_by(name="RENT-CEMENT").first() is None
        assert float(Material.query.filter_by(name="12MM STEEL").first().total) == 100


def test_service_total_is_read_only(app):
    """A stray stock write on a service must fail loudly, not silently."""
    with app.app_context():
        svc = Service(name="Delivery Rent", code="SVC-DR1", unit="Pcs")
        db.session.add(svc)
        db.session.commit()
        assert svc.total == 0
        with pytest.raises(AttributeError):
            svc.total = 5


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def test_services_page_renders_and_shows_pending_rent(app, client):
    login(client)
    with app.app_context():
        _make_rent_material()
        db.session.commit()

    resp = client.get("/services")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Services &amp; Charges" in body or "Services & Charges" in body
    assert "RENT-CEMENT" in body
    assert "Convert to Services" in body


def test_services_page_after_conversion_shows_no_pending(app, client):
    login(client)
    with app.app_context():
        _make_rent_material()
        db.session.commit()
    client.post("/services/migrate", data={"confirm": "yes"}, follow_redirects=True)

    resp = client.get("/services")
    body = resp.get_data(as_text=True)
    assert "No rent materials remain" in body


def test_delete_refused_while_history_references_service(app, client):
    login(client)
    with app.app_context():
        svc = Service(name="Delivery Rent", code="SVC-DR1", unit="Pcs")
        db.session.add(svc)
        db.session.flush()
        db.session.add(Entry(material="Delivery Rent", type="OUT", qty=3, date="2026-01-01"))
        db.session.commit()
        svc_id = svc.id

    client.post(f"/services/delete/{svc_id}", follow_redirects=True)
    with app.app_context():
        assert db.session.get(Service, svc_id) is not None


def test_rename_cascades_into_historical_rows(app, client):
    login(client)
    with app.app_context():
        svc = Service(name="Delivery Rent", code="SVC-DR1", unit="Pcs")
        db.session.add(svc)
        db.session.flush()
        db.session.add(Entry(material="Delivery Rent", type="OUT", qty=3, date="2026-01-01"))
        db.session.commit()
        svc_id = svc.id

    client.post(f"/services/edit/{svc_id}",
                data={"service_name": "Freight Rent", "service_code": "SVC-DR1",
                      "service_unit": "Pcs"},
                follow_redirects=True)

    with app.app_context():
        assert Service.query.filter_by(name="Freight Rent").first() is not None
        assert Entry.query.filter_by(material="Delivery Rent").count() == 0
        assert Entry.query.filter_by(material="Freight Rent").count() == 1


# ---------------------------------------------------------------------------
# End-to-end: sales, bookings, stock views, GRN rejection, negative-stock gate
# ---------------------------------------------------------------------------

def test_material_cannot_collide_with_service_name_or_code(app, client):
    login(client)
    with app.app_context():
        db.session.add(Service(name="RENT-CEMENT", code="FBMREN-000002", unit="Pcs"))
        db.session.commit()

    client.post("/add_material",
                data={"material_name": "RENT-CEMENT", "material_code": "NEWCODE-1", "material_unit": "Bags"},
                follow_redirects=True)
    with app.app_context():
        assert Material.query.filter_by(name="RENT-CEMENT").first() is None

    client.post("/add_material",
                data={"material_name": "New Brand", "material_code": "FBMREN-000002", "material_unit": "Bags"},
                follow_redirects=True)
    with app.app_context():
        assert Material.query.filter_by(code="FBMREN-000002").first() is None


def test_grn_rejects_service_line(app, client):
    login(client)
    with app.app_context():
        from models import GRNItem
        db.session.add(Service(name="RENT-CEMENT", code="FBMREN-000002", unit="Pcs"))
        db.session.commit()

    resp = client.post("/grn", data={
        "action": "add",
        "supplier": "Some Supplier",
        "manual_bill_no": "GRN-SVC-1",
        "mat_name[]": ["RENT-CEMENT"],
        "qty[]": ["10"],
        "price[]": ["50"],
    }, follow_redirects=True)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "service/charge and cannot be received on a GRN" in body
    with app.app_context():
        from models import GRNItem
        assert GRNItem.query.filter_by(mat_name="RENT-CEMENT").count() == 0


def test_service_booking_and_sale_with_negative_stock_disallowed(app, client):
    """A service can be booked and sold even when allow_negative_stock is False,
    never appears in stock_summary or dashboard, and cannot be used as an
    alternate material."""
    login(client)
    with app.app_context():
        from models import Client, Settings, Account
        settings = Settings.query.first()
        if not settings:
            settings = Settings(allow_global_negative_stock=False)
            db.session.add(settings)
        else:
            settings.allow_global_negative_stock = False
        c = Client(name="TEST CLIENT", code="FBM-90001", category="Credit Customer", is_active=True)
        db.session.add(c)
        acc = Account.query.first()
        if not acc:
            acc = Account(name="Main Cash", account_type="Cash", category="Cash", is_active=True)
            db.session.add(acc)
        mat = Material(name="12MM STEEL", code="FBMST-000001", unit="KG", total=100)
        svc = Service(name="RENT-CEMENT", code="FBMREN-000002", unit="Pcs")
        db.session.add_all([mat, svc])
        db.session.commit()
        svc_id = svc.id
        mat_id = mat.id
        acc_id = acc.id

    # 1) Book 20 units of RENT-CEMENT and 50 KG of 12MM STEEL
    resp = client.post("/add_booking", data={
        "client_code": "FBM-90001",
        "material_name[]": ["RENT-CEMENT", "12MM STEEL"],
        "material_id[]": ["", str(mat_id)],
        "service_id[]": [str(svc_id), ""],
        "qty[]": ["20", "50"],
        "unit_rate[]": ["30", "250"],
        "amount": str(20 * 30 + 50 * 250),
        "paid_amount": "0",
        "payment_account_id": str(acc_id),
    }, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert BookingItem.query.filter_by(material_name="RENT-CEMENT").count() == 1

    # 2) Chargeable credit sale of RENT-CEMENT with allow_global_negative_stock=False
    resp = client.post("/add_direct_sale", data={
        "category": "Credit Customer",
        "client_code": "FBM-90001",
        "driver_name": "Driver 1",
        "product_name[]": ["RENT-CEMENT"],
        "material_id[]": [""],
        "service_id[]": [str(svc_id)],
        "alternate_material[]": [""],
        "alternate_material_id[]": [""],
        "qty[]": ["15"],
        "unit_rate[]": ["35"],
        "paid_amount": "0",
    }, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        # Sale item and Entry row are both created (booking pool + ledger stay intact)
        assert DirectSaleItem.query.filter_by(product_name="RENT-CEMENT").count() == 1
        assert Entry.query.filter_by(material="RENT-CEMENT", type="OUT").count() == 1
        sale = DirectSale.query.order_by(DirectSale.id.desc()).first()
        assert float(sale.rent_item_revenue or 0) == 15 * 35

    # 3) Attempting to use a Service as alternate_material on a booked delivery is rejected
    resp = client.post("/add_direct_sale", data={
        "category": "Booking Delivery",
        "client_code": "FBM-90001",
        "driver_name": "Driver 1",
        "product_name[]": ["12MM STEEL"],
        "material_id[]": [str(mat_id)],
        "service_id[]": [""],
        "alternate_material[]": ["RENT-CEMENT"],
        "alternate_material_id[]": [""],
        "qty[]": ["10"],
        "unit_rate[]": ["0"],
        "paid_amount": "0",
    }, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        # Must NOT have created a second DirectSale
        assert DirectSale.query.count() == 1

    # 4) Stock summary and dashboard exclude RENT-CEMENT while keeping 12MM STEEL
    stock_html = client.get("/inventory/stock_summary").get_data(as_text=True)
    assert "12MM STEEL" in stock_html
    # RENT-CEMENT must not appear as a row in stock_summary
    assert "RENT-CEMENT" not in stock_html

    dash_html = client.get("/").get_data(as_text=True)
    assert "12MM STEEL" in dash_html
    assert "RENT-CEMENT" not in dash_html


def test_production_snapshot_differential_invariants(app_factory, tmp_path):
    """Run the conversion on a copy of LEGACY DATA/AMS_SMOOTH_IMPORT.amsdb and
    verify every invariant in SERVICES_PLAN.md §12."""
    import shutil
    from pathlib import Path
    from sqlalchemy import func
    from models import (
        Booking, BookingItem, DirectSale, DirectSaleItem, Entry,
        GRNItem, Material, Payment, PendingBill, Service, db,
    )
    from app.services.lookups import is_service_name, _is_legacy_rent_name, invalidate_service_cache
    from app.services.void_rebuild import _rebuild_material_totals

    src = Path(__file__).resolve().parents[1] / "LEGACY DATA" / "AMS_SMOOTH_IMPORT.amsdb"
    if not src.exists():
        pytest.skip("Production snapshot not present")
    work_db = tmp_path / "snapshot_copy.db"
    shutil.copyfile(src, work_db)

    app = app_factory(work_db)
    app.config["TESTING"] = True
    client = app.test_client()

    def _freeze_invariants():
        non_rent_totals = {
            m.name: round(float(m.total or 0), 4)
            for m in Material.query.order_by(Material.id.asc()).all()
            if not _is_legacy_rent_name(m.name)
        }
        sales_summary = db.session.query(
            func.count(DirectSale.id),
            func.coalesce(func.sum(DirectSale.amount), 0),
            func.coalesce(func.sum(DirectSale.paid_amount), 0),
            func.coalesce(func.sum(DirectSale.discount), 0),
            func.coalesce(func.sum(DirectSale.rent_item_revenue), 0),
            func.coalesce(func.sum(DirectSale.delivery_rent_cost), 0),
        ).one()
        sale_items_summary = db.session.query(
            func.count(DirectSaleItem.id),
            func.coalesce(func.sum(DirectSaleItem.qty), 0),
            func.coalesce(func.sum(DirectSaleItem.price_at_time), 0),
        ).one()
        bookings_summary = db.session.query(
            func.count(Booking.id),
            func.coalesce(func.sum(Booking.amount), 0),
            func.coalesce(func.sum(Booking.paid_amount), 0),
            func.coalesce(func.sum(Booking.discount), 0),
        ).one()
        booking_items_summary = db.session.query(
            func.count(BookingItem.id),
            func.coalesce(func.sum(BookingItem.qty), 0),
            func.coalesce(func.sum(BookingItem.price_at_time), 0),
        ).one()
        pending_summary = db.session.query(
            func.count(PendingBill.id),
            func.coalesce(func.sum(PendingBill.amount), 0),
        ).one()
        payments_summary = db.session.query(
            func.count(Payment.id),
            func.coalesce(func.sum(Payment.amount), 0),
        ).one()
        entry_summary = {
            (r[0], r[1]): (int(r[2]), round(float(r[3] or 0), 4))
            for r in db.session.query(
                Entry.type, Entry.material, func.count(Entry.id), func.sum(Entry.qty)
            ).group_by(Entry.type, Entry.material).all()
        }
        grn_items_count = GRNItem.query.count()
        return {
            "non_rent_totals": non_rent_totals,
            "sales": tuple(round(float(x or 0), 4) for x in sales_summary),
            "sale_items": tuple(round(float(x or 0), 4) for x in sale_items_summary),
            "bookings": tuple(round(float(x or 0), 4) for x in bookings_summary),
            "booking_items": tuple(round(float(x or 0), 4) for x in booking_items_summary),
            "pending": tuple(round(float(x or 0), 4) for x in pending_summary),
            "payments": tuple(round(float(x or 0), 4) for x in payments_summary),
            "entry": entry_summary,
            "grn_items": grn_items_count,
        }

    with app.app_context():
        invalidate_service_cache()
        all_mats = Material.query.all()
        assert len(all_mats) == 70
        # Preflight #4: before cutover, is_service_name matches legacy rule for all 70
        for m in all_mats:
            assert is_service_name(m.name) == _is_legacy_rent_name(m.name), m.name
        before = _freeze_invariants()
        assert len(before["non_rent_totals"]) == 66

    login(client)
    resp = client.post("/services/migrate", data={"confirm": "yes"}, follow_redirects=True)
    assert resp.status_code == 200

    with app.app_context():
        invalidate_service_cache()
        _rebuild_material_totals()
        db.session.commit()

        assert Material.query.count() == 66
        services = Service.query.order_by(Service.name.asc()).all()
        assert [s.name for s in services] == ["RENT STEEL", "RENT-CEMENT", "RENT-GENERAL", "RENT-STEEL"]
        # RENT STEEL's unit_price is preserved on the Service row (§15 #2)
        rent_steel = Service.query.filter_by(name="RENT STEEL").first()
        assert round(float(rent_steel.unit_price or 0), 4) == 1.7857

        # Preflight #1 & #3: zero name/code overlap between Material and Service
        mat_names = {m.name.strip().lower() for m in Material.query.all()}
        mat_codes = {m.code.strip().lower() for m in Material.query.all()}
        for s in services:
            assert s.name.strip().lower() not in mat_names
            assert s.code.strip().lower() not in mat_codes
            assert is_service_name(s.name) is True

        # Preflight #6: all 1,608 rent Entry rows, 1,607 sale lines, 301 booking lines intact
        rent_names = ["RENT-CEMENT", "RENT-GENERAL", "RENT-STEEL", "RENT STEEL"]
        assert Entry.query.filter(Entry.material.in_(rent_names)).count() == 1608
        assert DirectSaleItem.query.filter(DirectSaleItem.product_name.in_(rent_names)).count() == 1607
        assert BookingItem.query.filter(BookingItem.material_name.in_(rent_names)).count() == 301

        after = _freeze_invariants()
        assert after == before

    # Preflight #5: stock_summary and dashboard have 0 rent rows
    stock_body = client.get("/inventory/stock_summary").get_data(as_text=True)
    dash_body = client.get("/").get_data(as_text=True)
    for rn in ["RENT-CEMENT", "RENT-GENERAL", "RENT-STEEL", "RENT STEEL"]:
        assert rn not in stock_body
        assert rn not in dash_body

    # Smoke-test historical rent sale & booking edit modals and bill views on the snapshot
    with app.app_context():
        svc_cement = Service.query.filter_by(name="RENT-CEMENT").first()
        rent_sale_item = DirectSaleItem.query.filter_by(product_name="RENT-CEMENT").first()
        rent_bk_item = BookingItem.query.filter_by(material_name="RENT-CEMENT").first()
        hist_sale_id = rent_sale_item.sale_id
        hist_bk_id = rent_bk_item.booking_id
        hist_sale_ref = db.session.get(DirectSale, hist_sale_id).auto_bill_no
        hist_bk_ref = db.session.get(Booking, hist_bk_id).auto_bill_no
        svc_cement_id = svc_cement.id

    sale_modal_html = client.get(f"/direct_sales/{hist_sale_id}/edit-modal").get_data(as_text=True)
    assert f'name="service_id[]"' in sale_modal_html
    assert f'value="{svc_cement_id}"' in sale_modal_html

    bk_modal_html = client.get(f"/bookings/{hist_bk_id}/edit-modal").get_data(as_text=True)
    assert f'name="service_id[]"' in bk_modal_html
    assert f'value="{svc_cement_id}"' in bk_modal_html

    for page_url in [
        f"/view_bill/{hist_sale_ref}",
        f"/view_bill/{hist_bk_ref}",
        "/direct_sales",
        "/bookings",
        "/material_returns",
        "/dispatching",
        "/tracking",
        "/grn",
        "/materials",
        "/services",
        "/inventory/daily_transactions",
        "/profit_reports?material=RENT-CEMENT",
    ]:
        r = client.get(page_url, follow_redirects=True)
        assert r.status_code == 200, (page_url, r.status_code)


def test_full_rent_transaction_lifecycle_and_smoke(app, client):
    """End-to-end rent transaction audit:
    1. Convert RENT-CEMENT, RENT-STEEL, RENT-GENERAL from Material to Service.
    2. Book 50 RENT-CEMENT @ Rs 30 + 100 12MM STEEL @ Rs 250.
    3. Verify /api/client_booking_status/<code > shows 50 RENT-CEMENT booked.
    4. Dispatch 20 RENT-CEMENT via /add_record (OUT) -> booking balance becomes 30.
    5. Direct Sale (Credit Customer) of 40 RENT-CEMENT @ Rs 35 with driver_rent=1000
       -> auto-splits 30 booked @ Rs 0 + 10 chargeable @ Rs 35 (= Rs 350).
    6. Suspend RENT-CEMENT and edit both the Booking and the DirectSale (P8: old
       bills must remain editable even when a service is suspended).
    7. Reactivate RENT-CEMENT and post a Booked Material Return of 5 RENT-CEMENT
       @ rent_rate=30 -> restores 5 units to the booking pool and credits Rs 150.
    8. Smoke-test /view_bill, /download_invoice (PDF), /client_ledger,
       /financial_ledger, /inventory/daily_transactions, /profit_reports,
       /dispatch_tracking, /inventory/stock_summary, /services, and /.
    """
    login(client)
    with app.app_context():
        from models import Client, Settings, Account
        settings = Settings.query.first()
        if not settings:
            settings = Settings(allow_global_negative_stock=False)
            db.session.add(settings)
        else:
            settings.allow_global_negative_stock = False
        c = Client(name="AUDIT CLIENT", code="FBM-99001", category="Credit Customer", is_active=True)
        acc = Account.query.first()
        if not acc:
            acc = Account(name="Main Cash", account_type="Cash", category="Cash", is_active=True)
            db.session.add(acc)
        _make_rent_material("RENT-CEMENT", "FBMREN-000002", "Pcs")
        _make_rent_material("RENT-STEEL", "FBMREN-000003", "Pcs")
        _make_rent_material("RENT-GENERAL", "FBMREN-000001", "Pcs")
        mat_steel = Material(name="12MM STEEL", code="FBMST-000001", unit="KG", total=500)
        db.session.add_all([c, mat_steel])
        db.session.commit()
        client_id = c.id
        mat_steel_id = mat_steel.id
        acc_id = acc.id

    # 1) Convert rent materials to services via the UI button endpoint
    resp = client.post("/services/migrate", data={"confirm": "yes"}, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        svc_cement = Service.query.filter_by(name="RENT-CEMENT").first()
        assert svc_cement is not None
        svc_id = svc_cement.id

    # 2) Book 50 RENT-CEMENT @ 30 + 100 12MM STEEL @ 250
    resp = client.post("/add_booking", data={
        "client_code": "FBM-99001",
        "material_name[]": ["RENT-CEMENT", "12MM STEEL"],
        "material_id[]": ["", str(mat_steel_id)],
        "service_id[]": [str(svc_id), ""],
        "qty[]": ["50", "100"],
        "unit_rate[]": ["30", "250"],
        "amount": str(50 * 30 + 100 * 250),
        "paid_amount": "0",
        "payment_account_id": str(acc_id),
        "manual_bill_no": "BK-RENT-101",
    }, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        bk = Booking.query.order_by(Booking.id.desc()).first()
        assert bk is not None
        bk_id = bk.id
        bk_ref = bk.auto_bill_no
        bk_items = {it.material_name: it.id for it in bk.items}
        assert "RENT-CEMENT" in bk_items

    # 3) Check /api/client_booking_status/FBM-99001
    api_resp = client.get("/api/client_booking_status/FBM-99001")
    assert api_resp.status_code == 200
    status_rows = {r["material"]: r for r in api_resp.get_json()}
    assert status_rows["RENT-CEMENT"]["booked"] == 50
    assert status_rows["RENT-CEMENT"]["balance"] == 50

    # 4) Dispatch 20 RENT-CEMENT via /add_record (Dispatching OUT)
    resp = client.post("/add_record", data={
        "type": "OUT",
        "client": "FBM-99001",
        "driver_name": "Driver Khan",
        "material": "RENT-CEMENT",
        "service_id": str(svc_id),
        "qty": "20",
        "bill_no": "DSP-RENT-1",
    }, follow_redirects=True)
    assert resp.status_code == 200
    status_rows = {r["material"]: r for r in client.get("/api/client_booking_status/FBM-99001").get_json()}
    assert status_rows["RENT-CEMENT"]["delivered"] == 20
    assert status_rows["RENT-CEMENT"]["balance"] == 30

    # 5) Direct Sale of 40 RENT-CEMENT (auto-splits 30 booked @ 0 + 10 excess @ 35 = Rs 350)
    resp = client.post("/add_direct_sale", data={
        "category": "Credit Customer",
        "client_code": "FBM-99001",
        "driver_name": "Driver Khan",
        "delivery_rent": "1000",
        "manual_bill_no": "DS-RENT-202",
        "product_name[]": ["RENT-CEMENT"],
        "material_id[]": [""],
        "service_id[]": [str(svc_id)],
        "alternate_material[]": [""],
        "alternate_material_id[]": [""],
        "qty[]": ["40"],
        "unit_rate[]": ["35"],
        "paid_amount": "0",
    }, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        sale = DirectSale.query.order_by(DirectSale.id.desc()).first()
        assert sale is not None
        sale_id = sale.id
        sale_ref = sale.auto_bill_no
        # Credit Customer ignores booking balance by design (chargeable sale: 40 * 35 = 1400)
        assert float(sale.amount or 0) == 40 * 35
        assert float(sale.rent_item_revenue or 0) == 40 * 35
        assert float(sale.delivery_rent_cost or 0) == 1000
        for it in sale.items:
            assert it.grn_item_id is None
            assert it.cost_rate_at_sale is None

    # 5b) Mixed Transaction / Booking Delivery sale consuming the remaining 30 booked + 10 excess @ 35
    resp = client.post("/add_direct_sale", data={
        "category": "Mixed Transaction",
        "client_code": "FBM-99001",
        "driver_name": "Driver Khan",
        "manual_bill_no": "DS-RENT-203",
        "product_name[]": ["RENT-CEMENT"],
        "material_id[]": [""],
        "service_id[]": [str(svc_id)],
        "alternate_material[]": [""],
        "alternate_material_id[]": [""],
        "qty[]": ["40"],
        "unit_rate[]": ["35"],
        "paid_amount": "0",
    }, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        mixed_sale = DirectSale.query.order_by(DirectSale.id.desc()).first()
        assert mixed_sale is not None
        # 30 booked @ 0 + 10 excess @ 35 = 350
        assert float(mixed_sale.amount or 0) == 10 * 35

    status_rows = {r["material"]: r for r in client.get("/api/client_booking_status/FBM-99001").get_json()}
    assert status_rows["RENT-CEMENT"]["balance"] == 0

    # 6) Suspend RENT-CEMENT and verify old Booking & DirectSale stay editable (P8)
    client.post(f"/services/toggle/{svc_id}", follow_redirects=True)
    with app.app_context():
        assert db.session.get(Service, svc_id).is_active is False

    # Edit modal renders with hidden service_id pre-filled even when suspended
    assert client.get(f"/bookings/{bk_id}/edit-modal").status_code == 200
    assert client.get(f"/direct_sales/{sale_id}/edit-modal").status_code == 200

    # Save edited Booking while RENT-CEMENT is suspended
    resp = client.post(f"/edit_bill/Booking/{bk_id}", data={
        "client_code": "FBM-99001",
        "booking_item_id[]": [str(bk_items["RENT-CEMENT"]), str(bk_items["12MM STEEL"])],
        "material_name[]": ["RENT-CEMENT", "12MM STEEL"],
        "material_id[]": ["", str(mat_steel_id)],
        "service_id[]": [str(svc_id), ""],
        "qty[]": ["50", "100"],
        "unit_rate[]": ["32", "250"],
        "amount": str(50 * 32 + 100 * 250),
        "paid_amount": "0",
        "manual_bill_no": "BK-RENT-101",
        "note": "Updated booking note",
    }, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert float(db.session.get(Booking, bk_id).amount) == 50 * 32 + 100 * 250

    # Save edited DirectSale while RENT-CEMENT is suspended
    resp = client.post(f"/edit_bill/DirectSale/{sale_id}", data={
        "category": "Credit Customer",
        "client_code": "FBM-99001",
        "driver_name": "Driver Khan",
        "delivery_rent": "1000",
        "manual_bill_no": "DS-RENT-202",
        "product_name[]": ["RENT-CEMENT"],
        "material_id[]": [""],
        "service_id[]": [str(svc_id)],
        "alternate_material[]": [""],
        "alternate_material_id[]": [""],
        "qty[]": ["40"],
        "unit_rate[]": ["36"],
        "paid_amount": "0",
        "note": "Updated sale rate",
    }, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert float(db.session.get(DirectSale, sale_id).amount) == 40 * 36

    # Reactivate RENT-CEMENT
    client.post(f"/services/toggle/{svc_id}", follow_redirects=True)

    # 7) Post a Booked Material Return of 5 RENT-CEMENT @ rent_rate=30
    resp = client.post("/add_material_return", data={
        "client_code": "FBM-99001",
        "return_type": "booked",
        "manual_bill_no": "RTN-RENT-1",
        "material_name[]": ["RENT-CEMENT"],
        "material_id[]": [""],
        "service_id[]": [str(svc_id)],
        "qty[]": ["5"],
        "unit_rate[]": ["0"],
        "rent_rate[]": ["30"],
        "note": "Returned 5 booked rent units",
    }, follow_redirects=True)
    assert resp.status_code == 200
    status_rows = {r["material"]: r for r in client.get("/api/client_booking_status/FBM-99001").get_json()}
    assert status_rows["RENT-CEMENT"]["returned"] == 5
    assert status_rows["RENT-CEMENT"]["balance"] == 5

    # 8) Smoke-test all bills, PDFs, ledgers, reports, and stock views
    for url in [
        f"/view_bill/{bk_ref}",
        f"/view_bill/{sale_ref}",
        f"/download_invoice/{sale_ref}",
        f"/client_ledger/{client_id}",
        f"/ledger/{client_id}",
        f"/financial_ledger/{client_id}",
        "/inventory/daily_transactions",
        "/profit_reports?material=RENT-CEMENT",
        "/tracking",
        "/services",
    ]:
        r = client.get(url, follow_redirects=True)
        assert r.status_code == 200, (url, r.status_code)

    # Stock views must remain 100% free of services
    for stock_url in ["/inventory/stock_summary", "/"]:
        body = client.get(stock_url).get_data(as_text=True)
        assert "12MM STEEL" in body
        for svc_name in ("RENT-CEMENT", "RENT-STEEL", "RENT-GENERAL"):
            assert svc_name not in body


def test_services_never_use_alternate_material_system_in_direct_sales(app, client):
    """Services (RENT-CEMENT, RENT-STEEL, custom services, and pre-migration
    legacy rent items) must never use the alternate material system in Direct
    Sales — neither as the primary item receiving an alternate nor as an
    alternate itself — while normal materials continue to use alternates."""
    import re as _re
    from models import Client, Settings, Account

    login(client)
    with app.app_context():
        settings = Settings.query.first()
        if not settings:
            settings = Settings(allow_global_negative_stock=False)
            db.session.add(settings)
        else:
            settings.allow_global_negative_stock = False
        c = Client(name="ALT TEST CLIENT", code="FBM-91001", category="Credit Customer", is_active=True)
        acc = Account.query.first()
        if not acc:
            acc = Account(name="Main Cash", account_type="Cash", category="Cash", is_active=True)
            db.session.add(acc)
        mat_12 = Material(name="12MM STEEL", code="FBMST-000012", unit="KG", total=500, unit_price=240)
        mat_20 = Material(name="20MM STEEL", code="FBMST-000020", unit="KG", total=500, unit_price=250)
        mat_kohat = Material(name="KOHAT CEMENT", code="FBMCEM-000001", unit="Bags", total=500, unit_price=1200)
        # Pre-migration legacy rent material still in Material table:
        legacy_rent_steel = _make_rent_material(name="RENT-STEEL", code="FBMREN-000003", unit="Pcs")
        # Registered services in Service master:
        svc_cement = Service(name="RENT-CEMENT", code="FBMREN-000002", unit="Pcs")
        svc_custom = Service(name="CRANE LOADING", code="SVC-CRANE-01", unit="Trip")
        db.session.add_all([
            c, mat_12, mat_20, mat_kohat, svc_cement, svc_custom,
            Entry(material="12MM STEEL", type="IN", qty=500, date="2026-01-01"),
            Entry(material="20MM STEEL", type="IN", qty=500, date="2026-01-01"),
            Entry(material="KOHAT CEMENT", type="IN", qty=500, date="2026-01-01"),
        ])
        db.session.commit()
        from app.services.lookups import invalidate_service_cache
        invalidate_service_cache()
        acc_id = acc.id
        mat_12_id = mat_12.id
        mat_20_id = mat_20.id
        mat_kohat_id = mat_kohat.id
        legacy_rent_steel_id = legacy_rent_steel.id
        svc_cement_id = svc_cement.id
        svc_custom_id = svc_custom.id

    # 1) Direct Sales page Alternate dropdown excludes services AND legacy rent materials
    ds_html = client.get("/direct_sales").get_data(as_text=True)
    alt_list_match = _re.search(
        r'<div id="addSaleAltMaterialList0"[^>]*>(.*?)</div>\s*<div id="addSaleAltMaterialDisplay0"',
        ds_html,
        _re.S,
    )
    assert alt_list_match is not None
    alt_list_html = alt_list_match.group(1)
    assert "12MM STEEL" in alt_list_html
    assert "20MM STEEL" in alt_list_html
    assert "KOHAT CEMENT" in alt_list_html
    assert "RENT-CEMENT" not in alt_list_html
    assert "RENT-STEEL" not in alt_list_html
    assert "CRANE LOADING" not in alt_list_html

    # 2) Book 12MM STEEL + RENT-CEMENT + RENT-STEEL + CRANE LOADING so all have booking balance
    resp = client.post("/add_booking", data={
        "client_code": "FBM-91001",
        "material_name[]": ["12MM STEEL", "RENT-CEMENT", "RENT-STEEL", "CRANE LOADING"],
        "material_id[]": [str(mat_12_id), "", str(legacy_rent_steel_id), ""],
        "service_id[]": ["", str(svc_cement_id), "", str(svc_custom_id)],
        "qty[]": ["100", "50", "50", "5"],
        "unit_rate[]": ["240", "30", "25", "1000"],
        "amount": str(100 * 240 + 50 * 30 + 50 * 25 + 5 * 1000),
        "paid_amount": "0",
        "payment_account_id": str(acc_id),
    }, follow_redirects=True)
    assert resp.status_code == 200

    # 3) Attempting to supply a physical alternate ON booked RENT-CEMENT is rejected
    resp = client.post("/add_direct_sale", data={
        "category": "Booking Delivery",
        "client_code": "FBM-91001",
        "driver_name": "Driver A",
        "product_name[]": ["RENT-CEMENT"],
        "material_id[]": [""],
        "service_id[]": [str(svc_cement_id)],
        "alternate_material[]": ["KOHAT CEMENT"],
        "alternate_material_id[]": [str(mat_kohat_id)],
        "qty[]": ["10"],
        "unit_rate[]": ["0"],
        "paid_amount": "0",
    }, follow_redirects=True)
    assert "Alternate material cannot be used for service" in resp.get_data(as_text=True)
    with app.app_context():
        assert DirectSale.query.count() == 0

    # 4) Attempting to supply a physical alternate ON booked custom service (CRANE LOADING) is rejected
    resp = client.post("/add_direct_sale", data={
        "category": "Booking Delivery",
        "client_code": "FBM-91001",
        "driver_name": "Driver A",
        "product_name[]": ["CRANE LOADING"],
        "material_id[]": [""],
        "service_id[]": [str(svc_custom_id)],
        "alternate_material[]": ["20MM STEEL"],
        "alternate_material_id[]": [str(mat_20_id)],
        "qty[]": ["1"],
        "unit_rate[]": ["0"],
        "paid_amount": "0",
    }, follow_redirects=True)
    assert "Alternate material cannot be used for service" in resp.get_data(as_text=True)
    with app.app_context():
        assert DirectSale.query.count() == 0

    # 5) Attempting to supply a physical alternate ON pre-migration RENT-STEEL is rejected
    resp = client.post("/add_direct_sale", data={
        "category": "Booking Delivery",
        "client_code": "FBM-91001",
        "driver_name": "Driver A",
        "product_name[]": ["RENT-STEEL"],
        "material_id[]": [str(legacy_rent_steel_id)],
        "service_id[]": [""],
        "alternate_material[]": ["20MM STEEL"],
        "alternate_material_id[]": [str(mat_20_id)],
        "qty[]": ["10"],
        "unit_rate[]": ["0"],
        "paid_amount": "0",
    }, follow_redirects=True)
    assert "Alternate material cannot be used for service" in resp.get_data(as_text=True)
    with app.app_context():
        assert DirectSale.query.count() == 0

    # 6) Attempting to select pre-migration RENT-STEEL AS alternate for 12MM STEEL is rejected
    resp = client.post("/add_direct_sale", data={
        "category": "Booking Delivery",
        "client_code": "FBM-91001",
        "driver_name": "Driver A",
        "product_name[]": ["12MM STEEL"],
        "material_id[]": [str(mat_12_id)],
        "service_id[]": [""],
        "alternate_material[]": ["RENT-STEEL"],
        "alternate_material_id[]": [str(legacy_rent_steel_id)],
        "qty[]": ["10"],
        "unit_rate[]": ["0"],
        "paid_amount": "0",
    }, follow_redirects=True)
    assert "cannot be used as an alternate material" in resp.get_data(as_text=True)
    with app.app_context():
        assert DirectSale.query.count() == 0

    # 7) Multi-row sale: normal material WITH alternate + RENT-CEMENT & CRANE LOADING WITHOUT alternate succeeds!
    resp = client.post("/add_direct_sale", data={
        "category": "Mixed Transaction",
        "client_code": "FBM-91001",
        "driver_name": "Driver A",
        "manual_bill_no": "DS-MIX-SVC-ALT-1",
        "product_name[]": ["12MM STEEL", "RENT-CEMENT", "CRANE LOADING"],
        "material_id[]": [str(mat_12_id), "", ""],
        "service_id[]": ["", str(svc_cement_id), str(svc_custom_id)],
        "alternate_material[]": ["20MM STEEL", "", ""],
        "alternate_material_id[]": [str(mat_20_id), "", ""],
        "qty[]": ["40", "60", "2"],
        "unit_rate[]": ["0", "35", "0"],
        "paid_amount": "0",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert "Direct sale added successfully" in resp.get_data(as_text=True)
    with app.app_context():
        assert DirectSale.query.count() == 1
        sale = DirectSale.query.first()
        sale_id = sale.id
        # 40 of 20MM STEEL delivered against 12MM STEEL booking (rate 0),
        # 50 booked RENT-CEMENT @ 0 + 10 excess @ 35 = 350,
        # 2 booked CRANE LOADING @ 0
        assert float(sale.amount or 0) == 10 * 35
        assert float(Material.query.filter_by(name="12MM STEEL").first().total) == 500
        assert float(Material.query.filter_by(name="20MM STEEL").first().total) == 460

    # 8) Edit modal marks service rows with service-alt-disabled & readonly, keeps material row editable,
    #    and excludes services from editSaleAltMaterialList
    modal_html = client.get(f"/direct_sales/{sale_id}/edit-modal").get_data(as_text=True)
    assert "service-alt-disabled" in modal_html
    assert 'placeholder="N/A for services"' in modal_html
    assert 'placeholder="Alternate (optional)"' in modal_html
    for m in _re.finditer(r'<div id="editSaleAltMaterialList[^"]*"[^>]*>(.*?)</div>', modal_html, _re.S):
        sub = m.group(1)
        assert "12MM STEEL" in sub
        assert "RENT-CEMENT" not in sub
        assert "RENT-STEEL" not in sub
        assert "CRANE LOADING" not in sub

    # 9) Edit Direct Sale rejects adding an alternate onto a service row OR using a service as alternate
    resp = client.post(f"/edit_bill/DirectSale/{sale_id}", data={
        "category": "Mixed Transaction",
        "client_code": "FBM-91001",
        "driver_name": "Driver A",
        "manual_bill_no": "DS-MIX-SVC-ALT-1",
        "product_name[]": ["12MM STEEL", "RENT-CEMENT"],
        "material_id[]": [str(mat_12_id), ""],
        "service_id[]": ["", str(svc_cement_id)],
        "alternate_material[]": ["20MM STEEL", "KOHAT CEMENT"],
        "alternate_material_id[]": [str(mat_20_id), str(mat_kohat_id)],
        "qty[]": ["40", "60"],
        "unit_rate[]": ["0", "35"],
        "paid_amount": "0",
    }, follow_redirects=True)
    assert "Alternate material cannot be used for service" in resp.get_data(as_text=True)

    resp = client.post(f"/edit_bill/DirectSale/{sale_id}", data={
        "category": "Mixed Transaction",
        "client_code": "FBM-91001",
        "driver_name": "Driver A",
        "manual_bill_no": "DS-MIX-SVC-ALT-1",
        "product_name[]": ["12MM STEEL", "RENT-CEMENT"],
        "material_id[]": [str(mat_12_id), ""],
        "service_id[]": ["", str(svc_cement_id)],
        "alternate_material[]": ["RENT-STEEL", ""],
        "alternate_material_id[]": [str(legacy_rent_steel_id), ""],
        "qty[]": ["40", "60"],
        "unit_rate[]": ["0", "35"],
        "paid_amount": "0",
    }, follow_redirects=True)
    assert "cannot be used as an alternate material" in resp.get_data(as_text=True)

