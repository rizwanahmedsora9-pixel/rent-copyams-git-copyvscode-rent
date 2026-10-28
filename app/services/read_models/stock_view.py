"""Stock view read model — quantities from movements, value from the book.

The stock page has two questions that used to be answered by two different
subsystems:

* **how much is there?** — the ``Entry`` movement ledger (IN/OUT, non-voided),
  which is what the yard actually dispensed;
* **what is it worth?** — the journal's ``1200 Inventory`` balance, which is
  what the GRN postings put there.

This module answers both, side by side, and reports whether they agree.  A
disagreement is never hidden: ``reconciles`` is false and ``difference`` says by
how much, so the books-agree page can show it instead of a page quietly picking
one of the two truths (master plan §1, "two stock truths").
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import case, func

from models import (
    Entry,
    GRNAllocation,
    GRNItem,
    Material,
    db,
)
from app.services import cache
from app.services.lookups import is_service_name
from app.services.ledger import accounts as coa
from app.services.ledger import query as q

__all__ = ["load", "quantities", "valuation", "summary"]
_TABLES = (Entry, Material)


def quantities() -> list[dict]:
    """Per-material IN / OUT / remaining from the movement ledger.

    Every material from the master appears, including materials that have not
    moved yet (a 0/0/0 row is information), and any material that only exists
    in history is kept instead of being silently dropped.  Services are not
    stock — their ``Entry`` rows feed the booking pool, not the yard.
    """
    rows = (
        db.session.query(
            Entry.material,
            func.coalesce(func.sum(case((Entry.type == "IN", Entry.qty), else_=0)), 0),
            func.coalesce(func.sum(case((Entry.type == "OUT", Entry.qty), else_=0)), 0),
        )
        .filter(Entry.is_void.is_(False))
        .group_by(Entry.material)
        .all()
    )
    masters = Material.query.with_entities(Material.name, Material.unit).all()
    units = {(row[0] or "").strip().lower(): (row[1] or "Bags") for row in masters}
    master_names = {(row[0] or "").strip() for row in masters}
    moved = {
        (name or "").strip(): (float(inbound or 0), float(outbound or 0))
        for name, inbound, outbound in rows
        if (name or "").strip()
    }
    result = []
    for name in sorted(master_names | set(moved)):
        if is_service_name(name):
            continue
        inbound, outbound = moved.get(name, (0.0, 0.0))
        result.append({
            "name": name,
            "in": inbound,
            "out": outbound,
            "remaining": inbound - outbound,
            "unit": units.get(name.lower(), "Bags"),
            "has_movement": name in moved,
        })
    return result


def valuation() -> dict:
    """Value of the remaining GRN lots, and the book's inventory balance."""
    received = (
        db.session.query(
            func.coalesce(func.sum(GRNItem.qty * GRNItem.price_at_time), 0),
        )
        .filter(GRNItem.is_void.is_(False))
        .scalar()
        or 0
    )
    consumed = (
        db.session.query(
            func.coalesce(func.sum(GRNAllocation.qty * GRNAllocation.cost_rate), 0),
        )
        .filter(GRNAllocation.is_void.is_(False))
        .scalar()
        or 0
    )
    lot_value_minor = int(round((float(received) - float(consumed)) * 100))
    balances = {row["code"]: row for row in q.account_balances()}
    book_minor = int(balances.get(coa.GL.INVENTORY, {}).get("balance", 0))
    suspense_minor = int(balances.get(coa.GL.INVENTORY_SUSPENSE, {}).get("balance", 0))
    return {
        "lots_value": lot_value_minor,
        "book_value": book_minor,
        "suspense": suspense_minor,
        "difference": lot_value_minor - book_minor,
        "reconciles": lot_value_minor == book_minor,
    }


def load(as_of=None, *, limit: int = 200) -> dict:
    as_of = as_of or date.today()
    version = q.ledger_version()

    def build():
        rows = quantities()
        value = valuation()
        movements = (
            Entry.query.filter(Entry.is_void.is_(False))
            .order_by(Entry.created_at.desc(), Entry.id.desc())
            .limit(limit)
            .all()
        )
        lots = (
            db.session.query(
                GRNItem.mat_name,
                func.coalesce(func.sum(GRNItem.qty), 0),
                func.coalesce(func.sum(GRNItem.qty * GRNItem.price_at_time), 0),
            )
            .filter(GRNItem.is_void.is_(False))
            .group_by(GRNItem.mat_name)
            .order_by(GRNItem.mat_name.asc())
            .limit(limit)
            .all()
        )
        return {
            "as_of": as_of,
            "ledger_version": version,
            "rows": rows,
            "total_remaining": sum(row["remaining"] for row in rows),
            "value": value,
            "reconciles": value["reconciles"],
            "difference": value["difference"],
            "movements": movements,
            "lots": [
                {"material": row[0] or "Unknown/Unbranded",
                 "qty": float(row[1] or 0),
                 "value": int(round(float(row[2] or 0) * 100))}
                for row in lots
            ],
        }

    return cache.get_or_build(("stock_view", str(as_of)), version, build)


def summary() -> dict:
    """The dashboard's stock card: total remaining plus the value tie."""
    view = load()
    return {
        "rows": view["rows"],
        "total_remaining": view["total_remaining"],
        "value": view["value"],
        "reconciles": view["reconciles"],
        "ledger_version": view["ledger_version"],
    }
