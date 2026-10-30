"""transactions — split from accounts.py."""
from ._common import *  # noqa


def _void_permission_ok():
    """Atomic gate for removing/restoring account-ledger rows."""
    if current_user.role in ('admin', 'root'):
        return True
    from app.services.permissions import _user_can
    if not _user_can('can_void_transactions'):
        return False
    # Must also legitimately work in the module the row lives in.
    return (getattr(current_user, 'can_manage_accounts', False)
            or getattr(current_user, 'can_manage_payments', False))


def _audit_pending_account_transactions():
    """Append atomic structured audit events for newly posted ledger rows."""
    pending = [obj for obj in list(db.session.new) if isinstance(obj, AccountTransaction)]
    if not pending:
        return
    from utils.accounting_audit import record_accounting_audit
    db.session.flush()
    for tx in pending:
        record_accounting_audit(
            current_user, action='Create', entity_type='AccountTransaction', entity_id=tx.id,
            after={'id': tx.id, 'type': tx.transaction_type, 'amount': tx.amount,
                   'from_account_id': tx.from_account_id, 'to_account_id': tx.to_account_id,
                   'description': tx.description, 'note': tx.note,
                   'source_type': tx.source_type, 'source_id': tx.source_id},
            amount_after=tx.amount,
            account_after_id=tx.to_account_id or tx.from_account_id,
            reason=tx.note,
        )


@accounts_bp.route('/transfers')
@login_required
def transfers():
    """View account transfers."""
    page = request.args.get('page', 1, type=int)
    per_page = 50
    search = (request.args.get('q') or '').strip()
    date_from, date_to_excl = _parse_date_range()

    q = AccountTransaction.query.filter(
        AccountTransaction.is_void == False,
        AccountTransaction.transaction_type == 'Transfer',
        AccountTransaction.date_posted >= date_from,
        AccountTransaction.date_posted < date_to_excl
    )
    if search:
        like = f'%{search}%'
        q = q.filter(or_(AccountTransaction.description.ilike(like), AccountTransaction.note.ilike(like)))

    total_amount = q.with_entities(func.coalesce(func.sum(AccountTransaction.amount), 0)).scalar() or 0
    total_count = q.count()
    transfers = q.order_by(AccountTransaction.date_posted.desc()).paginate(page=page, per_page=per_page)

    return render_template('accounts/transfers.html', transfers=transfers,
                           date_from=date_from, date_to=date_to_excl - timedelta(days=1),
                           search=search, total_amount=total_amount, total_count=total_count)


@accounts_bp.route('/transactions/<int:tx_id>/void', methods=['POST'])
@login_required
def void_transaction(tx_id):
    """Legacy URL — permanently deletes the account entry."""
    return delete_account_transaction(tx_id)


@accounts_bp.route('/transactions/<int:tx_id>/delete', methods=['POST'])
@login_required
def delete_account_transaction(tx_id):
    """Soft-void an entry or delegate to its canonical source record."""
    if not _void_permission_ok():
        flash('You need the "Void / Delete / Restore Transactions" permission to delete account entries.', 'danger')
        return redirect(request.referrer or url_for('accounts.dashboard'))
    tx = AccountTransaction.query.get_or_404(tx_id)
    try:
        if tx.reconciliation_id or (tx.source_type or '') == 'AccountReconciliation':
            raise ValueError('Reconciliation adjustments are immutable and cannot be deleted.')
        source_type = (tx.source_type or '').strip()
        source_id = tx.source_id
        note_txt = tx.note or ''
        if not source_type:
            pay_match = re.search(r'\[SRC:(?:Payment|ClientRefund):(\d+)\]', note_txt, flags=re.IGNORECASE)
            sp_match = re.search(r'\[SRC:SupplierPayment:(\d+)\]', note_txt, flags=re.IGNORECASE)
            if pay_match:
                source_type, source_id = 'Payment', int(pay_match.group(1))
            elif sp_match:
                source_type, source_id = 'SupplierPayment', int(sp_match.group(1))

        if source_type == 'Payment' and source_id:
            from app.services.payments_crud import delete_client_payment
            payment = db.session.get(Payment, source_id)
            if not payment or not delete_client_payment(payment, actor=current_user):
                raise ValueError('The linked payment is already deleted or missing.')
        elif source_type == 'CashFlowEntry' and source_id:
            from app.services.cash_flow_svc import void_manual_cash_flow_entry
            from models import CashFlowEntry
            entry = db.session.get(CashFlowEntry, source_id)
            if not entry:
                raise ValueError('The linked Cash Flow entry is missing.')
            void_manual_cash_flow_entry(entry, reason=request.form.get('void_reason'), actor=current_user)
        elif source_type == 'SupplierPayment' and source_id:
            from app.services.payments_crud import delete_supplier_payment
            payment = db.session.get(SupplierPayment, source_id)
            if not payment or not delete_supplier_payment(payment, actor=current_user):
                raise ValueError('The linked supplier payment is already deleted or missing.')
        else:
            from app.services.accounting import _void_account_tx
            from app.services.payments_crud import _assert_period_open
            from utils.accounting_audit import record_accounting_audit
            if tx.is_void:
                raise ValueError('This account entry is already deleted.')
            for account_id in {tx.from_account_id, tx.to_account_id}:
                _assert_period_open(account_id, tx.date_posted, operation='deleted')
            before = {'id': tx.id, 'type': tx.transaction_type, 'amount': tx.amount,
                      'from_account_id': tx.from_account_id, 'to_account_id': tx.to_account_id,
                      'description': tx.description, 'note': tx.note, 'is_void': False}
            _void_account_tx(tx)
            tx.voided_by = getattr(current_user, 'username', None)
            tx.voided_at = pk_now()
            record_accounting_audit(
                current_user, action='Delete', entity_type='AccountTransaction', entity_id=tx.id,
                before=before, after={**before, 'is_void': True},
                amount_before=tx.amount, amount_after=0,
                account_before_id=tx.from_account_id or tx.to_account_id,
                reason=tx.note,
            )
        _audit_pending_account_transactions()
        db.session.commit()
        flash('Account entry reversed. The original record and audit history were retained.', 'success')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logger.exception('Delete account transaction failed')
        flash(f'Unable to delete transaction: {exc}', 'danger')
    return redirect(request.referrer or url_for('accounts.dashboard'))


