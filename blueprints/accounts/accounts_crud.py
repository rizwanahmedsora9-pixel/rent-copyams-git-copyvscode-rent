"""accounts_crud — split from accounts.py."""
from ._common import *  # noqa


def _account_master_permission_ok():
    # Atomic grant: managing account masters (create/edit/archive) is its own
    # switch, no longer hard-wired to admin/root only.
    if (getattr(current_user, 'role', '') or '').strip().lower() in ('admin', 'root'):
        return True
    return bool(getattr(current_user, 'can_manage_account_masters', False))


def _reconcile_permission_ok():
    """Who may settle an account (post a reconciliation + adjustment entry)."""
    if (getattr(current_user, 'role', '') or '').strip().lower() in ('admin', 'root'):
        return True
    return bool(getattr(current_user, 'can_reconcile_accounts', False))


def _deny_account_master_mutation():
    if _account_master_permission_ok():
        return None
    from flask import abort
    abort(403)


@accounts_bp.route('/accounts')
@login_required
def manage_accounts():
    """Manage Accounts tab of the rebuilt workspace."""
    from .workspace import render_workspace
    return render_workspace('accounts')


@accounts_bp.route('/categories/add', methods=['POST'])
@login_required
def add_account_category():
    _deny_account_master_mutation()
    name = (request.form.get('name') or '').strip()
    note = (request.form.get('note') or '').strip()

    if not name:
        flash('Category name is required.', 'danger')
        return redirect(url_for('accounts.manage_accounts'))

    existing = AccountCategory.query.filter(
        func.lower(func.trim(AccountCategory.name)) == name.lower(),
        AccountCategory.is_active == True
    ).first()
    if existing:
        flash('This account category already exists.', 'warning')
        return redirect(url_for('accounts.manage_accounts'))

    db.session.add(AccountCategory(name=name, note=note or None))
    db.session.commit()
    audit_log(current_user, 'account.category.create', f'name={name}')
    flash('Account category created successfully.', 'success')
    return redirect(url_for('accounts.manage_accounts'))


@accounts_bp.route('/accounts/add', methods=['GET', 'POST'])
@login_required
def add_account():
    """Create an account through the simple setup form."""
    _deny_account_master_mutation()
    from .workspace import setup_save_account, _tab_redirect
    if request.method == 'GET':
        return _tab_redirect('accounts', new=1)
    form = request.form.to_dict()
    # Compatibility for older integrations that still post the retired
    # classification payload. The UI no longer exposes or requires it.
    if 'account_type' not in form and form.get('class_subcategory'):
        sub = form['class_subcategory'].strip().lower()
        form['account_type'] = 'bank' if sub == 'bank' else ('wallet' if 'wallet' in sub else 'cash')
        opening = form.get('opening_amount') or form.get('opening_balance') or '0'
        if (form.get('opening_position') or '').strip().lower() == 'credit' and not opening.startswith('-'):
            opening = '-' + opening
        form['opening_balance'] = opening
        form.setdefault('account_number', form.get('wallet_number') or '')
    return setup_save_account(form_override=form)


