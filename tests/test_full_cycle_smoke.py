"""One complete business cycle, end to end through the real HTTP routes.

The scenario is the owner's acceptance walk-through:

    one supplier, one client, one material, one service ("Rent"), one cash
    account → one GRN (partly paid) → one cash booking with a due → the
    booked material supplied → read the supplier ledger, the client ledger,
    the transaction lists, the Profit & Loss report and the audit trail.

Every number is asserted against the source of truth as well as against the
rendered page, so a page that "looks fine" but posts the wrong figure fails.

``tools/audit_full_cycle.py`` runs this same cycle against a throw-away
database together with the full data-wipe phase; this module keeps the
cycle in the regular test suite.
"""
from __future__ import annotations

import pytest

from app.services.time_money import pk_today
from models import (
    Account,
    AuditLog,
    Booking,
    Client,
    GRN,
    Material,
    PendingBill,
    Service,
    Supplier,
    SupplierPayment,
    db,
)

SUPPLIER = "Ravi Cement Supplier"
CLIENT = "Bilal Construction"
MATERIAL = "OPC Cement"
SERVICE = "Rent"
ACCOUNT = "Main Cash"

OPENING_CASH = 200_000.0
GRN_QTY, GRN_RATE, GRN_PAID = 100.0, 1_200.0, 50_000.0
GRN_TOTAL = GRN_QTY * GRN_RATE            # 120,000
SUPPLIER_DUE = GRN_TOTAL - GRN_PAID       # 70,000
BOOK_QTY, BOOK_RATE, BOOK_PAID = 20.0, 1_400.0, 10_000.0
BOOK_SERVICE_QTY, BOOK_SERVICE_RATE = 1.0, 2_000.0
BOOK_TOTAL = BOOK_QTY * BOOK_RATE + BOOK_SERVICE_QTY * BOOK_SERVICE_RATE  # 30,000
BOOK_DUE = BOOK_TOTAL - BOOK_PAID         # 20,000
DISPATCH_QTY = 10.0
REVENUE = DISPATCH_QTY * BOOK_RATE        # 14,000
COGS = DISPATCH_QTY * GRN_RATE            # 12,000
PROFIT = REVENUE - COGS                   # 2,000
CASH_AFTER = OPENING_CASH - GRN_PAID + BOOK_PAID  # 160,000

ADMIN = {"username": "Admin", "password": "Admin@fbm12345"}


def _money(value: float) -> str:
    return f"{value:,.2f}"


def _login(client):
    resp = client.post("/login", data=ADMIN, follow_redirects=False)
    assert resp.status_code in (302, 303)


@pytest.fixture()
def cycle(app, client):
    """Run the whole cycle once and hand back the ids it produced."""
    _login(client)
    today = pk_today().strftime("%Y-%m-%d")

    for path, payload, expected in (
        ("/add_supplier", {"name": SUPPLIER, "phone": "0300-1112233"}, "Supplier Added"),
        ("/add_client", {"name": CLIENT, "code": "CYCLE-1",
                         "category": "Credit Customer"}, "Client Registered"),
        ("/add_material", {"material_name": MATERIAL, "material_unit": "Bags"}, "Brand Added"),
        ("/services/add", {"service_name": SERVICE, "service_unit": "Trip"}, "added"),
    ):
        resp = client.post(path, data=payload, follow_redirects=True)
        assert expected in resp.get_data(as_text=True), (path, expected)

    resp = client.post("/accounts/accounts/add", data={
        "name": ACCOUNT,
        "class_category": "Assets",
        "class_subcategory": "Cash",
        "class_account_type": "Main Cash",
        "account_status": "active",
        "opening_amount": str(OPENING_CASH),
        "opening_position": "debit",
        "opening_effective_date": today,
    }, follow_redirects=True)
    assert resp.status_code == 200

    with app.app_context():
        ids = {
            "supplier": Supplier.query.filter_by(name=SUPPLIER).one().id,
            "client": Client.query.filter_by(code="CYCLE-1").one().id,
            "material": Material.query.filter_by(name=MATERIAL).one().id,
            "service": Service.query.filter_by(name=SERVICE).one().id,
            "account": Account.query.filter_by(name=ACCOUNT).one().id,
        }

    resp = client.post("/grn", data={
        "action": "add",
        "supplier": SUPPLIER,
        "supplier_id": str(ids["supplier"]),
        "mat_name[]": MATERIAL,
        "qty[]": str(GRN_QTY),
        "price[]": str(GRN_RATE),
        "paid_amount": str(GRN_PAID),
        "payment_type": "Cash",
        "payment_account_id": str(ids["account"]),
        "manual_bill_no": "CYCLE-GRN-1",
        "date": today,
    }, follow_redirects=True)
    assert "GRN added successfully" in resp.get_data(as_text=True)

    resp = client.post("/add_booking", data={
        "client_code": "CYCLE-1",
        "material_name[]": [MATERIAL, SERVICE],
        "qty[]": [str(BOOK_QTY), str(BOOK_SERVICE_QTY)],
        "unit_rate[]": [str(BOOK_RATE), str(BOOK_SERVICE_RATE)],
        "amount": str(BOOK_TOTAL),
        "paid_amount": str(BOOK_PAID),
        "payment_account_id": str(ids["account"]),
        "payment_method": "Cash",
        "manual_bill_no": "CYCLE-BK-1",
        "date": today,
    }, follow_redirects=True)
    assert "Booking added successfully" in resp.get_data(as_text=True)

    resp = client.post("/add_record", data={
        "type": "OUT",
        "date": today,
        "client": CLIENT,
        "material": MATERIAL,
        "material_id": str(ids["material"]),
        "qty": str(DISPATCH_QTY),
        "driver_name": "Cycle Driver",
    }, follow_redirects=True)
    assert "Record Saved" in resp.get_data(as_text=True)

    ids["today"] = today
    return ids


