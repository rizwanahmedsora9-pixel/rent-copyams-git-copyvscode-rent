"""End-to-end tests for the daily Cash Flow reconciliation workflow:

1. Pick a date range ending on day X -> tiles show closing for X.
2. Save physical cash for day X.
3. Any period starting X+1 opens with that physical count AUTOMATICALLY.
4. The carry-forward keeps rolling through days without a count.
5. Saving a reconciliation while display filters are active must still
   store the FULL (unfiltered) closing, not the filtered one.
"""
from __future__ import annotations

from datetime import date, datetime


ADMIN = {"username": "Admin", "password": "Admin@fbm12345"}


def _login(client):
    return client.post("/login", data=dict(ADMIN), follow_redirects=True)


def _mk_cash_account(db, Account, opening=0.0):
    acc = Account(
        name="FBM CASH IN HAND",
        type="CASH",
        account_type="company",
        category="cash",
        balance=opening,
        opening_balance=opening,
        opening_balance_date=datetime(2026, 1, 1),
        is_active=True,
    )
    db.session.add(acc)
    db.session.commit()
    return acc


def _record(direction, amount, account, day, **kw):
    from app.services.cash_flow_svc import save_manual_cash_flow_entry
    entry, created = save_manual_cash_flow_entry(
        direction=direction,
        amount=amount,
        account_id=account.id,
        category_name=kw.pop("category", "SALE" if direction == "in" else "EXPENSE"),
        party_name=kw.pop("party", "TEST PARTY"),
        party_type="person",
        date_posted=datetime(day.year, day.month, day.day, 12, 0, 0),
        create_missing=True,
        **kw,
    )
    assert created
    return entry


def test_full_daily_reconciliation_chain(app):
    """Day 1 count -> Day 2 opening -> rolls forward to Day 3 automatically."""
    from models import db, Account, CashFlowDifferenceAdjustment
    from app.services.cash_flow_svc import _automatic_cash_opening_balance

    with app.app_context():
        acc = _mk_cash_account(db, Account, opening=2230.0)
        d1, d2, d3 = date(2026, 8, 25), date(2026, 8, 26), date(2026, 8, 27)

        # Day 1: +5,000 in, -1,000 out (manual Cash Flow entries)
        _record("in", 5000, acc, d1)
        _record("out", 1000, acc, d1)
        db.session.commit()

        # Opening for day 1 = account opening balance only (no prior movement)
        assert _automatic_cash_opening_balance(d1) == 2230.0

        # System closing for day 1 = 2,230 + 5,000 - 1,000 = 6,230.
        # Physical count finds 6,000 (Rs. 230 short) -> save it.
        db.session.add(CashFlowDifferenceAdjustment(
            adjustment_date=d1,
            amount=-230.0,
            difference=-230.0,
            calculated_closing=6230.0,
            physical_cash_available=6000.0,
        ))
        db.session.commit()

        # DAY 2 OPENING = the physical count, automatically.
        assert _automatic_cash_opening_balance(d2) == 6000.0

        # Day 2: +2,000 in, no count taken that evening.
        _record("in", 2000, acc, d2)
        db.session.commit()

        # DAY 3 OPENING rolls forward automatically: 6,000 + 2,000 = 8,000.
        assert _automatic_cash_opening_balance(d3) == 8000.0

        # A newer physical count replaces the roll-forward.
        db.session.add(CashFlowDifferenceAdjustment(
            adjustment_date=d2,
            amount=0.0,
            difference=0.0,
            calculated_closing=8000.0,
            physical_cash_available=8000.0,
        ))
        db.session.commit()
        assert _automatic_cash_opening_balance(d3) == 8000.0


