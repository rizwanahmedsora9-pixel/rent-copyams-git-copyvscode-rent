"""Regression tests for the Accounts money-control fixes.

1. **Authoritative balance drift** — manual Receive/Pay entries used to bump
   only the float ``Account.balance`` and never the authoritative
   ``balance_minor`` that every other money path (payments, transfers,
   reconciliations) prefers.  The next minor-unit write silently discarded
   the manual change.  Every manual path must now move both columns
   together.
2. **Expenditures drill-down** — the KPI API filtered ``to_account_id
   IS NOT NULL`` (internal transfers / loan repayments) instead of the real
   expense rows, and labelled them "Other Receipt".
3. **Receipts page** — must be a true money-in register (client payments,
   sale receipts, booking advances, other receipts) and treat GRN paid as
   money OUT in the net movement instead of adding it to receipts.
4. **Expense detail capture** — category / paid-to / voucher / method are
   stored with the ledger entry and shown on the expenditures page.
"""
from __future__ import annotations

from datetime import datetime

import pytest

ADMIN = {"username": "Admin", "password": "Admin@fbm12345"}


def _login(client):
    resp = client.post("/login", data=ADMIN, follow_redirects=False)
    assert resp.status_code in (302, 303)


def _cash_account(**overrides):
    from models import Account, db

    account = Account(
        name=overrides.pop("name", "Main Cash"),
        type="cash",
        category="cash",
        account_type="company",
        balance=10_000.0,
        balance_minor=1_000_000,  # authoritative paisa baseline
        opening_balance=10_000.0,
        opening_balance_minor=1_000_000,
        opening_balance_date=datetime.now(),
        is_active=True,
    )
    for key, value in overrides.items():
        setattr(account, key, value)
    db.session.add(account)
    db.session.flush()
    return account


def test_expense_entry_moves_balance_and_minor_together(app, client):
    from models import Account, AccountTransaction, CashFlowCategory, db
    from utils.money import to_minor

    with app.app_context():
        db.session.add(CashFlowCategory(name="Utilities", direction="out", is_active=True))
        account = _cash_account()
        db.session.commit()
        account_id = account.id

    _login(client)
    posted = client.post(
        "/accounts/transactions/new",
        data={
            "tx_mode": "pay",
            "pay_from_account_id": str(account_id),
            "pay_target": "personal_expense",
            "amount": "400",
            "method": "Cash",
            "target_label": "Office tea",
            "expense_category": "Utilities",
            "payee": "Corner Shop",
            "expense_reference": "V-1024",
            "date_posted": datetime.now().strftime("%Y-%m-%dT%H:%M"),
            "note": "cash expense",
        },
        follow_redirects=True,
    )
    assert posted.status_code == 200
    assert b"Outgoing payment recorded successfully" in posted.data

    with app.app_context():
        account = db.session.get(Account, account_id)
        # balance AND balance_minor must move together (drift regression).
        assert account.balance == pytest.approx(9_600.0)
        assert account.balance_minor == 960_000

        tx = AccountTransaction.query.filter_by(
            from_account_id=account_id, transaction_type="Expense"
        ).first()
        assert tx is not None
        assert tx.amount_minor == to_minor(400)
        assert tx.category == "Utilities"
        assert tx.payee == "Corner Shop"
        assert tx.reference_no == "V-1024"
        assert tx.payment_method == "Cash"
        assert tx.created_by == "Admin"


def test_receive_other_source_moves_balance_and_minor_together(app, client):
    from models import Account, AccountTransaction, db

    with app.app_context():
        account = _cash_account(balance=1_000.0, balance_minor=100_000,
                                opening_balance=1_000.0, opening_balance_minor=100_000)
        db.session.commit()
        account_id = account.id

    _login(client)
    posted = client.post(
        "/accounts/transactions/new",
        data={
            "tx_mode": "receive",
            "receive_from_category": "other_source",
            "receive_source_label": "Owner deposit",
            "receive_account_id": str(account_id),
            "amount": "5000",
            "method": "Cash",
            "date_posted": datetime.now().strftime("%Y-%m-%dT%H:%M"),
        },
        follow_redirects=True,
    )
    assert posted.status_code == 200
    assert b"Receive transaction recorded successfully" in posted.data

    with app.app_context():
        account = db.session.get(Account, account_id)
        assert account.balance == pytest.approx(6_000.0)
        assert account.balance_minor == 600_000
        tx = AccountTransaction.query.filter_by(
            to_account_id=account_id, transaction_type="Receipt"
        ).first()
        assert tx is not None
        assert tx.payee == "Owner deposit"
        assert tx.payment_method == "Cash"
        assert tx.amount_minor == 500_000


