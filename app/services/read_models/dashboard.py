"""Dashboard read model: one book, one set of numbers.

Replaces the per-page SQL sums in ``app/blueprints/core.py`` (cash today from
payments + bookings + sales, each with its own formula) with one query set over
the journal.  The tiles and the P&L therefore cannot disagree.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import func

from models import (
    Client,
    GLAccount,
    JournalEntry,
    JournalLine,
    Payment,
    db,
)
from app.services import cache
from app.services.ledger import accounts as coa
from app.services.ledger import query as q
from app.services.read_models.payables_view import load as payables_load
from app.services.read_models.stock_view import summary as stock_summary

__all__ = ["load", "today_totals"]


def _cash_flow_on(day) -> tuple[int, int]:
    rows = db.session.query(
        func.coalesce(func.sum(JournalLine.debit_minor), 0),
        func.coalesce(func.sum(JournalLine.credit_minor), 0),
    ).join(JournalEntry, JournalEntry.id == JournalLine.entry_id).join(
        GLAccount, GLAccount.id == JournalLine.gl_account_id
    ).filter(
        GLAccount.is_cash.is_(True),
        JournalEntry.entry_date == day,
    ).first()
    inbound, outbound = (int(rows[0] or 0), int(rows[1] or 0)) if rows else (0, 0)
    return inbound, outbound


def _receivable_flow_on(day) -> tuple[int, int]:
    """Credit granted (AR debit) and collected (AR credit) on one day."""
    account = coa.account_by_code(coa.GL.RECEIVABLE)
    if account is None:
        return 0, 0
    row = (
        db.session.query(
            func.coalesce(func.sum(JournalLine.debit_minor), 0),
            func.coalesce(func.sum(JournalLine.credit_minor), 0),
        )
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .filter(
            JournalLine.gl_account_id == account.id,
            JournalEntry.entry_date == day,
        )
        .first()
    )
    return (int(row[0] or 0), int(row[1] or 0)) if row else (0, 0)


def _payable_flow_on(day) -> tuple[int, int]:
    """Credit taken (AP credit) and paid (AP debit) on one day."""
    account = coa.account_by_code(coa.GL.PAYABLE)
    if account is None:
        return 0, 0
    row = (
        db.session.query(
            func.coalesce(func.sum(JournalLine.debit_minor), 0),
            func.coalesce(func.sum(JournalLine.credit_minor), 0),
        )
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .filter(
            JournalLine.gl_account_id == account.id,
            JournalEntry.entry_date == day,
        )
        .first()
    )
    return (int(row[0] or 0), int(row[1] or 0)) if row else (0, 0)


def sales_by_category(day=None) -> list[dict]:
    """Today's revenue split, straight from the revenue accounts."""
    day = day or date.today()
    kinds = q.revenue_by_kind(start=day, end=day)
    rows = [
        {"category": "Goods sales", "amount": kinds["goods"]},
        {"category": "Service sales", "amount": kinds["services"]},
        {"category": "Discounts", "amount": -kinds["discounts"]},
    ]
    return [row for row in rows if row["amount"]]


def today_totals(day=None) -> dict:
    day = day or date.today()
    inbound, outbound = _cash_flow_on(day)
    pnl = q.profit_and_loss(start=day.replace(day=1) if hasattr(day, "replace") else day, end=day)
    return {
        "date": day,
        "cash_in": inbound,
        "cash_out": outbound,
        "cash_net": inbound - outbound,
        "month_revenue": pnl["net_revenue"],
        "month_profit": pnl["net_profit"],
    }


def load(as_of=None) -> dict:
    as_of = as_of or date.today()
    version = q.ledger_version()

    def build():
        month_start = as_of.replace(day=1)
        pnl_month = q.profit_and_loss(start=month_start, end=as_of)
        pnl_today = q.profit_and_loss(start=as_of, end=as_of)
        cash = q.cash_positions(as_of=as_of)
        receivables = q.client_subledger(as_of=as_of)
        payables = q.supplier_subledger(as_of=as_of)
        drivers = q.driver_subledger(as_of=as_of)
        balances = {row["code"]: row for row in q.account_balances(end=as_of)}
        inventory = balances.get(coa.GL.INVENTORY, {}).get("balance", 0)
        suspense = balances.get(coa.GL.INVENTORY_SUSPENSE, {}).get("balance", 0)
        cash_in, cash_out = _cash_flow_on(as_of)
        credit_granted, credit_collected = _receivable_flow_on(as_of)
        payable_paid, payable_taken = _payable_flow_on(as_of)
        stock = stock_summary()
        payables = payables_load(as_of, include_bills=False)
        recent = (
            JournalEntry.query.order_by(JournalEntry.entry_date.desc(), JournalEntry.id.desc())
            .limit(8)
            .all()
        )
        payments_today = (
            Payment.query.filter(
                func.date(Payment.date_posted) == as_of,
                Payment.is_void.is_(False),
            ).count()
        )
        return {
            "as_of": as_of,
            "ledger_version": version,
            "cash_in_today": cash_in,
            "cash_out_today": cash_out,
            "cash_net_today": cash_in - cash_out,
            "cash_balance": cash["total"],
            "cash_accounts": cash["rows"],
            "receivable_total": sum(row["amount"] for row in receivables),
            "receivable_count": len(receivables),
            "payable_total": payables["total"],
            "payable_ageing": payables["ageing"],
            "payable_count": len(payables),
            "driver_payable_total": sum(row["amount"] for row in drivers),
            "inventory_value": inventory,
            "inventory_suspense": suspense,
            "credit_granted_today": credit_granted,
            "credit_collected_today": credit_collected,
            "credit_net_today": credit_granted - credit_collected,
            "receivable_collected_today": credit_collected,
            "payable_paid_today": payable_paid,
            "payable_taken_today": payable_taken,
            "sales_by_category": sales_by_category(as_of),
            "client_count": db.session.query(func.count(Client.id)).scalar() or 0,
            "stock": stock["rows"],
            "stock_remaining": stock["total_remaining"],
            "stock_value": stock["value"],
            "stock_reconciles": stock["reconciles"],
            "month_revenue": pnl_month["net_revenue"],
            "month_cogs": pnl_month["cost_total"],
            "month_gross_profit": pnl_month["gross_profit"],
            "month_profit": pnl_month["net_profit"],
            "today_revenue": pnl_today["net_revenue"],
            "today_profit": pnl_today["net_profit"],
            "payments_today": payments_today,
            "recent_entries": recent,
        }

    return cache.get_or_build(("dashboard", str(as_of)), version, build)