def test_manual_entries_and_transfers_in_opening_rollforward(app):
    """Manual CF entries count via their mirror AccountTransaction exactly
    once; internal transfers do not change the company-wide opening."""
    from models import db, Account
    from app.services.cash_flow_svc import (
        _automatic_cash_opening_balance,
        save_manual_cash_flow_entry,
    )

    with app.app_context():
        acc = _mk_cash_account(db, Account, opening=0.0)
        bank = Account(
            name="BANK ALFLAH", type="BANK", account_type="company",
            category="bank", balance=0.0, opening_balance=0.0, is_active=True,
        )
        db.session.add(bank)
        db.session.commit()

        d1, d2 = date(2026, 8, 25), date(2026, 8, 26)
        _record("in", 10000, acc, d1)
        # transfer cash -> bank must NOT change company-wide opening
        save_manual_cash_flow_entry(
            direction="transfer", amount=4000, account_id=acc.id,
            destination_account_id=bank.id,
            date_posted=datetime(2026, 8, 25, 15, 0, 0),
            create_missing=True,
        )
        db.session.commit()

        # Opening for day 2: 10,000 in, transfer ignored (company-wide).
        assert _automatic_cash_opening_balance(d2) == 10000.0
        # But the cash account itself moved: 10,000 - 4,000 = 6,000
        assert float(acc.balance) == 6000.0
        assert float(bank.balance) == 4000.0


def test_cash_flow_report_rejects_writes(app, client):
    """Cash Flow is now a report; it cannot save transactions or reconciliations."""
    from models import db, Account, CashFlowDifferenceAdjustment

    with app.app_context():
        acc = _mk_cash_account(db, Account, opening=0.0)
        d1 = date(2026, 8, 25)
        _record("in", 5000, acc, d1)
        _record("out", 1000, acc, d1)
        db.session.commit()
        acc_id = acc.id

    _login(client)

    # Attempts to post any data through the reporting page are rejected.
    resp = client.post(
        "/cash_flow?from_date=2026-08-25&to_date=2026-08-25&filter_type=received",
        data={
            "action": "save_reconciliation",
            "adjustment_date": "2026-08-25",
            "physical_cash_available": "4000",
            "reconciliation_reason": "evening count",
        },
    )
    assert resp.status_code == 405

    with app.app_context():
        assert CashFlowDifferenceAdjustment.query.filter_by(adjustment_date=d1).first() is None


def test_today_view_uses_automatic_carry_forward(app, client):
    """Default (today) view must open with yesterday's physical count and
    show entries recorded earlier today — no forced Rs. 0 fresh start."""
    from datetime import timedelta
    from models import db, Account, CashFlowDifferenceAdjustment
    from app.services.time_money import pk_now

    with app.app_context():
        acc = _mk_cash_account(db, Account, opening=0.0)
        today = pk_now().date()
        yesterday = today - timedelta(days=1)
        db.session.add(CashFlowDifferenceAdjustment(
            adjustment_date=yesterday,
            amount=0.0,
            difference=0.0,
            calculated_closing=7777.0,
            physical_cash_available=7777.0,
        ))
        db.session.commit()

        # An entry recorded earlier today (before the page is opened).
        from app.services.cash_flow_svc import save_manual_cash_flow_entry
        save_manual_cash_flow_entry(
            direction="in", amount=3333, account_id=acc.id,
            category_name="SALE", party_name="TODAY CLIENT",
            party_type="person", date_posted=pk_now(),
            create_missing=True,
        )
        db.session.commit()

    _login(client)
    resp = client.get("/cash_flow")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # Opening = yesterday's physical count, automatically.
    assert "7,777" in html
    # Today's earlier entry is visible.
    assert "3,333" in html
    # Fresh-start banner is NOT shown by default.
    assert "Fresh start mode" not in html


def test_cash_flow_report_does_not_mutate_fresh_start_state(app, client):
    """The report cannot mutate session/report state through POST actions."""
    from models import db, Account
    from app.services.time_money import pk_now

    with app.app_context():
        acc = _mk_cash_account(db, Account, opening=0.0)
        from app.services.cash_flow_svc import save_manual_cash_flow_entry
        from datetime import timedelta
        save_manual_cash_flow_entry(
            direction="in", amount=4444, account_id=acc.id,
            category_name="SALE", party_name="EARLIER TODAY",
            party_type="person",
            # clearly before the fresh-start cutoff (stored with second
            # precision), avoiding a same-second race in fast test runs
            date_posted=pk_now() - timedelta(seconds=5),
            create_missing=True,
        )
        db.session.commit()

    _login(client)
    resp = client.post("/cash_flow", data={"action": "reset_fresh_start"})
    assert resp.status_code == 405
    report = client.get("/cash_flow")
    assert report.status_code == 200
    html = report.get_data(as_text=True)
    assert "Fresh start mode" not in html
    assert "4,444" in html