@accounts_bp.route('/transactions/<int:tx_id>/restore', methods=['POST'])
@login_required
def restore_account_transaction(tx_id):
    """Restore a legacy Cash Flow entry from the Accounts transaction ledger."""
    if not _void_permission_ok():
        flash('You need the "Void / Delete / Restore Transactions" permission to restore account entries.', 'danger')
        return redirect(request.referrer or url_for('accounts.dashboard'))
    tx = AccountTransaction.query.get_or_404(tx_id)
    try:
        if (tx.source_type or '') != 'CashFlowEntry' or not tx.source_id:
            raise ValueError('Only a linked legacy Cash Flow entry can be restored here.')
        from app.services.cash_flow_svc import restore_manual_cash_flow_entry
        from models import CashFlowEntry
        entry = db.session.get(CashFlowEntry, tx.source_id)
        if not entry:
            raise ValueError('The linked Cash Flow entry is missing.')
        restore_manual_cash_flow_entry(entry, actor=current_user)
        db.session.commit()
        flash('Cash Flow transaction restored from Accounts.', 'success')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logger.exception('Restore account transaction failed')
        flash(f'Unable to restore transaction: {exc}', 'danger')
    return redirect(request.referrer or url_for('accounts.dashboard'))


@accounts_bp.route('/transactions/<int:tx_id>/edit', methods=['POST'])
@login_required
def edit_account_transaction(tx_id):
    tx = AccountTransaction.query.get_or_404(tx_id)
    try:
        if tx.reconciliation_id or tx.source_type in ('Payment', 'SupplierPayment', 'AccountReconciliation') or re.search(r'\[SRC:(?:Payment|ClientRefund|SupplierPayment):\d+\]', tx.note or '', re.IGNORECASE):
            raise ValueError('Linked financial entries must be edited from their shared source form; reconciliation entries are immutable.')
        from app.services.payments_crud import _assert_period_open
        for account_id in {tx.from_account_id, tx.to_account_id}:
            _assert_period_open(account_id, tx.date_posted, operation='edited')
        new_amount = _money_round(request.form.get('amount', tx.amount) or 0)
        if new_amount <= 0:
            raise ValueError('Amount must be greater than zero.')
        new_desc = (request.form.get('description') or tx.description or '').strip()
        new_note = (request.form.get('note') or tx.note or '').strip()
        date_raw = (request.form.get('date_posted') or '').strip()
        if date_raw:
            try:
                new_dt = datetime.strptime(date_raw, '%Y-%m-%dT%H:%M')
            except ValueError:
                new_dt = tx.date_posted
        else:
            new_dt = tx.date_posted

        if (tx.source_type or '') == 'CashFlowEntry' and tx.source_id:
            from app.services.cash_flow_svc import update_manual_cash_flow_entry
            from models import CashFlowEntry
            from app.services.payments_crud import _assert_period_open
            from utils.accounting_audit import record_accounting_audit
            entry = db.session.get(CashFlowEntry, tx.source_id)
            if not entry:
                raise ValueError('The linked Cash Flow entry is missing.')
            from_id = request.form.get('from_account_id', type=int) or tx.from_account_id
            to_id = request.form.get('to_account_id', type=int) or tx.to_account_id
            if entry.direction == 'in':
                account_id, destination_id = to_id, None
            elif entry.direction == 'out':
                account_id, destination_id = from_id, None
            else:
                account_id, destination_id = from_id, to_id
            for account_id_to_check in {account_id, destination_id}:
                if account_id_to_check:
                    _assert_period_open(account_id_to_check, new_dt, operation='edited')
            before = {'id': tx.id, 'amount': tx.amount, 'description': tx.description,
                      'note': tx.note, 'date_posted': tx.date_posted,
                      'from_account_id': tx.from_account_id, 'to_account_id': tx.to_account_id}
            update_manual_cash_flow_entry(
                entry,
                direction=entry.direction,
                amount=new_amount,
                account_id=account_id,
                destination_account_id=destination_id,
                category_id=entry.category_id,
                subcategory_id=entry.subcategory_id,
                party_id=entry.party_id,
                party_name=entry.party_name,
                party_type=entry.party_type,
                description=new_desc,
                note=new_note,
                reference=entry.reference,
                date_posted=new_dt,
                reason=new_note,
                actor=current_user,
            )
            after = {'id': tx.id, 'amount': tx.amount, 'description': tx.description,
                     'note': tx.note, 'date_posted': tx.date_posted,
                     'from_account_id': tx.from_account_id, 'to_account_id': tx.to_account_id}
            record_accounting_audit(
                current_user, action='Edit', entity_type='AccountTransaction', entity_id=tx.id,
                before=before, after=after, amount_before=before['amount'], amount_after=after['amount'],
                account_before_id=before['from_account_id'] or before['to_account_id'],
                account_after_id=after['from_account_id'] or after['to_account_id'], reason=new_note,
            )
            db.session.commit()
            flash('Cash Flow entry updated from Accounts.', 'success')
            return redirect(request.referrer or url_for('accounts.dashboard'))

        from app.services.accounting import _apply_account_tx_effect, _reverse_account_tx_effect
        from utils.accounting_audit import record_accounting_audit
        from utils.money import to_minor
        before = {'id': tx.id, 'amount': tx.amount, 'description': tx.description, 'note': tx.note,
                  'date_posted': tx.date_posted, 'from_account_id': tx.from_account_id,
                  'to_account_id': tx.to_account_id}
        if not tx.is_void:
            _reverse_account_tx_effect(tx)
        tx.amount = new_amount
        tx.amount_minor = to_minor(new_amount)
        tx.description = new_desc
        tx.note = new_note
        tx.date_posted = new_dt
        tx.is_void = False
        from_id = request.form.get('from_account_id', type=int)
        to_id = request.form.get('to_account_id', type=int)
        if from_id:
            tx.from_account_id = from_id
        if to_id:
            tx.to_account_id = to_id
        _apply_account_tx_effect(tx)
        after = {'id': tx.id, 'amount': tx.amount, 'description': tx.description, 'note': tx.note,
                 'date_posted': tx.date_posted, 'from_account_id': tx.from_account_id,
                 'to_account_id': tx.to_account_id}
        record_accounting_audit(
            current_user, action='Edit', entity_type='AccountTransaction', entity_id=tx.id,
            before=before, after=after, amount_before=before['amount'], amount_after=after['amount'],
            account_before_id=before['from_account_id'] or before['to_account_id'],
            account_after_id=after['from_account_id'] or after['to_account_id'], reason=new_note,
        )
        _audit_pending_account_transactions()
        db.session.commit()
        flash('Account entry updated.', 'success')
    except Exception as exc:
        db.session.rollback()
        logger.exception('Edit account transaction failed')
        flash(f'Unable to edit transaction: {exc}', 'danger')
    return redirect(request.referrer or url_for('accounts.dashboard'))


