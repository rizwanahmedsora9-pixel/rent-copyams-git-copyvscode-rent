from __future__ import annotations

from datetime import datetime


ADMIN = {"username": "Admin", "password": "Admin@fbm12345"}


def test_cash_flow_is_read_only_and_accounts_transactions_feed_report(app, client):
    from models import Account, AccountTransaction, db
    from app.services.cash_flow_svc import collect_cash_flow_rows

    with app.app_context():
        account = Account(
            name="Test Cash Account",
            type="CASH",
            account_type="company",
            category="cash",
            balance=0,
            opening_balance=0,
            opening_balance_date=datetime.now(),
            is_active=True,
        )
        db.session.add(account)
        db.session.commit()
        account_id = account.id

    client.post("/login", data=ADMIN, follow_redirects=True)

    # The report displays its read-only state and has no transaction composer.
    report = client.get("/cash_flow")
    assert report.status_code == 200
    assert b"View only" in report.data
    assert b'data-cf-tab="transaction"' not in report.data
    assert b'data-cf-tab="master"' not in report.data

    # Manual cash-flow posting/configuration endpoints are no longer write paths.
    assert client.post("/cash_flow", data={"action": "record_movement"}).status_code == 405
    assert client.post(
        "/cash_flow/meta/categories",
        json={"name": "Should Not Be Created", "direction": "out"},
    ).status_code == 405

    # Accounts is the canonical entry point for an independent cash receipt.
    posted = client.post(
        "/accounts/transactions/new",
        data={
            "tx_mode": "receive",
            "receive_from_category": "other_source",
            "receive_source_label": "Test Customer",
            "receive_account_id": str(account_id),
            "amount": "1250",
            "method": "Cash",
            "date_posted": datetime.now().strftime("%Y-%m-%dT%H:%M"),
            "note": "Account-only cash receipt",
        },
        follow_redirects=True,
    )
    assert posted.status_code == 200
    assert b"Receive transaction recorded successfully" in posted.data

    with app.app_context():
        tx = AccountTransaction.query.filter_by(
            to_account_id=account_id,
            description="Money received from Test Customer",
        ).first()
        assert tx is not None
        rows = collect_cash_flow_rows(
            tx.date_posted.strftime("%Y-%m-%d"),
            tx.date_posted.strftime("%Y-%m-%d"),
        )
        visible = [row for row in rows if row.get("tx_id") == tx.id]
        assert len(visible) == 1
        assert visible[0]["type"] == "in"
        assert visible[0]["amount"] == 1250


def test_legacy_cash_flow_entries_are_managed_from_accounts(app, client):
    from models import Account, AccountTransaction, CashFlowEntry, db
    from app.services.cash_flow_svc import save_manual_cash_flow_entry

    with app.app_context():
        account = Account(
            name="Legacy Cash Account",
            type="CASH",
            account_type="company",
            category="cash",
            balance=0,
            opening_balance=0,
            opening_balance_date=datetime.now(),
            is_active=True,
        )
        db.session.add(account)
        db.session.commit()
        entry, _ = save_manual_cash_flow_entry(
            direction="in",
            amount=500,
            account_id=account.id,
            category_name="Legacy receipt",
            description="Old Cash Flow entry",
            date_posted=datetime.now(),
            create_missing=True,
        )
        db.session.commit()
        entry_id, tx_id, account_id = entry.id, entry.account_tx_id, account.id

    client.post("/login", data=ADMIN, follow_redirects=True)
    edited = client.post(
        f"/accounts/transactions/{tx_id}/edit",
        data={"amount": "700", "description": "Edited in Accounts", "note": "legacy fix"},
        follow_redirects=True,
    )
    assert edited.status_code == 200

    with app.app_context():
        entry = db.session.get(CashFlowEntry, entry_id)
        tx = db.session.get(AccountTransaction, tx_id)
        account = db.session.get(Account, account_id)
        assert float(entry.amount) == 700
        assert float(tx.amount) == 700
        assert float(account.balance) == 700

    deleted = client.post(f"/accounts/transactions/{tx_id}/delete", follow_redirects=True)
    assert deleted.status_code == 200
    with app.app_context():
        assert db.session.get(CashFlowEntry, entry_id).is_void is True
        assert float(db.session.get(Account, account_id).balance) == 0

    restored = client.post(f"/accounts/transactions/{tx_id}/restore", follow_redirects=True)
    assert restored.status_code == 200
    with app.app_context():
        assert db.session.get(CashFlowEntry, entry_id).is_void is False
        assert float(db.session.get(Account, account_id).balance) == 700
