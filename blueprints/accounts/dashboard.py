"""Accounts dashboard route.

Intentionally blank: the previous dashboard was removed so the page can be
rebuilt from scratch. Other ``/accounts/*`` routes (payments, transfers,
reconciliations, account management) are untouched and keep working.
"""
from ._common import *  # noqa


@accounts_bp.route('/')
@login_required
def dashboard():
    """Blank Accounts page — rebuild from scratch."""
    return render_template('accounts/dashboard.html')
