"""Accounts workspace: Cash Flow, Categories, Accounts, New Transaction."""
from __future__ import annotations

from datetime import datetime, timedelta

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_

from models import Account, CashFlowCategory, CashFlowParty, Client, Supplier, db
from app.services import account_setup as setup
from app.services.cash_flow_svc import collect_cash_flow_rows, filter_cash_flow_rows, summarize_cash_flow_rows
from app.services.payments_crud import ledger_balances
from app.services.time_money import pk_today
from utils.money import from_minor, to_minor
from ._common import accounts_bp, logger


TABS = (
    ("cash-flow", "Cash Flow", "accounts.dashboard", "bi-water"),
    ("expenses", "Expenses", "accounts.expenses", "bi-receipt-cutoff"),
    ("categories", "Manage Categories", "accounts.manage_categories", "bi-tags"),
    ("accounts", "Manage Accounts", "accounts.manage_accounts", "bi-wallet2"),
    ("new-transaction", "New Transaction", "accounts.add_transaction", "bi-plus-circle"),
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


def can_post_transactions():
    """Setup-only access does not grant permission to move business money."""
    if not _is_admin() and (getattr(current_user, "access_mode", None) or "read_write").strip().lower() == "read_only":
        return False
    return _is_admin() or any(bool(getattr(current_user, flag, False)) for flag in (
        "can_manage_accounts", "can_manage_payments",
    ))


def _require_post_transactions():
    if not can_post_transactions():
        abort(403)


def _money_text(minor):
    return format(from_minor(minor), "f")


def _status_filter():
    value = (request.args.get("status") or "all").strip().lower()
    return value if value in ("all", "active", "suspended") else "all"


def _account_rows(apply_filters=True, with_usage=True):
    """Setup accounts with their calculated balance.

    Balances and "is this account used?" both used to be computed one account
    at a time (and the usage check scans every foreign key in the schema), which
    made this tab slower the more history the business had.  Both are now
    resolved in bulk, so the page cost no longer grows with account count.
    """
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
    accounts = query.order_by(Account.name.asc(), Account.id.asc()).all()
    balances = ledger_balances([account.id for account in accounts])
    usage = setup.account_reference_counts() if with_usage else {}
    rows = []
    for account in accounts:
        snapshot = setup.account_snapshot(account)
        used = int(usage.get(account.id, 0))
        snapshot.update({
            "type_label": setup.ACCOUNT_TYPES[snapshot["account_type"]]["label"],
            "type_icon": setup.ACCOUNT_TYPES[snapshot["account_type"]]["icon"],
            "opening_text": _money_text(snapshot["opening_balance_minor"]),
            "current_balance": balances.get(account.id, 0.0),
            "is_used": bool(used),
            "can_delete": not used,
        })
        rows.append(snapshot)
    return rows, q, status


def _light_account_rows(*, with_balances=False):
    """Account picker options, optionally including the spendable balance.

    The transaction form needs the same minor-unit balance that its posting
    services check. Other tabs only need names and should stay inexpensive.
    """
    if with_balances:
        from app.services.accounting import _account_minor

    rows = []
    for account in setup.setup_accounts_query().order_by(Account.name.asc(), Account.id.asc()).all():
        key = setup.account_type_key(account)
        row = {
            "id": account.id,
            "name": account.name,
            "is_active": account.is_active is not False,
            "account_type": key,
            "type_label": setup.ACCOUNT_TYPES[key]["label"],
            "type_icon": setup.ACCOUNT_TYPES[key]["icon"],
        }
        if with_balances:
            minor = _account_minor(account)
            row.update(current_balance_minor=minor, current_balance=float(from_minor(minor)))
        rows.append(row)
    return rows


def _category_rows():
    """Categories with their subcategories, usage counts and totals."""
    q = (request.args.get("q") or "").strip()
    status = _status_filter()
    query = CashFlowCategory.query
    if status == "active":
        query = query.filter(CashFlowCategory.is_active == True)  # noqa: E712
    elif status == "suspended":
        query = query.filter(CashFlowCategory.is_active == False)  # noqa: E712
    if q:
        query = query.filter(CashFlowCategory.name.ilike(f"%{q}%"))
    categories = query.order_by(CashFlowCategory.name.asc(), CashFlowCategory.id.asc()).all()
    usage_map = setup.category_usage_map()
    rows = []
    for category in categories:
        snapshot = setup.category_snapshot(category)
        children = sorted(category.subcategories, key=lambda sub: ((sub.name or "").lower(), sub.id))
        usage = setup.category_usage_from_map(usage_map, category, children)
        sub_rows = []
        for sub in children:
            sub_usage = setup.subcategory_usage_from_map(usage_map, sub)
            sub_rows.append({
                **setup.subcategory_snapshot(sub),
                "usage_count": sub_usage,
                "can_delete": sub_usage == 0,
            })
        snapshot.update({
            "direction_label": setup.CATEGORY_DIRECTIONS.get(snapshot["direction"], "Money in & out"),
            "usage_count": usage,
            "subcategory_count": len(children),
            "can_delete": usage == 0,
            "subcategories": sub_rows,
        })
        rows.append(snapshot)
    return rows, q, status


def _receive_client_options():
    """Active clients for the New Transaction receive picker.

    Payable amounts are deliberately *not* computed here: projecting a client
    ledger is expensive and only the picked client is ever displayed, so the
    amount is fetched on demand from ``accounts.transaction_client_dues``.
    """
    rows = []
    clients = Client.query.filter(
        func.coalesce(Client.is_active, True) == True,  # noqa: E712
    ).order_by(Client.name.asc(), Client.id.asc()).all()
    for client in clients:
        rows.append({
            "code": (client.code or "").strip(),
            "name": (client.name or "").strip(),
            "phone": (client.phone or "").strip(),
            "category": (client.category or "").strip(),
        })
    return rows


def _receive_party_options():
    """Reusable outside parties: loan givers, lenders, and other counterparties."""
    from app.services.account_transactions import _party_type_labels

    labels = _party_type_labels()
    rows = []
    parties = CashFlowParty.query.filter(
        func.coalesce(CashFlowParty.is_active, True) == True,  # noqa: E712
    ).order_by(CashFlowParty.name.asc(), CashFlowParty.id.asc()).all()
    for party in parties:
        party_type = (party.party_type or "other").strip().lower() or "other"
        rows.append({
            "id": party.id,
            "name": (party.name or "").strip(),
            "party_type": party_type,
            "type_label": labels.get(party_type, labels.get("other", "Other")),
            "phone": (party.phone or "").strip(),
            "note": (party.note or "").strip(),
        })
    return rows


def _party_type_options():
    from app.services.account_transactions import _party_type_labels

    labels = _party_type_labels()
    order = ("loan", "outsider", "person", "other")
    keys = [key for key in order if key in labels] + [key for key in labels if key not in order]
    return [{"key": key, "label": labels[key]} for key in keys]


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
    category = (request.args.get("category") or "").strip()
    subcategory = (request.args.get("subcategory") or "").strip()
    direction = (request.args.get("direction") or "all").strip().lower()
    if direction not in ("all", "in", "out", "transfer"):
        direction = "all"
    collected = collect_cash_flow_rows(from_str, to_str, include_voided=False)
    rows = filter_cash_flow_rows(collected, {
        "account_id": account_id,
        "q": q,
        "status": "active",
        "filter_type": direction,
        "category": category,
        "subcategory": subcategory,
    })
    rows.sort(key=lambda row: row.get("sort_dt"), reverse=True)
    summary = summarize_cash_flow_rows(rows, account_id=account_id, breakdown=False)
    page = max(request.args.get("page", 1, type=int) or 1, 1)
    per_page = 25
    total_rows = len(rows)
    total_pages = max((total_rows + per_page - 1) // per_page, 1)
    page = min(page, total_pages)
    page_rows = rows[(page - 1) * per_page: page * per_page]
    base_args = {"from_date": from_str, "to_date": to_str}
    if account_id:
        base_args["account_id"] = account_id
    for key, value in (("q", q), ("category", category), ("subcategory", subcategory)):
        if value:
            base_args[key] = value
    if direction != "all":
        base_args["direction"] = direction
    prev_url = url_for("accounts.dashboard", **{**base_args, "page": page - 1}) if page > 1 else None
    next_url = url_for("accounts.dashboard", **{**base_args, "page": page + 1}) if page < total_pages else None
    if account_id:
        active_balances = ledger_balances([account_id])
    else:
        active_balances = ledger_balances([a["id"] for a in all_accounts if a["is_active"]])
    # Filter options come from the unfiltered window so a chosen category never
    # removes the other choices from the dropdown.
    category_options, seen = [], set()
    subcategories_by_category = {}
    for row in collected:
        name = (row.get("category") or "").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            category_options.append(name)
        child = (row.get("subcategory") or "").strip()
        if name and child:
            bucket = subcategories_by_category.setdefault(name, [])
            if child not in bucket:
                bucket.append(child)
    for name in sorted(subcategories_by_category):
        subcategories_by_category[name].sort()
    category_options.sort(key=str.lower)
    subcategory_options = sorted({c for kids in subcategories_by_category.values() for c in kids}, key=str.lower)
    subcategory_parents = {}
    for parent, kids in subcategories_by_category.items():
        for kid in kids:
            subcategory_parents.setdefault(kid, parent)
    return {
        "cf_rows": page_rows,
        "cf_summary": summary,
        "cf_net": float(from_minor(
            to_minor(summary["total_cash_in"]) - to_minor(summary["total_cash_out"])
            + to_minor(summary["total_transfer_in"]) - to_minor(summary["total_transfer_out"])
        )),
        "cf_active_balance": float(from_minor(sum(to_minor(value) for value in active_balances.values()))),
        "cf_from": from_str,
        "cf_to": to_str,
        "cf_account_id": account_id,
        "cf_q": q,
        "cf_category": category,
        "cf_subcategory": subcategory,
        "cf_direction": direction,
        "cf_category_options": category_options,
        "cf_subcategory_options": subcategory_options,
        "cf_subcategories_by_category": subcategories_by_category,
        "cf_subcategory_parents": subcategory_parents,
        "cf_page": page,
        "cf_total_pages": total_pages,
        "cf_total_rows": total_rows,
        "cf_prev_url": prev_url,
        "cf_next_url": next_url,
    }


def _date_window(default_days=29):
    """Shared from/to parsing for the Cash Flow and Expenses tabs."""
    today = pk_today()
    raw_from = (request.args.get("from_date") or "").strip()
    raw_to = (request.args.get("to_date") or "").strip()
    try:
        from_date = datetime.strptime(raw_from, "%Y-%m-%d").date() if raw_from else today - timedelta(days=default_days)
    except ValueError:
        from_date = today - timedelta(days=default_days)
    try:
        to_date = datetime.strptime(raw_to, "%Y-%m-%d").date() if raw_to else today
    except ValueError:
        to_date = today
    if from_date > to_date:
        from_date, to_date = to_date, from_date
    return from_date, to_date, from_date.strftime("%Y-%m-%d"), to_date.strftime("%Y-%m-%d")


def _expenses_context(all_accounts):
    """Money-out register: every expense/payment with category tracing.

    Expenses are not a separate ledger: the rows are the same money-out
    movements that already appear in Cash Flow, the account ledger and
    transactions, filtered to outgoing entries and grouped by the
    category/subcategory they carry.
    """
    from_date, to_date, from_str, to_str = _date_window()
    account_id = request.args.get("account_id", type=int)
    q = (request.args.get("q") or "").strip()
    category = (request.args.get("category") or "").strip()
    subcategory = (request.args.get("subcategory") or "").strip()
    collected = collect_cash_flow_rows(from_str, to_str, include_voided=False)
    rows = filter_cash_flow_rows(collected, {
        "account_id": account_id,
        "q": q,
        "status": "active",
        "filter_type": "out",
        "category": category,
        "subcategory": subcategory,
    })
    rows.sort(key=lambda row: row.get("sort_dt"), reverse=True)
    summary = summarize_cash_flow_rows(rows, account_id=account_id, breakdown=False)

    by_category = {}
    for row in rows:
        key = (row.get("category") or "").strip() or "Uncategorized"
        bucket = by_category.setdefault(key, {"category": key, "total": 0.0, "count": 0, "subcategories": {}})
        amount = float(row.get("cash_out") or row.get("amount") or 0)
        bucket["total"] += amount
        bucket["count"] += 1
        child = (row.get("subcategory") or "").strip()
        if child:
            bucket["subcategories"][child] = bucket["subcategories"].get(child, 0.0) + amount
    category_totals = sorted(by_category.values(), key=lambda item: (-item["total"], item["category"].lower()))
    for item in category_totals:
        item["subcategory_rows"] = sorted(
            ({"name": name, "total": total} for name, total in item["subcategories"].items()),
            key=lambda sub: (-sub["total"], sub["name"].lower()),
        )

    category_options, seen = [], set()
    subcategories_by_category = {}
    for row in collected:
        if row.get("type") != "out":
            continue
        name = (row.get("category") or "").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            category_options.append(name)
        child = (row.get("subcategory") or "").strip()
        if name and child:
            bucket = subcategories_by_category.setdefault(name, [])
            if child not in bucket:
                bucket.append(child)
    category_options.sort(key=str.lower)
    for name in subcategories_by_category:
        subcategories_by_category[name].sort()
    subcategory_parents = {}
    for parent, kids in subcategories_by_category.items():
        for kid in kids:
            subcategory_parents.setdefault(kid, parent)

    page = max(request.args.get("page", 1, type=int) or 1, 1)
    per_page = 25
    total_rows = len(rows)
    total_pages = max((total_rows + per_page - 1) // per_page, 1)
    page = min(page, total_pages)
    page_rows = rows[(page - 1) * per_page: page * per_page]
    base_args = {"from_date": from_str, "to_date": to_str}
    if account_id:
        base_args["account_id"] = account_id
    for key, value in (("q", q), ("category", category), ("subcategory", subcategory)):
        if value:
            base_args[key] = value
    return {
        "ex_rows": page_rows,
        "ex_total": float(summary["total_cash_out"]),
        "ex_count": total_rows,
        "ex_category_totals": category_totals,
        "ex_from": from_str,
        "ex_to": to_str,
        "ex_account_id": account_id,
        "ex_q": q,
        "ex_category": category,
        "ex_subcategory": subcategory,
        "ex_category_options": category_options,
        "ex_subcategory_options": sorted({c for kids in subcategories_by_category.values() for c in kids}, key=str.lower),
        "ex_subcategories_by_category": subcategories_by_category,
        "ex_subcategory_parents": subcategory_parents,
        "ex_page": page,
        "ex_total_pages": total_pages,
        "ex_prev_url": url_for("accounts.expenses", **{**base_args, "page": page - 1}) if page > 1 else None,
        "ex_next_url": url_for("accounts.expenses", **{**base_args, "page": page + 1}) if page < total_pages else None,
    }


def render_workspace(active_tab="cash-flow", *, modal_open=None, form_values=None,
                     edit_account_id=None, edit_category_id=None, status=200):
    _require_view()
    if active_tab not in {key for key, *_ in TABS}:
        active_tab = "cash-flow"
    # Only build what the visible tab actually renders.  Previously every tab
    # paid for the full account list (with a per-account balance and a schema
    # wide usage scan) plus every category usage count, which is what made the
    # workspace feel slow to open.
    accounts, account_q, account_status = ([], "", "all")
    if active_tab == "accounts":
        accounts, account_q, account_status = _account_rows(apply_filters=True, with_usage=True)
    elif active_tab in ("cash-flow", "expenses", "new-transaction"):
        accounts = _light_account_rows(with_balances=active_tab == "new-transaction")
    categories, category_q, category_status = ([], "", "all")
    if active_tab in ("categories", "accounts"):
        categories, category_q, category_status = _category_rows()
    context = {
        "active_tab": active_tab,
        "tabs": TABS,
        "can_manage": can_manage_setup(),
        "can_post": can_post_transactions(),
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
    if active_tab == "expenses":
        context.update(_expenses_context(accounts))
    if active_tab == "new-transaction":
        from uuid import uuid4
        from app.services.time_money import pk_now
        from .transaction_tree import build_unified_category_catalog

        values = context["form_values"]
        values.setdefault("tx_mode", request.args.get("mode") or request.args.get("tx_mode") or "receive")
        if values["tx_mode"] not in ("receive", "pay", "transfer"):
            values["tx_mode"] = "receive"
        values.setdefault("date_posted", pk_now().strftime("%Y-%m-%dT%H:%M"))
        values.setdefault("idempotency_key", uuid4().hex)
        from app.services.account_transactions import DEFAULT_RECEIVE_SOURCE, RECEIVE_SOURCES

        source = (values.get("receive_source") or "").strip().lower()
        if source not in (*RECEIVE_SOURCES, "account"):
            # A fresh visit starts on the common case. A re-render of a
            # submitted form must show the source the service will actually
            # apply, or a counterparty typed without the field would vanish
            # between the failed post and the page that asks for a correction.
            source = DEFAULT_RECEIVE_SOURCE if form_values else "client"
        values["receive_source"] = source
        values.setdefault("receive_group", {"client": "client_ledger", "party": "party", "other": "other_source"}.get(source, ""))
        context.update({
            "tx_values": values,
            "tx_now": pk_now().strftime("%Y-%m-%dT%H:%M"),
            "tx_accounts": [account for account in accounts if account["is_active"]],
            "tx_categories": build_unified_category_catalog()["all_categories"],
            "tx_clients": _receive_client_options(),
            "tx_suppliers": [{"code": str(row.id), "display_code": f"SUP-{row.id:04d}",
                "name": row.name, "phone": getattr(row, "phone", "") or ""}
                for row in Supplier.query.filter(func.coalesce(Supplier.is_active, True) == True).order_by(Supplier.name).all()],
            "tx_parties": _receive_party_options(),
            "tx_source_accounts": [{"code": str(a.id), "name": a.name,
                "group": (a.source_category or "").strip(), "phone": ""}
                for a in Account.query.filter(Account.is_active != False).order_by(Account.name).all()
                if (a.source_category or "").strip()],
            "tx_receive_groups": sorted(set(["Clients", "Company", "External", "FBM-JPS", "Loan", "Own Funds"] +
                [(a.source_category or "").strip() for a in Account.query.all() if (a.source_category or "").strip()])),
            "tx_receive_sources": RECEIVE_SOURCES,
            "tx_party_types": _party_type_options(),
        })
    return render_template("accounts/workspace.html", **context), status


def _tab_redirect(tab, **params):
    endpoint = {key: endpoint for key, _, endpoint, _ in TABS}[tab]
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


@accounts_bp.route("/expenses")
@login_required
def expenses():
    """Expenses register: every money-out movement with category tracing."""
    return render_workspace("expenses")


@accounts_bp.route("/setup/subcategories", methods=["POST"])
@login_required
def setup_save_subcategory():
    _require_manage()
    subcategory_id = None
    category_id = None
    try:
        subcategory_id = _form_id(request.form.get("subcategory_id"))
        category_id = _form_id(request.form.get("category_id"))
        setup.save_subcategory(request.form, actor=current_user,
                               category_id=category_id, subcategory_id=subcategory_id)
        db.session.commit()
        flash("Subcategory created." if not subcategory_id else "Subcategory updated.", "success")
    except setup.SetupError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except Exception:
        db.session.rollback()
        logger.exception("Subcategory setup save failed")
        flash("The subcategory could not be saved. No changes were applied.", "danger")
    return _tab_redirect("categories")


@accounts_bp.route("/setup/subcategories/<int:subcategory_id>/status", methods=["POST"])
@login_required
def setup_subcategory_status(subcategory_id):
    _require_manage()
    try:
        active = request.form.get("active") == "1"
        sub = setup.set_subcategory_active(subcategory_id, active=active, actor=current_user)
        db.session.commit()
        flash(f"“{sub.name}” {'resumed' if active else 'suspended'}.", "success")
    except setup.SetupError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except Exception:
        db.session.rollback()
        logger.exception("Subcategory status change failed")
        flash("The subcategory status could not be changed.", "danger")
    return _tab_redirect("categories")


@accounts_bp.route("/setup/subcategories/<int:subcategory_id>/delete", methods=["POST"])
@login_required
def setup_delete_subcategory(subcategory_id):
    _require_manage()
    try:
        setup.delete_subcategory(subcategory_id, actor=current_user)
        db.session.commit()
        flash("Unused subcategory deleted.", "success")
    except setup.SetupError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except Exception:
        db.session.rollback()
        logger.exception("Subcategory setup delete failed")
        flash("The subcategory could not be deleted. No changes were applied.", "danger")
    return _tab_redirect("categories")
