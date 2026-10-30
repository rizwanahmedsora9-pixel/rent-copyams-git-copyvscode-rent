"""Domain service module — extracted from legacy ERP core."""
from __future__ import annotations

import os
import io
import secrets
import json
import calendar
import threading
import time
import smtplib
import shutil
import sqlite3
import zipfile
import urllib.request
import urllib.error
import re
import logging
import importlib
from itertools import zip_longest
from urllib.parse import unquote
from contextlib import redirect_stderr
from email.message import EmailMessage
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
from datetime import datetime, date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from sqlalchemy import func, case, text, or_, and_, exists, not_
from sqlalchemy.orm import selectinload
from types import SimpleNamespace
from flask import (
    current_app as app,
    render_template, request, redirect, url_for, flash, jsonify,
    send_file, Response, make_response, send_from_directory,
    got_request_exception, abort, session, g,
)
from flask_login import login_user, login_required, logout_user, current_user

from models import *
from utils.audit import audit_log
from utils.reconciliation import run_auto_reconcile
from cash_flow_reconciliation_helpers import (
    create_reconciliation, update_reconciliation, delete_reconciliation,
    get_reconciliation_history, migrate_legacy_record,
)
from app.services import constants as C
from app.services import state
from utils.money import from_minor, to_minor

# === explicit service imports ===
from app.services.time_money import (
    _money_round,
    pk_now,
)


# Rebind constants used as bare names
OPEN_KHATA_CODE = C.OPEN_KHATA_CODE
OPEN_KHATA_NAME = C.OPEN_KHATA_NAME
PK_TZ = C.PK_TZ
SALE_CATEGORY_CHOICES = C.SALE_CATEGORY_CHOICES
_SALE_CATEGORY_ALIASES = C._SALE_CATEGORY_ALIASES
DOMAIN_WIPE_REGISTRY = C.DOMAIN_WIPE_REGISTRY
USER_PERMISSION_DEFAULTS = C.USER_PERMISSION_DEFAULTS
PERMISSION_LEGACY_FALLBACKS = C.PERMISSION_LEGACY_FALLBACKS
ENDPOINT_PERMISSION_MAP = C.ENDPOINT_PERMISSION_MAP
AUTO_BILL_NS_DEFAULT = C.AUTO_BILL_NS_DEFAULT
AUTO_BILL_NAMESPACES = C.AUTO_BILL_NAMESPACES
EDITABLE_USER_PERMISSION_FIELDS = C.EDITABLE_USER_PERMISSION_FIELDS
basedir = C.basedir
legacy_instance_dir = C.legacy_instance_dir
legacy_db_path = C.legacy_db_path
db_path = C.db_path
_DB_HEALTH_SNAPSHOT_PATH = C._DB_HEALTH_SNAPSHOT_PATH
_max_upload_mb = C._max_upload_mb
_AUTO_BACKUP_ENABLED = C._AUTO_BACKUP_ENABLED
_WIPE_BACKUP_ENABLED = C._WIPE_BACKUP_ENABLED
_AUTO_RECONCILE_ENABLED = C._AUTO_RECONCILE_ENABLED
_AUTO_RECONCILE_FIX = C._AUTO_RECONCILE_FIX
_AUTO_RECONCILE_INTERVAL_SEC = C._AUTO_RECONCILE_INTERVAL_SEC
_AUTO_RECONCILE_TOL = C._AUTO_RECONCILE_TOL
_ALLOW_EMPTY_DB = C._ALLOW_EMPTY_DB
_ALLOW_DB_DROP = C._ALLOW_DB_DROP
_DB_HEALTH_DROP_RATIO = C._DB_HEALTH_DROP_RATIO
_DB_HEALTH_DROP_MIN = C._DB_HEALTH_DROP_MIN
_DB_HEALTH_MIN_BYTES = C._DB_HEALTH_MIN_BYTES

def _cash_flow_net_between(start_date=None, end_date=None):
    """Company-wide net of the very same movements shown in the report."""
    incoming, outgoing = _cash_flow_in_out_between(start_date, end_date)
    return float(from_minor(to_minor(incoming) - to_minor(outgoing)))


def _legacy_adjustments_total(start_date=None, end_date=None):
    query = CashFlowDifferenceAdjustment.query.filter(
        CashFlowDifferenceAdjustment.physical_cash_available.is_(None)
    )
    if start_date:
        query = query.filter(CashFlowDifferenceAdjustment.adjustment_date >= start_date)
    if end_date:
        query = query.filter(CashFlowDifferenceAdjustment.adjustment_date <= end_date)
    return float(query.with_entities(func.coalesce(func.sum(CashFlowDifferenceAdjustment.amount), 0)).scalar() or 0)


def _cash_flow_in_out_between(start_date, end_date):
    """Cash/bank in and out from the shared, deduplicated movement collector."""
    if end_date and start_date and start_date > end_date:
        return 0.0, 0.0
    start = start_date.strftime('%Y-%m-%d') if start_date else '0001-01-01'
    end = end_date.strftime('%Y-%m-%d') if end_date else '9999-12-31'
    summary = summarize_cash_flow_rows(collect_cash_flow_rows(start, end, include_voided=False), breakdown=False)
    return summary['total_cash_in'], summary['total_cash_out']


def _cash_accounts_opening_total(as_of_date=None):
    """Sum of opening balances of active company CASH accounts.

    Account opening balances are part of the physical cash in hand but are
    NOT ledger transactions, so the automatic Cash Flow opening must add
    them explicitly — otherwise the report's closing balance never ties to
    the cash account balance shown in Accounts.
    """
    from utils.money import from_minor
    total = 0.0
    for acc in _cf_company_accounts(active_only=True):
        if (acc.category or '').lower() != 'cash':
            continue
        ob_date = getattr(acc, 'opening_balance_date', None)
        if as_of_date is not None and ob_date is not None:
            ob_day = ob_date.date() if hasattr(ob_date, 'date') else ob_date
            if ob_day > as_of_date:
                continue
        if getattr(acc, 'opening_balance_minor', None) is not None:
            total += float(from_minor(acc.opening_balance_minor))
        elif acc.opening_balance is not None:
            total += float(acc.opening_balance or 0)
    return total


def _automatic_cash_opening_balance(from_date_dt):
    previous_day = from_date_dt - timedelta(days=1)
    last_physical = CashFlowDifferenceAdjustment.query.filter(
        CashFlowDifferenceAdjustment.adjustment_date < from_date_dt,
        CashFlowDifferenceAdjustment.physical_cash_available.isnot(None)
    ).order_by(CashFlowDifferenceAdjustment.adjustment_date.desc()).first()

    if last_physical:
        # A physical count is an absolute cash figure: it already contains
        # any account opening balances, so only roll movements forward.
        start_date = last_physical.adjustment_date + timedelta(days=1)
        opening = float(last_physical.physical_cash_available or 0)
        opening += _cash_flow_net_between(start_date, previous_day)
        opening -= _legacy_adjustments_total(start_date, previous_day)
        return opening

    opening = _cash_accounts_opening_total(from_date_dt)
    opening += _cash_flow_net_between(None, previous_day)
    opening -= _legacy_adjustments_total(None, previous_day)
    return opening


def _account_opening_minor(account):
    if account.opening_balance_minor is not None:
        return int(account.opening_balance_minor)
    if account.opening_balance is not None:
        return to_minor(account.opening_balance)
    # Old accounts may lack an opening field; use the same safe inference as
    # their account ledger rather than quietly treating their opening as zero.
    from app.services.payments_crud import ledger_balance
    return to_minor(ledger_balance(account.id, as_of=datetime.min))


def _opening_day(account):
    opening_date = getattr(account, 'opening_balance_date', None)
    return opening_date.date() if isinstance(opening_date, datetime) else opening_date


def _account_report_opening(account_id, from_day):
    """Selected-account balance at the start of the selected date."""
    from app.services.payments_crud import ledger_balance
    account = db.session.get(Account, account_id)
    if not account:
        return 0.0
    through = datetime.combine(from_day, datetime.min.time()) - timedelta(microseconds=1)
    opening = to_minor(ledger_balance(account.id, as_of=through))
    day = _opening_day(account)
    if day and day > from_day:
        opening -= _account_opening_minor(account)
    return float(from_minor(opening))


def _automatic_report_opening_balance(from_day):
    """Opening for the full company cash+bank movement report.

    A previous physical count represents *cash only*.  When present, add the
    bank balances at the count date before rolling company-wide activity
    forward; otherwise bank openings would silently disappear from Closing.
    """
    from app.services.payments_crud import ledger_balance
    previous = from_day - timedelta(days=1)
    accounts = _cf_company_accounts(active_only=False)
    last = CashFlowDifferenceAdjustment.query.filter(
        CashFlowDifferenceAdjustment.adjustment_date < from_day,
        CashFlowDifferenceAdjustment.physical_cash_available.isnot(None),
    ).order_by(CashFlowDifferenceAdjustment.adjustment_date.desc()).first()
    if last:
        count_day = last.adjustment_date
        opening = to_minor(last.physical_cash_available or 0)
        through = datetime.combine(count_day, datetime.max.time())
        for account in accounts:
            day = _opening_day(account)
            if day and day > from_day:
                continue
            if day and day > count_day:
                opening += _account_opening_minor(account)
            elif (account.category or '').strip().lower() == 'bank':
                opening += to_minor(ledger_balance(account.id, as_of=through))
        start = count_day + timedelta(days=1)
    else:
        opening = sum(_account_opening_minor(account) for account in accounts
                      if not _opening_day(account) or _opening_day(account) <= from_day)
        start = None
    opening += to_minor(_cash_flow_net_between(start, previous))
    opening -= to_minor(_legacy_adjustments_total(start, previous))
    return float(from_minor(opening))


def _report_period_openings(from_day, to_day, account_id=None, *, cash_only=False):
    """Baselines introduced after the report starts are not cash inflows."""
    result = []
    for account in _cf_company_accounts(active_only=False):
        day = _opening_day(account)
        if not day or not (from_day < day <= to_day):
            continue
        if account_id and account.id != account_id:
            continue
        if cash_only and (account.category or '').strip().lower() != 'cash':
            continue
        minor = _account_opening_minor(account)
        if minor:
            result.append({'account_id': account.id, 'account_name': account.name,
                           'date': day, 'amount_minor': minor,
                           'amount': float(from_minor(minor))})
    return result


