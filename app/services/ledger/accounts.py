"""The chart of accounts and the cash-account mapping.

One seeded chart, one code per economic concept, and one GL account per real
cash/bank/wallet master row.  Every posting resolves its account through this
module, so a typo cannot silently create a new account and a missing account is
a loud error instead of a silent fallback (plan §6).

The chart is *data*, not code: seeding only inserts codes that do not exist, so
renaming an account or adding a sub-account is safe and never overwritten.
"""
from __future__ import annotations

import logging
import threading

from sqlalchemy import func

from models import db, GLAccount, Account

log = logging.getLogger("app.ledger.accounts")

__all__ = [
    "GL",
    "CHART",
    "ensure_chart",
    "ensure_cash_accounts",
    "chart_accounts",
    "account_by_code",
    "account_id",
    "cash_gl_for_account",
    "default_cash_account",
    "account_class_of",
]


class GL:
    """Stable account codes. Import these instead of writing '4010' inline."""

    CASH = "1010"
    BANK = "1020"
    WALLET = "1030"
    RECEIVABLE = "1100"
    INVENTORY = "1200"
    INVENTORY_IN_TRANSIT = "1210"
    INPUT_TAX = "1300"
    INVENTORY_SUSPENSE = "1400"
    PAYABLE = "2010"
    CUSTOMER_ADVANCES = "2020"
    DRIVER_PAYABLE = "2030"
    OUTPUT_TAX = "2100"
    ACCRUED_EXPENSES = "2200"
    OPENING_EQUITY = "3010"
    RETAINED_EARNINGS = "3090"
    SALES_GOODS = "4010"
    SALES_SERVICES = "4020"
    OTHER_INCOME = "4030"
    TRADE_DISCOUNTS = "4090"
    ROUNDING = "4095"
    COGS_GOODS = "5010"
    COGS_SERVICES = "5020"
    STOCK_WRITE_OFF = "5030"
    LANDED_COST_VARIANCE = "5040"
    UNCLASSIFIED_EXPENSE = "6900"
    WAIVE_OFF = "8010"
    BAD_DEBT = "8020"
    CASH_DIFFERENCE = "8030"


# (code, name, class, subcategory, normal side, is_control, is_cash)
CHART = [
    ("1010", "Cash on hand", "asset", "Cash", "debit", False, True),
    ("1020", "Bank", "asset", "Bank", "debit", False, True),
    ("1030", "Digital wallet", "asset", "Digital wallet", "debit", False, True),
    ("1090", "Other cash equivalents", "asset", "Cash", "debit", False, True),
    ("1100", "Accounts receivable", "asset", "Receivables", "debit", True, False),
    ("1200", "Inventory — goods", "asset", "Inventory", "debit", True, False),
    ("1210", "Inventory — in transit", "asset", "Inventory", "debit", False, False),
    ("1300", "Input tax receivable", "asset", "Tax", "debit", False, False),
    ("1400", "Inventory suspense", "asset", "Suspense", "debit", True, False),
    ("2010", "Accounts payable", "liability", "Payables", "credit", True, False),
    ("2020", "Customer advances", "liability", "Advances", "credit", True, False),
    ("2030", "Driver / delivery payable", "liability", "Payables", "credit", True, False),
    ("2100", "Output tax payable", "liability", "Tax", "credit", False, False),
    ("2200", "Accrued expenses", "liability", "Accruals", "credit", False, False),
    ("3010", "Opening balance equity", "equity", "Equity", "credit", False, False),
    ("3090", "Retained earnings", "equity", "Equity", "credit", False, False),
    ("4010", "Sales — goods", "revenue", "Sales", "credit", False, False),
    ("4020", "Sales — services", "revenue", "Services", "credit", False, False),
    ("4030", "Other operating income", "revenue", "Other income", "credit", False, False),
    ("4090", "Trade discounts allowed", "revenue", "Discounts", "debit", False, False),
    ("4095", "Rounding gain / loss", "revenue", "Rounding", "debit", False, False),
    ("5010", "Cost of goods sold (FIFO)", "cost", "Cost of sales", "debit", False, False),
    ("5020", "Cost of services (driver rent)", "cost", "Cost of sales", "debit", False, False),
    ("5030", "Stock write-off / shortage", "cost", "Cost of sales", "debit", False, False),
    ("5040", "Landed-cost variance", "cost", "Cost of sales", "debit", False, False),
    ("6900", "Unclassified operating expense", "expense", "Operating", "debit", False, False),
    ("8010", "Customer waive-off / settlement discount", "other_loss", "Losses", "debit", False, False),
    ("8020", "Bad debt", "other_loss", "Losses", "debit", False, False),
    ("8030", "Cash reconciliation difference", "other_loss", "Losses", "debit", False, False),
]

_CLASS_ALIASES = {
    "income": "revenue",
    "cost_of_sales": "cost",
    "expenses": "expense",
    "other_expenses": "other_loss",
}

from types import SimpleNamespace

# Code → plain field dict.  Deliberately NOT ORM instances: this cache is
# process-wide and must survive app contexts (tests, multiple apps in one
# process), so it holds data, never attached objects.
_CACHE: dict[tuple, dict] = {}
_CACHE_LOCK = threading.RLock()
_CHART_READY: set = set()


def _scope() -> str:
    """Identify the database this cache belongs to.

    Tests and restores create several databases in one process; a chart cached
    from one must never be used against another (the row ids would point at the
    wrong accounts).  Same rule as ``app.services.cache``.
    """
    try:
        from flask import current_app

        return str(current_app.config.get("APP_DB_PATH") or "")
    except Exception:
        try:
            return str(db.engine.url)
        except Exception:
            return ""


def _key(code: str) -> tuple:
    return (_scope(), code)


