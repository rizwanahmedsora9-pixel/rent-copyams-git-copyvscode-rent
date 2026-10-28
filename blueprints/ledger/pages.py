"""Ledger pages — every one of them renders a read model."""
from __future__ import annotations

from datetime import date, timedelta

from ._common import *  # noqa: F401,F403


# ---------------------------------------------------------------------------
# Overview and chart of accounts
# ---------------------------------------------------------------------------
@bp.route("/")
@login_required
def overview():
    _guard()
    today = date.today()
    month_start = today.replace(day=1)
    summary = dashboard_model.load(today)
    pnl = pnl_view.load(month_start, today)
    agree = books_agree_model.load(today)
    return render_template(
        "ledger/overview.html",
        summary=summary,
        pnl=pnl,
        agree=agree,
        today=today,
        month_start=month_start,
    )


@bp.route("/chart")
@login_required
def chart():
    _guard()
    balances = {row["code"]: row for row in q.account_balances()}
    rows = []
    for account in coa.chart_accounts(include_inactive=True):
        row = balances.get(account.code, {})
        rows.append({
            "account": account,
            "debit": row.get("debit", 0),
            "credit": row.get("credit", 0),
            "balance": row.get("balance", 0),
            "natural": row.get("natural", 0),
        })
    groups = {}
    for row in rows:
        groups.setdefault(row["account"].account_class, []).append(row)
    return render_template(
        "ledger/chart.html",
        rows=rows,
        groups=groups,
        can_manage=_can_manage(),
        class_order=["asset", "liability", "equity", "revenue", "cost", "expense", "other_loss"],
    )


@bp.route("/chart/new", methods=["POST"])
@login_required
def chart_new():
    _guard(view=False)
    code = (request.form.get("code") or "").strip()
    name = (request.form.get("name") or "").strip()
    account_class = (request.form.get("account_class") or "expense").strip()
    normal_side = (request.form.get("normal_side") or "debit").strip()
    if not code or not name:
        flash("A code and a name are required.", "danger")
        return redirect(url_for("ledger.chart"))
    if GLAccount.query.filter_by(code=code).first():
        flash(f"Account code {code} already exists.", "warning")
        return redirect(url_for("ledger.chart"))
    db.session.add(GLAccount(
        code=code,
        name=name,
        account_class=account_class,
        normal_side=normal_side,
        is_control=False,
        is_cash=False,
        is_system=False,
        is_active=True,
    ))
    db.session.commit()
    coa.ensure_chart(force=True)
    cache.invalidate()
    flash(f"Account {code} {name} added.", "success")
    return redirect(url_for("ledger.chart"))


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------
@bp.route("/journal")
@login_required
def journal():
    _guard()
    page = request.args.get("page", 1, type=int)
    filters = {
        "start": _date_arg("start"),
        "end": _date_arg("end"),
        "source_type": (request.args.get("source") or "").strip() or None,
        "event": (request.args.get("event") or "").strip() or None,
        "text": (request.args.get("q") or "").strip() or None,
    }
    result = q.journal_entries(page=page, per_page=50, **filters)
    return render_template(
        "ledger/journal.html",
        result=result,
        filters=filters,
        sources=sorted(sync.MODELS.keys()),
        events=[
            "sale", "sale_cogs", "sale_receipt", "sale_service_cost", "receipt", "refund",
            "waive_off", "grn", "supplier_payment", "return", "return_cogs",
            "booking_advance", "driver_payment", "delivery_rent", "cash_transfer",
            "cash_in", "cash_out", "opening",
        ],
    )


@bp.route("/journal/<int:entry_id>")
@login_required
def entry(entry_id):
    _guard()
    row = db.session.get(JournalEntry, entry_id)
    if row is None:
        abort(404)
    history = posting.entries_for(row.source_type, row.source_id)
    return render_template(
        "ledger/entry.html",
        entry=row,
        display=posting.entry_display(row),
        history=history,
    )


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------
@bp.route("/trial_balance")
@login_required
def trial_balance():
    _guard()
    as_of = _date_arg("as_of", date.today())
    statement = q.trial_balance(as_of=as_of)
    return render_template("ledger/trial_balance.html", statement=statement, as_of=as_of)


@bp.route("/pnl")
@login_required
def pnl():
    _guard()
    start = _date_arg("start", date.today().replace(day=1))
    end = _date_arg("end", date.today())
    view = pnl_view.load(start, end)
    return render_template("ledger/pnl.html", view=view, start=start, end=end)


@bp.route("/balance_sheet")
@login_required
def balance_sheet():
    _guard()
    as_of = _date_arg("as_of", date.today())
    statement = q.balance_sheet(as_of=as_of)
    return render_template("ledger/balance_sheet.html", statement=statement, as_of=as_of)