def _cash_account_net_between(start_day, end_day):
    """Net of actual cash-account ledger entries, including cash/bank transfers."""
    if end_day and start_day and start_day > end_day:
        return 0
    cash_ids = {account.id for account in _cf_company_accounts(active_only=False)
                if (account.category or '').strip().lower() == 'cash'}
    if not cash_ids:
        return 0
    query = db.session.query(
        AccountTransaction.from_account_id, AccountTransaction.to_account_id,
        AccountTransaction.amount, AccountTransaction.amount_minor,
    ).filter(
        AccountTransaction.is_void == False,
        or_(AccountTransaction.from_account_id.in_(cash_ids), AccountTransaction.to_account_id.in_(cash_ids)),
    )
    if start_day:
        query = query.filter(func.date(AccountTransaction.date_posted) >= start_day.isoformat())
    if end_day:
        query = query.filter(func.date(AccountTransaction.date_posted) <= end_day.isoformat())
    net = 0
    for tx in query.all():
        minor = _cf_minor(tx)
        if tx.to_account_id in cash_ids:
            net += minor
        if tx.from_account_id in cash_ids:
            net -= minor
    return net


def _physical_cash_report_closing(from_day, to_day):
    """Expected physical cash, never the combined cash+bank report closing."""
    previous = from_day - timedelta(days=1)
    accounts = [account for account in _cf_company_accounts(active_only=False)
                if (account.category or '').strip().lower() == 'cash']
    last = CashFlowDifferenceAdjustment.query.filter(
        CashFlowDifferenceAdjustment.adjustment_date < from_day,
        CashFlowDifferenceAdjustment.physical_cash_available.isnot(None),
    ).order_by(CashFlowDifferenceAdjustment.adjustment_date.desc()).first()
    if last:
        count_day = last.adjustment_date
        opening = to_minor(last.physical_cash_available or 0)
        opening += sum(_account_opening_minor(account) for account in accounts
                       if _opening_day(account) and count_day < _opening_day(account) <= from_day)
        start = count_day + timedelta(days=1)
    else:
        opening = sum(_account_opening_minor(account) for account in accounts
                      if not _opening_day(account) or _opening_day(account) <= from_day)
        start = None
    opening += _cash_account_net_between(start, previous)
    opening -= to_minor(_legacy_adjustments_total(start, previous))
    period_openings = sum(row['amount_minor'] for row in
                          _report_period_openings(from_day, to_day, cash_only=True))
    return float(from_minor(opening + period_openings + _cash_account_net_between(from_day, to_day)))


def _cash_count_carry_adjustments(from_day, to_day):
    """Physical/legacy count resets within a report, effective the next day.

    These are display baselines, not postings. Compare each physical count
    with the preceding cash-only roll-forward so multiple counts, transfers
    and new cash-account openings do not double-count a difference.
    """
    if from_day >= to_day:
        return []
    counts = (CashFlowDifferenceAdjustment.query
              .filter(CashFlowDifferenceAdjustment.adjustment_date >= from_day,
                      CashFlowDifferenceAdjustment.adjustment_date < to_day)
              .order_by(CashFlowDifferenceAdjustment.adjustment_date).all())
    if not counts:
        return []
    openings = sorted(_report_period_openings(from_day, to_day, cash_only=True), key=lambda item: item['date'])
    opening_pos = 0
    cursor = from_day
    balance = to_minor(_physical_cash_report_closing(from_day, from_day - timedelta(days=1)))
    results = []
    for count in counts:
        count_day = count.adjustment_date
        balance += _cash_account_net_between(cursor, count_day)
        while opening_pos < len(openings) and openings[opening_pos]['date'] <= count_day:
            balance += openings[opening_pos]['amount_minor']
            opening_pos += 1
        if count.physical_cash_available is not None:
            counted = to_minor(count.physical_cash_available)
            delta = counted - balance
            kind = 'Physical count'
        else:
            delta = -to_minor(count.amount or 0)
            counted = balance + delta
            kind = 'Legacy difference'
        results.append({
            'date': count_day, 'effective_date': count_day + timedelta(days=1), 'kind': kind,
            'calculated': float(from_minor(balance)), 'counted': float(from_minor(counted)),
            'delta_minor': delta, 'delta': float(from_minor(delta)),
        })
        balance = counted
        cursor = count_day + timedelta(days=1)
    return results


def _current_username():
    return current_user.username if current_user and current_user.is_authenticated else None


def _cash_flow_today_opening_override(today_str):
    override = session.get('cash_flow_today_opening_override') or {}
    if override.get('date') != today_str:
        return None
    try:
        return _money_round(override.get('amount', 0))
    except Exception:
        return None


def _cash_flow_fresh_start_cutoff(today_str):
    cutoff = session.get('cash_flow_fresh_start_cutoff') or {}
    if cutoff.get('date') != today_str or not cutoff.get('at'):
        cutoff = {'date': today_str, 'at': pk_now().strftime('%Y-%m-%d %H:%M:%S')}
        session['cash_flow_fresh_start_cutoff'] = cutoff
    try:
        return datetime.strptime(cutoff['at'], '%Y-%m-%d %H:%M:%S')
    except Exception:
        cutoff = {'date': today_str, 'at': pk_now().strftime('%Y-%m-%d %H:%M:%S')}
        session['cash_flow_fresh_start_cutoff'] = cutoff
        return datetime.strptime(cutoff['at'], '%Y-%m-%d %H:%M:%S')


# ---------------------------------------------------------------------------
# Configuration-driven Cash Flow engine
# Rules live here. Business category names do not.
# ---------------------------------------------------------------------------

CF_DIR_IN = 'in'
CF_DIR_OUT = 'out'
CF_DIR_TRANSFER = 'transfer'
CF_DIRECTIONS = (CF_DIR_IN, CF_DIR_OUT, CF_DIR_TRANSFER)
CF_CAT_DIRECTIONS = (CF_DIR_IN, CF_DIR_OUT, 'both')

SRC_MANUAL = 'MANUAL_CASH_FLOW'
SRC_CLIENT_PAYMENT = 'CLIENT_PAYMENT'
SRC_SUPPLIER_PAYMENT = 'SUPPLIER_PAYMENT'
SRC_SALE = 'SALE'
SRC_BOOKING = 'BOOKING'
SRC_ADJUSTMENT = 'ACCOUNT_ADJUSTMENT'
SRC_TRANSFER = 'TRANSFER'
SRC_DRIVER_PAYMENT = 'DRIVER_PAYMENT'
SRC_ACCOUNT_RECEIPT = 'ACCOUNT_RECEIPT'
SRC_ACCOUNT_EXPENSE = 'ACCOUNT_EXPENSE'

CF_SOURCE_LABELS = {
    SRC_MANUAL: 'Manual',
    SRC_CLIENT_PAYMENT: 'Client',
    SRC_SUPPLIER_PAYMENT: 'Supplier',
    SRC_SALE: 'Sale',
    SRC_BOOKING: 'Booking',
    SRC_ADJUSTMENT: 'Account adjustment',
    SRC_TRANSFER: 'Transfer',
    SRC_DRIVER_PAYMENT: 'Driver',
    SRC_ACCOUNT_RECEIPT: 'Other system-generated',
    SRC_ACCOUNT_EXPENSE: 'Other system-generated',
}

CF_PARTY_TYPES = [
    ('person', 'Person'),
    ('outsider', 'Outsider / other party'),
    ('loan', 'Loan (person or outside lender)'),
    ('other', 'Other'),
]

CF_SRC_MARKER = '[SRC:CashFlow]'
_MIRROR_MARKERS = (
    '[SRC:BOOKING:',
    '[SRC:DIRECTSALE:',
    '[SRC:PAYMENT:',
    '[SRC:SUPPLIERPAYMENT:',
    '[SRC:CLIENTREFUND:',
)


def _cf_actor_name(actor=None):
    if actor is not None and getattr(actor, 'username', None):
        return actor.username
    return _current_username()


def _cf_is_money_account(account):
    if account is None:
        return False
    if getattr(account, 'is_active', True) is False:
        return False
    return (account.category or '').lower() in ('cash', 'bank')


def _cf_company_accounts(active_only=True):
    from app.services.account_setup import setup_accounts_query
    q = setup_accounts_query()
    if active_only:
        q = q.filter(func.coalesce(Account.is_active, True) == True)
    return q.order_by(Account.name.asc(), Account.id.asc()).all()


def _cf_account_label(account):
    if account is None:
        return ''
    from app.services.account_setup import account_type_key, ACCOUNT_TYPES
    label = ACCOUNT_TYPES[account_type_key(account)]['label']
    from app.services.accounting import _account_minor
    bal = float(from_minor(_account_minor(account)))
    return f'{account.name} · {label} · Rs. {bal:,.2f}'


def _cf_normalize_direction(value):
    raw = (value or '').strip().lower()
    if raw in ('received', 'receive', 'in', 'cash_in'):
        return CF_DIR_IN
    if raw in ('spent', 'spend', 'out', 'cash_out', 'paid'):
        return CF_DIR_OUT
    if raw in ('transfer', 'xfer'):
        return CF_DIR_TRANSFER
    return raw


def _cf_type_label(direction):
    return {
        CF_DIR_IN: 'Received',
        CF_DIR_OUT: 'Spent',
        CF_DIR_TRANSFER: 'Transfer',
    }.get(direction, direction or '')