def _snapshot(row) -> dict:
    return {
        "id": row.id,
        "code": row.code,
        "name": row.name,
        "account_class": row.account_class,
        "subcategory": row.subcategory,
        "normal_side": row.normal_side,
        "is_control": bool(row.is_control),
        "is_cash": bool(row.is_cash),
        "is_active": bool(row.is_active),
        "linked_account_id": row.linked_account_id,
    }


def _reload_cache() -> None:
    scope = _scope()
    with _CACHE_LOCK:
        for key in [k for k in _CACHE if k[0] == scope]:
            _CACHE.pop(key, None)
        for row in GLAccount.query.all():
            _CACHE[(scope, row.code)] = _snapshot(row)


def _reset_all_caches() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
        _CHART_READY.clear()


def _reset_cache() -> None:
    _reset_all_caches()


def ensure_chart(*, force: bool = False) -> None:
    """Insert any missing chart lines. Safe on every boot and in every test."""
    scope = _scope()
    with _CACHE_LOCK:
        if scope in _CHART_READY and not force:
            return
    try:
        existing = {
            row.code: row
            for row in GLAccount.query.all()
        }
        created = 0
        for code, name, cls, sub, side, control, is_cash in CHART:
            if code in existing:
                continue
            db.session.add(GLAccount(
                code=code,
                name=name,
                account_class=_CLASS_ALIASES.get(cls, cls),
                subcategory=sub,
                normal_side=side,
                is_control=control,
                is_cash=is_cash,
                is_system=True,
                is_active=True,
            ))
            created += 1
        if created:
            db.session.flush()
        _reload_cache()
        with _CACHE_LOCK:
            _CHART_READY.add(scope)
        if created:
            log.info("Seeded %s chart-of-accounts lines", created)
    except Exception:
        # A chart problem must never take the app down; the posting engine will
        # fail loudly on the first attempt to use a missing account instead.
        db.session.rollback()
        log.exception("Could not seed the chart of accounts")


def chart_accounts(*, include_inactive: bool = False) -> list[GLAccount]:
    ensure_chart()
    query = GLAccount.query
    if not include_inactive:
        query = query.filter(GLAccount.is_active.is_(True))
    return query.order_by(GLAccount.code.asc()).all()


def account_by_code(code: str):
    """A read-only view of one chart line (plain data, safe across contexts)."""
    code = (code or "").strip()
    if not code:
        return None
    ensure_chart()
    scoped = _key(code)
    with _CACHE_LOCK:
        snapshot = _CACHE.get(scoped)
    if snapshot is None:
        row = GLAccount.query.filter_by(code=code).first()
        if row is None:
            return None
        snapshot = _snapshot(row)
        with _CACHE_LOCK:
            _CACHE[scoped] = snapshot
    return SimpleNamespace(**snapshot)


def account_id(code: str) -> int:
    account = account_by_code(code)
    if account is None:
        raise LookupError(
            f"Chart of accounts is missing '{code}'. Open Ledger → Chart of accounts "
            "and restore it before posting."
        )
    return int(account.id)


def account_class_of(code: str) -> str:
    account = account_by_code(code)
    return (account.account_class if account else "") or ""


# ---------------------------------------------------------------------------
# Cash / bank / wallet mapping
# ---------------------------------------------------------------------------
def _cash_base_for(account: Account) -> str:
    channel = (getattr(account, "channel", None) or "").strip().lower()
    if channel in {"cash"}:
        return GL.CASH
    if channel in {"bank"}:
        return GL.BANK
    if channel in {"digital_wallet", "wallet"}:
        return GL.WALLET
    category = (getattr(account, "category", None) or "").strip().lower()
    if category == "bank":
        return GL.BANK
    if category == "cash":
        return GL.CASH
    return "1090"


def cash_gl_for_account(account) -> GLAccount:
    """Return (creating if necessary) the GL account linked to a cash master row."""
    ensure_chart()
    account_id_value = getattr(account, "id", account)
    if account_id_value in (None, ""):
        return default_cash_account()
    try:
        account_id_value = int(account_id_value)
    except (TypeError, ValueError):
        return default_cash_account()

    row = GLAccount.query.filter_by(linked_account_id=account_id_value).first()
    if row is not None:
        return row

    master = account if hasattr(account, "id") else db.session.get(Account, account_id_value)
    if master is None:
        return default_cash_account()
    base = _cash_base_for(master)
    code = f"{base}.{account_id_value:05d}"
    row = GLAccount.query.filter_by(code=code).first()
    if row is None:
        parent = account_by_code(base)
        row = GLAccount(
            code=code,
            name=(master.name or f"Cash account {account_id_value}"),
            account_class="asset",
            subcategory=(getattr(parent, "subcategory", None) or "Cash"),
            normal_side="debit",
            is_control=False,
            is_cash=True,
            is_system=False,
            linked_account_id=account_id_value,
            is_active=True,
        )
        db.session.add(row)
        db.session.flush()
        log.info("Mapped cash account %s (%s) to GL %s", account_id_value, master.name, code)
    return row


def default_cash_account() -> GLAccount:
    ensure_chart()
    row = account_by_code(GL.CASH)
    if row is None:
        raise LookupError("The default cash account (1010) is missing from the chart.")
    return row


def ensure_cash_accounts() -> int:
    """Create a GL account for every master cash/bank/wallet row. Returns count."""
    ensure_chart()
    created = 0
    for master in Account.query.order_by(Account.id.asc()).all():
        if master.id is None:
            continue
        before = GLAccount.query.filter_by(linked_account_id=master.id).first()
        cash_gl_for_account(master)
        if before is None:
            created += 1
    if created:
        db.session.flush()
    return created