@accounts_bp.route('/ledger/<int:account_id>')
@login_required
def account_ledger(account_id):
    account = Account.query.get_or_404(account_id)
    page = request.args.get('page', 1, type=int)
    per_page = 50
    search = (request.args.get('q') or '').strip()
    type_f = (request.args.get('type') or '').strip()
    category_f = (request.args.get('category') or '').strip()
    subcategory_f = (request.args.get('subcategory') or '').strip()
    show_voided = request.args.get('show_voided') == '1'
    date_from, date_to_excl = _parse_date_range(default_days=90)

    base_filters = [
        or_(AccountTransaction.from_account_id == account.id,
            AccountTransaction.to_account_id == account.id)
    ]
    if not show_voided:
        base_filters.append(AccountTransaction.is_void == False)

    # Reproducible opening from the explicit account baseline + ledger, not from
    # a frontend/current-balance subtraction that can include later periods.
    from app.services.payments_crud import ledger_balance
    opening_cutoff = datetime.combine(date_from, datetime.min.time()) - timedelta(microseconds=1)
    opening_balance = ledger_balance(account.id, as_of=opening_cutoff)

    q = AccountTransaction.query.filter(*base_filters,
        AccountTransaction.date_posted >= date_from,
        AccountTransaction.date_posted < date_to_excl
    )
    if search:
        like = f'%{search}%'
        q = q.filter(or_(AccountTransaction.description.ilike(like), AccountTransaction.note.ilike(like)))
    if type_f:
        q = q.filter(AccountTransaction.transaction_type == type_f)
    if category_f:
        q = q.filter(func.lower(func.trim(AccountTransaction.category)) == category_f.lower())
    if subcategory_f:
        q = q.filter(func.lower(func.trim(AccountTransaction.subcategory)) == subcategory_f.lower())

    period_in = q.with_entities(func.coalesce(func.sum(AccountTransaction.amount), 0)).filter(
        AccountTransaction.to_account_id == account.id, AccountTransaction.is_void == False
    ).scalar() or 0
    period_out = q.with_entities(func.coalesce(func.sum(AccountTransaction.amount), 0)).filter(
        AccountTransaction.from_account_id == account.id, AccountTransaction.is_void == False
    ).scalar() or 0

    # Running balances include *all* active movements, even when the displayed
    # rows are narrowed by search/type filters.
    all_period_rows = AccountTransaction.query.filter(
        or_(AccountTransaction.from_account_id == account.id, AccountTransaction.to_account_id == account.id),
        AccountTransaction.date_posted >= date_from,
        AccountTransaction.date_posted < date_to_excl,
    ).order_by(AccountTransaction.date_posted.asc(), AccountTransaction.id.asc()).all()
    running = opening_balance
    running_by_id = {}
    for row in all_period_rows:
        if not row.is_void:
            if row.to_account_id == account.id:
                running += float(row.amount or 0)
            if row.from_account_id == account.id:
                running -= float(row.amount or 0)
        running_by_id[row.id] = running

    rows_asc = q.order_by(AccountTransaction.date_posted.asc(), AccountTransaction.id.asc()).all()
    enriched = []
    for r in rows_asc:
        delta = 0.0
        if not r.is_void:
            if r.to_account_id == account.id:
                delta += float(r.amount or 0)
            if r.from_account_id == account.id:
                delta -= float(r.amount or 0)
        enriched.append({'tx': r, 'delta': delta, 'running': running_by_id.get(r.id)})

    enriched.reverse()  # display newest first

    # Manual pagination over enriched list
    total_rows = len(enriched)
    start = (page - 1) * per_page
    end = start + per_page
    page_rows = enriched[start:end]

    types = ['Receipt', 'Refund', 'Payment', 'Transfer', 'Supplier Payment', 'Driver Payment', 'Expense', 'Loss', 'Adjustment', 'Reconciliation Loss', 'Reconciliation Excess']

    # Category tracing for this account: what it received and what it paid for.
    label_rows = db.session.query(
        AccountTransaction.category, AccountTransaction.subcategory,
    ).filter(
        or_(AccountTransaction.from_account_id == account.id, AccountTransaction.to_account_id == account.id),
        AccountTransaction.date_posted >= date_from,
        AccountTransaction.date_posted < date_to_excl,
        AccountTransaction.category.isnot(None),
    ).distinct().all()
    category_options = sorted({(row[0] or '').strip() for row in label_rows if (row[0] or '').strip()}, key=str.lower)
    subcategory_options = sorted({(row[1] or '').strip() for row in label_rows if (row[1] or '').strip()}, key=str.lower)

    return render_template('accounts/account_ledger.html', account=account, page_rows=page_rows,
                           opening_balance=opening_balance, period_in=period_in, period_out=period_out,
                           date_from=date_from, date_to=date_to_excl - timedelta(days=1),
                           search=search, type_f=type_f, types=types, show_voided=show_voided,
                           category_f=category_f, subcategory_f=subcategory_f,
                           category_options=category_options, subcategory_options=subcategory_options,
                           page=page, per_page=per_page, total_rows=total_rows,
                           has_prev=page > 1, has_next=end < total_rows)


