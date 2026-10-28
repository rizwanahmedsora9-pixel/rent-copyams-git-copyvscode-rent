"""The journal: posting, idempotency, immutability, reversal, and the rules.

These tests drive the real sync path (``ledger.sync`` and the session listener
that fires on commit), so a failure means a real document would not reach the
book — not that a helper disagrees with itself.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from models import (
    Account,
    Client,
    DeliveryPerson,
    DeliveryPersonPayment,
    DirectSale,
    DirectSaleItem,
    GRN,
    GRNItem,
    Material,
    Payment,
    SaleDeliveryPerson,
    Supplier,
    SupplierPayment,
    db,
)
from app.services.ledger import accounts as coa
from app.services.ledger import posting, query as q, sync
from app.services.ledger.accounts import GL


def _code_balance(code: str) -> int:
    for row in q.account_balances():
        if row["code"] == code:
            return row["balance"]
    return 0


def _cash_total() -> int:
    """Cash lives on the GL account linked to each cash master row."""
    return q.cash_positions()["total"]


def _entries(source_type=None, source_id=None, event=None):
    from models import JournalEntry

    query = JournalEntry.query
    if source_type:
        query = query.filter_by(source_type=source_type)
    if source_id:
        query = query.filter_by(source_id=int(source_id))
    if event:
        query = query.filter_by(event=event)
    return query.order_by(JournalEntry.id.asc()).all()


@pytest.fixture()
def world(app):
    """One client, one supplier, one material, one cash account."""
    with app.app_context():
        client = Client(code="CL-1", name="Bilal Construction", opening_balance=0)
        supplier = Supplier(name="Ravi Cement Supplier", opening_balance=0)
        material = Material(code="OPC", name="OPC Cement", unit="Bags", unit_price=1200)
        cash = Account(
            name="Main Cash", type="cash", category="cash", account_type="company",
            channel="cash", balance=0, balance_minor=0, is_active=True,
        )
        bank = Account(
            name="Operating Bank", type="bank", category="bank", account_type="company",
            channel="bank", balance=0, balance_minor=0, is_active=True,
        )
        db.session.add_all([client, supplier, material, cash, bank])
        db.session.commit()
        coa.ensure_chart()
        coa.ensure_cash_accounts()
        db.session.commit()
        return {
            "client": client.id,
            "supplier": supplier.id,
            "material": material.id,
            "cash": cash.id,
            "bank": bank.id,
        }


def _grn(ids, *, qty=100, rate=1000, freight=500, paid=0):
    grn = GRN(
        supplier_id=ids["supplier"],
        supplier="Ravi Cement Supplier",
        manual_bill_no=f"G-{datetime.now().timestamp()}",
        date_posted=datetime.now(),
        freight_cost=freight,
        paid_amount=paid,
        loading_cost=0,
        other_expense=0,
        adjustment_amount=0,
        discount=0,
        tax_amount=0,
    )
    grn.items = [GRNItem(mat_name="OPC Cement", qty=qty, price_at_time=rate)]
    db.session.add(grn)
    db.session.commit()
    return grn.id


def _sale(ids, *, qty=10, rate=1200, discount=0, paid=0, cost=1000, account=None):
    sale = DirectSale(
        client_name="Bilal Construction",
        client_code="CL-1",
        category="Credit Customer",
        amount=qty * rate,
        paid_amount=paid,
        discount=discount,
        date_posted=datetime.now(),
        payment_account_id=account,
    )
    db.session.add(sale)
    db.session.flush()
    db.session.add(DirectSaleItem(
        sale_id=sale.id,
        product_name="OPC Cement",
        qty=qty,
        price_at_time=rate,
        cost_rate_at_sale=cost,
        discount=discount,
        discount_kind="manual" if discount else "none",
    ))
    db.session.commit()
    return sale.id


def test_chart_is_seeded(world, app):
    with app.app_context():
        codes = {account.code for account in coa.chart_accounts()}
        for code in (GL.CASH, GL.RECEIVABLE, GL.INVENTORY, GL.PAYABLE, GL.SALES_GOODS,
                     GL.COGS_GOODS, GL.TRADE_DISCOUNTS, GL.OPENING_EQUITY):
            assert code in codes


def test_grn_posts_balanced_inventory_and_payable(world, app):
    with app.app_context():
        grn_id = _grn(world)
        entries = _entries("GRN", grn_id)
        assert [entry.event for entry in entries] == ["grn"]
        entry = entries[0]
        debits = sum(line.debit_minor for line in entry.lines)
        credits = sum(line.credit_minor for line in entry.lines)
        assert debits == credits == 100_500_00  # 100 × 1,000 + 500 freight
        assert _code_balance(GL.INVENTORY) == 100_500_00
        assert _code_balance(GL.PAYABLE) == -100_500_00  # credit balance


def test_sale_posts_revenue_discount_cogs_and_receivable(world, app):
    with app.app_context():
        sale_id = _sale(world, qty=10, rate=1200, discount=2000, paid=5000, account=world["cash"])
        events = [entry.event for entry in _entries("DirectSale", sale_id)]
        assert set(events) == {"sale", "sale_cogs", "sale_receipt"}

        assert _code_balance(GL.SALES_GOODS) == -12_000_00       # credited gross
        assert _code_balance(GL.TRADE_DISCOUNTS) == 2_000_00     # contra-revenue debit
        assert _code_balance(GL.COGS_GOODS) == 10_000_00         # 10 × 1,000 frozen cost
        assert _code_balance(GL.INVENTORY) == -10_000_00         # inventory released
        assert _code_balance(GL.RECEIVABLE) == 12_000_00 - 2_000_00 - 5_000_00
        assert _cash_total() == 5_000_00

        tb = q.trial_balance()
        assert tb["balanced"], tb

        pnl = q.profit_and_loss()
        assert pnl["gross_revenue"] == 12_000_00
        assert pnl["discount_total"] == 2_000_00
        assert pnl["net_revenue"] == 10_000_00
        assert pnl["gross_profit"] == 0  # 10,000 revenue − 10,000 FIFO cost

        sheet = q.balance_sheet()
        assert sheet["balanced"], sheet


def test_posting_is_idempotent(world, app):
    with app.app_context():
        sale_id = _sale(world, qty=4, rate=1000, cost=800)
        before = len(_entries("DirectSale", sale_id))
        again = sync.sync_document("DirectSale", sale_id)
        assert again["posted"] == []
        assert len(_entries("DirectSale", sale_id)) == before


def test_editing_a_document_reverses_then_reposts(world, app):
    with app.app_context():
        sale_id = _sale(world, qty=4, rate=1000, cost=800)
        sale = db.session.get(DirectSale, sale_id)
        sale.items[0].price_at_time = 1250
        sale.items[0].qty = 4
        sale.amount = 5000
        db.session.commit()

        revenue_entries = _entries("DirectSale", sale_id, "sale")
        # original + mirror + new
        assert len(revenue_entries) == 3
        active = posting.active_entry("DirectSale", sale_id, "sale")
        assert active.payload_hash == revenue_entries[-1].payload_hash
        assert _code_balance(GL.SALES_GOODS) == -5_000_00
        assert _code_balance(GL.RECEIVABLE) == 5_000_00
        assert q.trial_balance()["balanced"]


def test_voiding_a_document_reverses_it(world, app):
    with app.app_context():
        grn_id = _grn(world, qty=10, rate=1000, freight=0)
        assert _code_balance(GL.INVENTORY) == 10_000_00
        grn = db.session.get(GRN, grn_id)
        grn.is_void = True
        db.session.commit()
        assert _code_balance(GL.INVENTORY) == 0
        assert _code_balance(GL.PAYABLE) == 0
        entries = _entries("GRN", grn_id)
        assert len(entries) == 2
        assert entries[1].is_reversal_of == entries[0].id
        assert q.trial_balance()["balanced"]


def test_payment_receipt_and_waive_off(world, app):
    with app.app_context():
        _sale(world, qty=10, rate=1000, cost=800)  # AR 10,000
        payment = Payment(
            client_id=world["client"],
            client_name="Bilal Construction",
            amount=4000,
            discount=1000,
            discount_reason="settlement",
            payment_type="Receipt",
            method="Cash",
            payment_account_id=world["cash"],
            date_posted=datetime.now(),
        )
        db.session.add(payment)
        db.session.commit()
        assert _cash_total() == 4_000_00
        assert _code_balance(GL.WAIVE_OFF) == 1_000_00
        assert _code_balance(GL.RECEIVABLE) == 10_000_00 - 5_000_00
        assert q.trial_balance()["balanced"]


def test_supplier_payment_reduces_payable(world, app):
    with app.app_context():
        _grn(world, qty=10, rate=1000, freight=0)
        db.session.add(SupplierPayment(
            supplier_id=world["supplier"],
            amount=4000,
            payment_type="Payment",
            method="Cash",
            payment_account_id=world["cash"],
            date_posted=datetime.now(),
        ))
        db.session.commit()
        assert _code_balance(GL.PAYABLE) == -6_000_00
        assert _cash_total() == -4_000_00
        assert q.trial_balance()["balanced"]


def test_driver_payable_accrual_and_settlement(world, app):
    with app.app_context():
        driver = DeliveryPerson(name="Akram Driver", opening_balance=0)
        db.session.add(driver)
        db.session.flush()
        sale_id = _sale(world, qty=10, rate=1000, cost=800)
        db.session.add(SaleDeliveryPerson(
            sale_id=sale_id, delivery_person_id=driver.id, bags_delivered=10, rent_amount=1500,
        ))
        db.session.commit()
        db.session.add(DeliveryPersonPayment(
            delivery_person_id=driver.id,
            sale_id=sale_id,
            amount_paid=1000,
            waive_off_amount=200,
            payment_account_id=world["cash"],
            date_posted=datetime.now(),
        ))
        db.session.commit()
        assert _code_balance(GL.COGS_SERVICES) == 1_500_00
        assert _code_balance(GL.DRIVER_PAYABLE) == -(1_500_00 - 1_200_00)
        assert _code_balance(GL.OTHER_INCOME) == -200_00
        assert q.trial_balance()["balanced"]


def test_journal_lines_are_immutable_in_the_database(world, app):
    from sqlalchemy import text
    from sqlalchemy.exc import DatabaseError, OperationalError

    with app.app_context():
        _grn(world, qty=1, rate=100, freight=0)
        line = db.session.execute(text("SELECT id FROM journal_line LIMIT 1")).fetchone()
        assert line is not None
        with pytest.raises((DatabaseError, OperationalError)):
            db.session.execute(text("UPDATE journal_line SET debit_minor = 1 WHERE id = :i"), {"i": line[0]})
            db.session.commit()
        db.session.rollback()
        with pytest.raises((DatabaseError, OperationalError)):
            db.session.execute(text("DELETE FROM journal_line WHERE id = :i"), {"i": line[0]})
            db.session.commit()
        db.session.rollback()


def test_ledger_version_bumps_on_every_posting(world, app):
    with app.app_context():
        start = q.ledger_version()
        _grn(world, qty=1, rate=100, freight=0)
        assert q.ledger_version() > start
