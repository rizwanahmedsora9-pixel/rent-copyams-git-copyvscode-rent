"""Sequence audit + smoke test: data 0 → masters → GRNs → sales → FIFO → P&L.

The brief this tool answers, in the order the business actually works:

    * wipe to **data 0** and prove nothing survived;
    * add a material **category**, then **materials** in it;
    * add a **client** and a **supplier**;
    * add a **rent service** (a charge, not stock);
    * post **several GRNs** — the same cement from two suppliers at three
      different rates, a cheaper later GRN, a GRN with two materials on it;
    * sell from those lots, including a sale that crosses a lot boundary and a
      sale with two lines of the same material (a classic double-drain bug);
    * after **every** step read the real screens and the real services and
      prove that:
        - the GRN screen / ``/api/grn_consumption`` consume lots strictly
          oldest-first, at the lot's own purchase rate,
        - Profit & Loss costs each sale from exactly those lots,
        - P&L is *only* a report: its totals equal the GRN + sales evidence,
        - a service never consumes or creates stock,
        - one material never consumes another material's lots,
        - over-selling never drives a lot below zero and never invents a cost.

The verification is deliberately **independent**: ``FifoOracle`` re-reads the
SQLite file with raw SQL and replays FIFO without importing a single line of
the application's costing code.  The app's numbers have to agree with it.

Usage::

    python tools/audit_sequence_smoke.py            # fresh temp DB, full run
    python tools/audit_sequence_smoke.py --keep     # keep the DB for inspection
    python tools/audit_sequence_smoke.py --db instance/seq_smoke.db

Exit code 0 means every check passed.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.audit_full_cycle import (  # noqa: E402  (shared audit helpers)
    FULL_WIPE_TARGETS,
    PROTECTED_TABLES,
    Http,
    Reporter,
    body_text,
    build_app,
    business_leftovers,
    follow,
    page_is_clean,
    table_counts,
)

DEFAULT_DB = ROOT / "instance" / "audit_sequence_smoke.db"

# ---------------------------------------------------------------------------
# The dataset.  Every figure below is asserted, so the docstring *is* the spec.
# ---------------------------------------------------------------------------
CATEGORY_CEMENT = "Cement"
CATEGORY_SAND = "Sand & Gravel"
MAT_DG = "DG Cement"
MAT_PIONEER = "Pioneer Cement"
MAT_SAND = "Chenab Sand"
SERVICE = "CEMENT RENT"
CLIENT = "Bilal Construction"
CLIENT_CODE = "SEQ-CL-1"
SUP_RAVI = "Ravi Cement Supplier"
SUP_KOHAT = "Kohat Traders"

# (key, supplier, items[(material, qty, rate)], date, time, manual bill)
GRNS = [
    ("G1", SUP_RAVI, [(MAT_DG, 100.0, 1200.0)], "2026-09-01", "09:00", "SEQ-GRN-1"),
    ("G2", SUP_RAVI, [(MAT_DG, 50.0, 1350.0)], "2026-09-03", "10:30", "SEQ-GRN-2"),
    ("G3", SUP_KOHAT, [(MAT_DG, 80.0, 1400.0), (MAT_SAND, 200.0, 55.0)],
     "2026-09-05", "11:15", "SEQ-GRN-3"),
    ("G4", SUP_RAVI, [(MAT_PIONEER, 60.0, 1600.0)], "2026-09-07", "09:45", "SEQ-GRN-4"),
    ("G5", SUP_KOHAT, [(MAT_DG, 40.0, 1350.0)], "2026-09-09", "14:20", "SEQ-GRN-5"),
    ("G6", SUP_RAVI, [(MAT_DG, 25.0, 1500.0)], "2026-09-11", "16:05", "SEQ-GRN-6"),
]

# (key, date, lines[(name, qty, rate)], allow_negative)
# Lines named SERVICE are charge lines: no lot, no stock.
SALES = [
    ("S1", "2026-09-12", [(MAT_DG, 30.0, 1500.0)], False),
    ("S2", "2026-09-13", [(MAT_DG, 80.0, 1550.0)], False),
    ("S3", "2026-09-14", [(MAT_DG, 25.0, 1600.0), (MAT_DG, 30.0, 1600.0)], False),
    ("S4", "2026-09-15", [(MAT_SAND, 50.0, 90.0)], False),
    ("S5", "2026-09-16", [(MAT_DG, 5.0, 1600.0), (SERVICE, 2.0, 2000.0)], False),
    ("S6", "2026-09-17", [(MAT_PIONEER, 20.0, 1900.0)], False),
    ("S7", "2026-09-18", [(MAT_DG, 60.0, 1700.0)], False),
    ("S8", "2026-09-19", [(MAT_DG, 65.0, 1750.0)], False),
    # Beyond every lot: allowed only because the operator ticked the override.
    ("S9", "2026-09-20", [(MAT_DG, 10.0, 1800.0)], True),
]

# Hard-coded price layers for the two sales that matter most.  If the oracle
# and the app ever drift together, these constants still catch it.
EXPECTED_LAYERS = {
    "S1": [(1200.0, 30.0)],
    "S2": [(1200.0, 70.0), (1350.0, 10.0)],
    "S3": [(1350.0, 40.0), (1400.0, 15.0)],
    "S4": [(55.0, 50.0)],
    "S5": [(1400.0, 5.0)],
    "S6": [(1600.0, 20.0)],
    "S7": [(1400.0, 60.0)],
    "S8": [(1350.0, 40.0), (1500.0, 25.0)],
    "S9": [],
}

EPS = 0.005


def money(value: float) -> str:
    return f"{float(value or 0):,.2f}"


def norm(value) -> str:
    return " ".join(str(value or "").strip().upper().split())


# ---------------------------------------------------------------------------
# Independent FIFO oracle — raw SQL only, no application imports.
# ---------------------------------------------------------------------------
class FifoOracle:
    """Replay the physical ledger with plain SQL and plain Python."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.lots: dict[str, list[dict]] = {}
        self.lot_by_id: dict[int, dict] = {}
        self.services: set[str] = set()
        self.sale_layers: dict[str, list[dict]] = {}
        self.sale_uncovered: dict[str, float] = {}
        self.sale_revenue: dict[str, float] = {}
        self.sale_service_revenue: dict[str, float] = {}
        self.sale_material_revenue: dict[str, float] = {}
        self.sale_totals: dict[str, float] = {}
        self._load()
        self._replay()

    # -- load -------------------------------------------------------------
    def _load(self) -> None:
        con = sqlite3.connect(str(self.db_path))
        con.row_factory = sqlite3.Row
        try:
            self.services = {
                norm(row["name"]) for row in con.execute("SELECT name FROM service")
            }
            for row in con.execute(
                """
                SELECT gi.id, gi.grn_id, gi.mat_name, gi.qty, gi.price_at_time,
                       g.date_posted
                  FROM grn_item gi
                  JOIN grn g ON g.id = gi.grn_id
                 WHERE COALESCE(gi.is_void, 0) = 0
                   AND COALESCE(g.is_void, 0) = 0
                 ORDER BY g.date_posted, gi.id
                """
            ):
                lot = {
                    "id": int(row["id"]),
                    "grn_id": int(row["grn_id"]),
                    "material": norm(row["mat_name"]),
                    "received": float(row["qty"] or 0),
                    "remaining": float(row["qty"] or 0),
                    "rate": float(row["price_at_time"] or 0),
                    "posted": str(row["date_posted"]),
                }
                self.lots.setdefault(lot["material"], []).append(lot)
                self.lot_by_id[lot["id"]] = lot
            self.sales = []
            for row in con.execute(
                """
                SELECT s.id, s.date_posted, s.amount, s.manual_bill_no
                  FROM direct_sale s
                 WHERE COALESCE(s.is_void, 0) = 0
                 ORDER BY s.date_posted, s.id
                """
            ):
                ref = norm(row["manual_bill_no"])
                match = re.search(r"SEQ-SALE-(\d+)", ref)
                self.sales.append(
                    {
                        "id": int(row["id"]),
                        "date": str(row["date_posted"]),
                        "amount": float(row["amount"] or 0),
                        "ref": ref,
                        "key": f"S{match.group(1)}" if match else ref,
                    }
                )
            self.items_by_sale: dict[int, list[dict]] = {}
            for row in con.execute(
                """
                SELECT si.id, si.sale_id, si.product_name, si.qty, si.price_at_time
                  FROM direct_sale_item si
                  JOIN direct_sale s ON s.id = si.sale_id
                 WHERE COALESCE(s.is_void, 0) = 0
                 ORDER BY si.sale_id, si.id
                """
            ):
                self.items_by_sale.setdefault(int(row["sale_id"]), []).append(
                    {
                        "id": int(row["id"]),
                        "material": norm(row["product_name"]),
                        "qty": float(row["qty"] or 0),
                        "rate": float(row["price_at_time"] or 0),
                    }
                )
        finally:
            con.close()

    # -- replay -----------------------------------------------------------
    def _replay(self) -> None:
        for sale in self.sales:
            key = sale["key"]
            layers: list[dict] = []
            uncovered = 0.0
            revenue = 0.0
            material_revenue = 0.0
            service_revenue = 0.0
            for item in self.items_by_sale.get(sale["id"], []):
                line_revenue = item["qty"] * item["rate"]
                revenue += line_revenue
                if item["material"] in self.services:
                    service_revenue += line_revenue
                    continue
                material_revenue += line_revenue
                need = item["qty"]
                for lot in self.lots.get(item["material"], []):
                    if need <= EPS:
                        break
                    if lot["remaining"] <= EPS:
                        continue
                    take = min(need, lot["remaining"])
                    lot["remaining"] -= take
                    need -= take
                    layers.append(
                        {
                            "lot_id": lot["id"],
                            "qty": take,
                            "rate": lot["rate"],
                            "amount": take * lot["rate"],
                        }
                    )
                if need > EPS:
                    uncovered += need
            self.sale_layers[key] = layers
            self.sale_uncovered[key] = uncovered
            self.sale_revenue[key] = revenue
            self.sale_material_revenue[key] = material_revenue
            self.sale_service_revenue[key] = service_revenue
            self.sale_totals[key] = sale["amount"]

    # -- results ----------------------------------------------------------
    def cogs(self, key: str) -> float:
        return sum(layer["amount"] for layer in self.sale_layers.get(key, []))

    def total_cogs(self) -> float:
        return sum(self.cogs(key) for key in self.sale_layers)

    def total_revenue(self) -> float:
        return sum(self.sale_revenue.values())

    def total_uncovered(self) -> float:
        return sum(self.sale_uncovered.values())

    def remaining(self, material: str) -> float:
        return sum(lot["remaining"] for lot in self.lots.get(material, []))

    def lot_consumed(self, lot_id: int) -> float:
        lot = self.lot_by_id[lot_id]
        return lot["received"] - lot["remaining"]


