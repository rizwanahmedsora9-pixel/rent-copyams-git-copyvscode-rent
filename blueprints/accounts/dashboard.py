"""Owner-led cash and bank workspace."""
from ._common import *  # noqa


@accounts_bp.route('/')
@login_required
def dashboard():
    from models import CashFlowCategory
    from app.services.payments_crud import ledger_balance
    accounts = (Account.query.filter(
        func.coalesce(Account.is_active, True) == True,
        func.lower(func.trim(Account.account_type)) == 'company',
        func.lower(func.trim(Account.category)).in_(['cash', 'bank']),
    ).order_by(Account.name, Account.id).all())
    balances = {a.id: ledger_balance(a.id) for a in accounts}
    categories = (CashFlowCategory.query.filter_by(is_active=True)
                  .order_by(CashFlowCategory.sort_order, CashFlowCategory.name).all())
    return render_template('accounts/dashboard.html', accounts=accounts, balances=balances,
                           total=sum(balances.values()), categories=categories,
                           category_children={c.name: [s.name for s in c.subcategories if s.is_active] for c in categories},
                           can_manage_master=_account_master_permission_ok())


@accounts_bp.route('/company-account/add', methods=['POST'])
@login_required
def add_company_account():
    """Create a cash/bank account without imposing a business classification."""
    from utils.money import to_minor, from_minor
    from utils.accounting_audit import record_accounting_audit
    _deny_account_master_mutation()
    try:
        name = (request.form.get('name') or '').strip()
        kind = (request.form.get('category') or '').strip().lower()
        if not name or len(name) > 100:
            raise ValueError('Enter an account name of up to 100 characters.')
        if kind not in ('cash', 'bank'):
            raise ValueError('Choose cash or bank.')
        if Account.query.filter(func.lower(func.trim(Account.name)) == name.lower()).first():
            raise ValueError('An account with this name already exists.')
        opening = to_minor(request.form.get('opening_balance') or '0', field='Opening balance')
        if opening < 0:
            raise ValueError('Opening balance cannot be negative.')
        raw_date = (request.form.get('opening_date') or '').strip()
        try:
            opening_date = datetime.strptime(raw_date, '%Y-%m-%d') if raw_date else pk_now()
        except ValueError:
            raise ValueError('Enter a valid opening date.')
        bank_name = (request.form.get('bank_name') or '').strip()
        account_number = (request.form.get('account_number') or '').strip()
        if len(bank_name) > 100 or len(account_number) > 50:
            raise ValueError('Bank details are too long.')
        value = float(from_minor(opening))
        account = Account(name=name, type='company', account_type='company', category=kind,
                          channel=kind, balance=value, balance_minor=opening,
                          opening_balance=value, opening_balance_minor=opening,
                          opening_balance_date=opening_date, is_active=True, account_status='active',
                          bank_name=bank_name or None if kind == 'bank' else None,
                          account_number=account_number or None if kind == 'bank' else None)
        db.session.add(account)
        db.session.flush()
        record_accounting_audit(current_user, action='Create', entity_type='Account', entity_id=account.id,
                                after={'name': name, 'category': kind, 'opening_balance': value},
                                amount_after=value, account_after_id=account.id)
        db.session.commit()
        flash('Company account created.', 'success')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception:
        db.session.rollback()
        logger.exception('Company account creation failed')
        flash('Unable to create the account. Please check the details and try again.', 'danger')
    return redirect(url_for('accounts.dashboard'))
