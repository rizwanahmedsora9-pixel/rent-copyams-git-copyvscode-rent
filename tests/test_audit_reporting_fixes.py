"""Regression tests for two reporting defects found in the full-app audit.

1. ``tools/consistency_report.py`` compared ``Account.balance`` against the raw
   ``account_transaction`` net and ignored the opening baseline.  Every account
   opened with an opening balance was therefore reported as a mismatch — a
   healthy Main Cash account (200,000 opening, 40,000 net movement, stored
   160,000) failed the "one-command health check" with a 200,000 difference.
2. ``templates/accounts/receipts.html`` showed a GRN's *landed costs only* in
   the Total column.  A 100-bag purchase of 120,000 displayed "Total Rs. 0.00"
   next to a 50,000 paid amount.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from models import Account, AccountTransaction, Client, GRN, GRNItem, Material, Supplier, db

ROOT = Path(__file__).resolve().parents[1]
ADMIN = {"username": "Admin", "password": "Admin@fbm12345"}


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _login(client):
    resp = client.post("/login", data=ADMIN, follow_redirects=False)
    assert resp.status_code in (302, 303)


def test_consistency_report_accepts_opening_balance(app_factory, tmp_path, monkeypatch):
    db_file = tmp_path / "opening.db"
    app = app_factory(db_file)
    with app.app_context():
        account = Account(
            name="Main Cash", type="cash", category="cash", account_type="company",
            balance=160_000.0, opening_balance=200_000.0,
            opening_balance_minor=20_000_000, class_category="Assets",
        )
        db.session.add(account)
        db.session.flush()
        # 50,000 paid out (GRN) and 10,000 received (booking) = -40,000 net.
        db.session.add_all([
            AccountTransaction(from_account_id=account.id, amount=50_000.0,
                               description="GRN payment"),
            AccountTransaction(to_account_id=account.id, amount=10_000.0,
                               description="Booking receipt"),
        ])
        db.session.commit()

    tool = _load_tool("consistency_report")
    monkeypatch.setattr(tool, "DB_PATH", db_file)
    monkeypatch.setattr(tool, "HEALTH_SNAPSHOT", tmp_path / "health_snapshot.json")
    report = tool.check_account_balances(tool.connect())
    assert report["status"] == "OK", report


def test_consistency_report_still_flags_real_drift(app_factory, tmp_path, monkeypatch):
    db_file = tmp_path / "drift.db"
    app = app_factory(db_file)
    with app.app_context():
        account = Account(
            name="Main Cash", type="cash", category="cash", account_type="company",
            # 500 short of opening (200,000) plus net movement (-40,000).
            balance=159_500.0, opening_balance=200_000.0,
            opening_balance_minor=20_000_000, class_category="Assets",
        )
        db.session.add(account)
        db.session.flush()
        db.session.add_all([
            AccountTransaction(from_account_id=account.id, amount=50_000.0,
                               description="GRN payment"),
            AccountTransaction(to_account_id=account.id, amount=10_000.0,
                               description="Booking receipt"),
        ])
        db.session.commit()

    tool = _load_tool("consistency_report")
    monkeypatch.setattr(tool, "DB_PATH", db_file)
    monkeypatch.setattr(tool, "HEALTH_SNAPSHOT", tmp_path / "health_snapshot.json")
    report = tool.check_account_balances(tool.connect())
    assert report["status"] == "FAIL"
    assert report["mismatches"][0]["difference"] == pytest.approx(-500.0)


def test_receipts_page_shows_full_grn_total(app, client):
    from app.services import grn_svc

    _login(client)
    with app.app_context():
        supplier = Supplier(name="Receipts Supplier", is_active=True)
        client_row = Client(code="RCPT-1", name="Receipts Client", is_active=True)
        db.session.add_all([supplier, client_row])
        db.session.flush()
        material = Material(name="Receipts Material", code="RCPT-MAT", unit="Bags")
        db.session.add(material)
        db.session.flush()
        grn = GRN(supplier=supplier.name, supplier_id=supplier.id,
                  manual_bill_no="MB NO.RCPT-GRN-1", paid_amount=50_000.0)
        db.session.add(grn)
        db.session.flush()
        db.session.add(GRNItem(grn_id=grn.id, mat_name=material.name,
                               qty=100, price_at_time=1_200))
        db.session.commit()
        expected = grn_svc.calculate_grn_total(grn)
        assert expected == pytest.approx(120_000.0)

    body = client.get("/accounts/receipts").get_data(as_text=True)
    assert "RCPT-GRN-1" in body
    assert "120,000.00" in body, "GRN total must include the received material value"