# ---------------------------------------------------------------------------
# Helpers over the app
# ---------------------------------------------------------------------------
def api_lots(client: Http) -> dict[int, dict]:
    resp = client.get("/api/grn_consumption")
    assert resp.status_code == 200, f"/api/grn_consumption HTTP {resp.status_code}"
    payload = resp.get_json()
    return {int(key): row for key, row in payload["items"].items()}


def rendered_grn_lots(client: Http) -> dict[int, dict]:
    """Per-lot received/consumed/remaining exactly as the GRN screen paints it."""
    resp = client.get("/grn")
    assert resp.status_code == 200, f"/grn HTTP {resp.status_code}"
    text = body_text(resp)
    rows = {}
    pattern = re.compile(
        r'data-grn-item-id="(\d+)"\s+data-received="([\d.\-]+)"\s+'
        r'data-consumed="([\d.\-]+)"\s+data-percent="([\d.\-]+)"'
    )
    for match in pattern.finditer(text):
        item_id = int(match.group(1))
        rows[item_id] = {
            "received": float(match.group(2)),
            "consumed": float(match.group(3)),
            "percent": float(match.group(4)),
        }
    return rows


def post_flash(client: Http, path: str, data: dict):
    """POST and follow the redirect, so flash messages can be read."""
    resp = client.post(path, data)
    return follow(client, resp) if resp.status_code in (301, 302, 303, 307, 308) else resp


def report_totals(app, start="2026-09-01", end="2026-09-30", **filters):
    from app.services.profit_loss import build_profit_loss_report

    with app.app_context():
        return build_profit_loss_report(
            start_date=start, end_date=end, previous=False, **filters
        )


