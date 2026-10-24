"""Regression tests for the preflight watch list (tools/health/preflight_check.py).

Two watches fired on a perfectly healthy database after the full-cycle audit:

* **w7** compared ``account.balance`` with the raw transaction net and ignored
  the opening baseline, so every account opened with an opening balance showed
  up as "drift".
* **w2** counted a booking's derived pending bill (which deliberately carries
  the booking's own bill number) as a duplicate manual bill, so every normal
  booking with a due appeared on the watch list.

Both now fire only for real problems, which these tests pin down.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

from models import (
    Account,
    AccountTransaction,
    Booking,
    Client,
    PendingBill,
    db,
)

ROOT = Path(__file__).resolve().parents[1]


def _load_preflight():
    spec = importlib.util.spec_from_file_location(
        "ams_preflight", ROOT / "tools" / "health" / "preflight_check.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _watch(checks, watch_id):
    return [w for w in checks["watch"] if w["id"] == watch_id]


def _run(app_factory, tmp_path, seed):
    db_file = tmp_path / "preflight.db"
    app = app_factory(db_file)
    with app.app_context():
        seed()
        db.session.commit()
    con = sqlite3.connect(str(db_file))
    con.row_factory = sqlite3.Row
    try:
        return _load_preflight().run_checks(con)
    finally:
        con.close()


def test_watch_is_clean_for_opening_balance_and_derived_due(app_factory, tmp_path):
    def seed():
        account = Account(
            name="Main Cash", type="cash", category="cash", account_type="company",
            balance=160_000.0, opening_balance=200_000.0,
            opening_balance_minor=20_000_000, class_category="Assets",
        )
        booking = Booking(client_name="Bilal Construction", amount=28_000.0,
                          paid_amount=10_000.0, manual_bill_no="MB NO.BK-1")
        db.session.add_all([account, booking])
        db.session.flush()
        db.session.add_all([
            AccountTransaction(from_account_id=account.id, amount=50_000.0),
            AccountTransaction(to_account_id=account.id, amount=10_000.0),
            # The booking's own due row — same bill number, by design.
            PendingBill(client_code="C-1", client_name="Bilal Construction",
                        bill_no="MB NO.BK-1", amount=18_000.0, reason="booking"),
        ])

    checks = _run(app_factory, tmp_path, seed)
    assert _watch(checks, "w7_account_ledger_drift") == []
    assert _watch(checks, "w2_duplicate_manual_bills") == []


def test_watch_still_flags_real_account_drift(app_factory, tmp_path):
    def seed():
        account = Account(
            name="Main Cash", type="cash", category="cash", account_type="company",
            balance=159_500.0, opening_balance=200_000.0,
            opening_balance_minor=20_000_000, class_category="Assets",
        )
        db.session.add(account)
        db.session.flush()
        db.session.add_all([
            AccountTransaction(from_account_id=account.id, amount=50_000.0),
            AccountTransaction(to_account_id=account.id, amount=10_000.0),
        ])

    checks = _run(app_factory, tmp_path, seed)
    assert _watch(checks, "w7_account_ledger_drift"), "500 real drift must be reported"


def test_watch_still_flags_two_documents_sharing_a_bill(app_factory, tmp_path):
    def seed():
        db.session.add_all([
            Booking(client_name="Dup A", amount=100.0, manual_bill_no="MB NO.DUP-1"),
            Booking(client_name="Dup B", amount=200.0, manual_bill_no="MB NO.DUP-1"),
        ])
        db.session.add(Client(code="C-DUP", name="Dup Client"))

    checks = _run(app_factory, tmp_path, seed)
    assert _watch(checks, "w2_duplicate_manual_bills"), "two bookings sharing a bill must be reported"
