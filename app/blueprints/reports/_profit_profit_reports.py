"""Profit & Loss Control Center routes.

All accounting lives in :mod:`app.services.profit_loss`.  These routes only
validate request input, enforce report permission, and serialize the same view
model to HTML, PDF or Excel.
"""
from ._common import *  # noqa
from functools import wraps

from app.services.profit_loss import (
    build_profit_loss_report,
    build_transaction_detail,
    resolve_report_period,
)
from app.services.profit_loss_exports import build_profit_loss_excel
from app.services.files_pdf import _try_render_weasy_pdf


def _profit_permission(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user.role not in {"admin", "root"} and not bool(getattr(current_user, "can_view_reports", False)):
            flash("Permission denied for this module.", "danger")
            return redirect(url_for("index"))
        return view(*args, **kwargs)
    return wrapped


def _request_period():
    period = (request.args.get("period") or "").strip().lower()
    start_raw = (request.args.get("start_date") or "").strip()
    end_raw = (request.args.get("end_date") or "").strip()
    if not period:
        period = "custom" if (start_raw or end_raw) else "this_month"
    for raw in (start_raw, end_raw):
        if raw:
            datetime.strptime(raw, "%Y-%m-%d")
    start, end, label = resolve_report_period(period, start_raw or None, end_raw or None)
    return period, start, end, label


def _report_from_request(*, include_position=False):
    period, start, end, period_label = _request_period()
    report = build_profit_loss_report(
        start_date=start,
        end_date=end,
        client=request.args.get("client", ""),
        material=request.args.get("material", ""),
        supplier=request.args.get("supplier", ""),
        category=request.args.get("category", ""),
        grn_id=request.args.get("grn_id", ""),
        metric=request.args.get("metric", ""),
        q=request.args.get("q", ""),
        entry_type=request.args.get("entry_type", ""),
        cost_status=request.args.get("cost_status", ""),
        page=request.args.get("page", 1),
        per_page=request.args.get("per_page", 25),
        include_position=include_position,
    )

    def report_url(endpoint="profit_reports", **overrides):
        args = request.args.to_dict(flat=True)
        args.pop("page", None)
        args["start_date"] = report["start_date_str"]
        args["end_date"] = report["end_date_str"]
        args["period"] = "all" if start is None and end is None else "custom"
        for key, value in overrides.items():
            if value is None or value == "":
                args.pop(key, None)
            else:
                args[key] = value
        return url_for(endpoint, **args)

    return report, period, period_label, report_url


def _template_context(report, period, period_label, report_url):
    context = dict(report)
    context.update({
        "report": report,
        "period": period,
        "period_label": period_label,
        "report_url": report_url,
        "start_date": report["start_date_str"],
        "end_date": report["end_date_str"],
    })
    return context


@bp.route("/profit_reports")
@login_required
@_profit_permission
def profit_reports():
    try:
        report, period, period_label, report_url = _report_from_request(
            include_position=(request.args.get("view") != "entries")
        )
    except (TypeError, ValueError):
        flash("Invalid date. Please use YYYY-MM-DD.", "danger")
        return redirect(url_for("profit_reports"))
    view_mode = (request.args.get("view") or "").strip().lower()
    template_name = "profit_entries.html" if view_mode == "entries" else "profit_reports.html"
    return render_template(template_name, **_template_context(report, period, period_label, report_url))


@bp.route("/profit_reports/transaction/<string:document_type>/<int:document_id>")
@login_required
@_profit_permission
def profit_transaction_detail(document_type, document_id):
    start_raw = (request.args.get("start_date") or "").strip() or None
    end_raw = (request.args.get("end_date") or "").strip() or None
    try:
        for raw in (start_raw, end_raw):
            if raw:
                datetime.strptime(raw, "%Y-%m-%d")
    except (TypeError, ValueError):
        flash("Invalid date. Please use YYYY-MM-DD.", "danger")
        return redirect(url_for("profit_reports"))
    detail = build_transaction_detail(document_type, document_id, start_date=start_raw, end_date=end_raw)
    if detail is None:
        abort(404)
    return render_template("profit_transaction_detail.html", detail=detail, **detail)


@bp.route("/profit_reports/export/pdf")
@login_required
@_profit_permission
def profit_report_pdf():
    try:
        report, period, period_label, report_url = _report_from_request(include_position=False)
    except (TypeError, ValueError):
        flash("Invalid date. Please use YYYY-MM-DD.", "danger")
        return redirect(url_for("profit_reports"))
    context = _template_context(report, period, period_label, report_url)
    context.update({
        "summary": report["export_summary"],
        "previous_summary": report["export_previous_summary"],
        "comparison": report["export_comparison"],
        "daily_insights": report["export_daily_insights"],
        "top_items": report["export_top_items"],
    })
    rendered = render_template("profit_report_pdf.html", **context)
    stamp = f"{report['start_date_str'] or 'all'}-{report['end_date_str'] or 'today'}"
    response = _try_render_weasy_pdf(rendered, f"profit-loss-{stamp}.pdf")
    if response is None:
        abort(500, "PDF generation failed")
    return response


@bp.route("/profit_reports/export/excel")
@login_required
@_profit_permission
def profit_report_excel():
    try:
        report, _period, _period_label, _report_url = _report_from_request(include_position=True)
    except (TypeError, ValueError):
        flash("Invalid date. Please use YYYY-MM-DD.", "danger")
        return redirect(url_for("profit_reports"))
    payload = build_profit_loss_excel(report)
    stamp = f"{report['start_date_str'] or 'all'}-{report['end_date_str'] or 'today'}"
    return send_file(
        payload,
        as_attachment=True,
        download_name=f"profit-loss-{stamp}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        max_age=0,
    )
