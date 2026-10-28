"""Payables view read model — open supplier bills and ageing from the book.

``/payables`` and the dashboard's payables tile answer the same question from
the same place: the ``2010 Accounts payable`` control account, aged by the
``journal_line`` date.  The document tables are only listed for drill-down
(which GRN created the balance), never summed to answer "how much do we owe".
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import case, func

from models import GRN, JournalEntry, JournalLine, Supplier, db
from app.services import cache
from app.services.ledger import accounts as coa
from app.services.ledger import query as q

__all__ = ["load", "ageing", "open_bills"]

BUCKETS = ("current", "1-30", "31-60", "61-90", "90+")


def ageing(as_of=None) -> dict[int, dict[str, int]]:
    """Payable balance per supplier, split by how long each line has been open."""
    as_of = as_of or date.today()
    account = coa.account_by_code(coa.GL.PAYABLE)
    if account is None:
        return {}
    age_days = func.julianday(as_of.isoformat()) - func.julianday(JournalEntry.entry_date)
    bucket = case(
        (age_days <= 0, "current"),
        (age_days <= 30, "1-30"),
        (age_days <= 60, "31-60"),
        (age_days <= 90, "61-90"),
        else_="90+",
    ).label("bucket")
    rows = (
        db.session.query(
            JournalLine.supplier_id,
            bucket,
            func.coalesce(func.sum(JournalLine.credit_minor - JournalLine.debit_minor), 0),
        )
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .filter(
            JournalLine.gl_account_id == account.id,
            JournalLine.supplier_id.isnot(None),
            JournalEntry.entry_date <= as_of,
        )
        .group_by(JournalLine.supplier_id, bucket)
        .all()
    )
    result: dict[int, dict[str, int]] = {}
    for supplier_id, name, amount in rows:
        result.setdefault(int(supplier_id), {key: 0 for key in BUCKETS})
        result[int(supplier_id)][name] = int(amount or 0)
    return result


def open_bills(suppliers: dict[int, str] | None = None, *, limit: int = 60) -> list[dict]:
    """The GRN bills behind the balance — a document list, not a second total."""
    suppliers = suppliers or {}
    rows = (
        GRN.query.filter(GRN.is_void.is_(False))
        .order_by(GRN.date_posted.desc(), GRN.id.desc())
        .limit(limit)
        .all()
    )
    bills = []
    for grn in rows:
        items = sum(
            float(item.qty or 0) * float(item.price_at_time or 0)
            for item in grn.items
            if not getattr(item, "is_void", False)
        )
        total = (
            items
            + float(grn.loading_cost or 0)
            + float(grn.freight_cost or 0)
            + float(grn.other_expense or 0)
            + float(grn.adjustment_amount or 0)
            + float(grn.tax_amount or 0)
            - float(grn.discount or 0)
        )
        outstanding = total - float(grn.paid_amount or 0)
        if outstanding <= 0.004:
            continue
        bills.append({
            "id": grn.id,
            "supplier_id": grn.supplier_id,
            "supplier": suppliers.get(grn.supplier_id) or grn.supplier or "",
            "bill_no": grn.manual_bill_no or grn.auto_bill_no or f"GRN-{grn.id}",
            "date": grn.date_posted,
            "total": int(round(total * 100)),
            "paid": int(round(float(grn.paid_amount or 0) * 100)),
            "outstanding": int(round(outstanding * 100)),
        })
    return bills


def load(as_of=None, *, include_bills: bool = True) -> dict:
    as_of = as_of or date.today()
    version = q.ledger_version()

    def build():
        rows = q.supplier_subledger(as_of=as_of)
        buckets = ageing(as_of)
        for row in rows:
            row["ageing"] = buckets.get(row["id"], {key: 0 for key in BUCKETS})
        names = {row["id"]: row["name"] for row in rows}
        bills = open_bills(names) if include_bills else []
        return {
            "as_of": as_of,
            "ledger_version": version,
            "rows": rows,
            "total": sum(row["amount"] for row in rows),
            "bills": bills,
            "bill_total": sum(bill["outstanding"] for bill in bills),
            "ageing": {
                key: sum(row["ageing"].get(key, 0) for row in rows) for key in BUCKETS
            },
        }

    return cache.get_or_build(("payables_view", str(as_of), bool(include_bills)), version, build)
