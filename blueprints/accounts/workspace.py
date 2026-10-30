"""The from-scratch Accounts workspace: Cash Flow, Categories, Accounts."""
from __future__ import annotations

from datetime import datetime, timedelta

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_

from models import Account, CashFlowCategory, db
from app.services import account_setup as setup
from app.services.cash_flow_svc import collect_cash_flow_rows, filter_cash_flow_rows, summarize_cash_flow_rows
from app.services.payments_crud import ledger_balance
from app.services.time_money import pk_today
from utils.money import from_minor
from ._common import accounts_bp, logger


TABS = (
    ("cash-flow", "Cash Flow", "accounts.dashboard", "bi-water"),
    ("categories", "Manage Categories", "accounts.manage_categories", "bi-tags"),
    ("accounts", "Manage Accounts", "accounts.manage_accounts", "bi-wallet2"),
)


def _is_admin():
    return (getattr(current_user, "role", "") or "").strip().lower() in ("admin", "root")


def can_manage_setup():
    return _is_admin() or bool(getattr(current_user, "can_manage_account_masters", False))


def _require_view():
    if _is_admin() or any(bool(getattr(current_user, flag, False)) for flag in (
        "can_manage_accounts", "can_manage_payments", "can_manage_account_masters", "can_view_cash_flow",
    )):
        return
    abort(403)


def _require_manage():
    if not can_manage_setup():
        abort(403)


def _money_text(minor):
    return format(from_minor(minor), "f")


def _status_filter():
    value = (request.args.get("status") or "all").strip().lower()
    return value if value in ("all", "active", "suspended") else "all"


def _account_rows(apply_filters=True):
    q = (request.args.get("q") or "").strip() if apply_filters else ""
    status = _status_filter() if apply_filters else "all"
    query = setup.setup_accounts_query()
    if status == "active":
        query = query.filter(func.coalesce(Account.is_active, True) == True)  # noqa: E712
    elif status == "suspended":
        query = query.filter(func.coalesce(Account.is_active, True) == False)  # noqa: E712
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Account.name.ilike(like), Account.account_number.ilike(like), Account.wallet_number.ilike(like)))
    rows = []
    for account in query.order_by(Account.name.asc(), Account.id.asc()).all():
        snapshot = setup.account_snapshot(account)
        used = setup.account_reference_count(account.id)
        snapshot.update({
            "type_label": setup.ACCOUNT_TYPES[snapshot["account_type"]]["label"],
            "type_icon": setup.ACCOUNT_TYPES[snapshot["account_type"]]["icon"],
            "opening_text": _money_text(snapshot["opening_balance_minor"]),
            "current_balance": ledger_balance(account.id),
            "is_used": bool(used),
            "can_delete": not used,
        })
        rows.append(snapshot)
    return rows, q, status


def _category_rows():
    q = (request.args.get("q") or "").strip()
    status = _status_filter()
    query = CashFlowCategory.query
    if status == "active":
        query = query.filter(CashFlowCategory.is_active == True)  # noqa: E712
    elif status == "suspended":
        query = query.filter(CashFlowCategory.is_active == False)  # noqa: E712
    if q:
        query = query.filter(CashFlowCategory.name.ilike(f"%{q}%"))
    rows = []
    for category in query.order_by(CashFlowCategory.name.asc(), CashFlowCategory.id.asc()).all():
        snapshot = setup.category_snapshot(category)
        usage = setup.category_usage_count(category)
        snapshot.update({
            "direction_label": setup.CATEGORY_DIRECTIONS.get(snapshot["direction"], "Money in & out"),
            "usage_count": usage,
            "subcategory_count": len(category.subcategories),
            "can_delete": usage == 0,
        })
        rows.append(snapshot)
    return rows, q, status


