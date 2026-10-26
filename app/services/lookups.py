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

def get_client_by_input(input_str):
    """Helper to find client by name, code, or mixed string."""
    if not input_str:
        return None
    input_str = input_str.strip()

    # 1. Exact Code Match
    client = Client.query.filter_by(code=input_str).first()
    if client: return client

    # 2. Exact Name Match
    client = Client.query.filter_by(name=input_str).first()
    if client: return client

    # 3. Try to extract code from format "Name (Code)" or "Code - Name"
    match = re.search(r'\((tmpc-\d+|FBM-\d+|FBMCL-\d+)\)$', input_str, re.IGNORECASE)
    if match:
        code = match.group(1)
        client = Client.query.filter_by(code=code).first()
        if client: return client

    # 4. Case-insensitive Code/Name
    client = Client.query.filter(or_(Client.code.ilike(input_str), Client.name.ilike(input_str))).first()
    if client: return client

    return None


MATERIAL_NOT_SELECTED_MSG = (
    'Material not selected. Please select an existing material from the Material Master.'
)


def get_material_by_input(input_str):
    """Helper to find material by name or code.

    This is an exact identity lookup (case-insensitive). It never creates a
    Material and never treats a partial search string as a match.
    """
    if not input_str:
        return None
    input_str = input_str.strip()

    mat = Material.query.filter(or_(Material.name == input_str, Material.code == input_str)).first()
    if mat: return mat

    mat = Material.query.filter(or_(Material.name.ilike(input_str), Material.code.ilike(input_str))).first()
    return mat


def _material_lookup_key(value):
    return re.sub(r'\s+', ' ', (value or '').strip().lower())


def _material_text_matches_record(mat, typed_text):
    """True when typed text is empty or is exactly this material's name/code."""
    typed = _material_lookup_key(typed_text)
    if not typed:
        return True
    if not mat:
        return False
    return typed in {
        _material_lookup_key(mat.name),
        _material_lookup_key(mat.code),
    }


def resolve_transaction_material(material_id=None, typed_text=None, *, require_active=True, allow_inactive_names=None):
    """Resolve an existing Material Master record for an inventory transaction.

    ``material_id`` is authoritative when provided. Typed text is only a
    search/display value and is never used to create a Material.

    Returns the Material, or None when both id and text are empty (blank row).
    Raises ValueError when the user supplied text/id that is not a selected
    existing Material Master record.
    """
    allow_inactive_names = {
        (n or '').strip().lower()
        for n in (allow_inactive_names or [])
        if (n or '').strip()
    }

    raw_id = '' if material_id is None else str(material_id).strip()
    typed = (typed_text or '').strip()
    mat = None

    if raw_id:
        try:
            mid = int(raw_id)
        except (TypeError, ValueError):
            raise ValueError(MATERIAL_NOT_SELECTED_MSG)
        mat = db.session.get(Material, mid)
        if not mat:
            raise ValueError(MATERIAL_NOT_SELECTED_MSG)
        # A stale hidden id after the user edited the search box is invalid.
        if typed and not _material_text_matches_record(mat, typed):
            raise ValueError(MATERIAL_NOT_SELECTED_MSG)
    elif typed:
        mat = get_material_by_input(typed)
        if not mat:
            raise ValueError(MATERIAL_NOT_SELECTED_MSG)
    else:
        return None

    if require_active and not bool(getattr(mat, 'is_active', True)):
        hist = (mat.name or '').strip().lower()
        if hist not in allow_inactive_names:
            raise ValueError(f'Material "{mat.name}" was not found or is suspended.')
    return mat


# ---------------------------------------------------------------------------
# Services — charges that are billed like a material but are never stock.
#
# Everything here is name-keyed, because that is how the schema works:
# BookingItem.material_name / DirectSaleItem.product_name / Entry.material are
# free-text strings, not FKs. A material can therefore become a service without
# touching a single historical row — the name simply resolves elsewhere.
# ---------------------------------------------------------------------------

# Small TTL cache: `is_service_name()` is called per bill line inside report
# loops, and a query per line would be wasteful. Invalidated explicitly by
# every service mutation.
_SERVICE_CACHE: dict = {'ts': 0.0, 'names': set(), 'codes': set()}
_SERVICE_CACHE_TTL = 30.0


def invalidate_service_cache() -> None:
    """Drop the cached service name/code sets (call after any mutation)."""
    _SERVICE_CACHE['ts'] = 0.0


def _service_cache() -> dict:
    now = time.time()
    if now - float(_SERVICE_CACHE.get('ts') or 0.0) < _SERVICE_CACHE_TTL:
        return _SERVICE_CACHE
    try:
        rows = db.session.query(Service.name, Service.code).all()
    except Exception:
        # Schema not ready yet (very early bootstrap) — degrade to "no services"
        # rather than break every page, matching the dropdown-cache behaviour
        # in app/hooks.py.
        return {'ts': now, 'names': set(), 'codes': set()}
    names = {_material_norm_key(n) for n, _c in rows if n}
    codes = {_material_norm_key(c) for _n, c in rows if c}
    _SERVICE_CACHE['ts'] = now
    _SERVICE_CACHE['names'] = names
    _SERVICE_CACHE['codes'] = codes
    return _SERVICE_CACHE


