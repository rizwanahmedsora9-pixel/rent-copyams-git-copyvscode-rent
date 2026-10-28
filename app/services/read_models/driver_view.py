"""Driver (delivery person) view read model — what we owe, from the book.

A sale charges the driver (``2030 Driver payable`` credit) and a settlement pays
it down (debit).  Both are journal lines carrying ``delivery_person_id``, so the
driver ledger, the sale screen's "driver rent" card and the driver page cannot
disagree about a balance.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func

from models import DeliveryPerson, JournalEntry, JournalLine, db
from app.services import cache
from app.services.ledger import accounts as coa
from app.services.ledger import query as q

__all__ = ["load", "balances"]


def _gl_id():
    account = coa.account_by_code(coa.GL.DRIVER_PAYABLE)
    return account.id if account else None


def balances(as_of=None) -> list[dict]:
    as_of = as_of or date.today()
    rows = q.driver_subledger(as_of=as_of)
    return sorted(rows, key=lambda row: row["amount"], reverse=True)


def load(driver_id=None, *, as_of=None, limit: int = 400) -> dict:
    as_of = as_of or date.today()
    version = q.ledger_version()

    def build():
        gl_id = _gl_id()
        drivers = DeliveryPerson.query.order_by(DeliveryPerson.name.asc()).all()
        rows = balances(as_of)
        totals = {row["id"]: row["amount"] for row in rows}
        page = []
        for driver in drivers:
            page.append({
                "id": driver.id,
                "name": driver.name,
                "phone": getattr(driver, "phone", None),
                "is_active": getattr(driver, "is_active", True),
                "balance": totals.get(driver.id, 0),
            })
        statement = []
        if driver_id and gl_id is not None:
            query = (
                db.session.query(JournalLine, JournalEntry)
                .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
                .filter(
                    JournalLine.gl_account_id == gl_id,
                    JournalLine.delivery_person_id == int(driver_id),
                    JournalEntry.entry_date <= as_of,
                )
                .order_by(JournalEntry.entry_date.asc(), JournalEntry.id.asc())
            )
            running = 0
            for line, entry in query.limit(limit).all():
                running += int(line.credit_minor or 0) - int(line.debit_minor or 0)
                statement.append({
                    "entry": entry,
                    "line": line,
                    "charge": int(line.credit_minor or 0),
                    "settled": int(line.debit_minor or 0),
                    "balance": running,
                })
        return {
            "as_of": as_of,
            "ledger_version": version,
            "driver_id": int(driver_id) if driver_id else None,
            "rows": page,
            "total": sum(row["amount"] for row in rows),
            "statement": statement,
            "statement_balance": statement[-1]["balance"] if statement else 0,
        }

    return cache.get_or_build(("driver_view", int(driver_id or 0), str(as_of)), version, build)
