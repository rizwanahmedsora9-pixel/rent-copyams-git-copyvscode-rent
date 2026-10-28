"""Thin serializers: the JSON API returns read models, never SQL.

Every endpoint here is a projection of ``app/services/read_models/``, which in
turn reads the journal.  That is the whole point of plan §16 (S11): the API is a
serializer, so a number cannot be computed twice and drift.
"""
from __future__ import annotations

from datetime import date, datetime

from app.services.ledger import periods as period_svc
from app.services.ledger import query as q
from app.services.read_models import (
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
    "api_ledger_version",
    "api_dashboard",
    "api_trial_balance",
    "api_pnl",
    "api_balance_sheet",
    "api_receivables",
    "api_payables",
    "api_cash",
    "api_stock",
    "api_drivers",
    "jsonable",
]


def jsonable(value):
    """Plain JSON types only — dates become ISO strings, nothing else changes."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def api_ledger_version() -> dict:
    return {"version": q.ledger_version()}


def api_dashboard(as_of=None) -> dict:
    return jsonable(dashboard.load(as_of or date.today()))


def api_trial_balance(as_of=None, start=None, end=None) -> dict:
    return jsonable(q.trial_balance(start=start, end=end, as_of=as_of))


def api_pnl(start=None, end=None) -> dict:
    return jsonable(pnl_view.load(start, end))


def api_balance_sheet(as_of=None) -> dict:
    return jsonable(q.balance_sheet(as_of=as_of))


def api_receivables(as_of=None) -> dict:
    return jsonable(client_account.balances(as_of))


def api_payables(as_of=None) -> dict:
    return jsonable(payables_view.load(as_of))


def api_cash(as_of=None) -> dict:
    return jsonable(cash_view.load(as_of=as_of))


def api_stock(as_of=None) -> dict:
    return jsonable(stock_view.load(as_of))


def api_drivers(driver_id=None, as_of=None) -> dict:
    return jsonable(driver_view.load(driver_id, as_of=as_of))


def api_periods(limit: int = 36) -> dict:
    return jsonable([
        {
            "id": period.id,
            "year": period.year,
            "month": period.month,
            "status": period.status,
            "start": period.start_date,
            "end": period.end_date,
        }
        for period in period_svc.list_periods(limit=limit)
    ])
