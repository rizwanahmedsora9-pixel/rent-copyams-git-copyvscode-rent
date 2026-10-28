"""Read models — the only shapes pages are allowed to render.

Each module answers one page's question from the book (``ledger.query``), caches
the answer against ``ledger_version`` (``app.services.cache``), and returns plain
dicts with integer paisa plus the version it was built at.  Templates and exports
format; they never aggregate.

See the master plan §16 (S11) for the full contract.
"""
from __future__ import annotations

from app.services.read_models import (
    books_agree,
    cash_view,
    client_account,
    dashboard,
    driver_view,
    payables_view,
    pnl_view,
    stock_view,
    supplier_account,
)

__all__ = [
    "books_agree",
    "cash_view",
    "client_account",
    "dashboard",
    "driver_view",
    "payables_view",
    "pnl_view",
    "stock_view",
    "supplier_account",
]