def test_grn_and_supplier_ledger(cycle, client, app):
    with app.app_context():
        grn = GRN.query.filter(GRN.manual_bill_no.like("%CYCLE-GRN-1%")).one()
        total = sum(float(i.qty) * float(i.price_at_time) for i in grn.items)
        assert total == pytest.approx(GRN_TOTAL)
        payment = SupplierPayment.query.filter_by(supplier_id=cycle["supplier"]).one()
        assert float(payment.amount) == pytest.approx(GRN_PAID)
        material = db.session.get(Material, cycle["material"])
        assert float(material.total) == pytest.approx(GRN_QTY - DISPATCH_QTY)

    body = client.get(f"/supplier_ledger/{cycle['supplier']}").get_data(as_text=True)
    assert "CYCLE-GRN-1" in body
    assert _money(GRN_TOTAL) in body          # purchase debit
    assert _money(GRN_PAID) in body           # cash payment credit
    assert _money(SUPPLIER_DUE) in body       # closing payable

    pdf = client.get(f"/download_supplier_ledger/{cycle['supplier']}?action=pdf")
    assert pdf.status_code == 200 and pdf.get_data()[:4] == b"%PDF"

    assert "CYCLE-GRN-1" in client.get("/grn").get_data(as_text=True)


def test_cash_booking_due_and_ledgers(cycle, client, app):
    with app.app_context():
        booking = Booking.query.filter(Booking.manual_bill_no.like("%CYCLE-BK-1%")).one()
        assert float(booking.amount) == pytest.approx(BOOK_TOTAL)
        assert float(booking.paid_amount) == pytest.approx(BOOK_PAID)
        assert {item.material_name for item in booking.items} == {MATERIAL, SERVICE}
        pending = PendingBill.query.filter(PendingBill.bill_no.like("%CYCLE-BK-1%")).one()
        assert float(pending.amount) == pytest.approx(BOOK_DUE)
        account = db.session.get(Account, cycle["account"])
        assert float(account.balance) == pytest.approx(CASH_AFTER)

    ledger = client.get(f"/ledger/{cycle['client']}").get_data(as_text=True)
    assert "CYCLE-BK-1" in ledger and _money(BOOK_DUE) in ledger
    assert "CYCLE-BK-1" in client.get("/bookings").get_data(as_text=True)
    assert _money(BOOK_DUE) in client.get("/pending_bills").get_data(as_text=True)


def test_profit_loss_recognises_the_supplied_booking(cycle, client):
    period = f"?period=custom&start_date={cycle['today']}&end_date={cycle['today']}"
    body = client.get("/profit_reports" + period).get_data(as_text=True)
    assert _money(REVENUE) in body      # revenue at the booked rate
    assert _money(COGS) in body         # FIFO cost from the GRN
    assert _money(PROFIT) in body       # profit

    pdf = client.get("/profit_reports/export/pdf" + period)
    assert pdf.status_code == 200 and pdf.get_data()[:4] == b"%PDF"
    excel = client.get("/profit_reports/export/excel" + period)
    assert excel.status_code == 200 and excel.get_data()[:2] == b"PK"


def test_audit_trail_records_the_cycle(cycle, client, app):
    with app.app_context():
        actions = {row.action for row in AuditLog.query.all()}
    assert "http.post.misc.add_material" in actions
    assert "http.post.ops.grn" in actions
    assert "http.post.sales.add_booking" in actions
    assert "http.post.ops.add_record" in actions

    assert client.get("/settings/activity").status_code == 200
    assert client.get("/void_audit").status_code == 200