@bp.route("/cash_flow")
@login_required
def cash_flow():
    from app.services.reporting import cash_flow as cash_flow_report

    _guard()
    start = _date_arg("start", date.today().replace(day=1))
    end = _date_arg("end", date.today())
    statement = cash_flow_report.build(start=start, end=end)
    return render_template("ledger/cash_flow.html", statement=statement, start=start, end=end)


@bp.route("/cash")
@login_required
def cash():
    _guard()
    view = cash_view.load(as_of=_date_arg("as_of", date.today()))
    return render_template("ledger/cash.html", view=view)


# ---------------------------------------------------------------------------
# Subledgers
# ---------------------------------------------------------------------------
@bp.route("/receivables")
@login_required
def receivables():
    _guard()
    as_of = _date_arg("as_of", date.today())
    view = client_account.balances(as_of)
    return render_template("ledger/receivables.html", view=view)


@bp.route("/client/<int:client_id>")
@login_required
def client_statement(client_id):
    _guard()
    as_of = _date_arg("as_of", date.today())
    view = client_account.load(client_id, as_of=as_of)
    return render_template("ledger/client_statement.html", view=view, as_of=as_of)


@bp.route("/payables")
@login_required
def payables():
    _guard()
    as_of = _date_arg("as_of", date.today())
    view = supplier_account.balances(as_of)
    return render_template("ledger/payables.html", view=view)


@bp.route("/supplier/<int:supplier_id>")
@login_required
def supplier_statement(supplier_id):
    _guard()
    as_of = _date_arg("as_of", date.today())
    view = supplier_account.load(supplier_id, as_of=as_of)
    return render_template("ledger/supplier_statement.html", view=view, as_of=as_of)


@bp.route("/inventory")
@login_required
def inventory():
    _guard()
    as_of = _date_arg("as_of", date.today())
    checks = {row["key"]: row for row in books_agree_model.checks(as_of)}
    return render_template(
        "ledger/control.html",
        title="Inventory control",
        subtitle="Book inventory against goods received minus FIFO consumption",
        check=checks.get("inventory"),
        extra=checks.get("uncosted"),
        as_of=as_of,
        back=url_for("ledger.overview"),
    )


@bp.route("/driver_payable")
@login_required
def driver_payable():
    _guard()
    as_of = _date_arg("as_of", date.today())
    checks = {row["key"]: row for row in books_agree_model.checks(as_of)}
    return render_template(
        "ledger/control.html",
        title="Driver payable control",
        subtitle="Accrued delivery rent against settlements",
        check=checks.get("driver_payable"),
        extra=None,
        as_of=as_of,
        back=url_for("ledger.overview"),
    )


@bp.route("/books_agree")
@login_required
def books_agree():
    _guard()
    as_of = _date_arg("as_of", date.today())
    view = books_agree_model.load(as_of)
    return render_template("ledger/books_agree.html", view=view)


@bp.route("/coverage")
@login_required
def coverage():
    _guard()
    return render_template("ledger/coverage.html", coverage=q.coverage())


# ---------------------------------------------------------------------------
# Sync controls and API
# ---------------------------------------------------------------------------
@bp.route("/sync", methods=["POST"])
@login_required
def run_sync():
    _guard(view=False)
    full = (request.form.get("mode") or "missing").strip().lower() == "full"
    try:
        report = replay.replay_all(actor=_actor()) if full else replay.sync_missing(actor=_actor())
        cache.invalidate()
        flash(
            f"Ledger sync complete — {report.get('posted', 0)} entry(ies) posted"
            + (f", {report.get('failed', 0)} failed" if report.get("failed") else "")
            + ".",
            "success" if not report.get("failed") else "warning",
        )
    except Exception as exc:  # pragma: no cover - surfaced to the user
        db.session.rollback()
        flash(f"Ledger sync failed: {exc}", "danger")
    return redirect(request.referrer or url_for("ledger.overview"))


@bp.route("/api/version")
@login_required
def api_version():
    _guard()
    return jsonify({"version": q.ledger_version(), "entries": JournalEntry.query.count()})


@bp.route("/api/summary")
@login_required
def api_summary():
    _guard()
    summary = dashboard_model.load(date.today())
    return jsonify({
        "version": summary["ledger_version"],
        "cash_balance_minor": summary["cash_balance"],
        "receivable_total_minor": summary["receivable_total"],
        "payable_total_minor": summary["payable_total"],
        "driver_payable_total_minor": summary["driver_payable_total"],
        "inventory_value_minor": summary["inventory_value"],
        "month_revenue_minor": summary["month_revenue"],
        "month_profit_minor": summary["month_profit"],
    })
