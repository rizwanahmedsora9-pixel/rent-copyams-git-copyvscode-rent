"""Owner-defined account and category setup for the Accounts workspace.

This is deliberately small and independent of the old classification wizard:
an account is a place where the business holds money, and a category is a
label for money moving in or out.  Nothing is seeded.  Historical records are
never deleted by a setup action; used records can only be suspended.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from sqlalchemy import func

from models import (
    Account,
    AccountTransaction,
    CashDayAccountPosition,
    CashFlowCategory,
    CashFlowEntry,
    CashFlowSubcategory,
    GLAccount,
    JournalLine,
    db,
)
from app.services.time_money import pk_now
from utils.accounting_audit import record_accounting_audit
from utils.money import from_minor


ACCOUNT_TYPES = {
    "cash": {"label": "Cash", "icon": "bi-cash-stack", "channel": "cash", "legacy_category": "cash"},
    "bank": {"label": "Bank", "icon": "bi-bank", "channel": "bank", "legacy_category": "bank"},
    "wallet": {"label": "Mobile wallet", "icon": "bi-phone", "channel": "digital_wallet", "legacy_category": "bank"},
}
CATEGORY_DIRECTIONS = {
    "in": "Money in",
    "out": "Money out",
    "both": "Money in & out",
}
_MAX_OPENING_MINOR = 99_999_999_999_99  # 999,999,999,999.99


class SetupError(ValueError):
    """A user-facing setup validation error."""


def _text(form, key, default=""):
    value = form.get(key, default)
    return (value if isinstance(value, str) else ("" if value is None else str(value))).strip()


def _normalized_number(value):
    return re.sub(r"[\s\-().]", "", value or "").upper()


def account_type_key(account):
    channel = (getattr(account, "channel", None) or "").strip().lower()
    if channel == "digital_wallet":
        return "wallet"
    if channel in ("cash", "bank"):
        return channel
    return "bank" if (getattr(account, "category", None) or "").strip().lower() == "bank" else "cash"


def setup_accounts_query():
    """Only real money accounts owned by the business, not party ledgers."""
    return Account.query.filter(
        func.lower(func.trim(Account.account_type)) == "company",
        func.lower(func.trim(Account.category)).in_(("cash", "bank")),
    )


def account_reference_count(account_id):
    """Count business/history references, excluding the account's GL mirror.

    Scans every declared foreign key to ``account.id`` so new payment modules
    are covered automatically, plus the non-FK day-position snapshot table.
    Audit rows are intentionally excluded: they keep their snapshots after a
    master is deleted and must not make every account undeletable.
    """
    total = 0
    for table in db.metadata.sorted_tables:
        for column in table.columns:
            if table.name == "gl_account" and column.name == "linked_account_id":
                continue
            if any(fk.target_fullname == "account.id" for fk in column.foreign_keys):
                total += int(db.session.query(func.count()).select_from(table).filter(column == account_id).scalar() or 0)
    total += CashDayAccountPosition.query.filter_by(account_id=account_id).count()
    return total


def _check_revision(account, form):
    raw = _text(form, "expected_revision")
    if not raw:
        return
    try:
        expected = int(raw)
    except ValueError as exc:
        raise SetupError("This form is out of date. Refresh the page and try again.") from exc
    if expected != int(account.revision or 1):
        raise SetupError("This account was changed in another session. Refresh the page and try again.")


def _opening_minor(form, account_type):
    raw = _text(form, "opening_balance")
    if not raw:
        raise SetupError("Opening balance is required. Enter 0 if the account starts empty.")
    try:
        amount = Decimal(raw.replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise SetupError("Opening balance must be a valid number.") from exc
    if not amount.is_finite():
        raise SetupError("Opening balance must be a finite number.")
    if amount.as_tuple().exponent < -2:
        raise SetupError("Opening balance can have at most 2 decimal places.")
    minor = int(amount * 100)
    if abs(minor) > _MAX_OPENING_MINOR:
        raise SetupError("Opening balance is too large.")
    if minor < 0 and account_type != "bank":
        raise SetupError("Only a bank account can start with a negative balance (overdraft).")
    return minor


def validate_account_form(form, *, account=None):
    name = _text(form, "name")
    if not name or len(name) > 100:
        raise SetupError("Enter an account name of up to 100 characters.")
    if any(ord(ch) < 32 for ch in name):
        raise SetupError("Account name cannot contain control characters.")

    account_type = _text(form, "account_type").lower()
    if account_type not in ACCOUNT_TYPES:
        raise SetupError("Choose an account type: Cash, Bank, or Mobile wallet.")

    number = _text(form, "account_number")
    if len(number) > 50 or any(ord(ch) < 32 for ch in number):
        raise SetupError("Account number must be up to 50 characters.")
    if number and not re.fullmatch(r"[A-Za-z0-9+() .\-]+", number):
        raise SetupError("Account number contains unsupported characters.")
    if account_type in ("bank", "wallet") and not number:
        raise SetupError("Account number is required for bank and mobile wallet accounts.")

    opening = _opening_minor(form, account_type)
    used = bool(account and account_reference_count(account.id))
    if used:
        if account_type != account_type_key(account):
            raise SetupError("Account type is locked after activity. Suspend this account and create a new one instead.")
        current_opening = account.opening_balance_minor
        if current_opening is None:
            current_opening = int(Decimal(str(account.opening_balance or 0)) * 100)
        if opening != int(current_opening):
            raise SetupError("Opening balance is locked after activity. Record a correction as a transaction instead.")

    name_clash = Account.query.filter(func.lower(func.trim(Account.name)) == name.lower())
    if account:
        name_clash = name_clash.filter(Account.id != account.id)
    if name_clash.first():
        raise SetupError("An account with this name already exists. Choose a different name.")

    if number:
        normalized = _normalized_number(number)
        for other in setup_accounts_query().all():
            if account and other.id == account.id:
                continue
            if _normalized_number(other.account_number or other.wallet_number or "") == normalized:
                raise SetupError("That account number is already used by another account.")

    return {
        "name": name,
        "account_type": account_type,
        "account_number": number or None,
        "opening_balance_minor": opening,
        "is_used": used,
    }


def _apply_account_type(account, account_type, number):
    meta = ACCOUNT_TYPES[account_type]
    account.channel = meta["channel"]
    account.category = meta["legacy_category"]
    account.account_type = "company"
    account.type = "company"
    account.account_number = number
    account.wallet_number = number if account_type == "wallet" else None
    # Clear details that do not belong to the selected type.
    account.bank_name = None
    account.account_holder_name = None
    account.branch_code = None
    account.wallet_provider = None
    account.wallet_holder = None
    account.cash_location = None
    account.cash_responsible = None


def account_snapshot(account):
    opening = account.opening_balance_minor
    if opening is None:
        opening = int(Decimal(str(account.opening_balance or 0)) * 100)
    return {
        "id": account.id,
        "name": account.name,
        "account_type": account_type_key(account),
        "account_number": account.account_number or account.wallet_number or "",
        "opening_balance_minor": int(opening),
        "is_active": bool(account.is_active),
        "account_status": account.account_status or ("active" if account.is_active else "inactive"),
        "revision": int(account.revision or 1),
    }


def save_account(form, *, actor, account_id=None):
    account = db.session.get(Account, account_id) if account_id else None
    if account_id and not account:
        raise SetupError("Account not found.")
    if account:
        _check_revision(account, form)
    cleaned = validate_account_form(form, account=account)
    before = account_snapshot(account) if account else None
    created = account is None
    if created:
        account = Account()
        account.is_active = True
        account.account_status = "active"
        account.opening_balance_date = pk_now()
        account.linked_entity_type = "none"
        account.source_category = None
        account.class_category = None
        account.class_subcategory = None
        account.class_account_type = None
        db.session.add(account)

    account.name = cleaned["name"]
    previous_type = account_type_key(account) if not created else None
    if created or previous_type != cleaned["account_type"]:
        _apply_account_type(account, cleaned["account_type"], cleaned["account_number"])
    else:
        # Same type: update the identifier without erasing legacy bank/wallet
        # details that this simple form does not ask for.
        account.account_number = cleaned["account_number"]
        if cleaned["account_type"] == "wallet":
            account.wallet_number = cleaned["account_number"]
    if not cleaned["is_used"]:
        account.opening_balance_minor = cleaned["opening_balance_minor"]
        account.opening_balance = float(from_minor(cleaned["opening_balance_minor"]))
        account.balance_minor = cleaned["opening_balance_minor"]
        account.balance = float(from_minor(cleaned["opening_balance_minor"]))
    account.updated_by = getattr(actor, "username", None)
    db.session.flush()
    record_accounting_audit(
        actor,
        action="Create" if created else "Update",
        entity_type="Account",
        entity_id=account.id,
        before=before,
        after=account_snapshot(account),
        amount_after=float(from_minor(cleaned["opening_balance_minor"])),
        account_after_id=account.id,
        reason="Account setup",
    )
    db.session.flush()
    return account, created


def set_account_active(account_id, *, active, actor, form=None):
    account = db.session.get(Account, account_id)
    if not account:
        raise SetupError("Account not found.")
    if form is not None:
        _check_revision(account, form)
    before = account_snapshot(account)
    account.is_active = bool(active)
    account.account_status = "active" if active else "inactive"
    account.updated_by = getattr(actor, "username", None)
    db.session.flush()
    record_accounting_audit(
        actor, action="Resume" if active else "Suspend", entity_type="Account", entity_id=account.id,
        before=before, after=account_snapshot(account), account_before_id=account.id, account_after_id=account.id,
        reason="Account setup",
    )
    db.session.flush()
    return account


def delete_account_setup(account_id, *, actor, form=None):
    account = db.session.get(Account, account_id)
    if not account:
        raise SetupError("Account not found.")
    if form is not None:
        _check_revision(account, form)
    if account_reference_count(account.id):
        raise SetupError("This account has financial history and cannot be deleted. Suspend it instead.")
    before = account_snapshot(account)
    # The GL mirror is bookkeeping, not a business reference.  Keep a posted
    # mirror (detached) so its journal remains readable; remove an unused one.
    for mirror in GLAccount.query.filter_by(linked_account_id=account.id).all():
        posted = db.session.query(func.count(JournalLine.id)).filter(JournalLine.gl_account_id == mirror.id).scalar() or 0
        if posted:
            mirror.linked_account_id = None
            mirror.is_active = False
        else:
            db.session.delete(mirror)
    record_accounting_audit(
        actor, action="Delete", entity_type="Account", entity_id=account.id,
        before=before, after={"deleted": True}, account_before_id=account.id,
        reason="Unused account deleted during setup",
    )
    db.session.delete(account)
    db.session.flush()
    return True


def category_usage_count(category):
    """Business records that still point at this category or its children."""
    child_names = [s.name for s in category.subcategories]
    total = CashFlowEntry.query.filter(CashFlowEntry.category_id == category.id).count()
    total += AccountTransaction.query.filter(func.lower(func.trim(AccountTransaction.category)) == category.name.strip().lower()).count()
    if child_names:
        child_ids = [s.id for s in category.subcategories]
        total += CashFlowEntry.query.filter(CashFlowEntry.subcategory_id.in_(child_ids)).count()
        total += AccountTransaction.query.filter(func.lower(func.trim(AccountTransaction.subcategory)).in_([n.lower() for n in child_names])).count()
    return total


def category_snapshot(category):
    return {
        "id": category.id,
        "name": category.name,
        "direction": category.direction or "both",
        "notes": category.notes or "",
        "is_active": bool(category.is_active),
    }


def save_category(form, *, actor, category_id=None):
    category = db.session.get(CashFlowCategory, category_id) if category_id else None
    if category_id and not category:
        raise SetupError("Category not found.")
    name = _text(form, "name")
    if not name or len(name) > 120:
        raise SetupError("Enter a category name of up to 120 characters.")
    direction = _text(form, "direction", "both").lower()
    if direction not in CATEGORY_DIRECTIONS:
        raise SetupError("Choose what this category is used for: money in, money out, or both.")
    notes = _text(form, "notes")
    if len(notes) > 300:
        raise SetupError("Description must be up to 300 characters.")
    clash = CashFlowCategory.query.filter(func.lower(func.trim(CashFlowCategory.name)) == name.lower())
    if category:
        clash = clash.filter(CashFlowCategory.id != category.id)
    if clash.first():
        raise SetupError("A category with this name already exists, including suspended categories.")
    used = bool(category and category_usage_count(category))
    if used and direction != (category.direction or "both"):
        raise SetupError("This category is already used. Its direction is locked; suspend it and create a new category instead.")
    before = category_snapshot(category) if category else None
    created = category is None
    old_name = category.name if category else None
    if created:
        category = CashFlowCategory(is_active=True)
        db.session.add(category)
    category.name = name
    category.direction = direction
    category.notes = notes or None
    category.updated_at = pk_now()
    if used and old_name and old_name.strip().lower() != name.lower():
        # Keep the denormalised ledger label in sync so history stays grouped
        # under the renamed category and the delete guard remains accurate.
        AccountTransaction.query.filter(
            func.lower(func.trim(AccountTransaction.category)) == old_name.strip().lower()
        ).update({AccountTransaction.category: name}, synchronize_session=False)
    db.session.flush()
    record_accounting_audit(
        actor, action="Create" if created else "Update", entity_type="CashFlowCategory", entity_id=category.id,
        before=before, after=category_snapshot(category), reason="Category setup",
    )
    db.session.flush()
    return category, created


def set_category_active(category_id, *, active, actor):
    category = db.session.get(CashFlowCategory, category_id)
    if not category:
        raise SetupError("Category not found.")
    before = category_snapshot(category)
    category.is_active = bool(active)
    category.updated_at = pk_now()
    db.session.flush()
    record_accounting_audit(
        actor, action="Resume" if active else "Suspend", entity_type="CashFlowCategory", entity_id=category.id,
        before=before, after=category_snapshot(category), reason="Category setup",
    )
    db.session.flush()
    return category


def delete_category_setup(category_id, *, actor):
    category = db.session.get(CashFlowCategory, category_id)
    if not category:
        raise SetupError("Category not found.")
    if category_usage_count(category):
        raise SetupError("This category is used by financial records and cannot be deleted. Suspend it instead.")
    before = category_snapshot(category)
    before["subcategories_removed"] = len(category.subcategories)
    for child in list(category.subcategories):
        db.session.delete(child)
    record_accounting_audit(
        actor, action="Delete", entity_type="CashFlowCategory", entity_id=category.id,
        before=before, after={"deleted": True}, reason="Unused category deleted during setup",
    )
    db.session.delete(category)
    db.session.flush()
    return True