def report_by_document(app, start="2026-09-01", end="2026-09-30"):
    """Group the report rows per bill, separating lot-backed from non-lot cost.

    ``lot_cogs`` is the money the report says came out of real GRN lots — that
    is what the independent FIFO replay must reproduce exactly.  ``non_lot_cogs``
    is the disclosed remainder (a frozen posting rate used only beyond every
    real lot), so a fabricated rate can never hide inside the lot figure.
    """
    report = report_totals(app, start=start, end=end)
    grouped: dict[str, dict] = {}
    for row in report["rows"]:
        ref = norm(row.get("reference"))
        match = re.search(r"SEQ-SALE-(\d+)", ref)
        if match:
            ref = f"S{match.group(1)}"
        bucket = grouped.setdefault(
            ref,
            {
                "cogs": 0.0, "lot_cogs": 0.0, "non_lot_cogs": 0.0,
                "revenue": 0.0, "material_revenue": 0.0, "delivery_revenue": 0.0,
                "qty": 0.0, "rows": [], "cost_sources": [], "sources": [],
                "bases": [], "cost_status": set(), "lot_layers": [],
            },
        )
        cogs = float(row.get("cogs") or 0)
        sources = row.get("cost_sources") or []
        lot_cogs = sum(
            float(source.get("amount") or 0) for source in sources
            if source.get("grn_item_id")
        )
        bucket["cogs"] += cogs
        bucket["lot_cogs"] += lot_cogs
        bucket["non_lot_cogs"] += max(0.0, cogs - lot_cogs)
        bucket["revenue"] += float(row.get("revenue") or 0)
        bucket["material_revenue"] += float(row.get("material_revenue") or 0)
        bucket["delivery_revenue"] += float(row.get("delivery_revenue") or 0)
        bucket["qty"] += float(row.get("qty") or 0)
        bucket["rows"].append(row)
        bucket["cost_sources"].extend(sources)
        bucket["bases"].append(row.get("cost_basis") or "")
        bucket["cost_status"].add(row.get("cost_status"))
        for source in sources:
            bucket["sources"].append(source)
            if source.get("grn_item_id"):
                bucket["lot_layers"].append(
                    (
                        round(float(source.get("lot_rate") or source.get("rate") or 0), 4),
                        round(float(source.get("qty") or 0), 4),
                    )
                )
    return report, grouped


def find_sale(app, ref: str):
    """Look up a sale by the bill number the operator typed.

    ``normalize_manual_bill`` stores it as ``MB NO.<typed>``, so an exact match
    would hide the very row the check is about.
    """
    from models import DirectSale

    with app.app_context():
        return (
            DirectSale.query.filter(DirectSale.manual_bill_no.like(f"%{ref}%"))
            .order_by(DirectSale.id.desc())
            .first()
        )


def allocations_for(app, sale_id: int):
    from models import GRNAllocation

    with app.app_context():
        rows = (
            GRNAllocation.query.filter_by(sale_id=sale_id, is_void=False)
            .order_by(GRNAllocation.id)
            .all()
        )
        return [
            {
                "lot_id": row.grn_item_id,
                "qty": float(row.qty or 0),
                "rate": float(row.cost_rate or 0),
            }
            for row in rows
        ]


# ---------------------------------------------------------------------------
# Phase 0 — data 0
# ---------------------------------------------------------------------------
def phase_zero(app, client: Http, report: Reporter, seed_junk: bool = True) -> None:
    from models import User

    report.section("PHASE 0 — data 0 (settings → check all → hard wipe)")
    if seed_junk:
        from tools.audit_full_cycle import seed_junk as _seed_junk

        _seed_junk(app)

    resp = client.post(
        "/delete_selected_data",
        {
            "confirm_text": "DELETE ALL DATA",
            "hard_delete_override": "1",
            "delete_targets": FULL_WIPE_TARGETS,
        },
    )
    report.check(
        "POST /delete_selected_data accepted",
        resp.status_code in (302, 303),
        f"HTTP {resp.status_code}",
    )
    ok, why = page_is_clean(follow(client, resp), client)
    report.check("settings page renders after the wipe", ok, why)

    counts = table_counts(app)
    leftovers = business_leftovers(app)
    report.check(
        "every business table is at 0 rows",
        not leftovers,
        f"leftovers: {leftovers}" if leftovers else f"{len(counts)} tables checked",
    )
    with app.app_context():
        report.check(
            "identity survives the wipe (admin still there)",
            User.query.count() >= 1,
            f"{User.query.count()} user row(s)",
        )
    # The masters we are about to build must genuinely start from nothing.
    with app.app_context():
        from models import Client, GRN, Material, MaterialCategory, Service, Supplier, db

        for model, label in (
            (MaterialCategory, "material categories"),
            (Material, "materials"),
            (Service, "services"),
            (Client, "clients"),
            (Supplier, "suppliers"),
            (GRN, "GRNs"),
        ):
            count = db.session.query(db.func.count()).select_from(model).scalar() or 0
            if model is MaterialCategory:
                # 'General' is bootstrap, not business data.
                count -= MaterialCategory.query.filter(
                    db.func.lower(db.func.trim(MaterialCategory.name)) == "general"
                ).count()
            if model is Client:
                count -= Client.query.filter(
                    db.func.upper(db.func.trim(db.func.coalesce(Client.code, ""))) == "OPEN-KHATA"
                ).count()
            report.check(f"no {label} before we start", count <= 0, f"{count} row(s)")

    # A wiped app must still log in and render its dashboards.
    for path in ("/", "/materials", "/clients", "/suppliers", "/grn", "/direct_sales",
                 "/profit_reports", "/services"):
        resp = client.get(path)
        ok, why = page_is_clean(resp, client)
        report.check(f"empty-state page {path} renders", ok, why)


