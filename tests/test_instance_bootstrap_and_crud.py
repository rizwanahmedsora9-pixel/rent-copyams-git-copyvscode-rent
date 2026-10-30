"""Empty-instance bootstrap + CRUD coverage for every major section.

On start:
* missing / deleted runtime DB  → create empty schema (0 business rows)
* existing runtime DB           → reuse it, never wipe
* leftover -wal/-shm            → drop lock leftovers, then recreate
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from models import (
    Account,
    Booking,
    Client,
    DeliveryPerson,
    DirectSale,
    GRN,
    Material,
    Payment,
    PendingBill,
    Supplier,
    User,
    db,
)
ADMIN = {"username": "Admin", "password": "Admin@fbm12345"}

REQUIRED_TABLES = {
    "user",
    "client",
    "supplier",
    "material",
    "material_category",
    "delivery_person",
    "booking",
    "direct_sale",
    "payment",
    "grn",
    "entry",
    "pending_bill",
    "account",
    "account_transaction",
    "system_lock",
}


def _login(client):
    resp = client.post("/login", data=ADMIN, follow_redirects=False)
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:500]
    return resp


def _close(app):
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


def _table_names(app):
    with app.app_context():
        rows = db.session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        ).fetchall()
    return {r[0] for r in rows}


def _assert_empty_business(app):
    with app.app_context():
        assert User.query.count() == 1
        assert DirectSale.query.count() == 0
        assert Booking.query.count() == 0
        assert Payment.query.count() == 0
        assert GRN.query.count() == 0
        assert PendingBill.query.count() == 0
        assert Supplier.query.count() == 0
        assert Material.query.count() == 0
        # OPEN-KHATA is a system seed, not business data.
        extras = Client.query.filter(db.func.lower(Client.code) != "open-khata").count()
        assert extras == 0


# ---------------------------------------------------------------------------
# Instance / database file lifecycle
# ---------------------------------------------------------------------------
def test_empty_instance_folder_creates_runtime_db(app_factory, tmp_path):
    instance = tmp_path / "instance"
    instance.mkdir()
    db_file = instance / "ahmed_cement_v44_fresh.db"
    assert not db_file.exists()
    assert list(instance.glob("*.db")) == []

    app = app_factory(db_file)
    assert app.config.get("AMS_BOOTSTRAP_ERROR") is None
    assert db_file.exists() and db_file.stat().st_size > 0
    assert app.config.get("AMS_RUNTIME_DB_CREATED") is True
    names = _table_names(app)
    missing = REQUIRED_TABLES - names
    assert not missing, missing
    _assert_empty_business(app)
    # No leftover lock sidecars on a DELETE-journal fresh file after close.
    _close(app)
    assert not Path(str(db_file) + "-wal").exists()
    assert not Path(str(db_file) + "-shm").exists()
    assert not Path(str(db_file) + "-journal").exists()


def test_existing_database_is_not_wiped(app_factory, tmp_path):
    db_file = tmp_path / "keep.db"
    first = app_factory(db_file)
    with first.app_context():
        db.session.add(Client(code="KEEP-1", name="Keep Me", is_active=True))
        db.session.commit()
    _close(first)

    second = app_factory(db_file)
    assert second.config.get("AMS_RUNTIME_DB_CREATED") is False
    with second.app_context():
        row = Client.query.filter_by(code="KEEP-1").one()
        assert row.name == "Keep Me"
        assert DirectSale.query.count() == 0
    _close(second)


def test_deleted_db_with_orphan_wal_recreates_empty(app_factory, tmp_path):
    db_file = tmp_path / "gone.db"
    first = app_factory(db_file)
    with first.app_context():
        db.session.add(Client(code="GONE-1", name="Will Vanish", is_active=True))
        db.session.commit()
    _close(first)

    db_file.unlink()
    Path(str(db_file) + "-wal").write_bytes(b"garbage-lock")
    Path(str(db_file) + "-shm").write_bytes(b"garbage-lock")
    Path(str(db_file) + "-journal").write_bytes(b"garbage-lock")

    second = app_factory(db_file)
    assert second.config.get("AMS_BOOTSTRAP_ERROR") is None
    assert second.config.get("AMS_RUNTIME_DB_CREATED") is True
    assert db_file.exists()
    assert not Path(str(db_file) + "-wal").exists()
    assert not Path(str(db_file) + "-shm").exists()
    with second.app_context():
        assert Client.query.filter_by(code="GONE-1").first() is None
        assert User.query.count() == 1
        assert DirectSale.query.count() == 0
    _close(second)


def test_zero_byte_placeholder_is_recreated(app_factory, tmp_path):
    db_file = tmp_path / "empty.db"
    db_file.write_bytes(b"")
    app = app_factory(db_file)
    assert app.config.get("AMS_BOOTSTRAP_ERROR") is None
    assert db_file.stat().st_size > 0
    _assert_empty_business(app)
    _close(app)


def test_stale_system_lock_is_unlocked_on_start(app_factory, tmp_path):
    db_file = tmp_path / "locks.db"
    first = app_factory(db_file)
    with first.app_context():
        db.session.execute(text(
            "INSERT INTO system_lock (name, status, owner, ttl_seconds) "
            "VALUES ('accounts_domain_wipe', 'locked', 'crashed-worker', 3600)"
        ))
        db.session.commit()
    _close(first)

    second = app_factory(db_file)
    with second.app_context():
        status = db.session.execute(
            text("SELECT status FROM system_lock WHERE name='accounts_domain_wipe'")
        ).scalar()
        assert status == "unlocked"
    _close(second)


# ---------------------------------------------------------------------------
# CRUD across sections
# ---------------------------------------------------------------------------
def test_crud_works_in_all_major_sections(app, client):
    _login(client)

    # --- Clients ---
    resp = client.post("/add_client", data={
        "name": "Ahmed Traders",
        "code": "C-AHM-01",
        "phone": "03001234567",
        "category": "General",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Client Registered" in resp.data
    with app.app_context():
        cli = Client.query.filter_by(code="C-AHM-01").one()
        cid = cli.id
    resp = client.post(f"/edit_client/{cid}", data={
        "name": "Ahmed Traders Updated",
        "code": "C-AHM-01",
        "phone": "03007654321",
        "category": "General",
    }, follow_redirects=True)
    assert b"Client updated" in resp.data
    resp = client.post(f"/delete_client/{cid}", data={}, follow_redirects=True)
    assert b"Client suspended" in resp.data
    with app.app_context():
        assert Client.query.get(cid).is_active is False
        # Reactivate so later sales/payments can use it.
        row = Client.query.get(cid)
        row.is_active = True
        db.session.commit()

    # --- Suppliers ---
    resp = client.post("/add_supplier", data={"name": "Lucky Mills", "phone": "042111"}, follow_redirects=True)
    assert b"Supplier Added" in resp.data
    with app.app_context():
        sid = Supplier.query.filter_by(name="Lucky Mills").one().id
    resp = client.post(f"/edit_supplier/{sid}", data={
        "name": "Lucky Mills Ltd",
        "phone": "042222",
        "is_active": "on",
    }, follow_redirects=True)
    assert b"Supplier updated" in resp.data
    resp = client.post(f"/delete_supplier/{sid}", data={}, follow_redirects=True)
    assert b"Supplier suspended" in resp.data
    with app.app_context():
        assert Supplier.query.get(sid).is_active is False
        row = Supplier.query.get(sid)
        row.is_active = True
        db.session.commit()

    # --- Materials ---
    resp = client.post("/add_material", data={
        "material_name": "OPC Test Cement",
        "material_unit": "Bags",
    }, follow_redirects=True)
    assert b"Brand Added" in resp.data or b"already exists" in resp.data
    with app.app_context():
        mat = Material.query.filter(db.func.lower(Material.name) == "opc test cement").one()
        mid = mat.id
        mcode = mat.code
    resp = client.post(f"/edit_material/{mid}", data={
        "material_name": "OPC Test Cement",
        "material_code": mcode,
        "material_unit": "Tons",
    }, follow_redirects=True)
    assert b"Brand Updated" in resp.data
    resp = client.post(f"/delete_material/{mid}", data={}, follow_redirects=True)
    assert b"Material status updated" in resp.data
    with app.app_context():
        # toggle back on for GRN/sale
        mat = Material.query.get(mid)
        mat.is_active = True
        db.session.commit()

    # --- Delivery persons ---
    resp = client.post("/delivery_persons/add", data={
        "name": "Driver Ali",
        "phone": "03111111111",
    }, follow_redirects=True)
    assert b"Delivery person saved" in resp.data
    with app.app_context():
        did = DeliveryPerson.query.filter_by(name="Driver Ali").one().id
    resp = client.post(f"/delivery_persons/edit/{did}", data={
        "name": "Driver Ali Khan",
        "phone": "03112223333",
    }, follow_redirects=True)
    assert b"Delivery person updated" in resp.data
    resp = client.post(f"/delivery_persons/toggle/{did}", data={}, follow_redirects=True)
    assert b"Delivery person status updated" in resp.data
    with app.app_context():
        row = DeliveryPerson.query.get(did)
        row.is_active = True
        row.name = "Driver Ali Khan"
        db.session.commit()

    # --- Accounts ---
    resp = client.post("/accounts/accounts/add", data={
        "name": "Yard Cash Drawer",
        "account_type": "cash",
        "account_number": "",
        "opening_balance": "100000",
    }, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        acc = Account.query.filter_by(name="Yard Cash Drawer").one()
        aid = acc.id
        assert acc.balance == 100000.0
    resp = client.post(f"/accounts/{aid}/edit", data={
        "name": "Yard Cash Drawer",
        "account_type": "cash",
        "account_number": "",
        "opening_balance": "100000",
    }, follow_redirects=True)
    assert b"Account updated" in resp.data
    resp = client.post(f"/accounts/{aid}/delete", data={}, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        # Unreferenced account is hard-deleted; recreate for later payments.
        if db.session.get(Account, aid) is None:
            pass
    resp = client.post("/accounts/accounts/add", data={
        "name": "Yard Cash Drawer",
        "account_type": "cash",
        "account_number": "",
        "opening_balance": "100000",
    }, follow_redirects=True)
    with app.app_context():
        aid = Account.query.filter_by(name="Yard Cash Drawer").one().id

    # --- Pending bills ---
    resp = client.post("/add_pending_bill", data={
        "client_code": "C-AHM-01",
        "bill_no": "MB-9001",
        "amount": "1500",
        "reason": "Hold",
    }, follow_redirects=True)
    assert b"Pending bill added" in resp.data
    with app.app_context():
        pb = PendingBill.query.filter_by(client_code="C-AHM-01").one()
        pbid = pb.id
    resp = client.post(f"/edit_pending_bill/{pbid}", data={
        "client_code": "C-AHM-01",
        "bill_no": "MB-9001",
        "amount": "1750",
        "reason": "Hold updated",
    }, follow_redirects=True)
    assert b"Bill updated" in resp.data
    resp = client.post(f"/delete_pending_bill/{pbid}", follow_redirects=True)
    assert b"Bill deleted" in resp.data
    with app.app_context():
        # Policy (v4.4, "no void flags"): deleting a pending bill removes the row
        # for real — hard_delete_pending_bill() — and its follow-ups go with it.
        # The old behaviour (soft void, row kept with is_void=1) is gone, and the
        # migration purge assumes it, so a voided row must never survive here.
        assert PendingBill.query.get(pbid) is None
        from models.ops_meta import FollowUpContact, FollowUpReminder
        assert db.session.query(FollowUpContact).filter_by(
            pending_bill_id=pbid).count() == 0
        assert db.session.query(FollowUpReminder).filter_by(
            pending_bill_id=pbid).count() == 0

    # --- GRN ---
    with app.app_context():
        mat_name = Material.query.get(mid).name
        supplier_name = Supplier.query.get(sid).name
    resp = client.post("/grn", data={
        "action": "add",
        "supplier": supplier_name,
        "supplier_id": str(sid),
        "mat_name[]": mat_name,
        "qty[]": "50",
        "price[]": "1200",
        "paid_amount": "0",
    }, follow_redirects=True)
    assert b"GRN added successfully" in resp.data
    with app.app_context():
        grn = GRN.query.order_by(GRN.id.desc()).first()
        assert grn is not None
        gid = grn.id
        assert Material.query.get(mid).total >= 50
    resp = client.post("/grn", data={"action": "delete", "id": str(gid)}, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        leftover = db.session.get(GRN, gid)
        assert leftover is None or leftover.is_void is True

    # Restock for a cash sale (GRN was deleted).
    resp = client.post("/grn", data={
        "action": "add",
        "supplier": supplier_name,
        "supplier_id": str(sid),
        "mat_name[]": mat_name,
        "qty[]": "20",
        "price[]": "1200",
        "paid_amount": "0",
    }, follow_redirects=True)
    assert b"GRN added successfully" in resp.data

    # --- Bookings ---
    resp = client.post("/add_booking", data={
        "client_code": "C-AHM-01",
        "material_name[]": mat_name,
        "qty[]": "5",
        "unit_rate[]": "1400",
        "amount": "7000",
        "paid_amount": "0",
    }, follow_redirects=True)
    assert b"Booking added successfully" in resp.data
    with app.app_context():
        bk = Booking.query.order_by(Booking.id.desc()).first()
        bid = bk.id
    resp = client.post(f"/edit_bill/Booking/{bid}", data={
        "client_code": "C-AHM-01",
        "material_name[]": mat_name,
        "qty[]": "6",
        "unit_rate[]": "1400",
        "amount": "8400",
        "paid_amount": "0",
        "booking_item_id[]": "",
    }, follow_redirects=True)
    assert b"Booking updated" in resp.data

    # --- Payments ---
    resp = client.post("/add_payment", data={
        "client_code": "C-AHM-01",
        "amount": "500",
        "method": "Cash",
        "payment_type": "Receipt",
        "payment_account_id": str(aid),
    }, follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert "Payment received successfully" in body or "Unable to save payment" not in body
    with app.app_context():
        pay = Payment.query.order_by(Payment.id.desc()).first()
        assert pay is not None
        assert float(pay.amount or 0) == 500

    # --- Direct sale (credit, unpaid) ---
    resp = client.post("/add_direct_sale", data={
        "client_name": "Ahmed Traders Updated",
        "client_code": "C-AHM-01",
        "driver_name": "Driver Ali Khan",
        "category": "Credit Customer",
        "product_name[]": mat_name,
        "qty[]": "2",
        "unit_rate[]": "1400",
        "paid_amount": "0",
        "ignore_booking_item[]": "1",
        "allow_negative_stock": "1",
    }, follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert "Direct sale added successfully" in body or "sale could not be saved" not in body.lower()
    with app.app_context():
        sale = DirectSale.query.order_by(DirectSale.id.desc()).first()
        assert sale is not None
        assert sale.client_code == "C-AHM-01"

    # --- Cash Flow is now a read-only report; configuration is not edited there ---
    resp = client.post("/cash_flow", data={
        "action": "add_category",
        "new_category_name": "Office Expense",
        "new_category_direction": "out",
    })
    assert resp.status_code == 405

    # --- Section pages must not 500 after writes ---
    for path in (
        "/",
        "/clients",
        "/suppliers",
        "/materials",
        "/delivery_persons",
        "/grn",
        "/bookings",
        "/direct_sales",
        "/payments",
        "/pending_bills",
        "/accounts/accounts",
        "/cash_flow",
        "/ledger",
        "/financial_ledger",
    ):
        page = client.get(path)
        assert page.status_code < 500, (path, page.status_code)