def _material_norm_key(value):
    """Normalised identity key used for name/code comparisons.

    Deliberately identical to ``app.services.sales_core._material_norm_key``.
    These domain modules are parallel copies that do not import each other
    (they are aggregated by ``app/services/api.py``), so the helper is mirrored
    rather than shared — keep the two in step.
    """
    txt = (value or '').strip().lower()
    return re.sub(r'[^a-z0-9]+', '', txt)


def get_service_by_input(input_str):
    """Resolve a Service by exact name or code, then a case-insensitive match.

    Mirrors ``get_material_by_input`` so both masters behave identically.
    Never creates a Service and never treats a partial search as a match.
    """
    if not input_str:
        return None
    input_str = input_str.strip()
    svc = Service.query.filter(
        or_(Service.name == input_str, Service.code == input_str)
    ).first()
    if svc:
        return svc
    return Service.query.filter(
        or_(Service.name.ilike(input_str), Service.code.ilike(input_str))
    ).first()


# RENT WORDS (word-boundary match, never a substring match)
_RENT_WORDS = frozenset({"RENT", "RENTS", "RENTED", "RENTING", "RENTAL", "RENTALS"})


def is_rent_like_name(name):
    """True when *name* contains the **word** rent (RENT / RENTAL / RENTING …).

    The historic rule was ``'rent' in name``, which is a *substring* test and
    therefore also matches ordinary physical materials:

        TRANSPARENT SHEET, CURRENT STOCK, PARENT BOARD, DIFFERENT SHEET,
        INHERENT MORTAR …

    Every one of those was treated as a service: excluded from stock reports,
    excluded from the GRN FIFO consumption replay and — worst — excluded from
    Profit & Loss cost of goods, so their real purchase cost vanished from
    profit.  A token match keeps the genuine charges (``RENT-CEMENT``,
    ``RENT STEEL``, ``CEMENT RENT``, ``RENTAL MIXER``) and leaves every other
    material alone.

    The rule is shared: ``is_service_name``, the Profit & Loss projection, the
    GRN consumption replay and the Services page all call this function, so the
    four can never disagree about what a rent charge is.
    """
    txt = str(name or "").upper()
    if not txt:
        return False
    for token in re.split(r"[^A-Z0-9]+", txt):
        if token in _RENT_WORDS:
            return True
    return False


def _is_legacy_rent_name(name):
    """The historic 'is this rent?' rule, kept only as a transition fallback.

    Before the rent rows are moved into the Service master they are still
    Materials, so ``is_service_name`` must keep answering True for them —
    otherwise the rent box on the sale form would go dark the day service
    support ships.  It is now a word-boundary rule (see ``is_rent_like_name``);
    on the production snapshot it returns exactly the same four rent materials
    as the old substring test, without capturing physical names that merely
    contain the letters r-e-n-t.
    """
    return is_rent_like_name(name)


def is_service_name(name):
    """True when *name* identifies a Service (a charge, not stock).

    Two rules, in order:

    1. the name resolves to a Service — the explicit master, which is the
       destination state;
    2. the legacy ``'rent' in name`` heuristic — which is how the business
       identifies a rent charge *today*.

    Rule 2 is kept live **across** the conversion rather than being dropped the
    moment the rent rows move. Rent is a charge while it is still a Material and
    it stays a charge afterwards; keeping the heuristic in place is exactly what
    makes the rent box on the sale form, ``rent_item_revenue`` and the profit
    variance block identical before and after — with no dark window between
    shipping the code and pressing Convert.

    It is a strict superset of today's ``_is_rent_material_name``: same answer
    for every name in the master today, plus every Service ever added.
    """
    key = _material_norm_key(name)
    if not key:
        return False
    cache = _service_cache()
    if key in cache['names'] or key in cache['codes']:
        return True
    return _is_legacy_rent_name(name)


