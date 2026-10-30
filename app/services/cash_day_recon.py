"""Daily Cash & Bank Reconciliation service.

Implements the per-account "Account Positions" board shown on the reference
layout (references-images/): for a financial day each company money account
shows OPENING / IN / OUT / TRANSFER IN / TRANSFER OUT / EXPECTED CLOSING plus
the user's ACTUAL COUNTED figure and the DIFFERENCE.

Locking a day freezes the per-account counted figures; the locked counted
closing for an account becomes that account's opening on the next day, which
is exactly the "locked amount shows as next day opening balances" behaviour.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from models import (
    db, Account, CashDayLock, CashDayAccountPosition,
)
from utils.money import from_minor
from app.services.time_money import _money_round, pk_now, pk_today
from app.services.cash_flow_svc import (
    CF_DIR_IN, CF_DIR_OUT, CF_DIR_TRANSFER,
    collect_cash_flow_rows, _cf_company_accounts, _cf_account_label,
)

_EPOCH = "0001-01-01"


def _to_date(value) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _parse_counted(raw) -> float:
    """Parse a counted balance the way the rest of the app parses money.

    Accepts plain numbers and thousands-separated text ('1,500', '50 000').
    Anything unreadable (or infinite) raises ValueError with a readable
    message — the old code used bare ``float()`` in the route, so pasting a
    comma-formatted figure failed with a generic 'Enter a valid counted
    amount' even though the value was fine.
    """
    text_value = str(raw if raw is not None else "").strip()
    if not text_value:
        raise ValueError("Enter the counted amount.")
    cleaned = text_value.replace(",", "").replace(" ", "")
    try:
        value = float(cleaned)
    except (TypeError, ValueError):
        raise ValueError(f"'{text_value}' is not a valid amount. Use digits only, e.g. 1500.")
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("Enter a valid finite amount.")
    return _money_round(value)


def _day_is_locked(day) -> bool:
    """A verified & locked day is frozen: counts may not change silently."""
    return get_day_lock(day) is not None


def _account_baseline(account, as_of: date) -> float:
    """Opening balance of an account if its opening date is on/before as_of."""
    ob_date = getattr(account, "opening_balance_date", None)
    if ob_date is not None:
        ob_day = ob_date.date() if hasattr(ob_date, "date") else ob_date
        if ob_day > as_of:
            return 0.0
    if getattr(account, "opening_balance_minor", None) is not None:
        return float(from_minor(account.opening_balance_minor))
    return float(account.opening_balance or 0)


def _activity_between(from_date: str, to_date: str) -> dict:
    """Per-account movement totals for a date window (inclusive)."""
    act = {}
    if from_date > to_date:
        return act
    rows = collect_cash_flow_rows(from_date, to_date)
    for r in rows:
        if (r.get("status") or "active") != "active":
            continue
        rtype = r.get("type")
        aid = r.get("account_id")
        if aid:
            a = act.setdefault(aid, {"in": 0.0, "out": 0.0, "tin": 0.0, "tout": 0.0})
            if rtype == CF_DIR_IN:
                a["in"] += float(r.get("cash_in") or 0)
            elif rtype == CF_DIR_OUT:
                a["out"] += float(r.get("cash_out") or 0)
            elif rtype == CF_DIR_TRANSFER:
                a["tout"] += float(r.get("transfer_amount") or 0)
        aid2 = r.get("account_to_id")
        if aid2 and rtype == CF_DIR_TRANSFER:
            b = act.setdefault(aid2, {"in": 0.0, "out": 0.0, "tin": 0.0, "tout": 0.0})
            b["tin"] += float(r.get("transfer_amount") or 0)
    return act


def account_positions_for_date(day) -> list:
    """Return the per-account position dicts for a financial day."""
    day = _to_date(day)
    day_str = day.strftime("%Y-%m-%d")
    prev_str = (day - timedelta(days=1)).strftime("%Y-%m-%d")
    accounts = _cf_company_accounts(active_only=True)

    saved = {
        p.account_id: p
        for p in CashDayAccountPosition.query.filter_by(position_date=day).all()
    }
    locks = {}
    prior_locked = CashDayAccountPosition.query.filter(
        CashDayAccountPosition.is_locked.is_(True),
        CashDayAccountPosition.position_date < day,
        CashDayAccountPosition.account_id.in_([a.id for a in accounts] or [0]),
    ).order_by(CashDayAccountPosition.position_date.asc()).all()
    for p in prior_locked:
        locks[p.account_id] = p

    # Group accounts by the window that feeds their opening so we only call
    # collect_cash_flow_rows once per distinct window.
    windows = {}
    for acc in accounts:
        lk = locks.get(acc.id)
        if lk is not None:
            start = (lk.position_date + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            start = _EPOCH
        windows.setdefault((start, prev_str), []).append(acc)

    opening_by_acc = {}
    for (start, end), accs in windows.items():
        act = _activity_between(start, end)
        for acc in accs:
            lk = locks.get(acc.id)
            base = float(lk.counted or 0) if lk is not None else _account_baseline(acc, day)
            a = act.get(acc.id, {"in": 0.0, "out": 0.0, "tin": 0.0, "tout": 0.0})
            opening_by_acc[acc.id] = _money_round(
                base + a["in"] - a["out"] + a["tin"] - a["tout"]
            )

    day_act = _activity_between(day_str, day_str)

    positions = []
    for acc in accounts:
        opening = opening_by_acc.get(acc.id, 0.0)
        a = day_act.get(acc.id, {"in": 0.0, "out": 0.0, "tin": 0.0, "tout": 0.0})
        amount_in = _money_round(a["in"])
        amount_out = _money_round(a["out"])
        transfer_in = _money_round(a["tin"])
        transfer_out = _money_round(a["tout"])
        expected = _money_round(
            opening + amount_in - amount_out + transfer_in - transfer_out
        )
        s = saved.get(acc.id)
        counted = float(s.counted) if s is not None and s.counted is not None else None
        is_locked = bool(s.is_locked) if s is not None else False
        eff_counted = counted if counted is not None else (expected if is_locked else None)
        difference = (
            _money_round(eff_counted - expected) if eff_counted is not None else 0.0
        )
        # After a day is locked, any transaction back-dated into that day moves
        # the recomputed expected closing away from the frozen snapshot.  The
        # locked count still carries to the next opening (that is the lock's
        # purpose), but the board must show the drift instead of hiding it.
        expected_at_lock = float(s.expected_closing) if (is_locked and s is not None and s.expected_closing is not None) else None
        drift_after_lock = (
            _money_round(expected - expected_at_lock)
            if expected_at_lock is not None else 0.0
        )
        account_category = (acc.category or "").lower()
        # Daily reconciliation already has dedicated balance columns.  Keep the
        # account title clean here; showing the live current balance in the
        # account label makes the row noisy and can be confused with the dated
        # opening/closing figures on this board.
        positions.append({
            "account_id": acc.id,
            "account_name": acc.name or _cf_account_label(acc),
            "category": account_category,
            "category_label": account_category.upper(),
            "opening": opening,
            "in": amount_in,
            "out": amount_out,
            "transfer_in": transfer_in,
            "transfer_out": transfer_out,
            "expected_closing": expected,
            "expected_at_lock": expected_at_lock,
            "drift_after_lock": drift_after_lock,
            "counted": counted,
            "effective_counted": eff_counted,
            "difference": difference,
            "is_locked": is_locked,
        })
    return positions


def get_day_lock(day):
    return CashDayLock.query.filter_by(lock_date=_to_date(day)).first()


def save_count(day, account_id, counted, actor=None):
    """Store the user-entered physical count for one account on a day.

    A verified & locked day is frozen — the count feeding the next day's
    opening must not change silently behind the lock.  Unlock the day first
    (the counted values survive as drafts), correct, then lock again.
    """
    day = _to_date(day)
    if _day_is_locked(day):
        raise ValueError(
            f"Day {day.strftime('%Y-%m-%d')} is locked. Unlock it first if you need to correct a count."
        )
    counted_value = _parse_counted(counted)
    actor_name = getattr(actor, "username", None) or actor
    pos = CashDayAccountPosition.query.filter_by(
        position_date=day, account_id=account_id
    ).first()
    if pos is None:
        acc = Account.query.get(account_id)
        pos = CashDayAccountPosition(
            position_date=day,
            account_id=account_id,
            account_name=(acc.name if acc else ""),
        )
        db.session.add(pos)
    pos.counted = counted_value
    pos.difference = None  # recomputed on read
    pos.updated_by = actor_name
    db.session.commit()
    return pos


def lock_day(day, actor=None, note=""):
    """Verify & lock a financial day.

    Every account's counted figure is frozen (defaulting to its expected
    closing when not manually counted) and a day-level lock row is written.
    The locked counted totals become the next day's opening positions.
    """
    day = _to_date(day)
    actor_name = getattr(actor, "username", None) or actor
    now = pk_now()
    positions = account_positions_for_date(day)

    total_expected = 0.0
    total_counted = 0.0
    for p in positions:
        counted = p["counted"] if p["counted"] is not None else p["expected_closing"]
        total_expected += p["expected_closing"]
        total_counted += counted
        pos = CashDayAccountPosition.query.filter_by(
            position_date=day, account_id=p["account_id"]
        ).first()
        if pos is None:
            pos = CashDayAccountPosition(
                position_date=day,
                account_id=p["account_id"],
                account_name=p["account_name"],
            )
            db.session.add(pos)
        pos.opening = p["opening"]
        pos.amount_in = p["in"]
        pos.amount_out = p["out"]
        pos.transfer_in = p["transfer_in"]
        pos.transfer_out = p["transfer_out"]
        pos.expected_closing = p["expected_closing"]
        pos.counted = counted
        pos.difference = _money_round(counted - p["expected_closing"])
        pos.is_locked = True
        pos.locked_by = actor_name
        pos.locked_at = now
        pos.updated_by = actor_name

    total_expected = _money_round(total_expected)
    total_counted = _money_round(total_counted)

    lock = get_day_lock(day)
    if lock is None:
        lock = CashDayLock(lock_date=day)
        db.session.add(lock)
    lock.total_expected = total_expected
    lock.total_counted = total_counted
    lock.difference = _money_round(total_counted - total_expected)
    lock.note = note or ""
    lock.locked_by = actor_name
    lock.locked_at = now
    db.session.commit()
    return lock


def unlock_day(day):
    day = _to_date(day)
    CashDayAccountPosition.query.filter_by(position_date=day).update({"is_locked": False})
    lock = get_day_lock(day)
    if lock:
        db.session.delete(lock)
    db.session.commit()


def delete_count(day, account_id):
    day = _to_date(day)
    if _day_is_locked(day):
        raise ValueError(
            f"Day {day.strftime('%Y-%m-%d')} is locked. Unlock it before clearing a counted value."
        )
    pos = CashDayAccountPosition.query.filter_by(
        position_date=day, account_id=account_id
    ).first()
    if pos:
        db.session.delete(pos)
        db.session.commit()
