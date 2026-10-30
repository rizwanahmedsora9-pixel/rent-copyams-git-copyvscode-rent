"""Day-close / daily reconciliation and atomic permission regression tests.

Day close (Daily Cash & Bank Reconciliation board):
1. A verified & locked day is frozen — saving or clearing a counted value
   behind the lock used to silently rewrite the frozen figure that feeds the
   next day's opening.  It must refuse with a clear message.
2. Counted amounts with thousands separators ('5,000') were rejected by a
   bare float() and looked like "the edit sometimes does not stick".
3. Locking a day must carry the locked counted closing into the next day's
   opening.
4. Back-dated transactions into a locked day must surface as a visible
   drift instead of silently diverging.

Atomic permissions:
5. Day close requires the can_close_cash_day switch (admin/root bypass).
6. Account settlement (reconcile + adjustment posting) requires
   can_reconcile_accounts.
7. Account master create requires can_manage_account_masters (was
   hard-wired to admin/root).
8. Void / delete / restore of payments requires can_void_transactions on
   top of the module grant (default True preserves existing behaviour).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from werkzeug.security import generate_password_hash

ADMIN = {"username": "Admin", "password": "Admin@fbm12345"}


def _login(client, username="Admin", password="Admin@fbm12345"):
    resp = client.post("/login", data={"username": username, "password": password},
                       follow_redirects=False)
    assert resp.status_code in (302, 303)


def _make_user(app, username, password, **perms):
    from models import User, db
    with app.app_context():
        user = User(username=username, password_hash=generate_password_hash(password),
                    role="user", status="active", **perms)
        db.session.add(user)
        db.session.commit()
        return user.id


def _cash_account(app, name="Main Cash", balance=10_000.0):
    from models import Account, db
    with app.app_context():
        acc = Account(
            name=name, type="cash", category="cash", account_type="company",
            balance=balance, balance_minor=int(round(balance * 100)),
            opening_balance=balance, opening_balance_minor=int(round(balance * 100)),
            opening_balance_date=datetime.now() - timedelta(days=30),
            is_active=True,
        )
        db.session.add(acc)
        db.session.commit()
        return acc.id


def _dr_post(client, account_id, counted, day=None, action=None):
    data = {"day": day, "account_id": str(account_id)}
    if action:
        data["action"] = action
    else:
        data["action"] = "save_count"
        data["counted"] = counted
    return client.post(
        "/daily_reconciliation",
        data=data,
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# Day close
# ---------------------------------------------------------------------------

def test_locked_day_refuses_new_counts(app, client):
    from app.services import cash_day_recon as recon

    account_id = _cash_account(app)
    _login(client)
    day = datetime.now().strftime("%Y-%m-%d")

    first = _dr_post(client, account_id, "9500", day=day)
    assert first.status_code == 200
    assert first.get_json()["ok"] is True

    lock = _dr_post(client, account_id, None, day=day, action="lock")
    assert lock.status_code == 200
    assert lock.get_json()["ok"] is True

    # Service-level guard: the frozen count must not change behind the lock.
    with app.app_context():
        with pytest.raises(ValueError, match="locked"):
            recon.save_count(day, account_id, 9000)
        with pytest.raises(ValueError, match="locked"):
            recon.delete_count(day, account_id)

    # Route-level guard with a readable message (JSON board flow).
    second = _dr_post(client, account_id, "9000", day=day)
    payload = second.get_json()
    assert second.status_code == 400
    assert "locked" in (payload.get("message") or "")

    clear = _dr_post(client, account_id, None, day=day, action="clear_count")
    assert "locked" in (clear.get_json().get("message") or "")


def test_save_count_accepts_thousand_separated_amount(app, client):
    from models import CashDayAccountPosition, db

    account_id = _cash_account(app)
    _login(client)
    day = datetime.now().strftime("%Y-%m-%d")

    resp = _dr_post(client, account_id, "9,500.50", day=day)
    payload = resp.get_json()
    assert resp.status_code == 200, payload
    assert payload["ok"] is True
    with app.app_context():
        pos = CashDayAccountPosition.query.filter_by(
            position_date=datetime.strptime(day, "%Y-%m-%d").date(),
            account_id=account_id,
        ).first()
        assert pos is not None and pos.counted == pytest.approx(9500.5)


def test_locked_count_becomes_next_day_opening(app, client):
    from app.services import cash_day_recon as recon

    account_id = _cash_account(app, balance=10_000.0)
    _login(client)
    today = datetime.now().date()
    day = today.strftime("%Y-%m-%d")

    assert _dr_post(client, account_id, "9500", day=day).status_code == 200
    assert _dr_post(client, account_id, None, day=day, action="lock").status_code == 200

    nxt = today + timedelta(days=1)
    with app.app_context():
        positions = {p["account_id"]: p for p in recon.account_positions_for_date(nxt)}
    assert positions[account_id]["opening"] == pytest.approx(9500.0)


def test_board_flags_ledger_drift_after_lock(app, client):
    from models import AccountTransaction, db
    from app.services import cash_day_recon as recon

    account_id = _cash_account(app, balance=10_000.0)
    _login(client)
    day = datetime.now().strftime("%Y-%m-%d")

    assert _dr_post(client, account_id, None, day=day, action="lock").status_code == 200
    with app.app_context():
        before = {p["account_id"]: p for p in recon.account_positions_for_date(day)}
    assert before[account_id]["drift_after_lock"] == 0

    # A back-dated transaction into the locked day moves the ledger expected
    # closing away from the frozen snapshot.
    with app.app_context():
        db.session.add(AccountTransaction(
            from_account_id=account_id, to_account_id=None,
            amount=250.0, transaction_type="Expense",
            description="Late fuel entry",
            date_posted=datetime.strptime(day, "%Y-%m-%d").replace(hour=12),
        ))
        db.session.commit()

    with app.app_context():
        after = {p["account_id"]: p for p in recon.account_positions_for_date(day)}
    assert after[account_id]["drift_after_lock"] == pytest.approx(-250.0)
    assert after[account_id]["expected_at_lock"] is not None
    # The locked count is still the next-day opening basis (lock semantics).
    assert after[account_id]["counted"] == pytest.approx(
        before[account_id]["expected_closing"])


# ---------------------------------------------------------------------------
# Atomic permissions
# ---------------------------------------------------------------------------

def test_day_close_requires_can_close_cash_day(app, client):
    account_id = _cash_account(app)
    _make_user(app, "dayclose", "DayClose@123",
               can_view_cash_flow=True, can_close_cash_day=False)
    day = datetime.now().strftime("%Y-%m-%d")

    _login(client, "dayclose", "DayClose@123")
    denied = _dr_post(client, account_id, "9000", day=day)
    assert denied.status_code == 403
    assert "Day Close" in (denied.get_json().get("message") or "")

    from models import User, db
    with app.app_context():
        user = User.query.filter_by(username="dayclose").first()
        user.can_close_cash_day = True
        db.session.commit()

    allowed = _dr_post(client, account_id, "9000", day=day)
    assert allowed.status_code == 200
    assert allowed.get_json()["ok"] is True


def test_reconcile_requires_can_reconcile_accounts(app, client):
    from models import AccountReconciliation, User, db

    account_id = _cash_account(app)
    _make_user(app, "settler", "Settle@123",
               can_manage_accounts=True, can_reconcile_accounts=False)

    _login(client, "settler", "Settle@123")
    denied = client.post(
        f"/accounts/{account_id}/reconcile",
        data={"actual_balance": "10000", "note": "attempt"},
        follow_redirects=True,
    )
    body = denied.get_data(as_text=True)
    assert "Account Reconciliation Settlement" in body
    with app.app_context():
        assert AccountReconciliation.query.filter_by(account_id=account_id).count() == 0

    with app.app_context():
        user = User.query.filter_by(username="settler").first()
        user.can_reconcile_accounts = True
        db.session.commit()

    allowed = client.post(
        f"/accounts/{account_id}/reconcile",
        data={"actual_balance": "10000", "note": "settled"},
        follow_redirects=True,
    )
    assert "reconciled" in allowed.get_data(as_text=True).lower()
    with app.app_context():
        assert AccountReconciliation.query.filter_by(account_id=account_id).count() == 1


def test_account_master_requires_can_manage_account_masters(app, client):
    _make_user(app, "acctmaster", "AcctM@123",
               can_manage_accounts=True, can_manage_account_masters=False)

    _login(client, "acctmaster", "AcctM@123")
    denied = client.get("/accounts/accounts/add")
    assert denied.status_code == 403

    from models import User, db
    with app.app_context():
        user = User.query.filter_by(username="acctmaster").first()
        user.can_manage_account_masters = True
        db.session.commit()

    allowed = client.get("/accounts/accounts/add", follow_redirects=True)
    assert allowed.status_code == 200
    assert b"New account" in allowed.data


def test_void_permission_gates_payment_delete(app, client):
    from models import Client, Payment, User, db

    with app.app_context():
        client_row = Client(code="VOID-1", name="Void Client", is_active=True)
        db.session.add(client_row)
        db.session.flush()
        payment = Payment(client_id=client_row.id, client_name=client_row.name,
                          amount=500.0, payment_type="Receipt", method="Cash")
        db.session.add(payment)
        db.session.commit()
        payment_id = payment.id

    _make_user(app, "clerk", "Clerk@123", can_manage_payments=True)

    _login(client, "clerk", "Clerk@123")
    # Default can_void_transactions=True preserves today's behaviour.
    voided = client.post(f"/accounts/payments/clients/void/{payment_id}",
                         follow_redirects=True)
    assert voided.status_code == 200
    with app.app_context():
        assert db.session.get(Payment, payment_id).is_void is True

    restored = client.post(f"/accounts/payments/clients/restore/{payment_id}",
                           follow_redirects=True)
    assert restored.status_code == 200
    with app.app_context():
        assert db.session.get(Payment, payment_id).is_void is False

    # Remove the atomic void switch: module manager can no longer delete.
    with app.app_context():
        user = User.query.filter_by(username="clerk").first()
        user.can_void_transactions = False
        db.session.commit()

    blocked = client.post(f"/accounts/payments/clients/void/{payment_id}",
                          follow_redirects=True)
    assert "Void / Delete / Restore" in blocked.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Payment, payment_id).is_void is False


def test_settings_page_renders_role_presets(app, client):
    _login(client)
    page = client.get("/settings")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    for preset in ("Cashier", "Accountant", "Manager", "Viewer (read only)"):
        assert preset in body
    assert "ROLE_PRESET_DATA" in body
    assert "can_close_cash_day" in body
    assert "can_reconcile_accounts" in body
    assert "can_manage_account_masters" in body
    assert "can_void_transactions" in body