@accounts_bp.route('/<int:account_id>/data')
@login_required
def account_data(account_id):
    """JSON data for an account (kept for backwards compatibility / tooling)."""
    from app.services.payments_crud import ledger_balance
    a = Account.query.get_or_404(account_id)
    return jsonify({
        'id': a.id,
        'name': a.name,
        'category': a.category,
        'source_category': a.source_category,
        'account_type': a.account_type or (getattr(a, 'type', None) or ''),
        'balance': float(a.balance or 0),
        'calculated_balance': ledger_balance(a.id),
        'bank_name': a.bank_name or '',
        'account_holder_name': a.account_holder_name or '',
        'account_number': a.account_number or '',
        'branch_code': a.branch_code or '',
        'note': a.note or '',
        'is_active': bool(a.is_active),
        'class_category': a.class_category or '',
        'class_subcategory': a.class_subcategory or '',
        'class_account_type': a.class_account_type or '',
        'channel': a.channel or '',
        'cash_location': a.cash_location or '',
        'cash_responsible': a.cash_responsible or '',
        'wallet_provider': a.wallet_provider or '',
        'wallet_number': a.wallet_number or '',
        'wallet_holder': a.wallet_holder or '',
        'linked_entity_type': a.linked_entity_type or 'none',
        'linked_client_id': a.linked_client_id,
        'linked_supplier_id': a.linked_supplier_id,
        'linked_party_name': a.linked_party_name or '',
        'account_status': a.account_status or ('active' if a.is_active else 'inactive'),
    })