def resolve_transaction_item(service_id=None, material_id=None, typed_text=None, *,
                             require_active=True, allow_inactive_names=None):
    """Resolve a bill line to ``('material', obj)`` or ``('service', obj)``.

    Resolution order is fixed and deliberately puts ``service_id`` first:

        service_id  ->  material_id  ->  typed text

    ``material_id`` must **never** carry a Service id. Service ids start at 1
    and so do Material ids, so a Service id reaching the unqualified
    ``db.session.get(Material, mid)`` lookup would silently resolve to whatever
    Material shares that integer — posting a rent charge as, say, 12MM STEEL.
    Callers therefore get a separate ``service_id`` channel and this function
    keeps the two strictly apart.

    Returns ``(None, None)`` for a blank row. Raises ``ValueError`` when the
    user supplied a value that does not resolve to exactly one master, or when
    the typed text contradicts the supplied id.
    """
    allow_inactive_names = {
        (n or '').strip().lower()
        for n in (allow_inactive_names or [])
        if (n or '').strip()
    }

    raw_service_id = '' if service_id is None else str(service_id).strip()
    raw_material_id = '' if material_id is None else str(material_id).strip()
    typed = (typed_text or '').strip()

    obj = None
    kind = None

    if raw_service_id:
        try:
            sid = int(raw_service_id)
        except (TypeError, ValueError):
            raise ValueError(MATERIAL_NOT_SELECTED_MSG)
        obj = db.session.get(Service, sid)
        if not obj:
            raise ValueError(MATERIAL_NOT_SELECTED_MSG)
        kind = 'service'
        # A stale hidden id after the user edited the search box is invalid.
        if typed and not _service_text_matches_record(obj, typed):
            raise ValueError(MATERIAL_NOT_SELECTED_MSG)
    elif raw_material_id:
        try:
            mid = int(raw_material_id)
        except (TypeError, ValueError):
            raise ValueError(MATERIAL_NOT_SELECTED_MSG)
        obj = db.session.get(Material, mid)
        if not obj:
            raise ValueError(MATERIAL_NOT_SELECTED_MSG)
        kind = 'material'
        if typed and not _material_text_matches_record(obj, typed):
            raise ValueError(MATERIAL_NOT_SELECTED_MSG)
    elif typed:
        # A name may legitimately live in either master, but never in both —
        # the collision guard (services blueprint) prevents that. If it ever
        # happens we refuse rather than guess.
        mat = get_material_by_input(typed)
        svc = None if mat else get_service_by_input(typed)
        if mat and svc:
            raise ValueError(
                f'"{typed}" matches both a material and a service. '
                'Ask an administrator to resolve the duplicate.'
            )
        if mat:
            obj, kind = mat, 'material'
        elif svc:
            obj, kind = svc, 'service'
        else:
            raise ValueError(MATERIAL_NOT_SELECTED_MSG)
    else:
        return (None, None)

    if require_active and not bool(getattr(obj, 'is_active', True)):
        hist = (obj.name or '').strip().lower()
        if hist not in allow_inactive_names:
            raise ValueError(f'{obj.name} was not found or is suspended.')
    return (kind, obj)


def _service_text_matches_record(svc, typed_text):
    """True when typed text is empty or is exactly this service's name/code."""
    typed = _material_lookup_key(typed_text)
    if not typed:
        return True
    if not svc:
        return False
    return typed in {
        _material_lookup_key(svc.name),
        _material_lookup_key(svc.code),
    }


def resolve_transaction_service(service_id=None, typed_text=None, *,
                                require_active=True, allow_inactive_names=None):
    """Resolve a Service, raising unless the result is one.

    Used by paths that must accept charges *only* — the counterpart of
    ``resolve_transaction_material`` for goods-only paths.
    """
    kind, obj = resolve_transaction_item(
        service_id=service_id,
        typed_text=typed_text,
        require_active=require_active,
        allow_inactive_names=allow_inactive_names,
    )
    if kind == 'service':
        return obj
    if kind is None:
        return None
    raise ValueError(MATERIAL_NOT_SELECTED_MSG)


def get_supplier_by_input(input_str):
    """Helper to find supplier by name."""
    if not input_str:
        return None
    input_str = input_str.strip()
    return Supplier.query.filter(func.lower(Supplier.name) == input_str.lower()).first()


def get_or_create_delivery_person(name_input, phone=None):
    name = (name_input or '').strip()
    if not name:
        return None
    phone_value = (phone or '').strip()
    existing = DeliveryPerson.query.filter(func.lower(func.trim(DeliveryPerson.name)) == name.lower()).first()
    if existing:
        if not existing.is_active:
            existing.is_active = True
        if phone_value:
            existing.phone = phone_value
        return existing
    # DeliveryPerson.name is globally unique in current schema.
    # Under tenant-scoped ORM filters, a name that exists in another tenant can be invisible here.
    # Use raw SQL fallback to avoid duplicate insert integrity errors.
    global_row = db.session.execute(
        text("SELECT id FROM delivery_person WHERE lower(trim(name)) = :n LIMIT 1"),
        {'n': name.lower()}
    ).fetchone()
    if global_row:
        return None
    # Two simultaneous first-time sales can both see "no driver D" and race
    # to create it.  The INSERT below — even when it ignores an existing row —
    # takes SQLite's write lock FIRST, so concurrent creators serialise here:
    # the first inserts the row, the waiters' inserts are ignored, and every
    # caller then adopts the same committed row.
    from app.services.time_money import pk_now
    now = pk_now()
    conn = db.session.connection()
    conn.execute(
        text(
            "INSERT OR IGNORE INTO delivery_person "
            "(name, phone, is_active, opening_balance, opening_balance_date, created_at) "
            "VALUES (:n, :p, 1, 0, :now, :now)"
        ),
        {'n': name, 'p': phone_value or None, 'now': now},
    )
    row = conn.execute(
        text("SELECT id FROM delivery_person WHERE lower(trim(name)) = :n LIMIT 1"),
        {'n': name.lower()},
    ).fetchone()
    if not row:
        return None
    existing = db.session.get(DeliveryPerson, int(row[0]))
    if existing is None:
        return None
    if not existing.is_active:
        existing.is_active = True
    if phone_value:
        existing.phone = phone_value
    return existing