def test_company_transfer_and_loan_repayment_move_minor_together(app, client):
    from models import Account, db

    with app.app_context():
        cash = _cash_account(name="Cash X")
        bank = _cash_account(name="Bank X", category="bank", type="bank")
        loan = _cash_account(name="Loan X", category="bank", type="loan",
                             source_category="Loan", balance=-5_000.0,
                             balance_minor=-500_000,
                             opening_balance=-5_000.0, opening_balance_minor=-500_000)
        db.session.commit()
        cash_id, bank_id, loan_id = cash.id, bank.id, loan.id

    _login(client)

    # Intra-company transfer
    posted = client.post(
        "/accounts/transactions/new",
        data={
            "tx_mode": "pay",
            "pay_from_account_id": str(cash_id),
            "pay_target": "company_transfer",
            "pay_to_account_id": str(bank_id),
            "amount": "2500",
            "method": "Cash",
            "date_posted": datetime.now().strftime("%Y-%m-%dT%H:%M"),
        },
        follow_redirects=True,
    )
    assert b"Transfer transaction recorded successfully" in posted.data

    # Loan repayment
    posted = client.post(
        "/accounts/transactions/new",
        data={
            "tx_mode": "pay",
            "pay_from_account_id": str(cash_id),
            "pay_target": "loan",
            "pay_to_account_id": str(loan_id),
            "amount": "1000",
            "method": "Cash",
            "target_label": "Loan installment",
            "expense_reference": "LN-9",
            "date_posted": datetime.now().strftime("%Y-%m-%dT%H:%M"),
        },
        follow_redirects=True,
    )
    assert b"Loan payment recorded successfully" in posted.data

    with app.app_context():
        cash = db.session.get(Account, cash_id)
        bank = db.session.get(Account, bank_id)
        loan = db.session.get(Account, loan_id)
        assert cash.balance == pytest.approx(6_500.0)
        assert cash.balance_minor == 650_000
        assert bank.balance == pytest.approx(12_500.0)
        assert bank.balance_minor == 1_250_000
        assert loan.balance == pytest.approx(-4_000.0)
        assert loan.balance_minor == -400_000


def test_expenditures_page_and_kpi_exclude_internal_transfers(app, client):
    from models import AccountTransaction, db

    with app.app_context():
        account = _cash_account()
        db.session.add_all([
            AccountTransaction(from_account_id=account.id, to_account_id=None,
                               amount=300, transaction_type="Expense",
                               description="Fuel", category="Transport",
                               payee="Petrol pump", reference_no="V-1",
                               payment_method="Cash"),
            # Loan repayment: money leaves the cash account but lands in a
            # company loan account — internal, not an expenditure.
            AccountTransaction(from_account_id=account.id,
                               to_account_id=_cash_account(
                                   name="Loan Y", source_category="Loan",
                                   balance=-1_000.0, balance_minor=-100_000).id,
                               amount=200, transaction_type="Payment",
                               description="Loan repayment to Loan Y"),
        ])
        db.session.commit()
        account_id = account.id

    _login(client)

    page = client.get("/accounts/expenditures")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Fuel" in body
    assert "Petrol pump" in body
    assert "V-1" in body
    # Total spent = 300 (the Fuel expense only; the 200 internal loan
    # repayment is tracked on the account ledger, not as an expenditure).
    assert "Rs. 300.00" in body
    assert "Rs. 500.00" not in body

    # The drill-down page must not list the internal movement at all.
    kpi_page = client.get("/accounts/kpi/expenditures")
    assert kpi_page.status_code == 200
    assert "Loan repayment to Loan Y" not in kpi_page.get_data(as_text=True)

    api = client.get("/accounts/api/kpi/expenditures")
    assert api.status_code == 200
    rows = api.get_json()
    categories = {row.get("category") for row in rows}
    assert "Transport" in categories
    # The old bug surfaced internal movements labelled "Other Receipt".
    assert "Loan repayment to Loan Y" not in {row.get("category") or "" for row in rows}
    fuel_rows = [row for row in rows if row.get("category") == "Transport"]
    assert fuel_rows and fuel_rows[0]["amount"] == 300
    assert all("client_name" not in row for row in rows)


def test_receipts_page_is_a_true_money_in_register(app, client):
    from models import Account, Client, GRN, GRNItem, Material, Payment, Supplier, db

    with app.app_context():
        supplier = Supplier(name="Register Supplier", is_active=True)
        client_row = Client(code="REG-1", name="Register Client", is_active=True)
        material = Material(name="Register Material", code="REG-MAT", unit="Bags")
        db.session.add_all([supplier, client_row, material])
        db.session.flush()
        grn = GRN(supplier=supplier.name, supplier_id=supplier.id,
                  manual_bill_no="MB NO.REG-GRN-1", paid_amount=50_000.0)
        db.session.add(grn)
        db.session.flush()
        db.session.add(GRNItem(grn_id=grn.id, mat_name=material.name,
                               qty=100, price_at_time=1_200))
        db.session.add(Payment(
            client_id=client_row.id, client_name=client_row.name,
            amount=7_500.0, payment_type="Receipt", method="Cash",
            auto_bill_no="SB-CP-9001",
        ))
        db.session.commit()

    _login(client)
    page = client.get("/accounts/receipts")
    assert page.status_code == 200
    body = page.get_data(as_text=True)

    # Client payments are receipts now (they were missing entirely before).
    assert "Register Client" in body
    assert "7,500.00" in body
    assert "SB-CP-9001" in body
    # GRN total keeps its full landed value (earlier regression).
    assert "REG-GRN-1" in body
    assert "120,000.00" in body
    # Direction-aware totals: 7,500 in − 50,000 out = −42,500 net.
    assert "Net Movement" in body
    assert "-42,500.00" in body
    assert "Money Out" in body


def test_material_return_not_listed_as_cash_receipt(app, client):
    from models import Client, Payment, db

    with app.app_context():
        client_row = Client(code="MR-1", name="MR Client", is_active=True)
        db.session.add(client_row)
        db.session.flush()
        db.session.add(Payment(
            client_id=client_row.id, client_name=client_row.name,
            amount=2_000.0, payment_type="Material Return", method="Material Return",
            auto_bill_no="SB-CP-9002",
        ))
        db.session.commit()

    _login(client)
    body = client.get("/accounts/receipts").get_data(as_text=True)
    # Goods returned against dues settle no cash: not a receipt row.
    assert "SB-CP-9002" not in body