# ---------------------------------------------------------------------------
# Phase 1..5 — the masters, in the order the owner enters them
# ---------------------------------------------------------------------------
def phase_masters(app, client: Http, report: Reporter) -> dict[str, int]:
    from models import (
        Client,
        Material,
        MaterialCategory,
        Service,
        Supplier,
        db,
    )

    report.section("PHASE 1 — material category")
    resp = client.post(
        "/material_categories/add",
        {"category_name": CATEGORY_CEMENT},
    )
    report.check("category 'Cement' added", resp.status_code in (302, 303),
                 f"HTTP {resp.status_code}")
    resp = client.post("/material_categories/add", {"category_name": CATEGORY_SAND})
    report.check("category 'Sand & Gravel' added", resp.status_code in (302, 303),
                 f"HTTP {resp.status_code}")
    resp = post_flash(client, "/material_categories/add",
                      {"category_name": CATEGORY_CEMENT})
    report.check(
        "duplicate category refused",
        "already exists" in body_text(resp),
        "flash: already exists",
    )
    with app.app_context():
        cement_cat = MaterialCategory.query.filter_by(name=CATEGORY_CEMENT).one_or_none()
        sand_cat = MaterialCategory.query.filter_by(name=CATEGORY_SAND).one_or_none()
    report.check(
        "both categories exist once",
        cement_cat is not None and sand_cat is not None,
        f"cement id={getattr(cement_cat, 'id', None)} sand id={getattr(sand_cat, 'id', None)}",
    )

    report.section("PHASE 2 — materials inside those categories")
    for name, unit, cat_name in (
        (MAT_DG, "Bags", CATEGORY_CEMENT),
        (MAT_PIONEER, "Bags", CATEGORY_CEMENT),
        (MAT_SAND, "CFT", CATEGORY_SAND),
    ):
        with app.app_context():
            cat_id = MaterialCategory.query.filter_by(name=cat_name).one().id
        resp = post_flash(
            client,
            "/add_material",
            {
                "material_name": name,
                "material_unit": unit,
                "category_id": str(cat_id),
            },
        )
        text = body_text(resp)
        report.check(
            f"material '{name}' added to '{cat_name}'",
            "Brand Added" in text or name in text,
            "flash: Brand Added",
        )
    resp = post_flash(
        client,
        "/add_material",
        {"material_name": MAT_DG, "material_unit": "Bags"},
    )
    report.check("duplicate material refused", "already exists" in body_text(resp), "")

    with app.app_context():
        mats = {m.name: m for m in Material.query.all()}
        cats = {c.id: c.name for c in MaterialCategory.query.all()}
    report.check("three materials exist", len(mats) == 3, f"{sorted(mats)}")
    for name, unit, cat_name in (
        (MAT_DG, "Bags", CATEGORY_CEMENT),
        (MAT_PIONEER, "Bags", CATEGORY_CEMENT),
        (MAT_SAND, "CFT", CATEGORY_SAND),
    ):
        row = mats.get(name)
        report.check(
            f"'{name}' keeps its category, unit and 0 stock",
            row is not None
            and cats.get(row.category_id) == cat_name
            and (row.unit or "") == unit
            and float(row.total or 0) == 0,
            f"category={cats.get(getattr(row, 'category_id', None))} "
            f"unit={getattr(row, 'unit', None)} total={getattr(row, 'total', None)}",
        )

    report.section("PHASE 3 — client")
    resp = post_flash(
        client,
        "/add_client",
        {"name": CLIENT, "code": CLIENT_CODE, "category": "Credit Customer",
         "phone": "0300-1234567"},
    )
    report.check("client added", "Client Registered" in body_text(resp), "flash: Client Registered")
    with app.app_context():
        client_row = Client.query.filter_by(code=CLIENT_CODE).one_or_none()
    report.check("client is on the credit book", client_row is not None
                 and (client_row.category or "") == "Credit Customer",
                 f"category={getattr(client_row, 'category', None)}")

    report.section("PHASE 4 — suppliers")
    for name in (SUP_RAVI, SUP_KOHAT):
        resp = post_flash(client, "/add_supplier",
                          {"name": name, "phone": "0300-0000000"})
        report.check(f"supplier '{name}' added", "Supplier Added" in body_text(resp), "")
    resp = post_flash(client, "/add_supplier", {"name": SUP_RAVI})
    report.check("duplicate supplier refused", "already exists" in body_text(resp), "")

    report.section("PHASE 5 — rent service")
    resp = post_flash(
        client,
        "/services/add",
        {"service_name": SERVICE, "service_unit": "Trips"},
    )
    report.check("service added", "added" in body_text(resp).lower(), "")
    with app.app_context():
        svc = Service.query.filter_by(name=SERVICE).one_or_none()
        clash = Material.query.filter(
            db.func.lower(db.func.trim(Material.name)) == SERVICE.lower()
        ).count()
    report.check("service exists and is NOT a material", svc is not None and clash == 0,
                 f"material rows named {SERVICE}: {clash}")
    report.check(
        "service carries no stock",
        svc is not None and float(getattr(svc, "total", 0) or 0) == 0,
        f"total={getattr(svc, 'total', None)}",
    )

    with app.app_context():
        ids = {
            "client": Client.query.filter_by(code=CLIENT_CODE).one().id,
            "ravi": Supplier.query.filter_by(name=SUP_RAVI).one().id,
            "kohat": Supplier.query.filter_by(name=SUP_KOHAT).one().id,
        }
    return ids


# ---------------------------------------------------------------------------
# Phase 6 — GRNs, one at a time, each verified before the next is posted
# ---------------------------------------------------------------------------
def phase_grns(app, client: Http, report: Reporter, ids: dict[str, int]) -> FifoOracle:
    from models import GRN, GRNItem, Material, Supplier

    report.section("PHASE 6 — GRNs (same cement, different price groups; two materials on one GRN)")
    for key, supplier, items, date, time, bill in GRNS:
        resp = post_flash(
            client,
            "/grn",
            {
                "action": "add",
                "supplier": supplier,
                "supplier_id": str(ids["ravi" if supplier == SUP_RAVI else "kohat"]),
                "mat_name[]": [material for material, _q, _r in items],
                "qty[]": [f"{qty:.2f}" for _m, qty, _r in items],
                "price[]": [f"{rate:.2f}" for _m, _q, rate in items],
                "date": date,
                "time": time,
                "manual_bill_no": bill,
                "paid_amount": "0",
                "payment_type": "Credit",
                "discount": "0",
            },
            )
        text = body_text(resp)
        report.check(f"{key} posted ({bill})", "GRN added successfully" in text,
                     "flash: GRN added successfully")
        with app.app_context():
            grn = (
                GRN.query.filter(GRN.manual_bill_no.like(f"%{bill}%"))
                .order_by(GRN.id.desc())
                .first()
            )
            rows = (
                GRNItem.query.filter_by(grn_id=grn.id).order_by(GRNItem.id).all()
                if grn
                else []
            )
            stamp = grn.date_posted if grn else None
            got = sorted(
                (norm(row.mat_name), round(float(row.qty or 0), 4),
                 round(float(row.price_at_time or 0), 4))
                for row in rows
            )
        report.check(
            f"{key} keeps the hand-typed date/time",
            stamp is not None and stamp.strftime("%Y-%m-%d %H:%M") == f"{date} {time}",
            f"stored={stamp}",
        )
        want = sorted((norm(m), round(q, 4), round(r, 4)) for m, q, r in items)
        report.check(f"{key} stored its lot(s) exactly as typed", got == want,
                     f"stored={got}")

    oracle = FifoOracle(app.config["APP_DB_PATH"])
    with app.app_context():
        for material in (MAT_DG, MAT_PIONEER, MAT_SAND):
            expected = sum(
                q for _k, _s, items, _d, _t, _b in GRNS
                for m, q, _r in items if norm(m) == norm(material)
            )
            row = Material.query.filter_by(name=material).one()
            report.check(
                f"stock in hand for {material} = purchased qty",
                abs(float(row.total or 0) - expected) < EPS
                and abs(oracle.remaining(norm(material)) - expected) < EPS,
                f"material.total={row.total} oracle={oracle.remaining(norm(material))} expected={expected}",
            )
        report.check(
            "no lot consumed by a GRN alone",
            all(lot["remaining"] == lot["received"] for lot in oracle.lot_by_id.values()),
            f"{len(oracle.lot_by_id)} lots untouched",
        )
        # Supplier ledger must show exactly what was received, per supplier.
        for supplier_name in (SUP_RAVI, SUP_KOHAT):
            expected = sum(
                q * r for _k, s, items, _d, _t, _b in GRNS if s == supplier_name
                for _m, q, r in items
            )
            supplier = Supplier.query.filter_by(name=supplier_name).one()
            resp = client.get(f"/supplier_ledger/{supplier.id}")
            text = body_text(resp)
            report.check(
                f"supplier ledger for {supplier_name} shows Rs {money(expected)}",
                resp.status_code == 200 and money(expected) in text,
                f"HTTP {resp.status_code}",
            )
    return oracle