def _client_dues_payload(client):
    """One client's live receivable, projected by the authoritative ledger.

    Returns ``balance: None`` when the projection itself fails so the form can
    say "unavailable" instead of quietly showing a wrong (or zero) payable.
    """
    from app.services.finance_clients import _client_balance_as_of
    from app.services.time_money import pk_now

    balance = None
    try:
        balance = round(float(_client_balance_as_of(client) or 0), 2)
    except Exception:
        logger.exception('Client dues lookup failed for %s', getattr(client, 'code', ''))
    payload = {
        'found': True,
        'kind': 'client',
        'code': (client.code or '').strip(),
        'name': (client.name or '').strip(),
        'phone': (client.phone or '').strip(),
        'is_active': client.is_active is not False,
        'balance': balance,
        'generated_at': pk_now().strftime('%Y-%m-%d %H:%M'),
    }
    if balance is None:
        payload.update({'payable': None, 'advance': None, 'status': 'unavailable'})
    else:
        payload.update({
            'payable': round(max(0.0, balance), 2),
            'advance': round(max(0.0, -balance), 2),
            'status': 'payable' if balance > 0.004 else ('advance' if balance < -0.004 else 'settled'),
        })
    return payload


@accounts_bp.route('/transactions/client-dues')
@login_required
def transaction_client_dues():
    """JSON payable snapshot behind the New Transaction client picker.

    Read-only and deliberately narrow: it answers "what does this one client
    owe right now?" so the receive form can show the payable under the picked
    client without projecting every client ledger on page load.
    """
    from .workspace import _require_view

    _require_view()
    picked = (request.args.get('client_code') or request.args.get('q') or '').strip()
    payload = {'found': False}
    if picked:
        from app.services.payments_crud import _resolve_client
        client = _resolve_client(picked, picked)
        if client:
            payload = _client_dues_payload(client)
    response = jsonify(payload)
    # A stale payable is worse than no payable: never let a proxy cache it.
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@accounts_bp.route('/transactions/new', methods=['GET', 'POST'])
@login_required
def add_transaction():
    from .workspace import render_workspace, _require_post_transactions

    if request.method == 'GET':
        return render_workspace('new-transaction')

    # The workspace has a small, validated entry adapter. Keep the legacy
    # party-ledger / driver / loan payloads below for older integrations.
    if request.form.get('entry_source') == 'accounts_workspace':
        _require_post_transactions()
        from app.services.account_transactions import save_workspace_transaction
        try:
            from app.services.account_transactions import success_message
            _record, created = save_workspace_transaction(request.form, actor=current_user)
            db.session.commit()
            flash(success_message(request.form, created=created), 'success')
            return redirect(url_for('accounts.add_transaction'))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
            return render_workspace('new-transaction', form_values=request.form.to_dict(), status=422)
        except Exception:
            db.session.rollback()
            logger.exception('Workspace transaction save failed')
            flash('The transaction could not be saved. No changes were applied. Please try again.', 'danger')
            return render_workspace('new-transaction', form_values=request.form.to_dict(), status=500)

    _ensure_default_account_categories()
    _backfill_legacy_account_groups()
    tx_mode = (request.form.get('tx_mode') or '').strip().lower()
    note = (request.form.get('note') or '').strip()
    method = (request.form.get('method') or 'Cash').strip()
    tx_date_raw = (request.form.get('date_posted') or '').strip()
    tx_date = pk_now()
    if tx_date_raw:
        try:
            tx_date = datetime.strptime(tx_date_raw, '%Y-%m-%dT%H:%M')
        except ValueError:
            tx_date = pk_now()

    try:
        if tx_mode == 'receive':
            receive_account_id = request.form.get('receive_account_id', type=int)
            receive_from_category = (request.form.get('receive_from_category') or 'client_ledger').strip()
            if receive_from_category == 'account_category':
                receive_from_category = (request.form.get('receive_account_group') or '').strip()
            client_input = (request.form.get('client_input') or '').strip()
            receive_from_account_id = request.form.get('receive_from_account_id', type=int)
            receive_source_label = (request.form.get('receive_source_label') or '').strip()
            receive_category = (request.form.get('receive_category') or request.form.get('tx_category') or '').strip()
            receive_subcategory = (request.form.get('receive_subcategory') or request.form.get('tx_subcategory') or '').strip()
            from .transaction_tree import validate_transaction_labels
            validate_transaction_labels(receive_category, receive_subcategory, 'in')
            receive_reference = (request.form.get('receive_reference') or request.form.get('expense_reference') or '').strip()
            amount = float(request.form.get('amount', 0) or 0)
            discount = float(request.form.get('discount', 0) or 0)

            if amount < 0:
                raise ValueError('Received amount cannot be negative.')
            if discount < 0:
                raise ValueError('Discount cannot be negative.')
            if (amount + discount) <= 0:
                raise ValueError('Received amount and discount cannot both be zero.')

            from utils.money import to_minor as _to_minor
            from app.services.accounting import _apply_account_tx_effect as _apply_effect

            receive_account = Account.query.get(receive_account_id) if receive_account_id else None
            if amount > 0:
                if not _is_account_active(receive_account):
                    raise ValueError('Please select a valid destination account.')
                _validate_account_matches_method(receive_account, method, 'Destination')

            if receive_from_category == 'client_ledger':
                client = _resolve_client(client_input, active_only=True)
                if not client:
                    raise ValueError('Client not found or suspended. Please select a valid client from the dues list.')

                from app.services.payments_crud import save_client_payment
                payment, _ = save_client_payment(
                    client_code=client.code, client_name=client.name,
                    amount=amount, discount=discount,
                    discount_reason=('Accounts receive transaction' if discount > 0 else ''),
                    payment_type=('Receipt' if amount > 0 else 'Waive-Off'),
                    method=method or 'Cash',
                    payment_account_id=(receive_account.id if receive_account else None),
                    date_posted=tx_date, note=note, actor=current_user,
                )
                account_label = receive_account.name if receive_account else 'discount-only'
                audit_log(current_user, 'account.transaction.receive', f'source_category=client_ledger, client={client.name}, account={account_label}, amount={amount}, discount={discount}')

            elif receive_from_category == 'other_source':
                if amount <= 0:
                    raise ValueError('Received amount must be greater than zero.')
                if not receive_source_label:
                    raise ValueError('Please enter who or what this money was received from.')
                if receive_category:
                    from .transaction_tree import is_valid_transaction_category
                    if not is_valid_transaction_category(receive_category, direction='in'):
                        raise ValueError('Select a valid receive category.')

                account_tx = AccountTransaction(
                    from_account_id=None,
                    to_account_id=receive_account.id,
                    amount=amount,
                    amount_minor=_to_minor(amount),
                    description=f"Money received from {receive_source_label}",
                    note=note,
                    transaction_type='Receipt',
                    category=(receive_category or None),
                    subcategory=(receive_subcategory or None),
                    payee=receive_source_label,
                    reference_no=(receive_reference or None),
                    payment_method=(method or 'Cash'),
                    date_posted=tx_date,
                    created_by=getattr(current_user, 'username', None),
                )
                # Post the effect through the single authoritative applier so
                # `balance` and the minor-unit baseline (`balance_minor`) can
                # never drift apart (legacy code bumped the float only, which
                # the next minor-unit write silently discarded).
                db.session.add(account_tx)
                db.session.flush()
                _apply_effect(account_tx)
                audit_log(current_user, 'account.transaction.receive', f'source_category=other_source, source={receive_source_label}, to={receive_account.name}, amount={amount}')

            else:
                from_account = Account.query.get(receive_from_account_id) if receive_from_account_id else None
                if not receive_from_category and from_account:
                    receive_from_category = (from_account.source_category or '').strip()
                category_exists = AccountCategory.query.filter(
                    func.lower(func.trim(AccountCategory.name)) == receive_from_category.lower(),
                    AccountCategory.is_active == True
                ).first()
                if not category_exists:
                    raise ValueError('Please select a valid receive source category.')
                if amount <= 0:
                    raise ValueError('Received amount must be greater than zero.')

                if not _is_account_active(from_account):
                    raise ValueError('Please select a valid source account.')
                if (from_account.source_category or '').strip().lower() != category_exists.name.lower():
                    raise ValueError('Selected source account does not belong to the chosen category.')
                if from_account.id == receive_account.id:
                    raise ValueError('Source and destination accounts cannot be the same.')
                # Allow loan accounts to go negative: do not enforce insufficient-balance check for Loan group
                is_loan_source = (
                    ((from_account.source_category or '').strip().lower() == 'loan')
                    or ((category_exists.name or '').strip().lower() == 'loan')
                    or ((from_account.class_subcategory or '').strip().lower() == 'loans')
                )
                if not is_loan_source and float(from_account.balance or 0) < amount:
                    raise ValueError('Insufficient balance in selected source account.')

                # For normal accounts this subtracts the amount; for Loan accounts this will produce a MORE NEGATIVE balance
                account_tx = AccountTransaction(
                    from_account_id=from_account.id,
                    to_account_id=receive_account.id,
                    amount=amount,
                    amount_minor=_to_minor(amount),
                    description=f"Funds received from account {from_account.name}",
                    note=note,
                    transaction_type='Transfer',
                    category=(receive_category or category_exists.name or None),
                    subcategory=(receive_subcategory or getattr(from_account, 'class_subcategory', None) or None),
                    reference_no=(receive_reference or None),
                    payment_method=(method or 'Cash'),
                    date_posted=tx_date,
                    created_by=getattr(current_user, 'username', None),
                )
                db.session.add(account_tx)
                db.session.flush()
                _apply_effect(account_tx)
                audit_log(current_user, 'account.transaction.receive', f'source_category={category_exists.name}, from={from_account.name}, to={receive_account.name}, amount={amount}')

            _audit_pending_account_transactions()
            db.session.commit()
            flash('Receive transaction recorded successfully.', 'success')

        elif tx_mode == 'pay':
            from_account_id = request.form.get('pay_from_account_id', type=int)
            to_account_id = request.form.get('pay_to_account_id', type=int)
            pay_target = (request.form.get('pay_target') or '').strip().lower()
            amount = float(request.form.get('amount', 0) or 0)

            if amount <= 0:
                raise ValueError('Payment amount must be greater than zero.')

            from utils.money import to_minor as _to_minor
            from app.services.accounting import _apply_account_tx_effect as _apply_effect

            # Structured detail for expenses and other pay-mode entries: what
            # it was for (category + subcategory), who was paid, the voucher/slip
            # number and an optional proof attachment — captured once, shown everywhere after.
            expense_category = (request.form.get('expense_category') or request.form.get('tx_category') or '').strip()
            expense_subcategory = (request.form.get('expense_subcategory') or request.form.get('tx_subcategory') or '').strip()
            from .transaction_tree import validate_transaction_labels
            validate_transaction_labels(expense_category, expense_subcategory, 'out')
            expense_payee = (request.form.get('payee') or '').strip()
            expense_reference = (request.form.get('expense_reference') or '').strip()
            expense_proof = None
            if pay_target in ('personal_expense', 'other_expense'):
                try:
                    from app.services.files_pdf import save_photo
                    expense_proof = save_photo(request.files.get('expense_proof'))
                except Exception:
                    expense_proof = None
                if expense_category:
                    from models import CashFlowCategory
                    from .transaction_tree import is_valid_transaction_category
                    cat_row = CashFlowCategory.query.filter(
                        func.lower(func.trim(CashFlowCategory.name)) == expense_category.lower(),
                        CashFlowCategory.is_active == True,
                        CashFlowCategory.direction.in_(['out', 'both']),
                    ).first()
                    if not cat_row and not is_valid_transaction_category(expense_category, direction='out'):
                        raise ValueError('Select a valid expense category.')

            from_account = Account.query.get(from_account_id) if from_account_id else None
            if not _is_account_active(from_account):
                raise ValueError('Please select a valid source account.')
            _validate_account_matches_method(from_account, method, 'Source')
            if float(from_account.balance or 0) < amount:
                raise ValueError('Insufficient balance in selected source account.')

            if pay_target == 'company_transfer':
                to_account = Account.query.get(to_account_id) if to_account_id else None
                if not _is_account_active(to_account):
                    raise ValueError('Please select a valid destination account.')
                if to_account.id == from_account.id:
                    raise ValueError('Source and destination accounts cannot be the same.')

                tx = AccountTransaction(
                    from_account_id=from_account.id,
                    to_account_id=to_account.id,
                    amount=amount,
                    amount_minor=_to_minor(amount),
                    description='Intra-company transfer',
                    note=note,
                    transaction_type='Transfer',
                    payment_method=(method or 'Cash'),
                    date_posted=tx_date,
                    created_by=getattr(current_user, 'username', None),
                )
                db.session.add(tx)
                db.session.flush()
                _apply_effect(tx)
                audit_log(current_user, 'account.transaction.transfer', f'from={from_account.name}, to={to_account.name}, amount={amount}')
                flash('Transfer transaction recorded successfully.', 'success')

            elif pay_target == 'supplier':
                supplier_id = request.form.get('supplier_id', type=int)
                supplier_input = (request.form.get('supplier_input') or '').strip()
                supplier = Supplier.query.get(supplier_id) if supplier_id else None
                if not supplier and supplier_input:
                    supplier = _resolve_supplier(supplier_input)
                if not supplier:
                    raise ValueError('Please select a valid supplier.')

                if not supplier.is_active:
                    raise ValueError('The selected supplier is suspended.')
                from app.services.payments_crud import save_supplier_payment
                sp, _ = save_supplier_payment(
                    supplier_id=supplier.id, amount=amount, method=method or 'Cash',
                    payment_account_id=from_account.id, date_posted=tx_date,
                    note=note, actor=current_user,
                )
                audit_log(current_user, 'account.transaction.supplier_payment', f'from={from_account.name}, supplier={supplier.name}, amount={amount}')
                flash('Supplier payment recorded successfully.', 'success')
            elif pay_target == 'driver':
                # Accounts-section entry point for a driver service payment.
                # Delegates to the same core as the Driver section, so exactly
                # one financial transaction exists and it is automatically
                # visible in the driver ledger.
                driver_id = request.form.get('delivery_person_id', type=int)
                driver_input = (request.form.get('driver_input') or '').strip()
                driver = db.session.get(DeliveryPerson, driver_id) if driver_id else None
                if not driver and driver_input:
                    driver = DeliveryPerson.query.filter(
                        func.lower(func.trim(DeliveryPerson.name)) == driver_input.lower()
                    ).first()
                if not driver:
                    raise ValueError('Please select a valid delivery person / driver.')
                if not driver.is_active:
                    raise ValueError('The selected delivery person is inactive.')

                from app.services.driver_payments import settle_driver_fifo
                rows = settle_driver_fifo(
                    delivery_person_id=driver.id, amount_paid=amount,
                    method=method or 'Cash', payment_account_id=from_account.id,
                    reference=(request.form.get('reference') or '').strip(),
                    date_posted=tx_date, note=note,
                    idempotency_key=(request.form.get('idempotency_key') or '').strip() or None,
                    actor=current_user,
                )
                audit_log(current_user, 'account.transaction.driver_payment',
                          f'from={from_account.name}, driver={driver.name}, amount={amount}, rows={len(rows)}')
                flash('Driver service payment recorded. The driver ledger and the account balance were updated together.', 'success')
            elif pay_target == 'client_refund':
                # Refund issued to a client: record refund audit (Payment negative for client ledger),
                # create account transaction for cash out so cash flow reflects it, and keep everything atomic.
                client_id = request.form.get('client_id_refund', type=int)
                client_input = (request.form.get('client_input_refund') or '').strip()
                client = Client.query.get(client_id) if client_id else None
                if not client and client_input:
                    client = _resolve_client(client_input, active_only=True)
                if not client:
                    raise ValueError('Please select a valid client.')

                if not client.is_active:
                    raise ValueError('The selected client is suspended.')
                from app.services.payments_crud import save_client_payment
                payment, _ = save_client_payment(
                    client_code=client.code, client_name=client.name,
                    amount=amount, discount=0, payment_type='Refund',
                    method=method or 'Cash', payment_account_id=from_account.id,
                    date_posted=tx_date, note=note, actor=current_user,
                )
                audit_log(current_user, 'account.transaction.client_refund', f'from={from_account.name}, client={client.name}, amount={amount}')
                flash('Client refund recorded successfully.', 'success')
            elif pay_target == 'loan':
                # Repayment to a Loan account: credit the loan account (moves negative toward zero)
                to_account = Account.query.get(to_account_id) if to_account_id else None
                if not _is_account_active(to_account):
                    raise ValueError('Please select a valid loan account.')
                is_loan_dest = (
                    (to_account.source_category or '').strip().lower() == 'loan'
                    or (to_account.class_subcategory or '').strip().lower() == 'loans'
                    or (to_account.type or '').strip().lower() == 'loan'
                )
                if not is_loan_dest:
                    raise ValueError('Selected destination is not a Loan account.')
                if to_account.id == from_account.id:
                    raise ValueError('Source and destination accounts cannot be the same.')

                tx = AccountTransaction(
                    from_account_id=from_account.id,
                    to_account_id=to_account.id,
                    amount=amount,
                    amount_minor=_to_minor(amount),
                    description=f'Loan repayment to {to_account.name}',
                    note=note,
                    transaction_type='Payment',
                    category=(expense_category or None),
                    subcategory=(expense_subcategory or None),
                    payee=to_account.name,
                    reference_no=(expense_reference or None),
                    payment_method=(method or 'Cash'),
                    date_posted=tx_date,
                    created_by=getattr(current_user, 'username', None),
                )
                db.session.add(tx)
                db.session.flush()
                _apply_effect(tx)

                audit_log(current_user, 'account.transaction.loan_payment', f'from={from_account.name}, to_loan={to_account.name}, amount={amount}')
                flash('Loan payment recorded successfully.', 'success')

            else:
                target_label = (request.form.get('target_label') or '').strip()
                if not target_label:
                    if pay_target == 'loan':
                        target_label = 'Loan Payment'
                    elif pay_target == 'personal_expense':
                        target_label = expense_subcategory or expense_category or 'Personal Expense'
                    else:
                        target_label = expense_subcategory or expense_category or 'Other Payment'

                tx_type = 'Expense' if pay_target in ['personal_expense', 'other_expense'] else 'Payment'
                tx = AccountTransaction(
                    from_account_id=from_account.id,
                    to_account_id=None,
                    amount=amount,
                    amount_minor=_to_minor(amount),
                    description=target_label,
                    note=note,
                    transaction_type=tx_type,
                    category=(expense_category or None),
                    subcategory=(expense_subcategory or None),
                    payee=(expense_payee or None),
                    reference_no=(expense_reference or None),
                    payment_method=(method or 'Cash'),
                    photo_path=expense_proof,
                    date_posted=tx_date,
                    created_by=getattr(current_user, 'username', None),
                )
                db.session.add(tx)
                db.session.flush()
                _apply_effect(tx)
                audit_log(current_user, 'account.transaction.pay', f'from={from_account.name}, target={target_label}, amount={amount}')
                flash('Outgoing payment recorded successfully.', 'success')

            _audit_pending_account_transactions()
            db.session.commit()

        elif tx_mode == 'transfer':
            from utils.money import to_minor as _to_minor
            from app.services.accounting import _apply_account_tx_effect as _apply_effect

            from_account_id = (
                request.form.get('transfer_from_account_id', type=int)
                or request.form.get('pay_from_account_id', type=int)
            )
            to_account_id = (
                request.form.get('transfer_to_account_id', type=int)
                or request.form.get('pay_to_account_id', type=int)
            )
            amount = float(request.form.get('amount', 0) or 0)
            if amount <= 0:
                raise ValueError('Transfer amount must be greater than zero.')

            from_account = Account.query.get(from_account_id) if from_account_id else None
            to_account = Account.query.get(to_account_id) if to_account_id else None
            if not _is_account_active(from_account):
                raise ValueError('Please select a valid source account.')
            if not _is_account_active(to_account):
                raise ValueError('Please select a valid destination account.')
            if from_account.id == to_account.id:
                raise ValueError('Source and destination accounts cannot be the same.')
            if float(from_account.balance or 0) < amount:
                raise ValueError('Insufficient balance in selected source account.')

            transfer_cat = (request.form.get('tx_category') or request.form.get('expense_category') or '').strip()
            transfer_subcat = (request.form.get('tx_subcategory') or request.form.get('expense_subcategory') or '').strip()
            from .transaction_tree import validate_transaction_labels
            validate_transaction_labels(transfer_cat, transfer_subcat)
            transfer_ref = (request.form.get('expense_reference') or request.form.get('reference_no') or '').strip()
            transfer_desc = (request.form.get('target_label') or request.form.get('description') or 'Intra-company transfer').strip()

            tx = AccountTransaction(
                from_account_id=from_account.id,
                to_account_id=to_account.id,
                amount=amount,
                amount_minor=_to_minor(amount),
                description=transfer_desc,
                note=note,
                transaction_type='Transfer',
                category=(transfer_cat or 'Internal Transfer'),
                subcategory=(transfer_subcat or None),
                reference_no=(transfer_ref or None),
                payment_method=(method or 'Cash'),
                date_posted=tx_date,
                created_by=getattr(current_user, 'username', None),
            )
            db.session.add(tx)
            db.session.flush()
            _apply_effect(tx)
            _audit_pending_account_transactions()
            db.session.commit()
            audit_log(current_user, 'account.transaction.transfer', f'from={from_account.name}, to={to_account.name}, amount={amount}')
            flash('Transfer transaction recorded successfully.', 'success')

        else:
            raise ValueError('Invalid transaction type selected.')

    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception as exc:
        db.session.rollback()
        logger.exception('Accounts transaction save failed')
        flash(f'Unable to save transaction: {exc}', 'danger')

    return redirect(url_for('accounts.dashboard'))