def _cf_ensure_indexes():
    try:
        db.session.execute(text(
            'CREATE UNIQUE INDEX IF NOT EXISTS uq_cash_flow_entry_idempotency_key '
            'ON cash_flow_entry(idempotency_key) WHERE idempotency_key IS NOT NULL'
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _cf_snapshot(entry):
    return {
        'id': entry.id,
        'direction': entry.direction,
        'amount': float(entry.amount or 0),
        'account_id': entry.account_id,
        'destination_account_id': getattr(entry, 'destination_account_id', None),
        'category_id': entry.category_id,
        'subcategory_id': entry.subcategory_id,
        'party_id': entry.party_id,
        'party_name': entry.party_name,
        'party_type': entry.party_type,
        'description': entry.description,
        'note': entry.note,
        'reference': getattr(entry, 'reference', None),
        'date_posted': str(entry.date_posted or ''),
        'is_void': bool(entry.is_void),
        'source_type': getattr(entry, 'source_type', None) or SRC_MANUAL,
    }


def _cf_write_audit(entry, action, before=None, after=None, reason=None, actor=None):
    db.session.add(CashFlowEntryAudit(
        entry_id=entry.id,
        action=action,
        before_json=json.dumps(before, default=str) if before is not None else None,
        after_json=json.dumps(after, default=str) if after is not None else None,
        reason=(reason or '').strip() or None,
        changed_by=_cf_actor_name(actor),
        changed_at=pk_now(),
    ))


def _cf_find_category(category_id=None, name=None, active_only=True):
    if category_id:
        cat = db.session.get(CashFlowCategory, category_id)
        if cat and (not active_only or cat.is_active):
            return cat
        return None
    name = (name or '').strip()
    if not name:
        return None
    q = CashFlowCategory.query.filter(func.lower(CashFlowCategory.name) == name.lower())
    if active_only:
        q = q.filter(CashFlowCategory.is_active == True)
    return q.first()


def _cf_find_subcategory(category, subcategory_id=None, name=None, active_only=True):
    if not category:
        return None
    if subcategory_id:
        sub = db.session.get(CashFlowSubcategory, subcategory_id)
        if sub and sub.category_id == category.id and (not active_only or sub.is_active):
            return sub
        return None
    name = (name or '').strip()
    if not name:
        return None
    q = CashFlowSubcategory.query.filter(
        CashFlowSubcategory.category_id == category.id,
        func.lower(CashFlowSubcategory.name) == name.lower(),
    )
    if active_only:
        q = q.filter(CashFlowSubcategory.is_active == True)
    return q.first()


def _cf_find_party(party_id=None, name=None, party_type=None, active_only=True):
    if party_id:
        party = db.session.get(CashFlowParty, party_id)
        if party and (not active_only or party.is_active):
            return party
        return None
    name = (name or '').strip()
    if not name:
        return None
    q = CashFlowParty.query.filter(func.lower(CashFlowParty.name) == name.lower())
    if party_type:
        q = q.filter(func.lower(func.coalesce(CashFlowParty.party_type, '')) == party_type.lower())
    if active_only:
        q = q.filter(CashFlowParty.is_active == True)
    return q.first()


def save_cf_category(name, direction='both', notes=None, actor=None):
    name = (name or '').strip()
    direction = (direction or 'both').strip().lower()
    if not name:
        raise ValueError('Category name is required.')
    if direction not in CF_CAT_DIRECTIONS:
        raise ValueError('Category direction must be Received, Spent, or Both.')
    existing = CashFlowCategory.query.filter(func.lower(CashFlowCategory.name) == name.lower()).first()
    if existing:
        if not existing.is_active:
            existing.is_active = True
        existing.direction = direction
        if notes is not None:
            existing.notes = (notes or '').strip() or None
        existing.updated_at = pk_now()
        db.session.flush()
        return existing, False
    cat = CashFlowCategory(
        name=name, direction=direction, is_active=True,
        notes=(notes or '').strip() or None, created_at=pk_now(), updated_at=pk_now(),
    )
    db.session.add(cat)
    db.session.flush()
    return cat, True


def save_cf_subcategory(category_id, name, notes=None, actor=None):
    name = (name or '').strip()
    cat = db.session.get(CashFlowCategory, category_id) if category_id else None
    if not cat:
        raise ValueError('Pick a parent category.')
    if not name:
        raise ValueError('Sub-category name is required.')
    existing = CashFlowSubcategory.query.filter(
        CashFlowSubcategory.category_id == cat.id,
        func.lower(CashFlowSubcategory.name) == name.lower(),
    ).first()
    if existing:
        if not existing.is_active:
            existing.is_active = True
        if notes is not None:
            existing.notes = (notes or '').strip() or None
        existing.updated_at = pk_now()
        db.session.flush()
        return existing, False
    sub = CashFlowSubcategory(
        category_id=cat.id, name=name, is_active=True,
        notes=(notes or '').strip() or None, created_at=pk_now(), updated_at=pk_now(),
    )
    db.session.add(sub)
    db.session.flush()
    return sub, True


def save_cf_party(name, party_type='person', note=None, actor=None):
    name = (name or '').strip()
    party_type = (party_type or 'other').strip().lower() or 'other'
    if not name:
        raise ValueError('Name is required.')
    existing = _cf_find_party(name=name, party_type=party_type, active_only=False)
    if existing:
        existing.is_active = True
        if note is not None:
            existing.note = (note or '').strip() or None
        existing.updated_at = pk_now()
        db.session.flush()
        return existing, False
    party = CashFlowParty(
        name=name, party_type=party_type, note=(note or '').strip() or None,
        is_active=True, created_at=pk_now(), updated_at=pk_now(),
    )
    db.session.add(party)
    db.session.flush()
    return party, True


def disable_cf_category(category_id):
    cat = db.session.get(CashFlowCategory, category_id)
    if not cat:
        raise ValueError('Category not found.')
    cat.is_active = False
    cat.updated_at = pk_now()
    db.session.flush()
    return cat


def enable_cf_category(category_id):
    cat = db.session.get(CashFlowCategory, category_id)
    if not cat:
        raise ValueError('Category not found.')
    cat.is_active = True
    cat.updated_at = pk_now()
    db.session.flush()
    return cat


def disable_cf_subcategory(subcategory_id):
    sub = db.session.get(CashFlowSubcategory, subcategory_id)
    if not sub:
        raise ValueError('Sub-category not found.')
    sub.is_active = False
    sub.updated_at = pk_now()
    db.session.flush()
    return sub


def enable_cf_subcategory(subcategory_id):
    sub = db.session.get(CashFlowSubcategory, subcategory_id)
    if not sub:
        raise ValueError('Sub-category not found.')
    sub.is_active = True
    sub.updated_at = pk_now()
    db.session.flush()
    return sub


def disable_cf_party(party_id):
    party = db.session.get(CashFlowParty, party_id)
    if not party:
        raise ValueError('Party not found.')
    party.is_active = False
    party.updated_at = pk_now()
    db.session.flush()
    return party


def enable_cf_party(party_id):
    party = db.session.get(CashFlowParty, party_id)
    if not party:
        raise ValueError('Party not found.')
    party.is_active = True
    party.updated_at = pk_now()
    db.session.flush()
    return party


def update_cf_party(party_id, name, party_type=None, note=None):
    party = db.session.get(CashFlowParty, party_id)
    if not party:
        raise ValueError('Party not found.')
    name = (name or '').strip()
    if not name:
        raise ValueError('Name is required.')
    party.name = name
    if party_type:
        party.party_type = (party_type or 'other').strip().lower() or 'other'
    if note is not None:
        party.note = (note or '').strip() or None
    party.updated_at = pk_now()
    db.session.flush()
    return party


def rename_cf_category(category_id, name, direction=None, notes=None):
    cat = db.session.get(CashFlowCategory, category_id)
    if not cat:
        raise ValueError('Category not found.')
    name = (name or '').strip()
    if not name:
        raise ValueError('Category name is required.')
    clash = CashFlowCategory.query.filter(
        func.lower(CashFlowCategory.name) == name.lower(),
        CashFlowCategory.id != cat.id,
    ).first()
    if clash:
        raise ValueError('Another category already uses that name.')
    cat.name = name
    if direction in CF_CAT_DIRECTIONS:
        cat.direction = direction
    if notes is not None:
        cat.notes = (notes or '').strip() or None
    cat.updated_at = pk_now()
    db.session.flush()
    return cat


def rename_cf_subcategory(subcategory_id, name, notes=None):
    sub = db.session.get(CashFlowSubcategory, subcategory_id)
    if not sub:
        raise ValueError('Sub-category not found.')
    name = (name or '').strip()
    if not name:
        raise ValueError('Sub-category name is required.')
    clash = CashFlowSubcategory.query.filter(
        CashFlowSubcategory.category_id == sub.category_id,
        func.lower(CashFlowSubcategory.name) == name.lower(),
        CashFlowSubcategory.id != sub.id,
    ).first()
    if clash:
        raise ValueError('Another sub-category already uses that name under this category.')
    sub.name = name
    if notes is not None:
        sub.notes = (notes or '').strip() or None
    sub.updated_at = pk_now()
    db.session.flush()
    return sub


def cf_used_category_ids():
    rows = db.session.query(CashFlowEntry.category_id).filter(
        CashFlowEntry.category_id.isnot(None)
    ).distinct().all()
    return {row[0] for row in rows}


def cf_used_subcategory_ids():
    rows = db.session.query(CashFlowEntry.subcategory_id).filter(
        CashFlowEntry.subcategory_id.isnot(None)
    ).distinct().all()
    return {row[0] for row in rows}


def cf_used_party_ids():
    rows = db.session.query(CashFlowEntry.party_id).filter(
        CashFlowEntry.party_id.isnot(None)
    ).distinct().all()
    return {row[0] for row in rows}


def _cf_category_is_used(category_id):
    if CashFlowEntry.query.filter(CashFlowEntry.category_id == category_id).first():
        return True
    sub_ids = [
        row[0] for row in db.session.query(CashFlowSubcategory.id).filter(
            CashFlowSubcategory.category_id == category_id
        ).all()
    ]
    if sub_ids and CashFlowEntry.query.filter(CashFlowEntry.subcategory_id.in_(sub_ids)).first():
        return True
    return False


def delete_cf_category(category_id):
    """Hard-delete an unused category. Historically used categories must be disabled."""
    cat = db.session.get(CashFlowCategory, category_id)
    if not cat:
        raise ValueError('Category not found.')
    linked_modules = []
    if CashFlowEntry.query.filter(CashFlowEntry.category_id == cat.id).first():
        linked_modules.append('CashFlowEntry')
    sub_ids = [
        row[0] for row in db.session.query(CashFlowSubcategory.id).filter(
            CashFlowSubcategory.category_id == cat.id
        ).all()
    ]
    if sub_ids:
        if CashFlowEntry.query.filter(CashFlowEntry.subcategory_id.in_(sub_ids)).first():
            linked_modules.append('CashFlowEntry (via subcategory)')
        # If subcategories exist but are not directly linked to entries,
        # deleting the category will cascade-remove them (clear link message)
        if not linked_modules:
            linked_modules.append('CashFlowSubcategory (will be removed with category)')
    if _cf_category_is_used(cat.id):
        msg = 'This category is linked to: ' + ', '.join(linked_modules) if linked_modules else 'This category is used by historical transactions.'
        msg += ' Cannot delete — disable it instead, or delete from the linked modules first.'
        raise ValueError(msg)
    CashFlowSubcategory.query.filter(CashFlowSubcategory.category_id == cat.id).delete(
        synchronize_session=False
    )
    db.session.delete(cat)
    db.session.flush()
    return True


def delete_cf_subcategory(subcategory_id):
    """Hard-delete an unused sub-category. Historically used ones must be disabled."""
    sub = db.session.get(CashFlowSubcategory, subcategory_id)
    if not sub:
        raise ValueError('Sub-category not found.')
    linked_modules = []
    if CashFlowEntry.query.filter(CashFlowEntry.subcategory_id == sub.id).first():
        linked_modules.append('CashFlowEntry')
    if linked_modules:
        msg = 'This sub-category is linked to: ' + ', '.join(linked_modules)
        msg += '. Cannot delete — disable it instead, or delete from the linked modules first.'
        raise ValueError(msg)
    db.session.delete(sub)
    db.session.flush()
    return True


def delete_cf_party(party_id):
    """Hard-delete an unused party. Historically used parties must be disabled."""
    party = db.session.get(CashFlowParty, party_id)
    if not party:
        raise ValueError('Party not found.')
    linked_modules = []
    if CashFlowEntry.query.filter(CashFlowEntry.party_id == party.id).first():
        linked_modules.append('CashFlowEntry')
    if linked_modules:
        msg = 'This party is linked to: ' + ', '.join(linked_modules)
        msg += '. Cannot delete — disable it instead, or delete from the linked modules first.'
        raise ValueError(msg)
    db.session.delete(party)
    db.session.flush()
    return True


def parse_physical_cash_amount(raw):
    """Parse Physical Cash Available.

    Zero is valid. Empty / missing is invalid. Invalid text is rejected.
    Do not treat 0 as missing.
    """
    if raw is None:
        raise ValueError('Physical Cash Available is required. Difference is calculated by the system.')
    text = str(raw).strip().replace(',', '')
    for prefix in ('Rs.', 'rs.', 'RS.', 'PKR', 'pkr'):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    if text == '':
        raise ValueError('Physical Cash Available is required. Difference is calculated by the system.')
    try:
        amount = Decimal(text)
    except Exception:
        raise ValueError('Physical Cash Available must be a valid number.')
    amount = amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if amount == Decimal('-0.00'):
        amount = Decimal('0.00')
    return float(amount)


def compute_physical_cash_difference(physical_cash, calculated_closing):
    """Difference = Physical Cash Available - System Calculated Closing."""
    return _money_round(physical_cash) - _money_round(calculated_closing)


def _cf_resolve_category(direction, category_id, category_name, *, required, create_if_missing):
    if direction == CF_DIR_TRANSFER:
        return None
    cat = _cf_find_category(category_id=category_id, name=category_name, active_only=True)
    if cat:
        allowed = (cat.direction or 'both').lower()
        if allowed not in ('both', direction):
            raise ValueError('Selected category is not allowed for this transaction type.')
        return cat
    name = (category_name or '').strip()
    if required and not name and not category_id:
        raise ValueError('Category is required for Received and Spent.')
    if category_id and not cat:
        raise ValueError('Selected category is missing or inactive.')
    if name and create_if_missing:
        cat, _ = save_cf_category(name, direction=direction)
        return cat
    if required:
        raise ValueError('Category must exist and be active. Create it in Categories first.')
    return None


def _cf_resolve_subcategory(category, subcategory_id, subcategory_name, *, create_if_missing):
    if not category:
        if subcategory_id or (subcategory_name or '').strip():
            raise ValueError('Sub-category requires a category.')
        return None
    sub = _cf_find_subcategory(category, subcategory_id=subcategory_id, name=subcategory_name, active_only=True)
    if sub:
        return sub
    if subcategory_id:
        raise ValueError('Sub-category does not belong to the selected category.')
    name = (subcategory_name or '').strip()
    if name and create_if_missing:
        sub, _ = save_cf_subcategory(category.id, name)
        return sub
    if name:
        raise ValueError('Sub-category must belong to the selected category.')
    return None


def _cf_build_tx_fields(direction, amount, account, destination, description, note, posted, entry_id=None):
    from utils.money import to_minor
    marker = CF_SRC_MARKER if not entry_id else f'[SRC:CashFlow:{int(entry_id)}]'
    note_txt = ' '.join(x for x in [(note or '').strip(), marker] if x).strip()
    amount = _money_round(amount)
    fields = {
        'amount': amount,
        'amount_minor': to_minor(amount),
        'description': description,
        'note': note_txt,
        'date_posted': posted,
        'source_type': 'CashFlowEntry',
        'source_id': entry_id,
        'is_void': False,
    }
    if direction == CF_DIR_IN:
        fields.update(from_account_id=None, to_account_id=account.id, transaction_type='Receipt')
    elif direction == CF_DIR_OUT:
        fields.update(from_account_id=account.id, to_account_id=None, transaction_type='Expense')
    else:
        fields.update(
            from_account_id=account.id,
            to_account_id=destination.id,
            transaction_type='Transfer',
        )
    return fields


def validate_manual_cash_flow(
    *, direction, amount, account_id, destination_account_id=None,
    category_id=None, category_name=None, subcategory_id=None, subcategory_name=None,
    require_category=True, create_missing=False, check_balance=True,
):
    direction = _cf_normalize_direction(direction)
    if direction not in CF_DIRECTIONS:
        raise ValueError('Choose Received, Spent, or Transfer.')
    amount = _money_round(amount)
    if amount <= 0:
        raise ValueError('Amount must be greater than zero.')
    account = db.session.get(Account, account_id) if account_id else None
    if not _cf_is_money_account(account):
        raise ValueError('Select a valid company cash or bank account.')
    destination = None
    if direction == CF_DIR_TRANSFER:
        destination = db.session.get(Account, destination_account_id) if destination_account_id else None
        if not _cf_is_money_account(destination):
            raise ValueError('Select a valid destination account.')
        if destination.id == account.id:
            raise ValueError('Source and destination accounts cannot be the same.')
    else:
        cat = _cf_resolve_category(
            direction, category_id, category_name,
            required=require_category, create_if_missing=create_missing,
        )
        _cf_resolve_subcategory(cat, subcategory_id, subcategory_name, create_if_missing=create_missing)
    if check_balance and direction in (CF_DIR_OUT, CF_DIR_TRANSFER) and float(account.balance or 0) < amount:
        raise ValueError(f'Insufficient balance in {account.name}.')
    return direction, amount, account, destination


def save_manual_cash_flow_entry(
    *, direction, amount, account_id, destination_account_id=None,
    category_id=None, category_name=None, subcategory_id=None, subcategory_name=None,
    party_id=None, party_name=None, party_type=None, description=None, note=None,
    reference=None, date_posted=None, idempotency_key=None, actor=None,
    create_missing=True,
):
    from app.services.accounting import _apply_account_tx_effect

    _cf_ensure_indexes()
    key = (idempotency_key or '').strip() or None
    if key:
        existing = CashFlowEntry.query.filter(CashFlowEntry.idempotency_key == key).first()
        if existing:
            return existing, False

    direction, amount, account, destination = validate_manual_cash_flow(
        direction=direction, amount=amount, account_id=account_id,
        destination_account_id=destination_account_id, category_id=category_id,
        category_name=category_name, subcategory_id=subcategory_id,
        subcategory_name=subcategory_name, require_category=True,
        create_missing=create_missing,
    )
    cat = _cf_resolve_category(
        direction, category_id, category_name, required=(direction != CF_DIR_TRANSFER),
        create_if_missing=create_missing,
    ) if direction != CF_DIR_TRANSFER else None
    sub = _cf_resolve_subcategory(cat, subcategory_id, subcategory_name, create_if_missing=create_missing)
    ptype = (party_type or 'other').strip().lower() or 'other'
    party = _cf_find_party(party_id=party_id, name=party_name, party_type=ptype, active_only=True)
    if not party and (party_name or '').strip():
        party, _ = save_cf_party(party_name, ptype)
    if party:
        party_name = party.name
        ptype = party.party_type or ptype

    posted = date_posted or pk_now()
    desc = (description or '').strip()
    if not desc:
        desc = (cat.name if cat else _cf_type_label(direction))
        if (party_name or '').strip():
            desc = f'{desc} — {party_name.strip()}'
    note = (note or '').strip() or None
    reference = (reference or '').strip() or None
    actor_name = _cf_actor_name(actor)

    from utils.money import to_minor
    entry = CashFlowEntry(
        direction=direction, amount=amount, amount_minor=to_minor(amount),
        account_id=account.id,
        destination_account_id=(destination.id if destination else None),
        category_id=cat.id if cat else None,
        subcategory_id=sub.id if sub else None,
        party_id=party.id if party else None,
        party_name=(party_name or '').strip() or None,
        party_type=ptype,
        description=desc, note=note, reference=reference,
        date_posted=posted, created_by=actor_name, updated_by=actor_name,
        source_type=SRC_MANUAL, is_void=False, revision=1,
        idempotency_key=key, created_at=pk_now(), updated_at=pk_now(),
    )
    db.session.add(entry)
    db.session.flush()

    tx_fields = _cf_build_tx_fields(direction, amount, account, destination, desc, note, posted, entry.id)
    tx_fields['created_by'] = actor_name
    tx = AccountTransaction(**tx_fields)
    db.session.add(tx)
    db.session.flush()
    _apply_account_tx_effect(tx)
    entry.account_tx_id = tx.id
    _cf_write_audit(entry, 'Created', after=_cf_snapshot(entry), actor=actor)
    db.session.flush()
    return entry, True


def update_manual_cash_flow_entry(
    entry, *, direction, amount, account_id, destination_account_id=None,
    category_id=None, category_name=None, subcategory_id=None, subcategory_name=None,
    party_id=None, party_name=None, party_type=None, description=None, note=None,
    reference=None, date_posted=None, reason=None, actor=None, create_missing=True,
):
    from app.services.accounting import _apply_account_tx_effect, _reverse_account_tx_effect

    if entry is None:
        raise ValueError('Entry not found.')
    if entry.is_void:
        raise ValueError('A voided transaction cannot be edited. Restore it first.')

    before = _cf_snapshot(entry)
    direction, amount, account, destination = validate_manual_cash_flow(
        direction=direction, amount=amount, account_id=account_id,
        destination_account_id=destination_account_id, category_id=category_id,
        category_name=category_name, subcategory_id=subcategory_id,
        subcategory_name=subcategory_name, require_category=True,
        create_missing=create_missing, check_balance=False,
    )
    # Reverse first so the insufficient-balance check sees the pre-transaction balance.
    tx = db.session.get(AccountTransaction, entry.account_tx_id) if entry.account_tx_id else None
    if tx and not tx.is_void:
        _reverse_account_tx_effect(tx)

    if direction in (CF_DIR_OUT, CF_DIR_TRANSFER) and float(account.balance or 0) < amount:
        if tx and not tx.is_void:
            _apply_account_tx_effect(tx)
        raise ValueError(f'Insufficient balance in {account.name}.')

    cat = _cf_resolve_category(
        direction, category_id, category_name, required=(direction != CF_DIR_TRANSFER),
        create_if_missing=create_missing,
    ) if direction != CF_DIR_TRANSFER else None
    sub = _cf_resolve_subcategory(cat, subcategory_id, subcategory_name, create_if_missing=create_missing)
    ptype = (party_type or entry.party_type or 'other').strip().lower() or 'other'
    party = _cf_find_party(party_id=party_id, name=party_name, party_type=ptype, active_only=True)
    if not party and (party_name or '').strip():
        party, _ = save_cf_party(party_name, ptype)
    if party:
        party_name = party.name
        ptype = party.party_type or ptype

    posted = date_posted or entry.date_posted or pk_now()
    desc = (description or '').strip() or entry.description or _cf_type_label(direction)
    note = (note or '').strip() or None
    reference = (reference if reference is not None else entry.reference)
    reference = (reference or '').strip() or None
    actor_name = _cf_actor_name(actor)

    from utils.money import to_minor
    entry.direction = direction
    entry.amount = amount
    entry.amount_minor = to_minor(amount)
    entry.account_id = account.id
    entry.destination_account_id = destination.id if destination else None
    entry.category_id = cat.id if cat else None
    entry.subcategory_id = sub.id if sub else None
    entry.party_id = party.id if party else None
    entry.party_name = (party_name or '').strip() or None
    entry.party_type = ptype
    entry.description = desc
    entry.note = note
    entry.reference = reference
    entry.date_posted = posted
    entry.updated_by = actor_name
    entry.updated_at = pk_now()
    entry.revision = int(entry.revision or 1) + 1

    tx_fields = _cf_build_tx_fields(direction, amount, account, destination, desc, note, posted, entry.id)
    if tx is None:
        tx = AccountTransaction(created_by=actor_name, **tx_fields)
        db.session.add(tx)
        db.session.flush()
        entry.account_tx_id = tx.id
    else:
        for key, value in tx_fields.items():
            setattr(tx, key, value)
    _apply_account_tx_effect(tx)
    _cf_write_audit(entry, 'Edited', before=before, after=_cf_snapshot(entry), reason=reason, actor=actor)
    db.session.flush()
    return entry


def void_manual_cash_flow_entry(entry, reason=None, actor=None):
    from app.services.accounting import _void_account_tx

    if entry is None:
        raise ValueError('Entry not found.')
    if entry.is_void:
        raise ValueError('This transaction is already voided.')
    before = _cf_snapshot(entry)
    tx = db.session.get(AccountTransaction, entry.account_tx_id) if entry.account_tx_id else None
    if tx:
        from app.services.payments_crud import _assert_period_open
        for account_id in {tx.from_account_id, tx.to_account_id}:
            _assert_period_open(account_id, tx.date_posted, operation='voided')
    if tx and not tx.is_void:
        _void_account_tx(tx)
        tx.voided_by = _cf_actor_name(actor)
        tx.voided_at = pk_now()
    entry.is_void = True
    entry.voided_at = pk_now()
    entry.voided_by = _cf_actor_name(actor)
    entry.void_reason = (reason or '').strip() or None
    entry.updated_by = _cf_actor_name(actor)
    entry.updated_at = pk_now()
    entry.revision = int(entry.revision or 1) + 1
    _cf_write_audit(entry, 'Voided', before=before, after=_cf_snapshot(entry), reason=reason, actor=actor)
    db.session.flush()
    return entry


def restore_manual_cash_flow_entry(entry, reason=None, actor=None):
    from app.services.accounting import _unvoid_account_tx

    if entry is None:
        raise ValueError('Entry not found.')
    if not entry.is_void:
        raise ValueError('This transaction is already active.')
    before = _cf_snapshot(entry)
    tx = db.session.get(AccountTransaction, entry.account_tx_id) if entry.account_tx_id else None
    if tx:
        from app.services.payments_crud import _assert_period_open
        for account_id in {tx.from_account_id, tx.to_account_id}:
            _assert_period_open(account_id, tx.date_posted, operation='restored')
    if tx and tx.is_void:
        _unvoid_account_tx(tx)
    entry.is_void = False
    entry.voided_at = None
    entry.voided_by = None
    entry.void_reason = None
    entry.updated_by = _cf_actor_name(actor)
    entry.updated_at = pk_now()
    entry.revision = int(entry.revision or 1) + 1
    _cf_write_audit(entry, 'Restored', before=before, after=_cf_snapshot(entry), reason=reason, actor=actor)
    db.session.flush()
    return entry


def _cf_row(
    *, sort_dt, direction, amount, reference, description, note='',
    category='', subcategory='', party_name='', party_type='',
    account_id=None, account_name='', dest_account_id=None, dest_account_name='',
    source_type=SRC_MANUAL, origin='recorded', origin_label='',
    created_by='', status='active', entry_id=None, tx_id=None, source_id=None, method='', warning='',
):
    amount = float(amount or 0)
    cash_in = amount if direction == CF_DIR_IN else 0.0
    cash_out = amount if direction == CF_DIR_OUT else 0.0
    transfer_amount = amount if direction == CF_DIR_TRANSFER else 0.0
    if dest_account_name and direction == CF_DIR_TRANSFER:
        account_display = f'{account_name or "—"} → {dest_account_name}'
    else:
        account_display = account_name or '—'
    dt = sort_dt
    date_val = dt.date() if hasattr(dt, 'date') else dt
    return {
        'date': date_val,
        'sort_dt': dt,
        'type': direction,
        'tx_type_label': _cf_type_label(direction),
        'cash_in': cash_in,
        'cash_out': cash_out,
        'transfer_amount': transfer_amount,
        'amount': amount,
        'account_id': account_id,
        'account_name': account_name or '',
        'account_to_id': dest_account_id,
        'account_to_name': dest_account_name or '',
        'account_display': account_display,
        'category': category or '',
        'subcategory': subcategory or '',
        'party_name': party_name or '',
        'party_type': party_type or '',
        'description': description or '',
        'note': note or '',
        'reference': reference or '',
        'source': 'MANUAL' if source_type == SRC_MANUAL else 'SYSTEM',
        'source_type': source_type,
        'source_id': source_id,
        'method': method or '',
        'warning': warning or '',
        'origin': origin,
        'origin_label': origin_label or CF_SOURCE_LABELS.get(source_type, source_type),
        'created_by': created_by or '',
        'status': status,
        'entry_id': entry_id,
        'tx_id': tx_id,
    }


def _cf_is_mirror_tx(tx):
    note_u = (tx.note or '').upper()
    if CF_SRC_MARKER.upper() in note_u or '[SRC:CASHFLOW' in note_u:
        return True
    source_type = (getattr(tx, 'source_type', None) or '').strip().lower()
    if source_type in ('cashflowentry', 'booking', 'directsale', 'payment', 'supplierpayment', 'clientrefund'):
        return True
    return any(marker in note_u for marker in _MIRROR_MARKERS)


def _cf_minor(row, field='amount'):
    """Use the stored integer amount where a source model provides one."""
    minor = getattr(row, f'{field}_minor', None)
    return int(minor) if minor is not None else to_minor(getattr(row, field, 0) or 0)


def _cf_money_account(account):
    return bool(account and (account.category or '').strip().lower() in ('cash', 'bank'))


def _cf_method(account, explicit=None, tx=None):
    value = (explicit or '').strip()
    if value:
        lowered = value.lower()
        if 'wallet' in lowered or lowered in {'online', 'digital'}:
            return 'Online'
        if lowered in {'cheque', 'check'}:
            return 'Check'
        return value
    if tx:
        match = re.search(r'\bMethod:\s*([^\[]+)', tx.note or '', re.IGNORECASE)
        if match and match.group(1).strip():
            return match.group(1).strip()
    if not _cf_money_account(account):
        return ''
    if (account.channel or '').strip().lower() == 'digital_wallet':
        return 'Online'
    return 'Cash' if (account.category or '').strip().lower() == 'cash' else 'Bank'


def _cf_company_money_account(account):
    # The setup workspace defines company-owned money accounts this way.  Do
    # not treat a loan/party account as a second company cash account just
    # because its legacy category happens to be named cash.
    return _cf_money_account(account) and (account.account_type or '').strip().lower() == 'company'


def _cf_source_account(accounts, account_id, tx, *, incoming):
    """Only a real linked ledger side is evidence of an account movement."""
    if not tx:
        return None
    return accounts.get(tx.to_account_id if incoming else tx.from_account_id)


def _cf_source_warning(source_minor, source_account_id, tx, account):
    warnings = []
    if abs(source_minor) != abs(_cf_minor(tx)):
        warnings.append(f'Source amount {float(from_minor(abs(source_minor))):.2f} differs from account posting')
    if source_account_id is not None and source_account_id != account.id:
        warnings.append('Source account differs from account posting')
    return '; '.join(warnings)


def _cf_linked_source(tx):
    kind = (getattr(tx, 'source_type', None) or '').strip().lower()
    source_id = getattr(tx, 'source_id', None)
    if kind in ('booking', 'directsale', 'payment', 'supplierpayment', 'clientrefund') and source_id:
        return ('payment' if kind == 'clientrefund' else kind, int(source_id))
    marker = re.search(r'\[SRC:(Booking|DirectSale|Payment|SupplierPayment|ClientRefund):(\d+)\]',
                       tx.note or '', re.IGNORECASE)
    if marker:
        kind = marker.group(1).lower()
        return ('payment' if kind == 'clientrefund' else kind, int(marker.group(2)))
    return None


def collect_cash_flow_rows(from_date, to_date, *, posted_after=None, include_voided=True):
    """Collect cash/bank movements once, retaining their original sources.

    Linked booking, sale and payment documents carry the useful party/bill
    details; their AccountTransaction mirrors are only used as a trace back to
    the account ledger, never counted a second time. Legacy source records
    without an account posting are listed separately, not added to balances.
    """
    rows = []
    account_by_id = {a.id: a for a in Account.query.all()}

    # One projected query serves both source-to-journal tracing and standalone
    # movements. Non-cash Loss rows have no money account and are ignored below.
    tx_q = db.session.query(
        AccountTransaction.id, AccountTransaction.transaction_type,
        AccountTransaction.from_account_id, AccountTransaction.to_account_id,
        AccountTransaction.amount, AccountTransaction.amount_minor,
        AccountTransaction.reference_no, AccountTransaction.payment_method, AccountTransaction.description,
        AccountTransaction.note, AccountTransaction.category,
        AccountTransaction.subcategory, AccountTransaction.payee,
        AccountTransaction.created_by, AccountTransaction.date_posted, AccountTransaction.is_void,
        AccountTransaction.source_type, AccountTransaction.source_id,
    ).filter(
        AccountTransaction.transaction_type.in_([
            'Expense', 'Payment', 'Supplier Payment', 'Driver Payment', 'Transfer',
            'Receipt', 'Refund', 'Adjustment', 'Reconciliation Loss',
            'Reconciliation Excess', 'Loss',
        ]),
        func.date(AccountTransaction.date_posted) >= from_date,
        func.date(AccountTransaction.date_posted) <= to_date,
    )
    if posted_after is not None:
        tx_q = tx_q.filter(AccountTransaction.date_posted > posted_after)
    if not include_voided:
        tx_q = tx_q.filter(AccountTransaction.is_void == False)
    txs = tx_q.order_by(AccountTransaction.id.desc()).all()
    txs_by_id = {tx.id: tx for tx in txs}
    linked_txs = {}
    for tx in txs:
        key = _cf_linked_source(tx)
        if key and tx.transaction_type != 'Loss':
            linked_txs.setdefault((key[0], key[1], bool(tx.is_void)), tx)

    recorded = CashFlowEntry.query.filter(
        func.date(CashFlowEntry.date_posted) >= from_date,
        func.date(CashFlowEntry.date_posted) <= to_date,
    )
    if posted_after is not None:
        recorded = recorded.filter(CashFlowEntry.date_posted > posted_after)
    if not include_voided:
        recorded = recorded.filter(CashFlowEntry.is_void == False)
    for e in recorded.all():
        mirror = txs_by_id.get(e.account_tx_id)
        if mirror and bool(mirror.is_void) != bool(e.is_void):
            mirror = None
        if not mirror:
            continue
        direction = e.direction
        dest = None
        if direction == CF_DIR_TRANSFER:
            source_account = account_by_id.get(mirror.from_account_id)
            target_account = account_by_id.get(mirror.to_account_id)
            source_company = _cf_company_money_account(source_account)
            target_company = _cf_company_money_account(target_account)
            if not (source_company or target_company):
                continue
            if source_company and target_company:
                acc, dest = source_account, target_account
            elif source_company:
                acc, direction = source_account, CF_DIR_OUT
            else:
                acc, direction = target_account, CF_DIR_IN
            warning = _cf_source_warning(_cf_minor(e), None, mirror, acc)
            if e.account_id != mirror.from_account_id:
                warning = '; '.join(filter(None, (warning, 'Source account differs from account posting')))
            if e.destination_account_id != mirror.to_account_id:
                warning = '; '.join(filter(None, (warning, 'Source destination differs from account posting')))
        else:
            acc = _cf_source_account(account_by_id, e.account_id, mirror, incoming=direction == CF_DIR_IN)
            if not _cf_company_money_account(acc):
                continue
            warning = _cf_source_warning(_cf_minor(e), e.account_id, mirror, acc)
        rows.append(_cf_row(
            sort_dt=mirror.date_posted,
            direction=direction,
            amount=from_minor(abs(_cf_minor(mirror))),
            reference=(getattr(e, 'reference', None) or f'CF-{e.id}'),
            description=e.description or '', note=e.note or '',
            category=e.category.name if e.category else '',
            subcategory=e.subcategory.name if e.subcategory else '',
            party_name=e.party_name or (e.party.name if e.party else ''),
            party_type=e.party_type or (e.party.party_type if e.party else ''),
            account_id=acc.id, account_name=acc.name,
            dest_account_id=dest.id if dest else None,
            dest_account_name=dest.name if dest else '',
            source_type=getattr(e, 'source_type', None) or SRC_MANUAL,
            source_id=getattr(e, 'source_id', None) or e.id,
            origin='recorded', origin_label='Recorded on Cash Flow',
            created_by=e.created_by or '', status='voided' if e.is_void else 'active',
            entry_id=e.id, tx_id=e.account_tx_id, method=_cf_method(acc),
            warning=warning,
        ))

    pay_q = Payment.query.filter(
        func.date(Payment.date_posted) >= from_date,
        func.date(Payment.date_posted) <= to_date,
    )
    if posted_after is not None:
        pay_q = pay_q.filter(Payment.date_posted > posted_after)
    if not include_voided:
        pay_q = pay_q.filter(Payment.is_void == False)
    for p in pay_q.all():
        minor = _cf_minor(p)
        method = (p.method or '').strip().lower()
        # A discount-only or material-return credit is not a movement of money.
        if not minor or method == 'material return' or (p.payment_type or '').strip().lower() == 'material return':
            continue
        tx = linked_txs.get(('payment', p.id, bool(p.is_void)))
        direction = CF_DIR_OUT if minor < 0 else CF_DIR_IN
        acc = _cf_source_account(account_by_id, p.payment_account_id, tx, incoming=direction == CF_DIR_IN)
        if not _cf_company_money_account(acc):
            continue
        rows.append(_cf_row(
            sort_dt=tx.date_posted, direction=direction, amount=from_minor(abs(_cf_minor(tx))),
            reference=p.manual_bill_no or p.auto_bill_no or f'PAY-{p.id}',
            description=f'Client {"Refund" if minor < 0 else "Payment"} — {p.client_name or ""}',
            note=p.note or '', party_name=p.client_name or '', party_type='client',
            account_id=acc.id, account_name=acc.name,
            source_type=SRC_CLIENT_PAYMENT, source_id=p.id, origin='derived',
            origin_label='From Accounts · Client Payments',
            created_by=getattr(p, 'created_by', None) or '', tx_id=tx.id if tx else None,
            status='voided' if p.is_void else 'active', method=_cf_method(acc, p.method, tx),
            warning=_cf_source_warning(minor, p.payment_account_id, tx, acc),
        ))

    sale_q = DirectSale.query.filter(
        DirectSale.paid_amount > 0,
        func.date(DirectSale.date_posted) >= from_date,
        func.date(DirectSale.date_posted) <= to_date,
    )
    if posted_after is not None:
        sale_q = sale_q.filter(DirectSale.date_posted > posted_after)
    if not include_voided:
        sale_q = sale_q.filter(DirectSale.is_void == False)
    for s in sale_q.all():
        tx = linked_txs.get(('directsale', s.id, bool(s.is_void)))
        acc = _cf_source_account(account_by_id, s.payment_account_id, tx, incoming=True)
        if not _cf_company_money_account(acc):
            continue
        rows.append(_cf_row(
            sort_dt=tx.date_posted, direction=CF_DIR_IN, amount=from_minor(abs(_cf_minor(tx))),
            reference=s.manual_bill_no or s.auto_bill_no or f'DS-{s.id}',
            description=f'Sale receipt — {s.client_name or ""}', note=s.note or '',
            party_name=s.client_name or '', party_type='client',
            account_id=acc.id, account_name=acc.name,
            source_type=SRC_SALE, source_id=s.id, origin='derived', origin_label='From Sales',
            created_by=getattr(s, 'created_by', None) or '', tx_id=tx.id if tx else None,
            status='voided' if s.is_void else 'active', method=_cf_method(acc, s.payment_method, tx),
            warning=_cf_source_warning(_cf_minor(s, 'paid_amount'), s.payment_account_id, tx, acc),
        ))

    booking_q = Booking.query.filter(
        Booking.paid_amount > 0,
        func.date(Booking.date_posted) >= from_date,
        func.date(Booking.date_posted) <= to_date,
    )
    if posted_after is not None:
        booking_q = booking_q.filter(Booking.date_posted > posted_after)
    if not include_voided:
        booking_q = booking_q.filter(Booking.is_void == False)
    for b in booking_q.all():
        tx = linked_txs.get(('booking', b.id, bool(b.is_void)))
        acc = _cf_source_account(account_by_id, b.receive_in_account_id, tx, incoming=True)
        if not _cf_company_money_account(acc):
            continue
        rows.append(_cf_row(
            sort_dt=tx.date_posted, direction=CF_DIR_IN, amount=from_minor(abs(_cf_minor(tx))),
            reference=b.manual_bill_no or b.auto_bill_no or f'BK-{b.id}',
            description=f'Booking receipt — {b.client_name or ""}', note=b.note or '',
            party_name=b.client_name or '', party_type='client',
            account_id=acc.id, account_name=acc.name,
            source_type=SRC_BOOKING, source_id=b.id, origin='derived', origin_label='From Bookings',
            tx_id=tx.id if tx else None, status='voided' if b.is_void else 'active', method=_cf_method(acc, tx=tx),
            warning=_cf_source_warning(_cf_minor(b, 'paid_amount'), b.receive_in_account_id, tx, acc),
        ))

    sp_q = SupplierPayment.query.options(selectinload(SupplierPayment.supplier)).filter(
        func.date(SupplierPayment.date_posted) >= from_date,
        func.date(SupplierPayment.date_posted) <= to_date,
    )
    if posted_after is not None:
        sp_q = sp_q.filter(SupplierPayment.date_posted > posted_after)
    if not include_voided:
        sp_q = sp_q.filter(SupplierPayment.is_void == False)
    for sp in sp_q.all():
        minor = _cf_minor(sp)
        if not minor:
            continue
        supplier_name = sp.supplier.name if sp.supplier else ''
        tx = linked_txs.get(('supplierpayment', sp.id, bool(sp.is_void)))
        acc = _cf_source_account(account_by_id, sp.payment_account_id, tx, incoming=minor < 0)
        if not _cf_company_money_account(acc):
            continue
        # Negative supplier credits may exist in legacy subledgers without a
        # cash receipt.  Only show them as money in if an actual account entry
        # exists; the ordinary supplier posting path only supports positive pay.
        if minor < 0 and not tx:
            continue
        grn_label = f' · GRN #{sp.source_id}' if (sp.source_type or '').strip().lower() == 'grn' and sp.source_id else ''
        rows.append(_cf_row(
            sort_dt=tx.date_posted, direction=CF_DIR_IN if minor < 0 else CF_DIR_OUT,
            amount=from_minor(abs(_cf_minor(tx))),
            reference=sp.manual_bill_no or sp.auto_bill_no or f'SUP-{sp.id}',
            description=f'Supplier {"Refund" if minor < 0 else "Payment"} — {supplier_name}',
            note=sp.note or '', party_name=supplier_name, party_type='supplier',
            account_id=acc.id, account_name=acc.name,
            source_type=SRC_SUPPLIER_PAYMENT, source_id=sp.id, origin='derived',
            origin_label=f'From Accounts · Supplier Payments{grn_label}',
            created_by=getattr(sp, 'created_by', None) or '', tx_id=tx.id if tx else None,
            status='voided' if sp.is_void else 'active', method=_cf_method(acc, sp.method, tx),
            warning=_cf_source_warning(minor, sp.payment_account_id, tx, acc),
        ))

    # Driver settlements already have a single account row; enrich that row
    # in bulk with the source voucher and person instead of counting it twice.
    driver_sources = {}
    for tx in txs:
        if tx.transaction_type != 'Driver Payment':
            continue
        if (tx.source_type or '').strip().lower() == 'deliverypersonpayment' and tx.source_id:
            driver_sources[tx.id] = int(tx.source_id)
        else:
            match = re.search(r'\[SRC:DeliveryPersonPayment:(\d+)\]', tx.note or '', re.IGNORECASE)
            if match:
                driver_sources[tx.id] = int(match.group(1))
    drivers_by_id = {}
    if driver_sources:
        drivers_by_id = {p.id: p for p in DeliveryPersonPayment.query.options(
            selectinload(DeliveryPersonPayment.delivery_person)
        ).filter(DeliveryPersonPayment.id.in_(set(driver_sources.values()))).all()}

    represented = {row.get('tx_id') for row in rows if row.get('tx_id')}
    for tx in txs:
        unmatched = _cf_is_mirror_tx(tx)
        if unmatched and tx.id in represented:
            continue
        key = _cf_linked_source(tx) if unmatched else None
        unmatched_warning = (f'Linked {key[0]} #{key[1]} could not be matched; review the source and dates.'
                             if key else 'Linked source could not be matched; review the source and dates.') if unmatched else ''
        from_acc = account_by_id.get(tx.from_account_id)
        to_acc = account_by_id.get(tx.to_account_id)
        amount = from_minor(_cf_minor(tx))
        if tx.transaction_type == 'Transfer':
            from_company = _cf_company_money_account(from_acc)
            to_company = _cf_company_money_account(to_acc)
            if not (from_company or to_company):
                continue
            if from_company and to_company:
                rows.append(_cf_row(
                    sort_dt=tx.date_posted, direction=CF_DIR_TRANSFER, amount=amount,
                    reference=tx.reference_no or f'TX-{tx.id}', description=tx.description or 'Account transfer',
                    note=tx.note or '', category=tx.category or 'Internal Transfer',
                    subcategory=tx.subcategory or '', account_id=tx.from_account_id,
                    account_name=from_acc.name, dest_account_id=tx.to_account_id,
                    dest_account_name=to_acc.name, source_type=SRC_TRANSFER, origin='derived',
                    origin_label='From Accounts · Transfer', created_by=tx.created_by or '', tx_id=tx.id,
                    status='voided' if tx.is_void else 'active', method=_cf_method(from_acc, tx.payment_method, tx),
                    warning=unmatched_warning,
                ))
            else:
                # A loan/party ledger is not another company money account:
                # record the company side as an external receipt/payment.
                incoming = to_company
                company = to_acc if incoming else from_acc
                party = from_acc if incoming else to_acc
                rows.append(_cf_row(
                    sort_dt=tx.date_posted, direction=CF_DIR_IN if incoming else CF_DIR_OUT, amount=amount,
                    reference=tx.reference_no or f'TX-{tx.id}', description=tx.description or 'Account movement',
                    note=tx.note or '', category=tx.category or '', subcategory=tx.subcategory or '',
                    party_name=tx.payee or (party.name if party else ''),
                    account_id=company.id, account_name=company.name,
                    source_type=SRC_ACCOUNT_RECEIPT if incoming else SRC_ACCOUNT_EXPENSE,
                    origin='derived', origin_label='From Accounts · External account',
                    created_by=tx.created_by or '', tx_id=tx.id, status='voided' if tx.is_void else 'active',
                    method=_cf_method(company, tx.payment_method, tx),
                    warning=unmatched_warning,
                ))
            continue
        adjustment = tx.transaction_type in ('Adjustment', 'Reconciliation Loss', 'Reconciliation Excess', 'Loss')
        if (tx.transaction_type == 'Receipt' or adjustment) and _cf_company_money_account(to_acc):
            rows.append(_cf_row(
                sort_dt=tx.date_posted, direction=CF_DIR_IN, amount=amount,
                reference=tx.reference_no or f'TX-{tx.id}', description=tx.description or 'Money received',
                note=tx.note or '', category=tx.category or '', subcategory=tx.subcategory or '',
                party_name=tx.payee or '', account_id=tx.to_account_id, account_name=to_acc.name,
                source_type=SRC_ADJUSTMENT if adjustment else SRC_ACCOUNT_RECEIPT,
                source_id=tx.source_id, origin='derived',
                origin_label='From Accounts · Adjustment' if adjustment else 'From Accounts · Other receive',
                created_by=tx.created_by or '', tx_id=tx.id, status='voided' if tx.is_void else 'active',
                method=_cf_method(to_acc, tx.payment_method, tx),
                warning=unmatched_warning,
            ))
            continue
        if tx.transaction_type in ('Expense', 'Payment', 'Supplier Payment', 'Driver Payment', 'Refund',
                                    'Adjustment', 'Reconciliation Loss', 'Reconciliation Excess', 'Loss') and _cf_company_money_account(from_acc):
            is_driver = tx.transaction_type == 'Driver Payment'
            driver_source_id = driver_sources.get(tx.id) if is_driver else None
            driver = drivers_by_id.get(driver_source_id)
            rows.append(_cf_row(
                sort_dt=tx.date_posted, direction=CF_DIR_OUT, amount=amount,
                reference=(driver.reference if driver and driver.reference else tx.reference_no) or f'TX-{tx.id}',
                description=tx.description or ('Driver service payment' if is_driver else 'Expense'),
                note=tx.note or '', category=tx.category or '', subcategory=tx.subcategory or '',
                party_name=(driver.delivery_person.name if driver and driver.delivery_person else tx.payee or ''),
                party_type='delivery_person' if is_driver else '',
                account_id=tx.from_account_id, account_name=from_acc.name,
                source_type=SRC_ADJUSTMENT if adjustment else (SRC_DRIVER_PAYMENT if is_driver else SRC_ACCOUNT_EXPENSE),
                source_id=driver_source_id or tx.source_id, origin='derived',
                origin_label=('From Accounts · Adjustment' if adjustment else
                              ('From Accounts · Driver Services' if is_driver else 'From Accounts · Expense')),
                created_by=tx.created_by or '', tx_id=tx.id, status='voided' if tx.is_void else 'active',
                method=_cf_method(from_acc, driver.method if driver else tx.payment_method, tx),
                warning=unmatched_warning,
            ))
    return rows


def collect_unassigned_cash_flow_rows(from_date, to_date, assigned_rows=(), posted_after=None):
    """Legacy source amounts without a traceable account; never book them.

    Some imports include paid documents or old drawer entries without an
    account posting, and can overlap other documents. Show them for review but
    do not invent an account or include them in cash/bank balances.
    """
    assigned = {(r.get('source_type'), r.get('source_id')) for r in assigned_rows
                if (r.get('status') or 'active') == 'active' and r.get('account_id')}
    rows = []

    def add(kind, obj, minor, account_id, reference, description, party='', method='', category='', direction=None, source_id=None):
        source_id = source_id if source_id is not None else obj.id
        if (not minor or (kind, source_id) in assigned or
                (posted_after is not None and obj.date_posted <= posted_after)):
            return
        rows.append(_cf_row(
            sort_dt=obj.date_posted, direction=direction or (CF_DIR_IN if minor > 0 else CF_DIR_OUT),
            amount=from_minor(abs(minor)), reference=reference, description=description,
            note=getattr(obj, 'note', None) or '', category=category,
            party_name=party, source_type=kind, source_id=source_id,
            origin='unassigned', origin_label='Unassigned legacy · ' + description,
            method=method,
        ))

    for b in Booking.query.filter(Booking.is_void == False,
                                  Booking.paid_amount > 0, func.date(Booking.date_posted) >= from_date,
                                  func.date(Booking.date_posted) <= to_date).all():
        add(SRC_BOOKING, b, _cf_minor(b, 'paid_amount'), b.receive_in_account_id,
            b.manual_bill_no or b.auto_bill_no or f'BK-{b.id}', 'Booking', b.client_name or '')
    for sale in DirectSale.query.filter(DirectSale.is_void == False,
                                        DirectSale.paid_amount > 0, func.date(DirectSale.date_posted) >= from_date,
                                        func.date(DirectSale.date_posted) <= to_date).all():
        add(SRC_SALE, sale, _cf_minor(sale, 'paid_amount'), sale.payment_account_id,
            sale.manual_bill_no or sale.auto_bill_no or f'DS-{sale.id}', 'Sale',
            sale.client_name or '', sale.payment_method or '')
    for payment in Payment.query.filter(Payment.is_void == False,
                                        func.date(Payment.date_posted) >= from_date,
                                        func.date(Payment.date_posted) <= to_date).all():
        if ((payment.method or '').strip().lower() == 'material return' or
                (payment.payment_type or '').strip().lower() == 'material return'):
            continue
        add(SRC_CLIENT_PAYMENT, payment, _cf_minor(payment), payment.payment_account_id,
            payment.manual_bill_no or payment.auto_bill_no or f'PAY-{payment.id}',
            'Client payment', payment.client_name or '', payment.method or '')
    for payment in SupplierPayment.query.options(selectinload(SupplierPayment.supplier)).filter(
            SupplierPayment.is_void == False,
            func.date(SupplierPayment.date_posted) >= from_date,
            func.date(SupplierPayment.date_posted) <= to_date).all():
        add(SRC_SUPPLIER_PAYMENT, payment, -_cf_minor(payment), payment.payment_account_id,
            payment.manual_bill_no or payment.auto_bill_no or f'SUP-{payment.id}',
            'Supplier payment', payment.supplier.name if payment.supplier else '', payment.method or '')
    for entry in CashFlowEntry.query.options(selectinload(CashFlowEntry.category)).filter(
            CashFlowEntry.is_void == False,
            func.date(CashFlowEntry.date_posted) >= from_date,
            func.date(CashFlowEntry.date_posted) <= to_date).all():
        direction = _cf_normalize_direction(entry.direction)
        if direction not in (CF_DIR_IN, CF_DIR_OUT, CF_DIR_TRANSFER):
            continue
        add(entry.source_type or SRC_MANUAL, entry, _cf_minor(entry), entry.account_id,
            entry.reference or f'CF-{entry.id}', 'Cash Flow entry', entry.party_name or '',
            category=entry.category.name if entry.category else '', direction=direction,
            source_id=entry.source_id or entry.id)
    for drawer in FbmCashDrawerEntry.query.filter(
            FbmCashDrawerEntry.is_void == False, func.date(FbmCashDrawerEntry.date_posted) >= from_date,
            func.date(FbmCashDrawerEntry.date_posted) <= to_date).all():
        direction = (drawer.entry_type or '').strip().lower()
        if direction not in ('in', 'out'):
            continue
        amount = abs(_cf_minor(drawer)) * (1 if direction == 'in' else -1)
        add('LEGACY_CASH_DRAWER', drawer, amount, None, f'DRAWER-{drawer.id}',
            'Cash drawer entry', method=drawer.method or '', category=drawer.category or '')
    rows.sort(key=_cf_sort_key)
    return rows


def _cf_sort_key(row):
    d = row.get('sort_dt') or row.get('date')
    ref = row.get('reference') or ''
    if hasattr(d, 'timestamp'):
        return (d.timestamp(), ref)
    try:
        return (datetime.combine(d, datetime.min.time()).timestamp(), ref)
    except Exception:
        return (0, ref)


def filter_cash_flow_rows(rows, filters):
    out = list(rows)
    ftype = (filters.get('filter_type') or 'all').strip().lower()
    if ftype in ('cash_in', 'received', 'in'):
        out = [r for r in out if r['type'] == CF_DIR_IN]
    elif ftype in ('cash_out', 'spent', 'out'):
        out = [r for r in out if r['type'] == CF_DIR_OUT]
    elif ftype == 'transfer':
        out = [r for r in out if r['type'] == CF_DIR_TRANSFER]

    origin = (filters.get('origin') or 'all').strip().lower()
    if origin in ('derived', 'recorded'):
        out = [r for r in out if r.get('origin') == origin]
    elif origin == 'manual':
        out = [r for r in out if r.get('source') == 'MANUAL']
    elif origin == 'system':
        out = [r for r in out if r.get('source') == 'SYSTEM']
    elif origin and origin not in ('all', ''):
        out = [r for r in out if (r.get('source_type') or '').lower() == origin.lower()]

    category = (filters.get('category') or '').strip().lower()
    if category:
        out = [r for r in out if category in (r.get('category') or '').lower()]
    subcategory = (filters.get('subcategory') or '').strip().lower()
    if subcategory:
        out = [r for r in out if subcategory in (r.get('subcategory') or '').lower()]
    party_type = (filters.get('party_type') or '').strip().lower()
    if party_type:
        out = [r for r in out if (r.get('party_type') or '').lower() == party_type]
    party = (filters.get('party') or '').strip().lower()
    if party:
        out = [r for r in out if party in (r.get('party_name') or '').lower()]
    account_id = filters.get('account_id')
    if account_id:
        out = [r for r in out if r.get('account_id') == account_id or r.get('account_to_id') == account_id]
    notes = (filters.get('notes') or '').strip().lower()
    if notes:
        out = [r for r in out if notes in (r.get('note') or '').lower()]
    reference = (filters.get('reference') or '').strip().lower()
    if reference:
        out = [r for r in out if reference in (r.get('reference') or '').lower()]
    description = (filters.get('description') or '').strip().lower()
    if description:
        out = [r for r in out if description in (r.get('description') or '').lower()]
    created_by = (filters.get('created_by') or '').strip().lower()
    if created_by:
        out = [r for r in out if created_by in (r.get('created_by') or '').lower()]
    status = (filters.get('status') or 'active').strip().lower()
    if status in ('active', 'voided'):
        out = [r for r in out if (r.get('status') or 'active') == status]
    try:
        amin = filters.get('amount_min')
        if amin not in (None, ''):
            amin = float(amin)
            out = [r for r in out if float(r.get('amount') or 0) >= amin]
    except (TypeError, ValueError):
        pass
    try:
        amax = filters.get('amount_max')
        if amax not in (None, ''):
            amax = float(amax)
            out = [r for r in out if float(r.get('amount') or 0) <= amax]
    except (TypeError, ValueError):
        pass
    q = (filters.get('q') or '').strip().lower()
    if q:
        def _blob(r):
            return ' '.join([
                str(r.get('entry_id') or ''),
                str(r.get('reference') or ''),
                str(r.get('description') or ''),
                str(r.get('note') or ''),
                str(r.get('party_name') or ''),
                str(r.get('category') or ''),
                str(r.get('subcategory') or ''),
                str(r.get('account_display') or ''),
            ]).lower()
        out = [r for r in out if q in _blob(r)]
    return out


def apply_running_balance(rows, opening_balance, account_id=None, *, baselines=()):
    """Apply movements and dated non-transaction baselines in integer paisa."""
    running = to_minor(opening_balance or 0)
    pending = sorted(baselines, key=lambda item: item['date'])
    index = 0
    for row in rows:
        row_day = str(row.get('date') or '')[:10]
        while index < len(pending) and pending[index]['date'].isoformat() <= row_day:
            running += pending[index]['amount_minor']
            index += 1
        if (row.get('status') or 'active') != 'voided':
            if account_id:
                if row['type'] == CF_DIR_IN and row.get('account_id') == account_id:
                    running += to_minor(row.get('cash_in') or 0)
                elif row['type'] == CF_DIR_OUT and row.get('account_id') == account_id:
                    running -= to_minor(row.get('cash_out') or 0)
                elif row['type'] == CF_DIR_TRANSFER:
                    if row.get('account_to_id') == account_id:
                        running += to_minor(row.get('transfer_amount') or 0)
                    if row.get('account_id') == account_id:
                        running -= to_minor(row.get('transfer_amount') or 0)
            else:
                running += to_minor(row.get('cash_in') or 0) - to_minor(row.get('cash_out') or 0)
        row['running_balance'] = float(from_minor(running))
    running += sum(item['amount_minor'] for item in pending[index:])
    return float(from_minor(running))


def summarize_cash_flow_rows(rows, account_id=None, *, breakdown=True):
    """Total money and breakdowns in integer paisa, not binary floats."""
    active = [r for r in rows if (r.get('status') or 'active') == 'active']
    total_in = total_out = transfer_in = transfer_out = 0
    breakdown_cat, breakdown_party, breakdown_account = {}, {}, {}
    for r in active:
        incoming = to_minor(r.get('cash_in') or 0)
        outgoing = to_minor(r.get('cash_out') or 0)
        transfer = to_minor(r.get('transfer_amount') or 0)
        total_in += incoming
        total_out += outgoing
        if r.get('type') == CF_DIR_TRANSFER:
            if not account_id or r.get('account_to_id') == account_id:
                transfer_in += transfer
            if not account_id or r.get('account_id') == account_id:
                transfer_out += transfer
        if breakdown:
            category = (r.get('category') or '—').strip() or '—'
            party = ((r.get('party_type') or '') + ' · ' + (r.get('party_name') or '—')).strip(' ·')
            for groups, key in ((breakdown_cat, category), (breakdown_party, party)):
                bucket = groups.setdefault(key, {'in': 0, 'out': 0, 'transfer': 0})
                bucket['in'] += incoming
                bucket['out'] += outgoing
                bucket['transfer'] += transfer
            account = (r.get('account_name') or '—').strip() or '—'
            if r.get('type') == CF_DIR_TRANSFER:
                if not account_id or r.get('account_id') == account_id:
                    source = breakdown_account.setdefault(account, {'in': 0, 'out': 0, 'transfer_in': 0, 'transfer_out': 0})
                    source['transfer_out'] += transfer
                if not account_id or r.get('account_to_id') == account_id:
                    destination = (r.get('account_to_name') or '—').strip() or '—'
                    target = breakdown_account.setdefault(destination, {'in': 0, 'out': 0, 'transfer_in': 0, 'transfer_out': 0})
                    target['transfer_in'] += transfer
            else:
                bucket = breakdown_account.setdefault(account, {'in': 0, 'out': 0, 'transfer_in': 0, 'transfer_out': 0})
                bucket['in'] += incoming
                bucket['out'] += outgoing
    for groups in (breakdown_cat, breakdown_party, breakdown_account):
        for bucket in groups.values():
            for key in bucket:
                bucket[key] = float(from_minor(bucket[key]))
    return {
        'total_cash_in': float(from_minor(total_in)),
        'total_cash_out': float(from_minor(total_out)),
        'total_transfer_in': float(from_minor(transfer_in)),
        'total_transfer_out': float(from_minor(transfer_out)),
        'breakdown_cat': breakdown_cat, 'breakdown_party': breakdown_party,
        'breakdown_account': breakdown_account,
    }


def categories_for_direction(direction, include_inactive=False):
    direction = _cf_normalize_direction(direction)
    q = CashFlowCategory.query
    if not include_inactive:
        q = q.filter(CashFlowCategory.is_active == True)
    rows = q.order_by(CashFlowCategory.sort_order, CashFlowCategory.name).all()
    if direction in (CF_DIR_IN, CF_DIR_OUT):
        rows = [c for c in rows if (c.direction or 'both') in (direction, 'both')]
    return rows


def subcategories_for_category(category_id, include_inactive=False):
    q = CashFlowSubcategory.query.filter(CashFlowSubcategory.category_id == category_id)
    if not include_inactive:
        q = q.filter(CashFlowSubcategory.is_active == True)
    return q.order_by(CashFlowSubcategory.name).all()


