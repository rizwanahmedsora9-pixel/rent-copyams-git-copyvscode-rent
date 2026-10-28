"""Month-end close: a checklist that must pass before a period can be locked.

The checklist is the subset of the "books agree" gate that concerns one month
(plan §7).  A close records the month's result on the period row and then locks
it, so last month's numbers are byte-identical on every later render.

Closing does **not** post a transfer of revenue and expense balances into
retained earnings.  The statements are computed from dated journal lines, so the
result is already correct in every period; posting a closing entry on top would
double-count equity.  ``closed_result_minor`` is the signed evidence of what the
month said.
"""
from __future__ import annotations

import logging

from models import JournalEntry, db
from app.services.ledger import periods as period_svc
from app.services.ledger import query as q

log = logging.getLogger("app.ledger.close")

__all__ = ["checklist", "close_period", "CloseRefused"]


class CloseRefused(ValueError):
    def __init__(self, failures):
        self.failures = failures
        names = ", ".join(item["title"] for item in failures)
        super().__init__(f"The month cannot be closed yet: {names}.")


def checklist(period) -> list[dict]:
    """Return the month's close checks, each with ok/fail and the numbers."""
    from app.services.read_models import books_agree

    totals = q.period_totals(period)
    all_checks = {row["key"]: row for row in books_agree.checks(period.end_date)}
    items = []

    def add(key, title, ok, detail, href=None):
        items.append({"key": key, "title": title, "ok": bool(ok), "detail": detail, "href": href})

    add(
        "period_balanced",
        "Trial balance balances for the month",
        totals.get("balanced", False),
        f"Difference {int(totals.get('difference') or 0) / 100:,.2f}",
        "/ledger/trial_balance",
    )
    add(
        "double_entry",
        "Every entry has equal debits and credits",
        all_checks.get("double_entry", {}).get("status") == "ok",
        all_checks.get("double_entry", {}).get("detail", ""),
        "/ledger/books_agree",
    )
    month_coverage = q.coverage(period.start_date, period.end_date)
    missing = sum(row["missing"] for row in month_coverage.values())
    add(
        "coverage",
        "Every document in the month has an entry",
        missing == 0,
        f"{missing} document(s) dated in this month have no live journal entry.",
        "/ledger/coverage",
    )
    add(
        "balance_sheet",
        "Balance sheet ties to equity",
        all_checks.get("balance_sheet", {}).get("status") == "ok",
        all_checks.get("balance_sheet", {}).get("detail", ""),
        "/ledger/balance_sheet",
    )
    return items


def close_period(period, *, actor=None, lock: bool = True, note: str | None = None):
    """Run the checklist, record the month's result, then lock (or soft-close)."""
    if period is None:
        raise ValueError("Period not found.")
    if (period.status or period_svc.OPEN) == period_svc.LOCKED:
        return period
    results = checklist(period)
    failures = [item for item in results if not item["ok"]]
    if failures:
        raise CloseRefused(failures)
    totals = q.period_totals(period)
    period_svc.set_period_result(period, totals.get("net_profit", 0), actor=actor)
    if lock:
        period_svc.lock_period(period, actor=actor, note=note)
    else:
        period_svc.soft_close_period(period, actor=actor, note=note)
    db.session.flush()
    log.info(
        "Closed %s (%s): result %.2f",
        period_svc.period_label(period),
        "locked" if lock else "soft-closed",
        float(totals.get("net_profit") or 0) / 100,
    )
    return period
