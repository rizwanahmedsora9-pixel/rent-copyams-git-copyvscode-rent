"""Accounting periods: open → soft-closed → locked.

Every posting is dated into exactly one monthly period.  While a period is
``open`` anything may post.  ``soft_closed`` still accepts postings, but they are
flagged as adjustments so a month-end review can find them.  ``locked`` accepts
nothing at all: the only way to change a locked month is a reversal dated into a
later, open period (plan §7 S2).

The module is deliberately the *only* place that decides whether a date is
open.  ``payments_crud._assert_period_open`` delegates here so a cash
reconciliation cutoff, a sale, a GRN and a journal posting can never disagree.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date, datetime

from models import db, AccountingPeriod, LedgerSyncEvent

log = logging.getLogger("app.ledger.periods")

__all__ = [
    "PeriodLocked",
    "normalize_date",
    "period_bounds",
    "get_or_create_period",
    "period_for",
    "assert_can_post",
    "ensure_current_period",
    "list_periods",
    "lock_period",
    "soft_close_period",
    "reopen_period",
    "set_period_result",
    "period_label",
]

OPEN = "open"
SOFT_CLOSED = "soft_closed"
LOCKED = "locked"


class PeriodLocked(ValueError):
    """Raised when a posting or edit targets a locked accounting period."""

    def __init__(self, period, message: str | None = None):
        self.period = period
        super().__init__(message or self._default_message(period))

    @staticmethod
    def _default_message(period) -> str:
        if period is None:
            return "The accounting period is locked; this transaction cannot be posted."
        return (
            f"{period_label(period)} is locked, so it cannot be changed. "
            "Post the correction in the current open period instead (Ledger → Periods)."
        )


def normalize_date(value) -> date:
    """Accept ``date``/``datetime``/``'YYYY-MM-DD'``/``'YYYY-MM-DDTHH:MM'``."""
    if value is None:
        return datetime.now().date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return datetime.now().date()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:19] if "T" in text or " " in text else text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return datetime.now().date()


def period_bounds(year: int, month: int) -> tuple[date, date]:
    last_day = calendar.monthrange(int(year), int(month))[1]
    return date(int(year), int(month), 1), date(int(year), int(month), last_day)


def period_label(period) -> str:
    if period is None:
        return "—"
    try:
        return date(int(period.year), int(period.month), 1).strftime("%B %Y")
    except Exception:  # pragma: no cover - defensive
        return f"{period.year}-{int(period.month):02d}"


def _find(period_date: date) -> AccountingPeriod | None:
    return AccountingPeriod.query.filter_by(
        year=period_date.year, month=period_date.month
    ).first()


def get_or_create_period(period_date=None) -> AccountingPeriod:
    """Return the period that owns *period_date*, creating it open if needed."""
    day = normalize_date(period_date)
    period = _find(day)
    if period is not None:
        return period
    start, end = period_bounds(day.year, day.month)
    period = AccountingPeriod(
        year=day.year, month=day.month, start_date=start, end_date=end, status=OPEN
    )
    db.session.add(period)
    try:
        db.session.flush()
    except Exception:
        # Another worker created it first (unique tenant/year/month or a race
        # on a NULL tenant); re-read instead of failing the posting.
        db.session.rollback()
        period = _find(day)
        if period is None:
            raise
    return period


def period_for(period_date=None) -> AccountingPeriod:
    return get_or_create_period(period_date)


def assert_can_post(period_date=None, *, actor=None) -> AccountingPeriod:
    """Return the period, raising :class:`PeriodLocked` when it is locked."""
    day = normalize_date(period_date)
    period = get_or_create_period(day)
    if (period.status or OPEN) == LOCKED:
        raise PeriodLocked(period)
    return period


def ensure_current_period() -> AccountingPeriod:
    return get_or_create_period(datetime.now().date())


def list_periods(*, limit: int = 240) -> list[AccountingPeriod]:
    return (
        AccountingPeriod.query.order_by(
            AccountingPeriod.year.desc(), AccountingPeriod.month.desc()
        )
        .limit(limit)
        .all()
    )


def _audit(action: str, period, actor=None, detail: str | None = None) -> None:
    db.session.add(LedgerSyncEvent(
        action=action,
        source_type="AccountingPeriod",
        source_id=getattr(period, "id", None),
        event=(period.status if period is not None else None),
        detail=(detail or "")[:500] or None,
        actor=(actor or "")[:80] or None,
    ))


def lock_period(period, *, actor=None, note: str | None = None) -> AccountingPeriod:
    if period is None:
        raise ValueError("Period not found.")
    if (period.status or OPEN) == LOCKED:
        return period
    period.status = LOCKED
    period.locked_by = (actor or "")[:80] or None
    period.locked_at = datetime.now()
    period.note = (note or period.note or "")[:500] or None
    _audit("lock", period, actor, note)
    db.session.flush()
    log.info("Locked accounting period %s", period_label(period))
    return period


def soft_close_period(period, *, actor=None, note: str | None = None) -> AccountingPeriod:
    if period is None:
        raise ValueError("Period not found.")
    if (period.status or OPEN) == LOCKED:
        raise PeriodLocked(period, f"{period_label(period)} is locked and cannot be soft-closed.")
    period.status = SOFT_CLOSED
    period.note = (note or period.note or "")[:500] or None
    _audit("soft_close", period, actor, note)
    db.session.flush()
    return period


def reopen_period(period, *, actor=None, reason: str = "") -> AccountingPeriod:
    if period is None:
        raise ValueError("Period not found.")
    if not (reason or "").strip():
        raise ValueError("A reason is required to reopen a closed period.")
    period.status = OPEN
    period.reopened_by = (actor or "")[:80] or None
    period.reopened_at = datetime.now()
    period.locked_by = None
    period.locked_at = None
    period.note = (reason or "")[:500]
    _audit("reopen", period, actor, reason)
    db.session.flush()
    log.warning("Reopened accounting period %s by %s: %s", period_label(period), actor, reason)
    return period


def set_period_result(period, result_minor: int, *, actor=None) -> AccountingPeriod:
    period.closed_result_minor = int(result_minor or 0)
    _audit("close_result", period, actor, f"result_minor={int(result_minor or 0)}")
    db.session.flush()
    return period
