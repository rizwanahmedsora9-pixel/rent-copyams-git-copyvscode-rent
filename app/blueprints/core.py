"""HTTP routes: core."""
from __future__ import annotations

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_file, Response, make_response, send_from_directory, abort, session
from flask_login import login_required, login_user, logout_user, current_user
from datetime import datetime, date, timedelta
from sqlalchemy import func, case, text, or_, and_, exists, not_
from types import SimpleNamespace
from decimal import Decimal, ROUND_HALF_UP
import os, io, json, re, logging, calendar

from models import *
from app.services.api import *  # noqa
from utils.audit import audit_log

bp = Blueprint('core', __name__)

@bp.route('/')
@login_required
def index():
    """Dashboard — every tile is a read model, never an inline SQL sum.

    The numbers come from ``read_models.dashboard`` (which reads the journal and
    is cached against ``ledger_version``), so the dashboard, the P&L and the
    ledger pages cannot disagree (master plan §16 S11).
    """
    from app.services.read_models import dashboard as dashboard_model

    today_date = pk_today()
    view = dashboard_model.load(today_date)

    # Templates format rupees; the read layer works in whole paisa.
    def rupees(minor):
        return round(float(minor or 0) / 100.0, 2)

    stats = sorted(
        [
            {
                'name': row['name'],
                'in': int(row['in']),
                'out': int(row['out']),
                'stock': row['remaining'],
                'unit': row['unit'],
                'has_movement': row['has_movement'],
            }
            for row in view['stock']
        ],
        key=lambda row: row['name'].lower(),
    )
    total_stock = sum(row['stock'] for row in stats)
    sales_breakdown_list = [
        {'category': row['category'], 'amount': rupees(row['amount'])}
        for row in view['sales_by_category']
    ]
    return render_template(
        'index.html',
        today_date=today_date.strftime('%B %d, %Y'),
        total_stock=int(round(total_stock)),
        client_count=view['client_count'],
        stats=stats,
        daily_cash=rupees(view['cash_in_today']),
        daily_credit=rupees(view['credit_granted_today']),
        total_outstanding=rupees(view['receivable_total']),
        sales_breakdown=sales_breakdown_list,
        book=view,
        book_cash=rupees(view['cash_balance']),
        book_payables=rupees(view['payable_total']),
        book_inventory=rupees(view['inventory_value']),
        ledger_version=view['ledger_version'],
    )


