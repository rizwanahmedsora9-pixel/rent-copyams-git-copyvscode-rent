"""Accounts landing page — the rebuilt four-tab workspace."""
from ._common import *  # noqa


@accounts_bp.route('/')
@login_required
def dashboard():
    from .workspace import render_workspace
    return render_workspace('cash-flow')


@accounts_bp.route('/company-account/add', methods=['POST'])
@login_required
def add_company_account():
    """Compatibility alias for the previous quick-create endpoint."""
    from .workspace import setup_save_account
    form = request.form.to_dict()
    form.setdefault("account_type", form.get("category") or "cash")
    return setup_save_account(form_override=form)