@accounts_bp.route('/transfers/add', methods=['GET', 'POST'])
@login_required
def add_transfer():
    """Add a new account transfer."""
    if request.method == 'POST':
        try:
            from utils.money import from_minor, to_minor
            from app.services.accounting import _apply_account_tx_effect
            from_account_id = request.form.get('from_account', type=int)
            to_account_id = request.form.get('to_account', type=int)
            amount_minor = to_minor(request.form.get('amount', 0), field='Transfer amount')
            if amount_minor <= 0:
                raise ValueError('Transfer amount must be greater than zero.')
            from_account = db.session.get(Account, from_account_id) if from_account_id else None
            to_account = db.session.get(Account, to_account_id) if to_account_id else None
            if not _is_account_active(from_account) or not _is_account_active(to_account):
                raise ValueError('Select valid active source and destination accounts.')
            if from_account.id == to_account.id:
                raise ValueError('Source and destination accounts cannot be the same.')
            available_minor = int(from_account.balance_minor) if from_account.balance_minor is not None else to_minor(from_account.balance or 0)
            if available_minor < amount_minor:
                raise ValueError('Insufficient balance in the source account.')
            transaction = AccountTransaction(
                from_account_id=from_account.id, to_account_id=to_account.id,
                amount=float(from_minor(amount_minor)), amount_minor=amount_minor,
                description=(request.form.get('description') or 'Account transfer').strip(),
                note=(request.form.get('note') or '').strip(),
                transaction_type='Transfer', created_by=getattr(current_user, 'username', None),
                date_posted=pk_now(),
            )
            db.session.add(transaction)
            db.session.flush()
            _apply_account_tx_effect(transaction)
            _audit_pending_account_transactions()
            db.session.commit()
            flash('Transfer completed successfully!', 'success')
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'danger')
        except Exception as exc:
            db.session.rollback()
            logger.exception('Account transfer failed')
            flash(f'Unable to record transfer: {exc}', 'danger')
        return redirect(url_for('accounts.transfers'))
    
    accounts = _active_accounts().order_by(Account.name.asc(), Account.id.asc()).all()
    return render_template(
        'accounts/add_transfer.html', accounts=accounts,
        account_options=[{'id': a.id, 'label': _account_option_label(a)} for a in accounts],
        account_data={str(a.id): {'name': a.name, 'balance': _money_round(a.balance)} for a in accounts},
    )


