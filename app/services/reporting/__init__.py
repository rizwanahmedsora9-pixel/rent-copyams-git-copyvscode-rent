"""Statement builders — thin, named entry points over the book.

The pages and the exports call these functions (or the matching read model), so
there is one definition of "trial balance", "balance sheet", "profit & loss" and
"cash flow" in the application.
"""
from __future__ import annotations

from app.services.reporting import (
    balance_sheet,
    cash_flow,
    pnl,
    trial_balance,
)

__all__ = ["balance_sheet", "cash_flow", "pnl", "trial_balance"]
