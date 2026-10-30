"""Tests for the data-driven Transaction Tree, full Category & Subcategory
hierarchy in transactions, and concrete visual flow / badge rendering.
"""
from __future__ import annotations

from datetime import datetime

import pytest


ADMIN = {"username": "Admin", "password": "Admin@fbm12345"}


def _login(client):
    resp = client.post("/login", data=ADMIN, follow_redirects=False)
    assert resp.status_code in (302, 303)


def _create_account(name="Main Cash", category="cash", balance=25_000.0, **extra):
    from models import Account, db
    from utils.money import to_minor

    minor = to_minor(balance)
    acc = Account(
        name=name,
        type=extra.pop("type", category),
        category=category,
        account_type=extra.pop("account_type", "company"),
        source_category=extra.pop("source_category", "Company"),
        class_category=extra.pop("class_category", "Assets"),
        class_subcategory=extra.pop("class_subcategory", "Cash" if category == "cash" else "Bank"),
        class_account_type=extra.pop("class_account_type", "Main Cash" if category == "cash" else "Current Account"),
        channel=extra.pop("channel", category),
        balance=balance,
        balance_minor=minor,
        opening_balance=balance,
        opening_balance_minor=minor,
        opening_balance_date=datetime.now(),
        is_active=True,
        **extra,
    )
    db.session.add(acc)
    db.session.flush()
    return acc


def test_unified_category_catalog_is_owner_defined_and_empty_by_default(app):
    """The transaction catalog exposes only owner-created active labels."""
    from blueprints.accounts.transaction_tree import (
        build_unified_category_catalog,
        transaction_tree_registry_json,
    )
    from models import CashFlowCategory, CashFlowSubcategory, db

    with app.app_context():
        reg = transaction_tree_registry_json()
        catalog = build_unified_category_catalog()
        assert [m["mode"] for m in reg["modes"]] == ["receive", "pay", "transfer"]
        assert catalog["all_categories"] == []
        assert catalog["classification_groups"] == []

        parent = CashFlowCategory(name="Fuel & Transport", direction="out", is_active=True)
        db.session.add(parent)
        db.session.flush()
        db.session.add(CashFlowSubcategory(category_id=parent.id, name="Generator Fuel", is_active=True))
        db.session.commit()

        catalog = build_unified_category_catalog()
        assert "Fuel & Transport" in catalog["by_name"]
        assert "Generator Fuel" in catalog["by_name"]["Fuel & Transport"]["subcategories"]
        parent.is_active = False
        db.session.commit()
        assert "Fuel & Transport" not in build_unified_category_catalog()["by_name"]