@accounts_bp.route('/<int:account_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_account(account_id):
    """Edit an account through the simple setup form."""
    _deny_account_master_mutation()
    from .workspace import render_workspace, _tab_redirect
    from app.services import account_setup as setup
    account = Account.query.get_or_404(account_id)
    if request.method == 'GET':
        return _tab_redirect('accounts', edit=account.id)
    try:
        form = request.form.to_dict()
        form['account_id'] = str(account.id)
        setup.save_account(form, actor=current_user, account_id=account.id)
        db.session.commit()
        flash('Account updated.', 'success')
        return _tab_redirect('accounts')
    except setup.SetupError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
        return render_workspace('accounts', modal_open='account', form_values=request.form,
                                edit_account_id=account.id, status=422)
    except Exception:
        db.session.rollback()
        logger.exception('Account edit failed')
        flash('The account could not be saved. No changes were applied.', 'danger')
        return _tab_redirect('accounts')


def _current_opening_minor(account):
    from utils.money import to_minor
    if getattr(account, 'opening_balance_minor', None) is not None:
        return int(account.opening_balance_minor)
    return to_minor(account.opening_balance or 0)


def _apply_opening_update(account, form):
    """Rewrite the auditable opening baseline without posting a ledger entry.

    Changing opening shifts the cached current balance by the same delta so
    ledger_balance() (opening + movements) stays in lockstep.  Missing
    ``opening_amount`` is treated as no change so older edit posts keep working.
    """
    from utils.money import from_minor, to_minor

    amount_raw = (form.get('opening_amount') if form is not None else None)
    amount_raw = (amount_raw or '').strip() if amount_raw is not None else ''
    date_raw = (form.get('opening_effective_date') if form is not None else None)
    date_raw = (date_raw or '').strip() if date_raw is not None else ''
    if amount_raw == '' and date_raw == '':
        return ''

    old_minor = _current_opening_minor(account)
    new_minor = old_minor
    if amount_raw != '':
        amount_minor = to_minor(amount_raw or 0, field='Opening amount')
        if amount_minor < 0:
            raise ValueError('Opening amount cannot be negative. Use Credit for money we owe.')
        position = ((form.get('opening_position') or 'debit') if form is not None else 'debit')
        position = (position or 'debit').strip().lower()
        if position not in ('debit', 'credit'):
            raise ValueError('Opening balance direction must be debit or credit.')
        if position == 'credit':
            amount_minor = -amount_minor
        new_minor = amount_minor

    date_changed = False
    if date_raw:
        try:
            effective_dt = datetime.strptime(date_raw, '%Y-%m-%d')
        except ValueError:
            raise ValueError('Opening effective date must be a valid date (YYYY-MM-DD).')
        old_date = account.opening_balance_date
        old_d = old_date.date() if isinstance(old_date, datetime) else old_date
        if old_d != effective_dt.date():
            account.opening_balance_date = effective_dt
            date_changed = True

    if new_minor == old_minor:
        return 'Opening effective date updated.' if date_changed else ''

    delta = new_minor - old_minor
    account.opening_balance_minor = new_minor
    account.opening_balance = float(from_minor(new_minor))

    current_minor = (
        int(account.balance_minor)
        if getattr(account, 'balance_minor', None) is not None
        else to_minor(account.balance or 0)
    )
    new_current = current_minor + delta
    account.balance_minor = new_current
    account.balance = float(from_minor(new_current))
    return (
        f'Opening balance updated from Rs. {float(from_minor(old_minor)):.2f} '
        f'to Rs. {float(from_minor(new_minor)):.2f}. '
        f'Current calculated balance shifted by the same amount.'
    )


def _apply_balance_adjustment(account, desired_raw, form):
    """Post a single traceable Adjustment ledger entry for the desired balance.

    Returns a short human message, or '' when no adjustment is needed.
    Raises ValueError for invalid input.  Implements PART 13/14/15:

    * Desired == current  -> no entry, no zero-value transaction (PART 15).
    * Difference < 0      -> money OUT (visible negative ledger entry).
    * Difference > 0      -> money IN (visible positive ledger entry).
    * Idempotency key     -> retried / double-clicked save cannot post twice.
    """
    from utils.money import from_minor, to_minor
    from app.services.payments_crud import _assert_period_open, ledger_balance

    if desired_raw == '':
        return ''

    desired_minor = to_minor(desired_raw, field='Desired balance')
    # Compare against the reproducible ledger (opening + movements), which
    # already includes any opening correction applied earlier in this save.
    current_minor = to_minor(ledger_balance(account.id), field='Current balance')
    diff_minor = desired_minor - current_minor
    if diff_minor == 0:
        return ''  # PART 15: no-change save

    # Adjustment reason is mandatory whenever an adjustment is made (PART 12).
    reason = (form.get('adjustment_reason') or '').strip()
    reason_other = (form.get('adjustment_reason_other') or '').strip()
    if (reason or '').lower() == 'other':
        reason = reason_other
    if not reason:
        raise ValueError('An adjustment reason is required when changing the balance.')

    adj_date_raw = (form.get('adjustment_date') or '').strip()
    if adj_date_raw:
        try:
            adj_dt = datetime.strptime(adj_date_raw, '%Y-%m-%d')
        except ValueError:
            raise ValueError('Adjustment date must be a valid date (YYYY-MM-DD).')
    else:
        adj_dt = pk_now()

    # Reject edits inside a finalised (reconciled) period (PART 14 safety).
    _assert_period_open(account.id, adj_dt, operation='adjusted')

    # Idempotency: a retried/double-clicked save with the same key skips the
    # duplicate post.  An adjustment marker keyed to the account+key means a
    # repeat submission finds the existing entry and does nothing.
    idem = (form.get('idempotency_key') or '').strip()
    if idem:
        from models import AccountTransaction
        replay = AccountTransaction.query.filter(
            AccountTransaction.idempotency_key == idem,
            AccountTransaction.is_void == False,
        ).first()
        if replay:
            return ''

    direction = 'decrease' if diff_minor < 0 else 'increase'
    amount_minor = abs(diff_minor)
    adj = AccountTransaction(
        from_account_id=(account.id if diff_minor < 0 else None),
        to_account_id=(account.id if diff_minor > 0 else None),
        amount=float(from_minor(amount_minor)), amount_minor=amount_minor,
        description='Balance adjustment (manual edit)',
        note=(f'Adjusted from Rs. {float(from_minor(current_minor)):.2f} '
              f'to Rs. {float(from_minor(desired_minor)):.2f} ({direction} of '
              f'Rs. {float(from_minor(amount_minor)):.2f})'),
        reason=reason[:300],
        transaction_type='Adjustment', source_type='Account', source_id=account.id,
        idempotency_key=(idem[:64] if idem else None),
        created_by=getattr(current_user, 'username', None), date_posted=adj_dt,
    )
    db.session.add(adj)
    db.session.flush()
    from app.services.accounting import _apply_account_tx_effect
    _apply_account_tx_effect(adj)
    account.balance_minor = desired_minor
    account.balance = float(from_minor(desired_minor))
    return (f'Balance {direction}d by Rs. {float(from_minor(amount_minor)):.2f} '
            f'via an Adjustment ledger entry.')


@accounts_bp.route('/<int:account_id>/toggle', methods=['POST'])
@login_required
def toggle_account(account_id):
    """Suspend/resume without touching balances or history."""
    _deny_account_master_mutation()
    from .workspace import _tab_redirect
    from app.services import account_setup as setup
    account = Account.query.get_or_404(account_id)
    try:
        setup.set_account_active(account.id, active=not bool(account.is_active), actor=current_user, form=request.form)
        db.session.commit()
        flash(f'Account {"resumed" if account.is_active else "suspended"}.', 'success')
    except setup.SetupError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception:
        db.session.rollback()
        logger.exception('Account status change failed')
        flash('The account status could not be changed.', 'danger')
    return _tab_redirect('accounts')


@accounts_bp.route('/<int:account_id>/delete', methods=['POST'])
@login_required
def delete_account(account_id):
    """Delete only an unused account; used accounts must be suspended."""
    _deny_account_master_mutation()
    from .workspace import _tab_redirect
    from app.services import account_setup as setup
    try:
        setup.delete_account_setup(account_id, actor=current_user, form=request.form)
        db.session.commit()
        flash('Unused account deleted.', 'success')
    except setup.SetupError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception:
        db.session.rollback()
        logger.exception('Account delete failed')
        flash('The account could not be deleted. No changes were applied.', 'danger')
    return _tab_redirect('accounts')


@accounts_bp.route('/reconciliations')
@login_required
def reconciliations():
    """List of per-account reconciliation records (immutable audit history)."""
    page = max(request.args.get('page', 1, type=int) or 1, 1)
    per_page = min(max(request.args.get('per_page', 50, type=int) or 50, 10), 100)
    account_id_f = request.args.get('account_id', type=int)
    status_f = (request.args.get('status') or '').strip()
    date_from, date_to_excl = _parse_date_range(default_days=365)
    from models import AccountReconciliation

    q = AccountReconciliation.query.filter(
        AccountReconciliation.reconciliation_date >= date_from,
        AccountReconciliation.reconciliation_date < date_to_excl,
    )
    if account_id_f:
        q = q.filter(AccountReconciliation.account_id == account_id_f)
    if status_f:
        q = q.filter(AccountReconciliation.difference_type == status_f)
    total_count = q.count()
    recs = q.order_by(
        AccountReconciliation.reconciliation_date.desc(),
        AccountReconciliation.id.desc()
    ).paginate(page=page, per_page=per_page, error_out=False)
    return render_template(
        'accounts/reconciliations.html', recs=recs, total_count=total_count,
        accounts=Account.query.order_by(Account.name.asc()).all(),
        account_id_f=account_id_f, status_f=status_f,
        date_from=date_from, date_to=date_to_excl - timedelta(days=1), per_page=per_page,
    )


@accounts_bp.route('/<int:account_id>/reconcile', methods=['GET', 'POST'])
@login_required
def reconcile_account(account_id):
    """Reconcile one account: compare ledger (expected) vs physical (actual)."""
    from app.services.payments_crud import ledger_balance, reconcile_account as do_reconcile
    from models import AccountReconciliation

    account = Account.query.get_or_404(account_id)
    expected = ledger_balance(account.id)
    recent = AccountReconciliation.query.filter_by(account_id=account.id).order_by(
        AccountReconciliation.reconciliation_date.desc(), AccountReconciliation.id.desc()
    ).limit(5).all()

    if request.method == 'POST':
        if not _reconcile_permission_ok():
            flash('You need the "Account Reconciliation Settlement" permission to settle accounts.', 'danger')
            return redirect(url_for('accounts.reconcile_account', account_id=account.id))
        try:
            actual = request.form.get('actual_balance', '').strip()
            if actual == '':
                raise ValueError('Actual balance is required.')
            note = request.form.get('note', '')
            date_raw = (request.form.get('reconciliation_date') or '').strip()
            if date_raw:
                try:
                    rec_date = datetime.strptime(date_raw, '%Y-%m-%d').date()
                except ValueError:
                    raise ValueError('Invalid reconciliation date.')
            else:
                rec_date = pk_today()
            rec = do_reconcile(
                account_id=account.id,
                actual_balance=actual,
                reconciliation_date=rec_date,
                note=note,
                actor=current_user,
            )
            db.session.commit()
            flash(
                f'Account reconciled as {rec.difference_type}. '
                f'Expected Rs. {rec.expected_balance:,.2f}, Actual Rs. {rec.actual_balance:,.2f}, '
                f'Difference Rs. {rec.difference:,.2f}.',
                'success' if rec.difference_type == 'Matched' else 'warning'
            )
            return redirect(url_for('accounts.account_ledger', account_id=account.id))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception:
            db.session.rollback()
            logger.exception('Account reconciliation failed')
            flash('Unable to reconcile account: the reconciliation could not be completed. Please try again.', 'danger')

    return render_template('accounts/reconcile_account.html', account=account,
                           expected=expected, recent=recent,
                           today=pk_today().strftime('%Y-%m-%d'))


@accounts_bp.route('/categories', methods=['GET'])
@login_required
def manage_categories():
    """Manage Categories tab of the rebuilt workspace."""
    from .workspace import render_workspace
    return render_workspace('categories')


@accounts_bp.route('/categories/<int:category_id>/edit', methods=['POST'])
@login_required
def edit_account_category_route(category_id):
    _deny_account_master_mutation()
    cat = AccountCategory.query.get_or_404(category_id)
    name = (request.form.get('name') or '').strip()
    note = (request.form.get('note') or '').strip()

    if not name:
        flash('Category name cannot be empty.', 'danger')
        return redirect(url_for('accounts.manage_categories'))

    existing = AccountCategory.query.filter(
        func.lower(func.trim(AccountCategory.name)) == name.lower(),
        AccountCategory.id != cat.id
    ).first()
    if existing:
        flash('Another category with this name already exists.', 'warning')
        return redirect(url_for('accounts.manage_categories'))

    old_name = cat.name
    cat.name = name
    cat.note = note or None
    
    if old_name.lower() != name.lower():
        Account.query.filter(
            func.lower(func.trim(Account.source_category)) == old_name.lower()
        ).update({Account.source_category: name}, synchronize_session=False)

    db.session.commit()
    audit_log(current_user, 'account.category.update', f'id={category_id}, name={name}')
    flash('Account category updated successfully.', 'success')
    return redirect(url_for('accounts.manage_categories'))


@accounts_bp.route('/categories/<int:category_id>/toggle', methods=['POST'])
@login_required
def toggle_account_category_route(category_id):
    _deny_account_master_mutation()
    cat = AccountCategory.query.get_or_404(category_id)
    cat.is_active = not cat.is_active
    db.session.commit()
    status_str = 'activated' if cat.is_active else 'disabled'
    audit_log(current_user, 'account.category.toggle', f'id={category_id}, active={cat.is_active}')
    flash(f'Account category {status_str} successfully.', 'success')
    return redirect(url_for('accounts.manage_categories'))


@accounts_bp.route('/categories/<int:category_id>/delete', methods=['POST'])
@login_required
def delete_account_category_route(category_id):
    _deny_account_master_mutation()
    cat = AccountCategory.query.get_or_404(category_id)
    
    used = Account.query.filter(
        func.lower(func.trim(Account.source_category)) == cat.name.lower()
    ).first() is not None

    if used:
        flash('This category is currently used by one or more accounts and cannot be deleted.', 'danger')
        return redirect(url_for('accounts.manage_categories'))

    db.session.delete(cat)
    db.session.commit()
    audit_log(current_user, 'account.category.delete', f'id={category_id}, name={cat.name}')
    flash('Account category deleted successfully.', 'success')
    return redirect(url_for('accounts.manage_categories'))


@accounts_bp.route('/api/categories/add', methods=['POST'])
@login_required
def api_add_account_category():
    _deny_account_master_mutation()
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    note = (data.get('note') or '').strip()

    if not name:
        return jsonify({'ok': False, 'error': 'Category name is required.'}), 400

    existing = AccountCategory.query.filter(
        func.lower(func.trim(AccountCategory.name)) == name.lower()
    ).first()
    if existing:
        if not existing.is_active:
            existing.is_active = True
            db.session.commit()
            return jsonify({'ok': True, 'category': {'id': existing.id, 'name': existing.name}})
        return jsonify({'ok': False, 'error': 'This category already exists.'}), 400

    new_cat = AccountCategory(name=name, note=note or None)
    db.session.add(new_cat)
    db.session.commit()
    audit_log(current_user, 'account.category.create', f'name={name}')
    return jsonify({'ok': True, 'category': {'id': new_cat.id, 'name': new_cat.name}})


@accounts_bp.route('/transaction-categories', methods=['GET', 'POST'])
@login_required
def transaction_categories():
    """Manage transaction labels independently of the chart of accounts."""
    from models import CashFlowCategory, CashFlowSubcategory
    _deny_account_master_mutation()
    if request.method == 'POST':
        try:
            kind = request.form.get('kind', 'category')
            if kind not in ('category', 'subcategory'):
                raise ValueError('Invalid category type.')
            model = CashFlowCategory if kind == 'category' else CashFlowSubcategory
            row_id = request.form.get('id', type=int)
            row = db.session.get(model, row_id) if row_id else None
            if row_id and not row:
                raise ValueError('Category not found.')
            if request.form.get('action') == 'toggle':
                if not row:
                    raise ValueError('Category not found.')
                row.is_active = not row.is_active
            else:
                name = (request.form.get('name') or '').strip()
                if not name or len(name) > 120:
                    raise ValueError('Enter a name between 1 and 120 characters.')
                query = model.query.filter(func.lower(func.trim(model.name)) == name.lower())
                if row:
                    query = query.filter(model.id != row.id)
                if kind == 'subcategory':
                    parent_id = request.form.get('category_id', type=int)
                    if not parent_id or not db.session.get(CashFlowCategory, parent_id):
                        raise ValueError('Select a parent category.')
                    if row and row.category_id != parent_id:
                        raise ValueError('A subcategory cannot be moved to another parent.')
                    query = query.filter(model.category_id == parent_id)
                if query.first():
                    raise ValueError('That name already exists. Edit or enable the existing entry.')
                if not row:
                    row = model()
                    db.session.add(row)
                    row.is_active = True
                row.name = name
                if kind == 'category':
                    direction = request.form.get('direction', 'both')
                    if direction not in ('in', 'out', 'both'):
                        raise ValueError('Select a valid direction.')
                    row.direction = direction
                else:
                    row.category_id = parent_id
            db.session.commit()
            audit_log(current_user, 'account.transaction_category.update', f'{kind} id={row.id}')
            flash('Transaction categories updated.', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        return redirect(url_for('accounts.manage_categories'))
    return redirect(url_for('accounts.manage_categories'))
