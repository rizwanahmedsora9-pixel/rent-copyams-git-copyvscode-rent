"""Client account read model — receivables straight from the book.

Every row here is a ``journal_line`` on ``1100 Accounts receivable`` carrying a
``client_id``.  That replaces the two independent replays in
``financial_ledgers.py`` and ``ledgers.py``: there is one subledger now, and the
control account always equals it by construction.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import case, func

from models import Client, JournalEntry, JournalLine, db
from app.services import cache
from app.services.ledger import accounts as coa
from app.services.ledger import query as q

__all__ = ["balances", "load", "ageing"]

BUCKETS = (("current", 0), ("1-30", 1), ("31-60", 31), ("61-90", 61), ("90+", 91))


def _gl_id(code: str):
    account = coa.account_by_code(code)
    return account.id if account else None


def _ageing_rows(as_of: date) -> dict[int, dict[str, int]]:
    gl_id = _gl_id(coa.GL.RECEIVABLE)
    if gl_id is None:
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
            JournalLine.client_id,
            bucket,
            func.coalesce(func.sum(JournalLine.debit_minor - JournalLine.credit_minor), 0),
        )
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .filter(
            JournalLine.gl_account_id == gl_id,
            JournalLine.client_id.isnot(None),
            JournalEntry.entry_date <= as_of,
        )
        .group_by(JournalLine.client_id, bucket)
        .all()
    )
    result: dict[int, dict[str, int]] = {}
    for client_id, name, amount in rows:
        result.setdefault(int(client_id), {key: 0 for key, _ in BUCKETS})
        result[int(client_id)][name] = int(amount or 0)
    return result


def balances(as_of=None) -> list[dict]:
    as_of = as_of or date.today()
    version = q.ledger_version()

    def build():
        ageing = _ageing_rows(as_of)
        rows = q.client_subledger(as_of=as_of)
        for row in rows:
            buckets = ageing.get(row["id"], {key: 0 for key, _ in BUCKETS})
            row["ageing"] = buckets
            row["oldest_bucket"] = next(
                (key for key, _ in reversed(BUCKETS) if buckets.get(key)), "current"
            )
        return {
            "as_of": as_of,
            "ledger_version": version,
            "rows": rows,
            "total": sum(row["amount"] for row in rows),
        }

    return cache.get_or_build(("client_accounts", str(as_of)), version, build)


def load(client_id, *, as_of=None, limit: int = 400) -> dict:
    as_of = as_of or date.today()
    client = Client.query.get(int(client_id)) if client_id else None
    version = q.ledger_version()

    def build():
        gl_id = _gl_id(coa.GL.RECEIVABLE)
        rows = []
        opening = 0
        if gl_id is not None:
            query = (
                db.session.query(JournalLine, JournalEntry)
                .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
                .filter(
                    JournalLine.gl_account_id == gl_id,
                    JournalLine.client_id == int(client_id),
                    JournalEntry.entry_date <= as_of,
                )
                .order_by(JournalEntry.entry_date.asc(), JournalEntry.id.asc())
            )
            running = 0
            for line, entry in query.limit(limit).all():
                running += int(line.debit_minor or 0) - int(line.credit_minor or 0)
                rows.append({
                    "entry": entry,
                    "line": line,
                    "debit": int(line.debit_minor or 0),
                    "credit": int(line.credit_minor or 0),
                    "balance": running,
                })
        ageing = _ageing_rows(as_of).get(int(client_id), {key: 0 for key, _ in BUCKETS}) if client_id else {}
        return {
            "client": client,
            "as_of": as_of,
            "ledger_version": version,
            "rows": rows,
            "opening": opening,
            "balance": rows[-1]["balance"] if rows else 0,
            "ageing": ageing,
        }

    return cache.get_or_build(("client_account", int(client_id or 0), str(as_of)), version, build)
