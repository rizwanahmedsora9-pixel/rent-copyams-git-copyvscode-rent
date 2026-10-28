"""Cash flow statement: opening + movement = closing, from the journal."""
from __future__ import annotations

from datetime import date

from sqlalchemy import func

from models import GLAccount, JournalEntry, JournalLine, db
from app.services.ledger import query as q


def build(*, start=None, end=None) -> dict:
    start = start or date.today().replace(day=1)
    end = end or date.today()
    opening_query = (
        db.session.query(func.coalesce(func.sum(JournalLine.debit_minor - JournalLine.credit_minor), 0))
        .join(GLAccount, GLAccount.id == JournalLine.gl_account_id)
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .filter(GLAccount.is_cash.is_(True), JournalEntry.entry_date < start)
    )
    opening = int(opening_query.scalar() or 0)
    rows = (
        db.session.query(
            JournalEntry.entry_date,
            func.coalesce(func.sum(JournalLine.debit_minor), 0),
            func.coalesce(func.sum(JournalLine.credit_minor), 0),
        )
        .join(GLAccount, GLAccount.id == JournalLine.gl_account_id)
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .filter(
            GLAccount.is_cash.is_(True),
            JournalEntry.entry_date >= start,
            JournalEntry.entry_date <= end,
        )
        .group_by(JournalEntry.entry_date)
        .order_by(JournalEntry.entry_date.asc())
        .all()
    )
    days = []
    running = opening
    for day, inflow, outflow in rows:
        net = int(inflow or 0) - int(outflow or 0)
        running += net
        days.append({
            "date": day,
            "in": int(inflow or 0),
            "out": int(outflow or 0),
            "net": net,
            "closing": running,
        })
    return {
        "start": start,
        "end": end,
        "opening": opening,
        "days": days,
        "inflow": sum(row["in"] for row in days),
        "outflow": sum(row["out"] for row in days),
        "closing": running,
        "positions": q.cash_positions(as_of=end),
    }
