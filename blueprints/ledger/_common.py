"""Ledger module: the book, its statements, and the period controls.

Registered by ``utils.module_loader`` at ``/ledger`` (the blueprint name is
``ledger``).  Every page here reads a read model — never a document table.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect, render_template,
    request, url_for,
)
from flask_login import current_user, login_required

from models import db, GLAccount, JournalEntry, JournalLine
from app.services import cache
from app.services.ledger import accounts as coa
from app.services.ledger import close as close_svc
from app.services.ledger import periods as period_svc
from app.services.ledger import posting, query as q, replay, sync
from app.services.read_models import (
    books_agree as books_agree_model,
    cash_view,
    client_account,
    dashboard as dashboard_model,
    pnl_view,
    supplier_account,
)

MODULE_CONFIG = {
    "name": "Ledger & Accounting Book",
    "description": "Journal, chart of accounts, statements, period locks and the sync layer",
    "url_prefix": "/ledger",
    "enabled": True,
    "requires_login": True,
    "allowed_roles": ["admin", "user"],
}

bp = Blueprint("ledger", __name__)


def _can_view() -> bool:
    return bool(
        getattr(current_user, "role", "") == "admin"
        or getattr(current_user, "can_view_reports", False)
        or getattr(current_user, "can_manage_accounts", False)
    )


def _can_manage() -> bool:
    return bool(
        getattr(current_user, "role", "") == "admin"
        or getattr(current_user, "can_manage_accounts", False)
    )


def _guard(view: bool = True):
    if view and not _can_view():
        abort(403)
    if not view and not _can_manage():
        abort(403)


def _actor() -> str:
    return (getattr(current_user, "username", "") or "system")[:80]


def _date_arg(name, default=None):
    raw = (request.args.get(name) or "").strip()
    if not raw:
        return default
    return period_svc.normalize_date(raw)


def _money(value) -> float:
    return round(int(value or 0) / 100.0, 2)
