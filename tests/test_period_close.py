"""Periods: a locked month stops moving; corrections go to the open month.

The point of these tests is the owner's requirement "past months must not
change": once a month is locked, a new posting inside it is refused with a
message naming the period, and a reversal of something in it lands in the
current open month instead.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from models import (
    Account,
    Client,
    GRN,
    GRNItem,
    Supplier,
    db,
)
from app.services.ledger import accounts as coa
from app.services.ledger import close as close_svc
from app.services.ledger import periods as period_svc
from app.services.ledger import posting, posting_rules, query as q


def _codes():
    return {row["code"]: row for row in q.account_balances()}


@pytest.fixture()
def world(app):
    with app.app_context():
        supplier = Supplier(name="Ravi Cement Supplier", opening_balance=0)
        client = Client(code="CL-1", name="Bilal Construction", opening_balance=0)
        cash = Account(
            name="Main Cash", type="cash", category="cash", account_type="company",
            channel="cash", balance=0, balance_minor=0, is_active=True,
        )
        db.session.add_all([supplier, client, cash])
        db.session.commit()
        coa.ensure_chart()
        coa.ensure_cash_accounts()
        db.session.commit()
        return {"supplier": supplier.id, "client": client.id, "cash": cash.id}


def _grn(ids, when, *, qty=10, rate=1000, freight=0):
    grn = GRN(
        supplier_id=ids["supplier"],
        supplier="Ravi Cement Supplier",
        manual_bill_no=f"G-{int(when.timestamp()) if hasattr(when, 'timestamp') else qty}",
        date_posted=datetime.combine(when, datetime.min.time()) if isinstance(when, date) else when,
        freight_cost=freight,
        paid_amount=0,
    )
    grn.items = [GRNItem(mat_name="OPC Cement", qty=qty, price_at_time=rate)]
    db.session.add(grn)
    db.session.commit()
    return grn.id


def test_locking_a_period_blocks_new_postings(world, app):
    with app.app_context():
        day = date(2024, 3, 15)
        period = period_svc.get_or_create_period(day)
        close_svc.close_period(period, actor="tester", lock=True)
        db.session.commit()
        assert period.status == period_svc.LOCKED

        with pytest.raises(period_svc.PeriodLocked) as excinfo:
            _grn(world, day, qty=1, rate=100)
        assert "March 2024" in str(excinfo.value)
        db.session.rollback()

        # A different, open month still posts.
        _grn(world, date.today(), qty=1, rate=100)
        assert _codes()["1200"]["debit"] >= 100_00


def test_soft_closed_still_posts_but_flags_adjustments(world, app):
    with app.app_context():
        day = date(2024, 4, 10)
        period = period_svc.get_or_create_period(day)
        period_svc.soft_close_period(period, actor="tester")
        db.session.commit()
        _grn(world, day, qty=2, rate=500)
        entries = [
            entry for entry in posting.entries_for("GRN", 1)
        ]
        assert entries and all(entry.is_adjustment for entry in entries)


def test_reversal_of_a_locked_month_posts_into_the_open_month(world, app):
    with app.app_context():
        day = date(2024, 5, 12)
        grn_id = _grn(world, day, qty=4, rate=1000)
        assert _codes()["1200"]["balance"] == 4_000_00
        period = period_svc.get_or_create_period(day)
        close_svc.close_period(period, actor="tester", lock=True)
        db.session.commit()

        grn = db.session.get(GRN, grn_id)
        grn.is_void = True
        db.session.commit()

        entries = posting.entries_for("GRN", grn_id)
        assert len(entries) == 2
        mirror = entries[1]
        assert mirror.is_reversal_of == entries[0].id
        assert mirror.entry_date == date.today()
        assert mirror.period_id != period.id
        # The month's own figure is untouched; the cumulative balance is zero.
        assert q.profit_and_loss(start=period.start_date, end=period.end_date)["cost_total"] == 0
        assert _codes()["1200"]["balance"] == 0


def test_reopen_requires_a_reason(world, app):
    with app.app_context():
        period = period_svc.get_or_create_period(date(2024, 6, 1))
        period_svc.lock_period(period, actor="tester")
        db.session.commit()
        with pytest.raises(ValueError):
            period_svc.reopen_period(period, actor="tester", reason="  ")
        period_svc.reopen_period(period, actor="tester", reason="late invoice found")
        db.session.commit()
        assert period.status == period_svc.OPEN
        assert period.reopened_by == "tester"


def test_close_records_the_month_result(world, app):
    with app.app_context():
        day = date(2024, 7, 5)
        _grn(world, day, qty=5, rate=1000)
        period = period_svc.get_or_create_period(day)
        close_svc.close_period(period, actor="tester", lock=True)
        db.session.commit()
        assert period.closed_result_minor == 0  # a purchase alone is not a result
        assert period.locked_by == "tester"
        assert period.closed_result_minor is not None


def test_close_refuses_when_a_structural_check_fails(world, app, monkeypatch):
    with app.app_context():
        day = date(2024, 8, 5)
        _grn(world, day, qty=1, rate=100)
        period = period_svc.get_or_create_period(day)
        monkeypatch.setattr(period_svc, "LOCKED", period_svc.LOCKED)
        real_checklist = close_svc.checklist
        monkeypatch.setattr(
            close_svc, "checklist",
            lambda p: [{"key": "double_entry", "title": "Every entry has two sides",
                        "ok": False, "detail": "simulated", "href": None}],
        )
        with pytest.raises(close_svc.CloseRefused):
            close_svc.close_period(period, actor="tester", lock=True)
        db.session.rollback()
        assert real_checklist is not None


def test_new_posting_into_a_locked_period_is_refused_by_the_engine(world, app):
    with app.app_context():
        day = date(2024, 9, 9)
        period = period_svc.get_or_create_period(day)
        period_svc.lock_period(period, actor="tester")
        db.session.commit()
        spec = posting_rules.EntrySpec(
            event="test",
            entry_date=day,
            description="manual",
            lines=[posting_rules.Line(code="1200", debit=100), posting_rules.Line(code="2010", credit=100)],
        )
        with pytest.raises(period_svc.PeriodLocked):
            posting.post_spec("Manual", 1, spec)
        db.session.rollback()