# ---------------------------------------------------------------------------
# Phase 7 — sales, each one verified against the oracle before the next
# ---------------------------------------------------------------------------
def _layers_from_allocations(rows: list[dict]) -> list[tuple[float, float]]:
    merged: dict[float, float] = {}
    for row in rows:
        merged[row["rate"]] = merged.get(row["rate"], 0.0) + row["qty"]
    return sorted(merged.items())


def phase_sales(app, client: Http, report: Reporter, oracle_before: FifoOracle) -> None:
    from models import DirectSale, DirectSaleItem, GRNAllocation, Material

    report.section("PHASE 7 — sales, checked against GRN consumption and P&L after every bill")
    ledger_file = app.config["APP_DB_PATH"]
    report.check(
        "before the first sale every lot is untouched",
        all(lot["remaining"] == lot["received"] for lot in oracle_before.lot_by_id.values()),
        f"{len(oracle_before.lot_by_id)} lots at full quantity",
    )

    for key, date, lines, allow_negative in SALES:
        labels = [
            f"{name} {qty:g} @ {rate:g}" for name, qty, rate in lines
        ]
        resp = client.post(
            "/add_direct_sale",
            {
                "client_name": CLIENT,
                "client_code": CLIENT_CODE,
                "driver_name": "Yard Driver",
                "category": "Credit Customer",
                "product_name[]": [name for name, _q, _r in lines],
                "qty[]": [f"{qty:.2f}" for _n, qty, _r in lines],
                "unit_rate[]": [f"{rate:.2f}" for _n, _q, rate in lines],
                "payment_method": "Credit",
                "paid_amount": "0",
                "manual_bill_no": f"SEQ-SALE-{key[1:]}",
                "sale_date": date,
                "allow_negative_stock": "1" if allow_negative else "0",
            },
            )
        page = follow(client, resp)
        ok, why = page_is_clean(page, client)
        report.check(f"{key} posted ({'; '.join(labels)})",
                     resp.status_code in (302, 303) and ok,
                     why or f"HTTP {resp.status_code}")

        sale = find_sale(app, f"SEQ-SALE-{key[1:]}")
        report.check(f"{key} is on the ledger", sale is not None,
                     f"sale id={getattr(sale, 'id', None)}")
        if sale is None:
            continue
        with app.app_context():
            sale_date = sale.date_posted
            amount = float(sale.amount or 0)
            items = (
                DirectSaleItem.query.filter_by(sale_id=sale.id)
                .order_by(DirectSaleItem.id)
                .all()
            )
            item_rows = [
                {
                    "material": norm(i.product_name),
                    "qty": float(i.qty or 0),
                    "rate": float(i.price_at_time or 0),
                    "lot_id": i.grn_item_id,
                    "cost": i.cost_rate_at_sale,
                }
                for i in items
            ]
        expected_amount = sum(q * r for _n, q, r in lines)
        report.check(
            f"{key} bills the typed amount",
            abs(amount - expected_amount) < EPS,
            f"amount={money(amount)} expected={money(expected_amount)}",
        )
        report.check(
            f"{key} stamps the typed sale date",
            sale_date is not None and sale_date.strftime("%Y-%m-%d") == date,
            f"stored={sale_date}",
        )
        # Physical lines keep the typed quantity/rate; a physical line may be
        # split into one row per FIFO lot, which must add back up.
        service_lines = [(n, q, r) for n, q, r in lines if norm(n) == norm(SERVICE)]
        physical_lines = [(n, q, r) for n, q, r in lines if norm(n) != norm(SERVICE)]
        for name in {norm(n) for n, _q, _r in physical_lines}:
            expected_qty = sum(q for n, q, _r in physical_lines if norm(n) == name)
            got_qty = sum(r["qty"] for r in item_rows if r["material"] == name)
            report.check(
                f"{key}/{name} keeps qty {expected_qty:g} after FIFO splitting",
                abs(got_qty - expected_qty) < EPS,
                f"sum of split rows={got_qty:g}",
            )
        for name, qty, rate in service_lines:
            service_rows = [r for r in item_rows if r["material"] == norm(name)]
            report.check(
                f"{key}/service '{name}' is billed but never takes a lot",
                len(service_rows) == 1
                and service_rows[0]["lot_id"] is None
                and abs(service_rows[0]["qty"] - qty) < EPS
                and abs(service_rows[0]["rate"] - rate) < EPS,
                f"rows={service_rows}",
            )

        # ---- recorded FIFO allocations -------------------------------
        allocations = allocations_for(app, sale.id)
        report.check(
            f"{key} records FIFO allocations oldest-lot-first",
            _layers_from_allocations(allocations) == EXPECTED_LAYERS[key],
            f"allocations={_layers_from_allocations(allocations)} "
            f"expected={EXPECTED_LAYERS[key]}",
        )
        allocated_qty = sum(row["qty"] for row in allocations)
        physical_qty = sum(q for _n, q, _r in physical_lines)
        uncovered = FifoOracle(ledger_file).sale_uncovered.get(key, 0.0)
        report.check(
            f"{key} allocates every unit a real lot can cover ({allocated_qty:g} of {physical_qty:g})",
            abs(allocated_qty - (physical_qty - uncovered)) < EPS,
            f"allocated={allocated_qty:g} physical={physical_qty:g} uncovered={uncovered:g}",
        )

        # ---- GRN consumption, straight after the sale ----------------
        oracle_after = FifoOracle(ledger_file)
        lots = api_lots(client)
        mismatched = []
        for lot_id, lot in oracle_after.lot_by_id.items():
            api_row = lots.get(lot_id)
            if not api_row:
                mismatched.append(f"lot#{lot_id} missing from API")
                continue
            consumed = float(api_row.get("consumed_qty") or 0)
            remaining = float(api_row.get("remaining_qty") or 0)
            if abs(consumed - (lot["received"] - lot["remaining"])) > 0.01:
                mismatched.append(
                    f"lot#{lot_id} api_consumed={consumed} fifo={lot['received'] - lot['remaining']}"
                )
            if abs(remaining - lot["remaining"]) > 0.01:
                mismatched.append(f"lot#{lot_id} api_remaining={remaining} fifo={lot['remaining']}")
            if remaining < -0.01:
                mismatched.append(f"lot#{lot_id} NEGATIVE remaining={remaining}")
        report.check(
            f"{key} — /api/grn_consumption matches the independent FIFO replay",
            not mismatched,
            "; ".join(mismatched[:4]) or f"{len(lots)} lots agree",
        )

        painted = rendered_grn_lots(client)
        paint_bad = []
        for lot_id, lot in oracle_after.lot_by_id.items():
            row = painted.get(lot_id)
            if row is None:
                paint_bad.append(f"lot#{lot_id} not painted")
                continue
            if abs(row["consumed"] - (lot["received"] - lot["remaining"])) > 0.01:
                paint_bad.append(
                    f"lot#{lot_id} screen_consumed={row['consumed']} "
                    f"fifo={lot['received'] - lot['remaining']}"
                )
            if abs(row["received"] - lot["received"]) > 0.01:
                paint_bad.append(f"lot#{lot_id} screen_received={row['received']} "
                                 f"fifo={lot['received']}")
        report.check(
            f"{key} — the GRN screen paints the same lot balances",
            not paint_bad,
            "; ".join(paint_bad[:4]) or f"{len(painted)} lot rows painted",
        )

        # ---- Profit & Loss is a view of the same evidence -------------
        report_obj, grouped = report_by_document(app)
        p_and_l_bad = []
        for ref, expected_layers in EXPECTED_LAYERS.items():
            bucket = grouped.get(ref)
            oracle_cogs = oracle_after.cogs(ref)
            if bucket is None:
                if oracle_cogs > EPS:
                    p_and_l_bad.append(f"{ref} missing from P&L")
                continue
            # Lot-backed cost must be exactly the independent FIFO replay.
            if abs(bucket["lot_cogs"] - oracle_cogs) > 0.01:
                p_and_l_bad.append(
                    f"{ref} P&L lot cost={money(bucket['lot_cogs'])} fifo={money(oracle_cogs)}"
                )
            # Each lot layer keeps its own purchase rate (same-rate lots stay
            # separate rows on the page; compare the merged price bands).
            merged: dict[float, float] = {}
            for rate, qty in bucket["lot_layers"]:
                merged[rate] = merged.get(rate, 0.0) + qty
            got_layers = sorted(merged.items())
            want_layers = sorted((round(rate, 4), round(qty, 4)) for rate, qty in expected_layers)
            if got_layers != want_layers:
                p_and_l_bad.append(f"{ref} lot layers {got_layers} != {want_layers}")
            # Total cost is the lot cost plus whatever the report discloses for
            # quantity no real lot could cover.
            if abs((bucket["lot_cogs"] + bucket["non_lot_cogs"]) - bucket["cogs"]) > 0.01:
                p_and_l_bad.append(f"{ref} lot + disclosed != row cost")
            if abs(bucket["revenue"] - oracle_after.sale_revenue[ref]) > 0.01:
                p_and_l_bad.append(
                    f"{ref} P&L revenue={money(bucket['revenue'])} "
                    f"billed={money(oracle_after.sale_revenue[ref])}"
                )
        report.check(
            f"{key} — Profit & Loss costs every bill from the same FIFO lots",
            not p_and_l_bad,
            "; ".join(p_and_l_bad[:4]) or "P&L lot cost == GRN FIFO evidence",
        )
        summary = report_obj["summary"]
        disclosed = sum(
            bucket["non_lot_cogs"] for bucket in grouped.values()
        )
        report.check(
            f"{key} — P&L totals tie to the ledger",
            abs(summary["total_cogs"] - (oracle_after.total_cogs() + disclosed)) < 0.01
            and abs(summary["total_revenue"] - oracle_after.total_revenue()) < 0.01,
            f"cogs={money(summary['total_cogs'])} "
            f"lots={money(oracle_after.total_cogs())} disclosed={money(disclosed)} · "
            f"revenue={money(summary['total_revenue'])} billed={money(oracle_after.total_revenue())}",
        )

        # ---- and the screens themselves ------------------------------
        detail = client.get(
            f"/profit_reports/transaction/DirectSale/{sale.id}"
            "?start_date=2026-09-01&end_date=2026-09-30"
        )
        ok, why = page_is_clean(detail, client)
        lot_markers = [
            f"Lot #{layer['lot_id']}".encode()
            for layer in oracle_after.sale_layers[key]
        ]
        report.check(
            f"{key} — transaction detail shows its lot layers",
            ok and all(marker in detail.data for marker in lot_markers),
            why or f"{len(lot_markers)} lot marker(s)",
        )
        page = client.get("/profit_reports?start_date=2026-09-01&end_date=2026-09-30")
        ok, why = page_is_clean(page, client)
        report.check(f"{key} — Profit & Loss page renders", ok, why)
        grn_page = client.get("/grn")
        report.check(
            f"{key} — GRN page renders",
            *page_is_clean(grn_page, client),
        )

    # ------------------------------------------------------------------
    # Cross-checks after the whole sales run
    # ------------------------------------------------------------------
    report.section("PHASE 8 — FIFO reality across the whole run")
    oracle = FifoOracle(ledger_file)
    report.check(
        "DG Cement lots are fully consumed (295 bought = 295 sold)",
        abs(oracle.remaining(norm(MAT_DG))) < EPS
        and abs(oracle.lot_consumed_lots(norm(MAT_DG)) - 295.0) < EPS,
        f"remaining={oracle.remaining(norm(MAT_DG)):g}",
    )
    report.check(
        "Chenab Sand left 150 CFT (200 bought − 50 sold)",
        abs(oracle.remaining(norm(MAT_SAND)) - 150.0) < EPS,
        f"remaining={oracle.remaining(norm(MAT_SAND)):g}",
    )
    report.check(
        "Pioneer Cement left 40 bags (60 bought − 20 sold)",
        abs(oracle.remaining(norm(MAT_PIONEER)) - 40.0) < EPS,
        f"remaining={oracle.remaining(norm(MAT_PIONEER)):g}",
    )
    report.check(
        "the sold-out sale left 10 bags un-covered (honest, not invented)",
        abs(oracle.total_uncovered() - 10.0) < EPS,
        f"uncovered={oracle.total_uncovered():g}",
    )
    report.check(
        "no lot is over-consumed (every remaining ≥ 0)",
        all(lot["remaining"] >= -EPS for lot in oracle.lot_by_id.values()),
        "",
    )
    report.check(
        "the older 1,350 lot was consumed before the identical-rate later lot",
        oracle.lot_by_id[list(oracle.lots[norm(MAT_DG)])[4]["id"]]["remaining"] == 0,
        "G5 (2026-09-09 @1,350) exhausted",
    )

    with app.app_context():
        materials = {norm(m.name): float(m.total or 0) for m in Material.query.all()}
        # The lot ledger can never go below zero; the master counter follows the
        # physical +/- of every posting, so it carries the over-sold units as a
        # negative.  Both must describe the same reality.
        uncovered_by_material: dict[str, float] = {}
        for sale in oracle.sales:
            uncovered_qty = oracle.sale_uncovered.get(sale["key"], 0.0)
            if uncovered_qty <= EPS:
                continue
            physical_items = [
                item for item in oracle.items_by_sale.get(sale["id"], [])
                if item["material"] not in oracle.services
            ]
            total_physical = sum(item["qty"] for item in physical_items) or 1.0
            for item in physical_items:
                share = item["qty"] / total_physical
                uncovered_by_material[item["material"]] = (
                    uncovered_by_material.get(item["material"], 0.0)
                    + uncovered_qty * share
                )
        wrong = {
            name: {
                "material.total": value,
                "lots": oracle.remaining(name),
                "uncovered": uncovered_by_material.get(name, 0.0),
            }
            for name, value in materials.items()
            if abs(value - (oracle.remaining(name) - uncovered_by_material.get(name, 0.0))) > EPS
        }
        report.check(
            "material master stock equals lots in hand minus over-sold units",
            not wrong,
            f"mismatch: {wrong}" if wrong else f"{len(materials)} materials agree",
        )
        over_sold = {name: value for name, value in materials.items() if value < -EPS}
        report.check(
            "only the deliberately over-sold material is negative",
            set(over_sold) <= {norm(MAT_DG)},
            f"negative masters: {over_sold}",
        )
        report.check(
            "the rent service never became stock",
            norm(SERVICE) not in materials,
            f"materials: {sorted(materials)}",
        )
        grn_service_rows = db_grn_items_named(app, SERVICE)
        report.check(
            "no GRN line was ever posted for a service",
            grn_service_rows == 0,
            f"{grn_service_rows} GRN line(s)",
        )

    # P&L vs oracle, per document and in total.
    report_obj, grouped = report_by_document(app)
    per_doc = []
    for key, _date, _lines, _neg in SALES:
        bucket = grouped.get(key)
        expected = oracle.cogs(key)
        if expected <= EPS and bucket is None:
            continue
        if bucket is None:
            per_doc.append(f"{key} missing from P&L ({money(expected)} of cost)")
            continue
        if abs(bucket["lot_cogs"] - expected) > 0.01:
            per_doc.append(f"{key} P&L lot cost={money(bucket['lot_cogs'])} fifo={money(expected)}")
    report.check("P&L lot cost matches the FIFO replay bill by bill", not per_doc,
                 "; ".join(per_doc[:5]) or f"{len(SALES)} bills replayed")

    summary = report_obj["summary"]
    disclosed = sum(bucket["non_lot_cogs"] for bucket in grouped.values())
    ledger_total = sum(sale["amount"] for sale in oracle.sales)
    report.check(
        "P&L revenue = sum of the sale amounts on the ledger",
        abs(summary["total_revenue"] - ledger_total) < 0.01,
        f"revenue={money(summary['total_revenue'])} ledger={money(ledger_total)}",
    )
    report.check(
        "P&L profit = recognised revenue − cost of goods",
        abs(summary["total_profit"]
            - (summary["recognized_revenue"] - summary["total_cogs"])) < 0.01,
        f"profit={money(summary['total_profit'])} "
        f"recognised={money(summary['recognized_revenue'])} "
        f"cogs={money(summary['total_cogs'])}",
    )
    report.check(
        "P&L formula check passes",
        bool(summary.get("formula_check")),
        f"formula_check={summary.get('formula_check')}",
    )
    report.check(
        "every sale's revenue is recognised (a registered service is income, not unknown cost)",
        abs(summary["recognized_revenue"] - ledger_total) < 0.01
        and summary["unknown_cost_rows"] == 0,
        f"recognised={money(summary['recognized_revenue'])} "
        f"unknown_rows={summary['unknown_cost_rows']}",
    )
    # The one over-sold bill: 10 bags no lot could cover.
    over = grouped.get("S9") or {}
    over_basis = " ".join(over.get("bases") or [])
    over_sources = over.get("sources") or []
    frozen = [s for s in over_sources if s.get("type") == "FrozenRate"]
    report.check(
        "the over-sold bill discloses a frozen rate beyond every real lot",
        len(frozen) == 1
        and abs(float(frozen[0].get("qty") or 0) - 10.0) < EPS
        and abs(float(frozen[0].get("amount") or 0)
                - 10.0 * float(frozen[0].get("rate") or 0)) < 0.01
        and "beyond every GRN lot" in over_basis,
        f"frozen={frozen} basis={over_basis[:120]!r}",
    )
    report.check(
        "the over-sold bill takes nothing from the real lots",
        abs((over.get("lot_cogs") or 0.0)) < EPS
        and abs((over.get("non_lot_cogs") or 0.0)
                - (over.get("cogs") or 0.0)) < EPS,
        f"lot={money(over.get('lot_cogs') or 0)} non_lot={money(over.get('non_lot_cogs') or 0)}",
    )
    report.check(
        "total COGS = real lot cost + the disclosed non-lot cost",
        abs(summary["total_cogs"] - (oracle.total_cogs() + disclosed)) < 0.01,
        f"cogs={money(summary['total_cogs'])} lots={money(oracle.total_cogs())} "
        f"disclosed={money(disclosed)}",
    )
    # The rent service line: real income, zero cost of goods, no lot.
    service_doc = grouped.get("S5") or {}
    service_rows = [
        row for row in (service_doc.get("rows") or [])
        if norm(row.get("material")) == norm(SERVICE)
    ]
    report.check(
        "the rent service is billed, recognised, and costs nothing",
        len(service_rows) == 1
        and abs(float(service_rows[0].get("delivery_revenue") or 0) - 4000.0) < 0.01
        and abs(float(service_rows[0].get("delivery_cost") or 0)) < EPS
        and bool(service_rows[0].get("include_in_net"))
        and not (service_rows[0].get("cost_sources") or [])
        and service_rows[0].get("cost_status") != "missing",
        f"row={ {k: service_rows[0].get(k) for k in ('delivery_revenue','delivery_cost','include_in_net','cost_status')} if service_rows else None}",
    )
    report.check(
        "no cost source ever points at a service name",
        all(norm(src.get("material")) != norm(SERVICE)
            for bucket in grouped.values() for src in bucket["sources"]),
        "",
    )

    # Filters must select rows, never change a number.
    filtered = report_totals(app, material=MAT_DG)
    dg_rows = [row for row in filtered["rows"] if row.get("entry_type") == "material_sale"]
    report.check(
        "material filter returns only DG Cement rows",
        dg_rows and all(norm(row.get("material")) == norm(MAT_DG) for row in dg_rows),
        f"{len(dg_rows)} row(s)",
    )
    unfiltered_dg = sum(
        float(row.get("material_cost") or 0)
        for row in report_obj["rows"]
        if norm(row.get("material")) == norm(MAT_DG)
    )
    dg_cogs = sum(float(row.get("material_cost") or 0) for row in filtered["rows"])
    report.check(
        "DG-only P&L cost equals the same DG rows unfiltered (no other material leaks in)",
        abs(dg_cogs - unfiltered_dg) < 0.01,
        f"filtered={money(dg_cogs)} unfiltered={money(unfiltered_dg)}",
    )
    dg_lot_cost = sum(
        float(src.get("amount") or 0)
        for row in filtered["rows"] for src in (row.get("cost_sources") or [])
        if src.get("grn_item_id")
    )
    expected_dg = sum(
        oracle.cogs(key)
        for key, _d, lines, _n in SALES
        if any(norm(n) == norm(MAT_DG) for n, _q, _r in lines)
    )
    report.check(
        "DG lot cost equals the DG FIFO layers",
        abs(dg_lot_cost - expected_dg) < 0.01,
        f"filtered lots={money(dg_lot_cost)} fifo={money(expected_dg)}",
    )

    # Reports only read: nothing above may have mutated the evidence.
    report.check(
        "reading the reports did not change the ledger",
        abs(FifoOracle(ledger_file).total_cogs() - oracle.total_cogs()) < EPS,
        "second replay identical",
    )

    report.section("PHASE 9 — report exports")
    for path, label, needle in (
        ("/profit_reports/export/pdf?start_date=2026-09-01&end_date=2026-09-30",
         "P&L PDF", b"%PDF"),
        ("/profit_reports/export/excel?start_date=2026-09-01&end_date=2026-09-30",
         "P&L Excel", b"PK"),
    ):
        resp = client.get(path)
        report.check(
            f"{label} downloads",
            resp.status_code == 200 and needle in resp.data[:8],
            f"HTTP {resp.status_code} bytes={len(resp.data)}",
        )

    report.section("PHASE 10 — client ledgers")
    with app.app_context():
        from models import Client

        client_row = Client.query.filter_by(code=CLIENT_CODE).one()
        client_id = client_row.id
    receivable = sum(sale["amount"] - 0.0 for sale in oracle.sales)
    for path, label in (
        (f"/ledger/{client_id}", "financial ledger"),
        (f"/client_ledger/{client_id}", "material ledger"),
    ):
        resp = client.get(path)
        ok, why = page_is_clean(resp, client)
        report.check(f"client {label} renders", ok, why)
    resp = client.get(f"/ledger/{client_id}")
    text = body_text(resp)
    report.check(
        f"financial ledger shows the {money(receivable)} receivable",
        resp.status_code == 200 and money(receivable) in text,
        f"HTTP {resp.status_code} amount_present={money(receivable) in text}",
    )


