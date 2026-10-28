"""Profit & loss read model.

The legacy report recomputed revenue, discount shares and FIFO cost on every
request (``profit_loss.py``).  This view is a prepared answer over the journal:
Gross → Discounts → Net → COGS → Gross profit → Expenses → Other losses → Net
profit, all in whole paisa.
"""
from __future__ import annotations

from datetime import date

from app.services import cache
from app.services.ledger import query as q

__all__ = ["load", "month"]


def load(start=None, end=None) -> dict:
    start = start or date.today().replace(day=1)
    end = end or date.today()
    version = q.ledger_version()

    def build():
        pnl = q.profit_and_loss(start=start, end=end)
        kinds = q.revenue_by_kind(start=start, end=end)
        return {
            "start": start,
            "end": end,
            "ledger_version": version,
            "statement": pnl,
            "by_kind": kinds,
            "expenses": q.expense_breakdown(start=start, end=end),
            "gross_revenue": pnl["gross_revenue"],
            "discount_total": pnl["discount_total"],
            "net_revenue": pnl["net_revenue"],
            "cost_total": pnl["cost_total"],
            "gross_profit": pnl["gross_profit"],
            "expense_total": pnl["expense_total"],
            "other_loss_total": pnl["other_loss_total"],
            "other_income_total": pnl["other_income_total"],
            "net_profit": pnl["net_profit"],
        }

    return cache.get_or_build(("pnl", str(start), str(end)), version, build)


def month(year: int, month_number: int) -> dict:
    from app.services.ledger import periods as period_svc

    start, end = period_svc.period_bounds(year, month_number)
    return load(start, end)