def test_transactions_record_category_and_subcategory_and_show_on_ledger(app, client):
    """Transactions posted from the Accounts API keep their Category /
    Subcategory and show up on the account ledger.

    The Accounts dashboard itself is blank by design (being rebuilt), so this
    test no longer asserts on its markup.
    """
    from models import db

    with app.app_context():
        cash = _create_account(name="Drawer Cash", category="cash", balance=50_000.0)
        bank = _create_account(name="MCB Operating", category="bank", balance=120_000.0)
        loan = _create_account(
            name="Partner Loan A",
            category="bank",
            type="loan",
            source_category="Loan",
            class_category="Liabilities",
            class_subcategory="Loans",
            class_account_type="Partner Loan",
            balance=-30_000.0,
        )
        db.session.commit()
        cash_id, bank_id, loan_id = cash.id, bank.id, loan.id

    _login(client)

    from models import CashFlowCategory, CashFlowSubcategory
    with app.app_context():
        for name, direction, child in (
            ("Fuel & Transport", "out", "Generator Fuel"),
            ("Other Income", "in", "Scrap / Misc Income"),
            ("Internal Transfer", "both", "Cash Deposit to Bank"),
        ):
            parent = CashFlowCategory(name=name, direction=direction, is_active=True)
            db.session.add(parent)
            db.session.flush()
            db.session.add(CashFlowSubcategory(category_id=parent.id, name=child, is_active=True))
        db.session.commit()

    # 1. Record an expense with an owner-defined Category + Subcategory.
    res_exp = client.post(
        "/accounts/transactions/new",
        data={
            "tx_mode": "pay",
            "pay_from_account_id": str(cash_id),
            "pay_target": "other_expense",
            "amount": "1500",
            "method": "Cash",
            "expense_category": "Fuel & Transport",
            "expense_subcategory": "Generator Fuel",
            "payee": "PSO Filling Station",
            "expense_reference": "V-501",
            "note": "Backup generator diesel",
        },
        follow_redirects=True,
    )
    assert res_exp.status_code == 200
    assert b"Outgoing payment recorded successfully" in res_exp.data

    # 2. Record a categorized receipt with Category + Subcategory
    res_rec = client.post(
        "/accounts/transactions/new",
        data={
            "tx_mode": "receive",
            "receive_from_category": "other_source",
            "receive_source_label": "Scrap Buyer Zahid",
            "receive_category": "Other Income",
            "receive_subcategory": "Scrap / Misc Income",
            "receive_reference": "REC-88",
            "receive_account_id": str(cash_id),
            "amount": "3200",
            "method": "Cash",
            "note": "Empty cement bags sold",
        },
        follow_redirects=True,
    )
    assert res_rec.status_code == 200
    assert b"Receive transaction recorded successfully" in res_rec.data

    # 3. Record a top-level Transfer using tx_mode='transfer'
    res_xfer = client.post(
        "/accounts/transactions/new",
        data={
            "tx_mode": "transfer",
            "transfer_from_account_id": str(cash_id),
            "transfer_to_account_id": str(bank_id),
            "tx_category": "Internal Transfer",
            "tx_subcategory": "Cash Deposit to Bank",
            "reference_no": "DEP-77",
            "amount": "5000",
            "method": "Cash",
            "note": "Evening bank deposit",
        },
        follow_redirects=True,
    )
    assert res_xfer.status_code == 200
    assert b"Transfer transaction recorded successfully" in res_xfer.data

    # 4. Verify Account Ledger shows concrete [From -> To] flow and subcategory pill
    ledger = client.get(f"/accounts/ledger/{cash_id}")
    assert ledger.status_code == 200
    ledger_html = ledger.get_data(as_text=True)
    assert "Generator Fuel" in ledger_html
    assert "Scrap / Misc Income" in ledger_html
    assert "Cash Deposit to Bank" in ledger_html


def test_strip_internal_markers_and_source_badge_on_ledger(app, client):
    """Internal [SRC:Payment:12] markers are stripped from visible notes and
    rendered as structured source badges instead.
    """
    from blueprints.accounts.transaction_tree import (
        describe_account_transaction,
        strip_internal_markers,
    )
    from models import AccountTransaction, db

    assert strip_internal_markers("Paid in full [SRC:Payment:42]") == "Paid in full"
    assert strip_internal_markers("[AUTO_GRN_PAY:19] Instant GRN") == "Instant GRN"

    with app.app_context():
        cash = _create_account(name="Ledger Cash", category="cash", balance=10_000.0)
        tx = AccountTransaction(
            from_account_id=None,
            to_account_id=cash.id,
            amount=4_500.0,
            amount_minor=450_000,
            description="Client payment received from Ali Traders (SB-PAY-1001)",
            note="First installment [SRC:Payment:42]",
            transaction_type="Receipt",
            source_type="Payment",
            source_id=42,
            category="Receivables",
            subcategory="Client Receivable",
            payee="Ali Traders",
            payment_method="Cash",
        )
        db.session.add(tx)
        db.session.commit()
        cash_id = cash.id

        info = describe_account_transaction(tx)
        assert info["source_badge"] == "Client Payment #42"
        assert info["clean_note"] == "First installment"
        assert "[SRC:" not in info["clean_note"]
        assert info["category"] == "Receivables"
        assert info["subcategory"] == "Client Receivable"

    _login(client)
    ledger_body = client.get(f"/accounts/ledger/{cash_id}").get_data(as_text=True)
    assert "Client Payment #42" in ledger_body
    assert "First installment" in ledger_body
    assert "[SRC:Payment:42]" not in ledger_body
