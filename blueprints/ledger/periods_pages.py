"""Periods: view, close, lock, reopen.  One state machine, one set of rules."""
from __future__ import annotations

from ._common import *  # noqa: F401,F403


@bp.route("/periods")
@login_required
def periods():
    _guard()
    rows = []
    for period in period_svc.list_periods():
        rows.append({"period": period, "totals": q.period_totals(period)})
    return render_template(
        "ledger/periods.html",
        rows=rows,
        can_manage=_can_manage(),
        statuses=[period_svc.OPEN, period_svc.SOFT_CLOSED, period_svc.LOCKED],
    )


@bp.route("/periods/<int:period_id>/checklist")
@login_required
def period_checklist(period_id):
    _guard()
    from models import AccountingPeriod
    from app.services.ledger import close as close_svc

    period = db.session.get(AccountingPeriod, period_id)
    if period is None:
        abort(404)
    return render_template(
        "ledger/period_checklist.html",
        period=period,
        items=close_svc.checklist(period),
        totals=q.period_totals(period),
        can_manage=_can_manage(),
    )


def _get_period(period_id):
    from models import AccountingPeriod

    period = db.session.get(AccountingPeriod, period_id)
    if period is None:
        abort(404)
    return period


@bp.route("/periods/<int:period_id>/lock", methods=["POST"])
@login_required
def lock(period_id):
    _guard(view=False)
    from app.services.ledger import close as close_svc

    period = _get_period(period_id)
    try:
        close_svc.close_period(period, actor=_actor(), lock=True,
                               note=(request.form.get("note") or "")[:500] or None)
        db.session.commit()
        cache.invalidate()
        flash(f"{period_svc.period_label(period)} closed and locked.", "success")
    except close_svc.CloseRefused as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("ledger.periods"))


@bp.route("/periods/<int:period_id>/soft_close", methods=["POST"])
@login_required
def soft_close(period_id):
    _guard(view=False)
    period = _get_period(period_id)
    try:
        period_svc.soft_close_period(period, actor=_actor(),
                                     note=(request.form.get("note") or "")[:500] or None)
        db.session.commit()
        cache.invalidate()
        flash(f"{period_svc.period_label(period)} soft-closed (adjustments still allowed).", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("ledger.periods"))


@bp.route("/periods/<int:period_id>/reopen", methods=["POST"])
@login_required
def reopen(period_id):
    _guard(view=False)
    period = _get_period(period_id)
    reason = (request.form.get("reason") or "").strip()
    try:
        period_svc.reopen_period(period, actor=_actor(), reason=reason)
        db.session.commit()
        cache.invalidate()
        flash(f"{period_svc.period_label(period)} reopened.", "warning")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("ledger.periods"))
