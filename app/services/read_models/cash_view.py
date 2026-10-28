"""Cash view read model — the cash module and the book on one page.

Each cash/bank/wallet master row has exactly one GL account
(``gl_account.linked_account_id``).  This view puts the book balance next to the
master balance so any drift is visible immediately instead of being discovered by
a customer.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func

from models import Account, GLAccount, JournalEntry, JournalLine, db
from app.services import cache
from app.services.ledger import query as q

__all__ = ["load", "positions"]


def positions(as_of=None) -> list[dict]:
    as_of = as_of or date.today()
    rows = [row for row in q.account_balances(end=as_of) if row["is_cash"]]
    ids = [row["linked_account_id"] for row in rows if row["linked_account_id"]]
    masters = {
        account.id: account
        for account in (Account.query.filter(Account.id.in_(ids)).all() if ids else [])
    }
    result = []
    for row in rows:
        master = masters.get(row["linked_account_id"])
        master_minor = 0
        if master is not None:
            master_minor = (
                int(master.balance_minor)
                if getattr(master, "balance_minor", None) is not None
                else int(round(float(master.balance or 0) * 100))
            )
        result.append({
            **row,
            "master": master,
            "book_balance": row["balance"],
            "master_balance": master_minor,
            "difference": row["balance"] - master_minor,
        })
    return result


def load(*, start=None, end=None, as_of=None) -> dict:
    as_of = as_of or date.today()
    version = q.ledger_version()

    def build():
        rows = positions(as_of)
        daily = (
            db.session.query(
                JournalEntry.entry_date,
                func.coalesce(func.sum(JournalLine.debit_minor), 0),
                func.coalesce(func.sum(JournalLine.credit_minor), 0),
            )
            .join(GLAccount, GLAccount.id == JournalLine.gl_account_id)
            .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
            .filter(GLAccount.is_cash.is_(True))
            .group_by(JournalEntry.entry_date)
            .order_by(JournalEntry.entry_date.desc())
            .limit(120)
            .all()
        )
        days = [
            {
                "date": row[0],
                "in": int(row[1] or 0),
                "out": int(row[2] or 0),
                "net": int(row[1] or 0) - int(row[2] or 0),
            }
            for row in daily
        ]
        return {
            "as_of": as_of,
            "ledger_version": version,
            "rows": rows,
            "total_book": sum(row["book_balance"] for row in rows),
            "total_master": sum(row["master_balance"] for row in rows),
            "difference": sum(row["difference"] for row in rows),
            "days": days,
            "matches": all(row["difference"] == 0 for row in rows),
        }

    return cache.get_or_build(("cash_view", str(as_of), str(start), str(end)), version, build)