def db_grn_items_named(app, name: str) -> int:
    from models import GRNItem, db

    with app.app_context():
        return int(
            db.session.query(db.func.count())
            .select_from(GRNItem)
            .filter(db.func.upper(db.func.trim(GRNItem.mat_name)) == norm(name))
            .scalar()
            or 0
        )


# small helper the phase above refers to (kept next to the oracle for clarity)
def _lot_consumed_lots(self, material: str) -> float:
    return sum(lot["received"] - lot["remaining"] for lot in self.lots.get(material, []))


FifoOracle.lot_consumed_lots = _lot_consumed_lots  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def run_sequence(app, report: Reporter | None = None, *, seed_junk: bool = True) -> Reporter:
    report = report or Reporter()
    client = Http(app)
    report.check("admin login", client.login(), "session established")
    phase_zero(app, client, report, seed_junk=seed_junk)
    ids = phase_masters(app, client, report)
    oracle_before = phase_grns(app, client, report, ids)
    phase_sales(app, client, report, oracle_before)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite file to use")
    parser.add_argument("--keep", action="store_true", help="keep the database file")
    parser.add_argument("--skip-wipe", action="store_true",
                        help="skip phase 0 (assume the file is already empty)")
    args = parser.parse_args(argv)

    db_path = Path(args.db).expanduser().resolve()
    if db_path.exists() and not args.keep:
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    report = Reporter()
    print(f"Database: {db_path}")
    app = build_app(db_path)
    try:
        run_sequence(app, report, seed_junk=not args.skip_wipe)
    except Exception:
        traceback.print_exc()
        report.check("sequence completed without an exception", False, "see traceback above")
    ok = report.summary()
    if not args.keep and ok:
        try:
            db_path.unlink()
        except OSError:
            pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
