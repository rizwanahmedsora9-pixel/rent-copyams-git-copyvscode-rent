"""Supplier account read model — payables straight from the book."""
from __future__ import annotations

from datetime import date

from models import JournalEntry, JournalLine, Supplier, db
from app.services import cache
from app.services.ledger import accounts as coa
from app.services.ledger import query as q

__all__ = ["balances", "load"]


def _gl_id(code: str):
    account = coa.account_by_code(code)
    return account.id if account else None


def balances(as_of=None) -> dict:
    as_of = as_of or date.today()
    version = q.ledger_version()

    def build():
        rows = q.supplier_subledger(as_of=as_of)
        return {
            "as_of": as_of,
            "ledger_version": version,
            "rows": rows,
            "total": sum(row["amount"] for row in rows),
        }

    return cache.get_or_build(("supplier_accounts", str(as_of)), version, build)


def load(supplier_id, *, as_of=None, limit: int = 400) -> dict:
    as_of = as_of or date.today()
    supplier = Supplier.query.get(int(supplier_id)) if supplier_id else None
    version = q.ledger_version()

    def build():
        gl_id = _gl_id(coa.GL.PAYABLE)
        rows = []
        if gl_id is not None:
            query = (
                db.session.query(JournalLine, JournalEntry)
                .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
                .filter(
                    JournalLine.gl_account_id == gl_id,
                    JournalLine.supplier_id == int(supplier_id),
                    JournalEntry.entry_date <= as_of,
                )
                .order_by(JournalEntry.entry_date.asc(), JournalEntry.id.asc())
            )
            running = 0
            for line, entry in query.limit(limit).all():
                running += int(line.credit_minor or 0) - int(line.debit_minor or 0)
                rows.append({
                    "entry": entry,
                    "line": line,
                    "debit": int(line.debit_minor or 0),
                    "credit": int(line.credit_minor or 0),
                    "balance": running,
                })
        return {
            "supplier": supplier,
            "as_of": as_of,
            "ledger_version": version,
            "rows": rows,
            "balance": rows[-1]["balance"] if rows else 0,
        }

    return cache.get_or_build(("supplier_account", int(supplier_id or 0), str(as_of)), version, build)
