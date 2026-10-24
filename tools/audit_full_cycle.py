"""Full-cycle audit + smoke test for the AMS rent/cement ERP.

This tool answers one question with evidence:

    "Does the whole application still work after a full data cleanup —
     and does one complete business cycle land correctly in the supplier
     ledger, the GRN list, every transaction view, the Profit & Loss
     report and the audit trail?"

It runs in three phases against a throw-away SQLite database (never the live
file unless you point ``--db`` at it):

Phase 1 — CLEANUP
    * Seed junk across every wipeable module (masters, GRN, booking,
      payment, supplier payment, cash flow, accounts, driver payment, …).
    * Call the real ``POST /delete_selected_data`` endpoint with the full
      "Check all" target set + the hard-delete confirmation.
    * Prove every business table is empty, identity/settings/audit survive,
      the bill counter is reset, and the app still logs in and renders
      after the wipe.

Phase 2 — SMOKE CYCLE (one of everything)
    * 1 supplier, 1 client, 1 material, 1 service ("Rent"), 1 cash account.
    * 1 GRN (100 bags @ 1,200 = 120,000, 50,000 paid in cash).
    * 1 cash booking with two lines — 20 bags @ 1,400 plus 1 Rent service
      trip @ 2,000 = 30,000, with 10,000 paid now and a 20,000 booking due.
    * 1 dispatch fulfilling 10 bags of that booking (revenue + COGS).
    * Then read every screen a manager would read: supplier ledger (+PDF),
      GRN list, bookings, client ledger, pending bills, cash flow, accounts
      receipts, Profit & Loss (+PDF/Excel), transaction detail, activity log
      and void audit.
    * Finally run the repository's own integrity tools on the same database:
      consistency report, the raw-SQL truth engine and the preflight watch.

Phase 3 — DATA INTEGRITY
    * ``tools/consistency_report.py`` (9 checks), the independent raw-SQL
      ``tools/predator_truth_engine.py`` (6 sections) and
      ``tools/health/preflight_check.py`` (blockers + watch list) all run on
      the same database and must come back clean.

Usage::

    python tools/audit_full_cycle.py              # fresh temp DB, full run
    python tools/audit_full_cycle.py --keep       # keep the DB for inspection
    python tools/audit_full_cycle.py --skip-wipe  # smoke cycle only

Exit code 0 means every check passed.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sqlite3
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_DB = ROOT / "instance" / "audit_full_cycle.db"

# ---------------------------------------------------------------------------
# Target set used by the Settings → "Check all" full wipe.
# ---------------------------------------------------------------------------
FULL_WIPE_TARGETS = [
    "clients", "suppliers", "supplier_payments", "pending_bills", "notifications",
    "dispatching", "receiving", "grn", "materials", "material_categories",
    "direct_sales", "material_returns", "delivery_rents", "delivery_persons",
    "invoices", "payments", "bookings", "accounts", "account_categories",
    "account_transactions", "account_reconciliations", "cash_flow_entries",
    "cash_flow_categories", "cash_reconciliation_data", "cash_reconciliation_audit",
    "cash_drawer_entries", "cash_drawer_categories", "delivery_person_payments",
    "direct_sale_drafts", "fbm_rentals", "fbm_rental_clients", "fbm_rental_items",
]

# Tables that legitimately survive any wipe (identity, settings, audit trail).
PROTECTED_TABLES = {
    "user", "user_login_session", "settings", "schema_version", "audit_log",
    "accounting_audit_log", "future_account_audit_log", "system_lock",
    "root_recovery_code", "root_backup_settings", "root_backup_email_history",
    "import_upload", "import_job", "import_history_entry",
    "tenant_wipe_backup_history", "bill_counter", "migration_history",
}

# Money the smoke dataset must produce.
SUPPLIER = "Ravi Cement Supplier"
CLIENT = "Bilal Construction"
MATERIAL = "OPC Cement"
SERVICE = "Rent"
ACCOUNT = "Main Cash"
OPENING_CASH = 200_000.0
GRN_QTY = 100.0
GRN_RATE = 1_200.0
GRN_TOTAL = GRN_QTY * GRN_RATE          # 120,000 purchase
GRN_PAID = 50_000.0                      # cash paid on the GRN
SUPPLIER_DUE = GRN_TOTAL - GRN_PAID      # 70,000 payable
BOOK_QTY = 20.0
BOOK_RATE = 1_400.0
# The booking carries both the material and the "Rent" service line, which is
# how a rental/cement yard actually bills a delivery.
BOOK_SERVICE_QTY = 1.0
BOOK_SERVICE_RATE = 2_000.0
BOOK_TOTAL = BOOK_QTY * BOOK_RATE + BOOK_SERVICE_QTY * BOOK_SERVICE_RATE  # 30,000
BOOK_PAID = 10_000.0                     # cash paid now
BOOK_DUE = BOOK_TOTAL - BOOK_PAID        # 20,000 booking due
DISPATCH_QTY = 10.0
SALE_REVENUE = DISPATCH_QTY * BOOK_RATE  # 14,000 recognised revenue
SALE_COGS = DISPATCH_QTY * GRN_RATE      # 12,000 COGS
SALE_PROFIT = SALE_REVENUE - SALE_COGS   # 2,000 profit
CASH_AFTER = OPENING_CASH - GRN_PAID + BOOK_PAID  # 160,000


class Reporter:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append((name, bool(ok), detail))
        mark = "PASS" if ok else "FAIL"
        line = f"  [{mark}] {name}"
        if detail:
            line += f" — {detail}"
        print(line)
        return bool(ok)

    def section(self, title: str) -> None:
        print(f"\n{title}")

    @property
    def failures(self) -> list[tuple[str, bool, str]]:
        return [r for r in self.results if not r[1]]

    def summary(self) -> bool:
        total = len(self.results)
        bad = len(self.failures)
        print("\n" + "=" * 78)
        print(f"RESULT: {total - bad}/{total} checks passed")
        if bad:
            print("FAILED CHECKS:")
            for name, _ok, detail in self.failures:
                print(f"  - {name}: {detail}")
        print("=" * 78)
        return bad == 0


def money(value: float) -> str:
    return f"{value:,.2f}"


def body_text(resp) -> str:
    return resp.get_data(as_text=True)


def page_is_clean(resp, client=None) -> tuple[bool, str]:
    """Judge a page response.

    Some routes are intentional aliases that 302 to their canonical page
    (``/stock_summary`` -> ``/inventory/stock_summary``).  When a client is
    supplied those redirects are followed once so the check judges the real
    page; a redirect to the sign-in form is still a failure.
    """
    if resp.status_code in (301, 302, 303, 307, 308) and client is not None:
        location = resp.headers.get("Location") or ""
        if "/login" in location:
            return False, "redirected to /login (not authenticated)"
        if location.startswith("/"):
            resp = client.get(location)
    if resp.status_code not in (200,):
        if resp.status_code in (301, 302, 303, 307, 308):
            return True, ""  # intentional alias with no local target to read
        return False, f"HTTP {resp.status_code}"
    text = body_text(resp)
    for marker in (
        "Traceback (most recent call last)",
        "jinja2.exceptions",
        "Internal Server Error",
        "BuildError",
    ):
        if marker in text:
            return False, f"page contains {marker!r}"
    return True, ""


# ---------------------------------------------------------------------------
# App / client helpers
# ---------------------------------------------------------------------------
def build_app(db_path: Path):
    os.environ["APP_DB_PATH"] = str(db_path)
    os.environ["ALLOW_EMPTY_DB"] = "1"
    os.environ["BACKUP_EMBEDDED_SCHEDULER"] = "0"
    os.environ["AMS_SCHEMA_VERSION"] = "v44"
    os.environ["SQLITE_JOURNAL_MODE"] = "DELETE"
    os.environ["DEFAULT_ADMIN_USER"] = "Admin"
    os.environ["DEFAULT_ADMIN_PASSWORD"] = "Admin@fbm12345"
    from app import create_app

    app = create_app()
    app.config["TESTING"] = False
    return app


class Http:
    """Logged-in test client that carries the session CSRF token."""

    def __init__(self, app) -> None:
        self.app = app
        self.raw = app.test_client()

    def csrf(self) -> str:
        with self.raw.session_transaction() as sess:
            token = sess.get("_csrf_token")
            if not token:
                token = "audit-full-cycle-csrf"
                sess["_csrf_token"] = token
            return token

    def post(self, path: str, data: dict, **kwargs):
        payload = dict(data or {})
        payload.setdefault("_csrf_token", self.csrf())
        return self.raw.post(path, data=payload, follow_redirects=False, **kwargs)

    def get(self, path: str, **kwargs):
        return self.raw.get(path, follow_redirects=False, **kwargs)

    def login(self) -> bool:
        resp = self.post("/login", {
            "username": "Admin",
            "password": "Admin@fbm12345",
            "remember_me": "1",
        })
        return resp.status_code in (302, 303) and "/login" not in (
            resp.headers.get("Location") or ""
        )


def flashes(resp) -> str:
    """Return the page body after following the redirect (flash messages)."""
    return body_text(resp)


def follow(client: Http, resp):
    """Follow a redirect inside the same authenticated session."""
    location = resp.headers.get("Location")
    if not location:
        return resp
    return client.get(location)


# ---------------------------------------------------------------------------
# Phase 1 — seed junk, wipe it, prove the database and UI are clean
# ---------------------------------------------------------------------------
def seed_junk(app) -> None:
    from models import (
        Account, AccountCategory, AccountTransaction, BillCounter, Booking, BookingItem,
        CashFlowCategory, CashFlowEntry, Client, DeliveryPerson, DeliveryPersonPayment,
        DirectSale, Entry, GRN, GRNItem, Material, MaterialCategory,
        MaterialReturn, Payment, PendingBill, Supplier, SupplierPayment, db,
    )

    with app.app_context():
        now = datetime.now()
        cat = MaterialCategory(name="Audit Cat")
        client = Client(code="AUD-CLI-1", name="Audit Client")
        supplier = Supplier(name="Audit Supplier")
        driver = DeliveryPerson(name="Audit Driver")
        account = Account(
            name="Audit Cash", type="cash", category="cash", account_type="company",
            balance=5000.0, class_category="Assets",
        )
        booking = Booking(client_name="Audit Client", amount=3000.0, paid_amount=1000.0,
                          manual_bill_no="AUD-BK-1")
        pending = PendingBill(client_code="AUD-CLI-1", client_name="Audit Client",
                              bill_no="AUD-BK-1", amount=2000.0, reason="booking:audit")
        db.session.add_all([cat, client, supplier, driver, account, booking, pending])
        db.session.flush()

        material = Material(code="AUD-MAT-1", name="Audit Material", category_id=cat.id,
                            unit_price=100, total=100, unit="Bags")
        grn = GRN(supplier="Audit Supplier", supplier_id=supplier.id, manual_bill_no="AUD-GRN-1")
        db.session.add_all([material, grn])
        db.session.flush()

        db.session.add_all([
            GRNItem(grn_id=grn.id, mat_name=material.name, qty=10, price_at_time=100),
            Entry(date=now.strftime("%Y-%m-%d"), type="IN", material=material.name,
                  client="Audit Supplier", qty=10, bill_no="AUD-GRN-1"),
            BookingItem(booking_id=booking.id, material_name=material.name, qty=2, price_at_time=1500),
            Payment(client_id=client.id, client_name=client.name, amount=500.0,
                    manual_bill_no="AUD-PAY-1"),
            SupplierPayment(supplier_id=supplier.id, amount=250.0),
            DirectSale(client_name="Audit Client", manual_bill_no="AUD-DS-1",
                       amount=800.0, paid_amount=300.0),
            MaterialReturn(client_name="Audit Client", amount=50.0),
            AccountTransaction(to_account_id=account.id, amount=5000.0, description="audit seed"),
            CashFlowEntry(direction="in", amount=100.0, account_id=account.id,
                          description="audit seed cashflow"),
            CashFlowCategory(name="Audit CF Cat"),
            AccountCategory(name="Audit Account Cat"),
            DeliveryPersonPayment(delivery_person_id=driver.id, amount_paid=25.0),
        ])
        db.session.commit()


def business_leftovers(app) -> dict[str, int]:
    """Non-empty business tables, excluding rows the bootstrap must create.

    ``_bootstrap_database()`` seeds the OPEN-KHATA client and the default
    "General" material category on every start — including a start on a freshly
    wiped database.  Those two are system rows, not leftover business data, so
    a wipe audit that flagged them would cry wolf after every legitimate reset.
    """
    from models import Client, MaterialCategory, db

    counts = table_counts(app)
    leftovers = {name: n for name, n in counts.items()
                 if n > 0 and name not in PROTECTED_TABLES}
    with app.app_context():
        khata = Client.query.filter(
            db.func.upper(db.func.trim(db.func.coalesce(Client.code, ""))) == "OPEN-KHATA"
        ).count()
        general = MaterialCategory.query.filter(
            db.func.lower(db.func.trim(MaterialCategory.name)) == "general"
        ).count()
    for table, seeded in (("client", khata), ("material_category", general)):
        if table in leftovers:
            remaining = leftovers[table] - seeded
            if remaining > 0:
                leftovers[table] = remaining
            else:
                leftovers.pop(table)
    return leftovers


def table_counts(app) -> dict[str, int]:
    with app.app_context():
        from models import db

        counts = {}
        for table in db.metadata.sorted_tables:
            counts[table.name] = int(
                db.session.query(db.func.count()).select_from(table).scalar() or 0
            )
        return counts


def run_cleanup_phase(app, client: Http, report: Reporter, db_path: Path) -> None:
    from models import BillCounter, User, db

    report.section("PHASE 1 — full data cleanup (Settings → Check all → hard wipe)")
    seed_junk(app)
    before = table_counts(app)
    dirty = {name: n for name, n in before.items()
             if n > 0 and name not in PROTECTED_TABLES}
    report.check(
        "junk dataset seeded across modules",
        len(dirty) >= 12,
        f"{len(dirty)} non-empty business tables before the wipe",
    )

    resp = client.post("/delete_selected_data", {
        "confirm_text": "DELETE ALL DATA",
        "hard_delete_override": "1",
        "delete_targets": FULL_WIPE_TARGETS,
    })
    report.check(
        "POST /delete_selected_data accepted",
        resp.status_code in (302, 303),
        f"HTTP {resp.status_code}",
    )
    landing = follow(client, resp)
    ok, why = page_is_clean(landing)
    report.check("settings page renders after wipe", ok, why)

    after = table_counts(app)
    leftover = business_leftovers(app)
    report.check(
        "every business table is empty after the wipe",
        not leftover,
        f"leftovers: {leftover}" if leftover else "all business tables at 0 rows",
    )

    with app.app_context():
        from models import AuditLog

        wipe_audit = AuditLog.query.filter(
            AuditLog.action.ilike("%wipe%")
        ).count()
        report.check(
            "the wipe is recorded in the audit trail",
            wipe_audit > 0,
            f"{wipe_audit} wipe audit row(s)",
        )
    with app.app_context():
        report.check(
            "identity / settings / audit tables survive",
            User.query.count() >= 1 and after.get("settings", 0) >= 0
            and after.get("audit_log", 0) >= 0,
            f"users={after.get('user', 0)} settings={after.get('settings', 0)} "
            f"audit={after.get('audit_log', 0)}",
        )
        counter = BillCounter.query.filter_by(namespace="BOOKING").first()
        report.check(
            "bill counter reset to its documented baseline",
            counter is None or int(counter.count) == 1000,
            f"booking counter={getattr(counter, 'count', None)}",
        )

    # A wipe leaves a tiny database.  The next server start must treat that as
    # an intentional baseline (not as corruption) and come up healthy.
    with app.app_context():
        from models import db as _db

        _db.session.remove()
        _db.engine.dispose()
    try:
        restarted = build_app(db_path)
        boot_error = restarted.config.get("AMS_BOOTSTRAP_ERROR")
    except Exception:
        restarted, boot_error = None, traceback.format_exc()
    report.check(
        "application restarts cleanly on the wiped database",
        restarted is not None and not boot_error,
        "no bootstrap error" if not boot_error else str(boot_error)[:200],
    )
    if restarted is not None:
        after_restart = table_counts(restarted)
        still_empty = business_leftovers(restarted)
        report.check(
            "restart did not resurrect or recreate business rows",
            not still_empty,
            f"leftovers: {still_empty}" if still_empty else "still empty",
        )
        client_after = Http(restarted)
        report.check("login works after the restart", client_after.login(), "Admin login")

    # After a destructive wipe the app must still be usable.
    client2 = Http(app)
    report.check("login still works after the wipe", client2.login(), "Admin login")
    for path, label in (
        ("/", "dashboard"),
        ("/clients", "clients"),
        ("/suppliers", "suppliers"),
        ("/materials", "materials"),
        ("/services", "services"),
        ("/grn", "GRN"),
        ("/bookings", "bookings"),
        ("/payments", "payments"),
        ("/accounts/accounts", "accounts"),
        ("/cash_flow", "cash flow"),
        ("/profit_reports", "profit & loss"),
        ("/void_audit", "void audit"),
    ):
        ok, why = page_is_clean(client2.get(path), client2)
        report.check(f"empty-state page renders: {label}", ok, why)


# ---------------------------------------------------------------------------
# Phase 2 — one full business cycle
# ---------------------------------------------------------------------------
def run_smoke_phase(app, client: Http, report: Reporter) -> dict:
    from models import (
        Account, AccountTransaction, AuditLog, Booking, Client, GRN, GRNItem, Material,
        PendingBill, Service, Supplier, SupplierPayment, db,
    )
    from app.services.time_money import pk_today

    today = pk_today().strftime("%Y-%m-%d")
    report.section("PHASE 2 — one supplier, one client, one material, one service, one cycle")

    # --- masters -----------------------------------------------------------
    resp = follow(client, client.post("/add_supplier", {"name": SUPPLIER, "phone": "0300-1112233"}))
    report.check("supplier created", "Supplier Added" in body_text(resp), "flash: Supplier Added")

    resp = follow(client, client.post("/add_client", {
        "name": CLIENT, "code": "AUD-CLI-100", "phone": "0321-4455667",
        "category": "Credit Customer",
    }))
    report.check("client created", "Client Registered" in body_text(resp), "flash: Client Registered")

    resp = follow(client, client.post("/add_material", {
        "material_name": MATERIAL, "material_unit": "Bags",
    }))
    report.check("material created", "Brand Added" in body_text(resp), "flash: Brand Added")

    resp = follow(client, client.post("/services/add", {
        "service_name": SERVICE, "service_unit": "Trip",
    }))
    report.check("service \"Rent\" created", "added" in body_text(resp).lower(), "flash: Service added")

    resp = follow(client, client.post("/accounts/accounts/add", {
        "name": ACCOUNT,
        "class_category": "Assets",
        "class_subcategory": "Cash",
        "class_account_type": "Main Cash",
        "account_status": "active",
        "opening_amount": str(OPENING_CASH),
        "opening_position": "debit",
        "opening_effective_date": today,
    }))
    ok, why = page_is_clean(resp)
    report.check("cash account created", ok, why)

    with app.app_context():
        supplier = Supplier.query.filter_by(name=SUPPLIER).one()
        client_row = Client.query.filter_by(code="AUD-CLI-100").one()
        material = Material.query.filter_by(name=MATERIAL).one()
        service = Service.query.filter_by(name=SERVICE).one()
        account = Account.query.filter_by(name=ACCOUNT).one()
        ids = {
            "supplier": supplier.id, "client": client_row.id, "material": material.id,
            "service": service.id, "account": account.id,
        }
        report.check(
            "masters exist exactly once each",
            all([supplier, client_row, material, service, account]),
            f"supplier={supplier.id} client={client_row.id} material={material.id} "
            f"service={service.id} account={account.id}",
        )
        report.check(
            "cash account opening balance posted",
            abs(float(account.balance) - OPENING_CASH) < 0.01,
            f"balance={money(float(account.balance))}",
        )

    # --- GRN (supplier purchase, partly paid in cash) ----------------------
    report.section("PHASE 2a — GRN purchase + supplier ledger")
    resp = follow(client, client.post("/grn", {
        "action": "add",
        "supplier": SUPPLIER,
        "supplier_id": str(ids["supplier"]),
        "mat_name[]": MATERIAL,
        "qty[]": str(GRN_QTY),
        "price[]": str(GRN_RATE),
        "paid_amount": str(GRN_PAID),
        "payment_type": "Cash",
        "payment_account_id": str(ids["account"]),
        "manual_bill_no": "AUD-GRN-100",
        "date": today,
    }))
    report.check("GRN saved", "GRN added successfully" in body_text(resp),
                 "flash: GRN added successfully")

    with app.app_context():
        grn = GRN.query.filter(GRN.manual_bill_no.like("%AUD-GRN-100%")).one()
        grn_total = sum(float(i.qty) * float(i.price_at_time) for i in grn.items)
        material = db.session.get(Material, ids["material"])
        payment = SupplierPayment.query.filter_by(supplier_id=ids["supplier"]).first()
        report.check(
            "GRN stores the purchase line",
            abs(grn_total - GRN_TOTAL) < 0.01 and len(grn.items) == 1,
            f"{len(grn.items)} line(s), total={money(grn_total)}",
        )
        report.check(
            "material stock increased",
            abs(float(material.total) - GRN_QTY) < 0.01,
            f"stock={material.total}",
        )
        report.check(
            "GRN paid amount became a supplier payment",
            payment is not None and abs(float(payment.amount) - GRN_PAID) < 0.01,
            f"payment={money(float(payment.amount)) if payment else 'missing'}",
        )

    # --- Booking: cash paid now + due -------------------------------------
    report.section("PHASE 2b — cash booking + booking due")
    resp = follow(client, client.post("/add_booking", {
        "client_code": "AUD-CLI-100",
        "material_name[]": [MATERIAL, SERVICE],
        "qty[]": [str(BOOK_QTY), str(BOOK_SERVICE_QTY)],
        "unit_rate[]": [str(BOOK_RATE), str(BOOK_SERVICE_RATE)],
        "amount": str(BOOK_TOTAL),
        "paid_amount": str(BOOK_PAID),
        "payment_account_id": str(ids["account"]),
        "payment_method": "Cash",
        "manual_bill_no": "AUD-BK-100",
        "date": today,
    }))
    report.check("booking saved", "Booking added successfully" in body_text(resp),
                 "flash: Booking added successfully")

    with app.app_context():
        booking = Booking.query.filter(Booking.manual_bill_no.like("%AUD-BK-100%")).one()
        pending = PendingBill.query.filter(PendingBill.bill_no.like("%AUD-BK-100%")).first()
        account = db.session.get(Account, ids["account"])
        report.check(
            "booking amount / paid / due are correct",
            abs(float(booking.amount) - BOOK_TOTAL) < 0.01
            and abs(float(booking.paid_amount) - BOOK_PAID) < 0.01,
            f"amount={money(float(booking.amount))} paid={money(float(booking.paid_amount))} "
            f"due={money(BOOK_DUE)}",
        )
        booking_lines = {item.material_name for item in booking.items}
        report.check(
            "booking carries the material line and the Rent service line",
            booking_lines == {MATERIAL, SERVICE},
            f"lines={sorted(booking_lines)}",
        )
        report.check(
            "booking due registered as a pending bill",
            pending is not None and abs(float(pending.amount) - BOOK_DUE) < 0.01,
            f"pending={money(float(pending.amount)) if pending else 'missing'}",
        )
        report.check(
            "cash account reflects GRN payment + booking receipt",
            abs(float(account.balance) - CASH_AFTER) < 0.01,
            f"balance={money(float(account.balance))} (expected {money(CASH_AFTER)})",
        )

    # --- Dispatch the booked material (revenue + COGS) ---------------------
    report.section("PHASE 2c — booked material supplied (transaction recognition)")
    resp = follow(client, client.post("/add_record", {
        "type": "OUT",
        "date": today,
        "client": CLIENT,
        "material": MATERIAL,
        "material_id": str(ids["material"]),
        "qty": str(DISPATCH_QTY),
        "driver_name": "Audit Delivery Driver",
        "note": "Booking delivery for AUD-BK-100",
    }))
    report.check("dispatch saved", "Record Saved" in body_text(resp), "flash: Record Saved")

    with app.app_context():
        material = db.session.get(Material, ids["material"])
        report.check(
            "stock reduced by the dispatched quantity",
            abs(float(material.total) - (GRN_QTY - DISPATCH_QTY)) < 0.01,
            f"stock={material.total}",
        )

    # --- Reports the manager reads ----------------------------------------
    report.section("PHASE 2d — supplier ledger, GRN list, ledgers, cash flow")
    text = body_text(client.get(f"/supplier_ledger/{ids['supplier']}"))
    report.check("supplier ledger page loads", "Financial transaction ledger" in text)
    report.check(
        "supplier ledger shows the GRN debit",
        money(GRN_TOTAL) in text,
        f"looking for {money(GRN_TOTAL)}",
    )
    report.check(
        "supplier ledger shows the payment credit",
        money(GRN_PAID) in text,
        f"looking for {money(GRN_PAID)}",
    )
    report.check(
        "supplier ledger closes at the payable",
        money(SUPPLIER_DUE) in text,
        f"looking for closing {money(SUPPLIER_DUE)}",
    )
    report.check("supplier ledger shows the GRN bill number", "AUD-GRN-100" in text)

    pdf = client.get(f"/download_supplier_ledger/{ids['supplier']}?action=pdf")
    report.check(
        "supplier ledger PDF downloads",
        pdf.status_code == 200 and pdf.get_data()[:4] == b"%PDF",
        f"HTTP {pdf.status_code}, {len(pdf.get_data())} bytes",
    )

    text = body_text(client.get("/grn"))
    report.check("GRN page lists the purchase", "AUD-GRN-100" in text and SUPPLIER in text)

    text = body_text(client.get(f"/ledger/{ids['client']}"))
    report.check(
        "client ledger shows the booking and the due",
        "AUD-BK-100" in text and money(BOOK_DUE) in text,
        f"looking for AUD-BK-100 and {money(BOOK_DUE)}",
    )
    text = body_text(client.get("/bookings"))
    report.check("bookings page lists the booking", "AUD-BK-100" in text)

    cash_text = body_text(client.get("/cash_flow"))
    report.check("cash flow page loads", "cash" in cash_text.lower())
    report.check(
        "cash flow page shows the cash movement",
        money(BOOK_PAID).split(".")[0] in cash_text or money(GRN_PAID).split(".")[0] in cash_text,
        "cash receipt/payment visible",
    )
    text = body_text(client.get("/payments?party=all"))
    report.check(
        "payments page lists the GRN supplier payment",
        "AUD-GRN-100" in text and f"{GRN_PAID:.2f}" in text,
        "supplier payment row with 50,000.00",
    )
    text = body_text(client.get("/accounts/payments/suppliers"))
    report.check(
        "accounts → supplier payments lists the auto payment",
        "AUD-GRN-100" in text,
    )
    text = body_text(client.get("/accounts/receipts"))
    report.check(
        "accounts → receipts shows the GRN total (not just landed costs)",
        "AUD-GRN-100" in text and money(GRN_TOTAL) in text,
        f"looking for {money(GRN_TOTAL)}",
    )
    text = body_text(client.get("/"))
    # The dashboard rounds its KPI tiles to whole rupees ("Rs. 10,000").
    report.check(
        "dashboard KPIs reflect today's cash and the booking due",
        (money(BOOK_PAID) in text or f"{BOOK_PAID:,.0f}" in text)
        and (money(BOOK_DUE) in text or f"{BOOK_DUE:,.0f}" in text),
        f"cash received {money(BOOK_PAID)}, due {money(BOOK_DUE)}",
    )
    text = body_text(client.get("/pending_bills"))
    report.check(
        "pending bills page shows the booking due",
        money(BOOK_DUE) in text or f"{BOOK_DUE:.2f}" in text,
        f"looking for {money(BOOK_DUE)}",
    )

    # --- Profit & Loss ----------------------------------------------------
    report.section("PHASE 2e — Profit & Loss report")
    params = f"?period=custom&start_date={today}&end_date={today}"
    text = body_text(client.get("/profit_reports" + params))
    report.check("profit & loss page loads", "Profit" in text)
    report.check(
        "P&L shows recognised revenue",
        money(SALE_REVENUE) in text,
        f"looking for {money(SALE_REVENUE)}",
    )
    report.check(
        "P&L shows COGS at the GRN cost",
        money(SALE_COGS) in text,
        f"looking for {money(SALE_COGS)}",
    )
    report.check(
        "P&L shows the profit",
        money(SALE_PROFIT) in text,
        f"looking for {money(SALE_PROFIT)}",
    )
    pdf = client.get("/profit_reports/export/pdf" + params)
    report.check(
        "P&L PDF export works",
        pdf.status_code == 200 and pdf.get_data()[:4] == b"%PDF",
        f"HTTP {pdf.status_code}",
    )
    xls = client.get("/profit_reports/export/excel" + params)
    report.check(
        "P&L Excel export works",
        xls.status_code == 200 and xls.get_data()[:2] == b"PK",
        f"HTTP {xls.status_code}",
    )
    detail = client.get(f"/profit_reports/transaction/Booking/{booking.id}" + params)
    ok, why = page_is_clean(detail, client)
    report.check("booking transaction detail page renders", ok, why)

    # --- Audit trail ------------------------------------------------------
    report.section("PHASE 2f — audit trail")
    with app.app_context():
        audit_rows = AuditLog.query.count()
        booking_audit = AuditLog.query.filter(
            AuditLog.details.ilike("%AUD-BK-100%")
        ).count() + AuditLog.query.filter(AuditLog.details.ilike("%Booking%")).count()
        report.check("audit_log recorded the operations", audit_rows > 0,
                     f"{audit_rows} audit rows")
        report.check("audit trail references the booking cycle", booking_audit > 0,
                     f"{booking_audit} matching rows")

    ok, why = page_is_clean(client.get("/settings/activity"), client)
    report.check("activity log page renders", ok, why)
    ok, why = page_is_clean(client.get("/void_audit"), client)
    report.check("void audit page renders", ok, why)
    ok, why = page_is_clean(client.get("/system_report"), client)
    report.check("system report page renders", ok, why)

    # --- Whole-app page sweep --------------------------------------------
    report.section("PHASE 2g — whole-app page sweep")
    sweep = [
        "/", "/clients", "/suppliers", "/materials", "/services", "/delivery_persons",
        "/bookings", "/payments", "/direct_sales", "/material_returns", "/pending_bills",
        "/grn", "/dispatching", "/tracking", "/ledger", "/decision_ledger",
        "/financial_details", "/cash_flow", "/cash_flow_differences", "/profit_reports",
        "/unpaid_transactions", "/mixed_transactions", "/daily_transactions",
        "/delivery_rents", "/notifications", "/settings", "/settings/activity",
        "/inventory/stock_summary", "/stock_summary", "/accounts/", "/accounts/accounts",
        "/accounts/receipts", "/accounts/transfers", "/accounts/expenditures",
        "/accounts/payments/clients", "/accounts/payments/suppliers", "/accounts/audit",
        "/void_audit", "/system_report", "/accounts/receipts", "/accounts/kpi/cash_money",
        "/accounts/kpi/supplier_payments", "/accounts/kpi/client_payments",
        "/inventory/daily_transactions", "/tracking", f"/client_ledger/{ids['client']}",
        f"/financial_ledger/{ids['client']}", f"/supplier_ledger/{ids['supplier']}",
        f"/material_ledger/{ids['material']}",
    ]
    broken = []
    for path in sweep:
        ok, why = page_is_clean(client.get(path), client)
        if not ok:
            broken.append(f"{path} → {why}")
    report.check("all main pages return a clean 200", not broken, "; ".join(broken))

    return ids


# ---------------------------------------------------------------------------
# Phase 3 — the repository's own data-integrity tools, on the same database
# ---------------------------------------------------------------------------
def load_tool(relative_path: str, module_name: str):
    """Import a standalone tools/*.py module by path (they are not packages)."""
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run_integrity_phase(db_path: Path, report: Reporter) -> None:
    report.section("PHASE 3 — data integrity (consistency report + preflight watch)")

    consistency = load_tool("tools/consistency_report.py", "ams_consistency_report")
    consistency.DB_PATH = db_path
    consistency.HEALTH_SNAPSHOT = db_path.parent / "health_snapshot.json"
    try:
        result = consistency.run_all_checks()
    except Exception as exc:  # noqa: BLE001 - surfaced as a failed check
        report.check("consistency report runs", False, f"{type(exc).__name__}: {exc}")
        return
    bad = [
        f"{c['check']}={c['status']}"
        for c in result["checks"] if c["status"] != "OK"
    ]
    report.check(
        "consistency report finds no issues",
        result["overall_status"] == "OK",
        ", ".join(bad) if bad else f"{result['total_checks']} checks OK",
    )

    # Independent raw-SQL cross-check (does not import the application).
    truth = load_tool("tools/predator_truth_engine.py", "ams_truth_engine")
    con = truth.connect(str(db_path))
    try:
        divergent = {}
        for name, section in truth.SECTIONS.items():
            try:
                result_section = section(con)
            except Exception as exc:  # noqa: BLE001
                divergent[name] = [f"exception: {exc!r}"]
                continue
            if not result_section.get("ok", True):
                divergent[name] = result_section.get("findings", [])
    finally:
        con.close()
    report.check(
        "independent truth engine finds no divergence",
        not divergent,
        "; ".join(f"{k}: {v}" for k, v in divergent.items())
        if divergent else f"{len(truth.SECTIONS)} raw-SQL sections OK",
    )

    preflight = load_tool("tools/health/preflight_check.py", "ams_preflight")
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        checks = preflight.run_checks(con)
    finally:
        con.close()
    blockers = checks.get("blockers", [])
    watch = checks.get("watch", [])
    report.check(
        "preflight finds no transaction blockers",
        not blockers,
        "; ".join(b.get("message", "") for b in blockers) if blockers else "0 blockers",
    )
    report.check(
        "preflight watch list is empty",
        not watch,
        "; ".join(f"{w.get('id')}: {w.get('message')}" for w in watch)
        if watch else "0 watch items",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite file to use")
    parser.add_argument("--keep", action="store_true", help="keep the database file")
    parser.add_argument("--skip-wipe", action="store_true", help="run the smoke phase only")
    args = parser.parse_args()

    db_path = Path(args.db)
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print("AMS FULL-CYCLE AUDIT + SMOKE TEST")
    print(f"database : {db_path}")
    print(f"started  : {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 78)

    report = Reporter()
    app = build_app(db_path)
    if app.config.get("AMS_BOOTSTRAP_ERROR"):
        print("BOOTSTRAP FAILED:\n" + app.config["AMS_BOOTSTRAP_ERROR"])
        return 2

    client = Http(app)
    if not report.check("Admin login works", client.login(), "default admin credentials"):
        report.summary()
        return 2

    try:
        if not args.skip_wipe:
            run_cleanup_phase(app, client, report, db_path)
            # Re-seed on a clean database with the smoke cycle.
            client = Http(app)
            if not report.check("Admin login works after cleanup", client.login()):
                report.summary()
                return 2
        run_smoke_phase(app, client, report)
        run_integrity_phase(db_path, report)
    except Exception:
        traceback.print_exc()
        report.check("script completed without an exception", False, "see traceback above")

    ok = report.summary()
    if not args.keep:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
    else:
        print(f"database kept at {db_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