def _cash_flow_context(all_accounts):
    today = pk_today()
    default_from = (today - timedelta(days=29)).strftime("%Y-%m-%d")
    default_to = today.strftime("%Y-%m-%d")
    raw_from = (request.args.get("from_date") or default_from).strip()
    raw_to = (request.args.get("to_date") or default_to).strip()
    try:
        from_date = datetime.strptime(raw_from, "%Y-%m-%d").date()
    except ValueError:
        from_date = today - timedelta(days=29)
    try:
        to_date = datetime.strptime(raw_to, "%Y-%m-%d").date()
    except ValueError:
        to_date = today
    if from_date > to_date:
        from_date, to_date = to_date, from_date
    from_str, to_str = from_date.strftime("%Y-%m-%d"), to_date.strftime("%Y-%m-%d")
    account_id = request.args.get("account_id", type=int)
    q = (request.args.get("q") or "").strip()
    rows = collect_cash_flow_rows(from_str, to_str, include_voided=False)
    rows = filter_cash_flow_rows(rows, {"account_id": account_id, "q": q, "status": "active"})
    rows.sort(key=lambda row: row.get("sort_dt"), reverse=True)
    summary = summarize_cash_flow_rows(rows, account_id=account_id)
    page = max(request.args.get("page", 1, type=int) or 1, 1)
    per_page = 25
    total_rows = len(rows)
    total_pages = max((total_rows + per_page - 1) // per_page, 1)
    page = min(page, total_pages)
    page_rows = rows[(page - 1) * per_page: page * per_page]
    base_args = {"from_date": from_str, "to_date": to_str}
    if account_id:
        base_args["account_id"] = account_id
    if q:
        base_args["q"] = q
    prev_url = url_for("accounts.dashboard", **{**base_args, "page": page - 1}) if page > 1 else None
    next_url = url_for("accounts.dashboard", **{**base_args, "page": page + 1}) if page < total_pages else None
    active_accounts = [a for a in all_accounts if a["is_active"]]
    return {
        "cf_rows": page_rows,
        "cf_summary": summary,
        "cf_net": float(from_minor(int(round((summary["total_cash_in"] - summary["total_cash_out"]) * 100)))),
        "cf_active_balance": sum(float(a["current_balance"]) for a in active_accounts),
        "cf_from": from_str,
        "cf_to": to_str,
        "cf_account_id": account_id,
        "cf_q": q,
        "cf_page": page,
        "cf_total_pages": total_pages,
        "cf_total_rows": total_rows,
        "cf_prev_url": prev_url,
        "cf_next_url": next_url,
    }


def render_workspace(active_tab="cash-flow", *, modal_open=None, form_values=None,
                     edit_account_id=None, edit_category_id=None, status=200):
    _require_view()
    if active_tab not in {key for key, *_ in TABS}:
        active_tab = "cash-flow"
    accounts, account_q, account_status = _account_rows(apply_filters=(active_tab == "accounts"))
    categories, category_q, category_status = _category_rows()
    context = {
        "active_tab": active_tab,
        "tabs": TABS,
        "can_manage": can_manage_setup(),
        "accounts": accounts,
        "categories": categories,
        "account_q": account_q,
        "account_status": account_status,
        "category_q": category_q,
        "category_status": category_status,
        "account_types": setup.ACCOUNT_TYPES,
        "category_directions": setup.CATEGORY_DIRECTIONS,
        "modal_open": modal_open,
        "form_values": dict(form_values) if form_values else {},
        "edit_account_id": edit_account_id,
        "edit_category_id": edit_category_id,
        "setup_account_count": setup.setup_accounts_query().count(),
        "setup_category_count": CashFlowCategory.query.count(),
    }
    if active_tab == "cash-flow":
        context.update(_cash_flow_context(accounts))
    return render_template("accounts/workspace.html", **context), status


def _tab_redirect(tab, **params):
    endpoint = {"cash-flow": "accounts.dashboard", "categories": "accounts.manage_categories", "accounts": "accounts.manage_accounts"}[tab]
    return redirect(url_for(endpoint, **params))


def _form_id(raw):
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise setup.SetupError("Invalid record id.") from exc
    if value <= 0:
        raise setup.SetupError("Invalid record id.")
    return value


@accounts_bp.route("/setup/accounts", methods=["POST"])
@login_required
def setup_save_account(form_override=None):
    _require_manage()
    form = request.form if form_override is None else form_override
    account_id = None
    try:
        account_id = _form_id(form.get("account_id"))
        account, created = setup.save_account(form, actor=current_user, account_id=account_id)
        db.session.commit()
        flash("Account created." if created else "Account updated.", "success")
        return _tab_redirect("accounts")
    except setup.SetupError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return render_workspace("accounts", modal_open="account", form_values=form,
                                edit_account_id=account_id, status=422)
    except Exception:
        db.session.rollback()
        logger.exception("Account setup save failed")
        flash("The account could not be saved. No changes were applied.", "danger")
        return _tab_redirect("accounts")


@accounts_bp.route("/setup/accounts/<int:account_id>/status", methods=["POST"])
@login_required
def setup_account_status(account_id):
    _require_manage()
    try:
        active = request.form.get("active") == "1"
        account = setup.set_account_active(account_id, active=active, actor=current_user, form=request.form)
        db.session.commit()
        flash(f"“{account.name}” {'resumed' if active else 'suspended'}.", "success")
    except setup.SetupError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except Exception:
        db.session.rollback()
        logger.exception("Account status change failed")
        flash("The account status could not be changed.", "danger")
    return _tab_redirect("accounts")


@accounts_bp.route("/setup/accounts/<int:account_id>/delete", methods=["POST"])
@login_required
def setup_delete_account(account_id):
    _require_manage()
    try:
        setup.delete_account_setup(account_id, actor=current_user, form=request.form)
        db.session.commit()
        flash("Unused account deleted.", "success")
    except setup.SetupError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except Exception:
        db.session.rollback()
        logger.exception("Account setup delete failed")
        flash("The account could not be deleted. No changes were applied.", "danger")
    return _tab_redirect("accounts")


@accounts_bp.route("/setup/categories", methods=["POST"])
@login_required
def setup_save_category():
    _require_manage()
    category_id = None
    try:
        category_id = _form_id(request.form.get("category_id"))
        category, created = setup.save_category(request.form, actor=current_user, category_id=category_id)
        db.session.commit()
        flash("Category created." if created else "Category updated.", "success")
        return _tab_redirect("categories")
    except setup.SetupError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return render_workspace("categories", modal_open="category", form_values=request.form,
                                edit_category_id=category_id, status=422)
    except Exception:
        db.session.rollback()
        logger.exception("Category setup save failed")
        flash("The category could not be saved. No changes were applied.", "danger")
        return _tab_redirect("categories")


@accounts_bp.route("/setup/categories/<int:category_id>/status", methods=["POST"])
@login_required
def setup_category_status(category_id):
    _require_manage()
    try:
        active = request.form.get("active") == "1"
        category = setup.set_category_active(category_id, active=active, actor=current_user)
        db.session.commit()
        flash(f"“{category.name}” {'resumed' if active else 'suspended'}.", "success")
    except setup.SetupError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except Exception:
        db.session.rollback()
        logger.exception("Category status change failed")
        flash("The category status could not be changed.", "danger")
    return _tab_redirect("categories")


@accounts_bp.route("/setup/categories/<int:category_id>/delete", methods=["POST"])
@login_required
def setup_delete_category(category_id):
    _require_manage()
    try:
        setup.delete_category_setup(category_id, actor=current_user)
        db.session.commit()
        flash("Unused category deleted.", "success")
    except setup.SetupError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except Exception:
        db.session.rollback()
        logger.exception("Category setup delete failed")
        flash("The category could not be deleted. No changes were applied.", "danger")
    return _tab_redirect("categories")
