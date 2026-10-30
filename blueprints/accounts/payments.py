"""payments — split from accounts.py."""
from ._common import *  # noqa

def _payment_page_size():
    try:
        raw = request.args.get('per_page', 50, type=int)
        if raw is None:
            raw = 50
        return min(max(int(raw), 10), 100)
    except Exception:
        return 50


def _normalize_method_filter(method_raw):
    if not method_raw:
        return []
    m = method_raw.strip().lower()
    if not m:
        return []
    if m == 'bank':
        return ['bank', 'bank transfer']
    if m in ('check', 'cheque'):
        return ['check', 'cheque']
    return [m]


def _payment_filter_state():
    q = (request.args.get('q') or '').strip()
    method = (request.args.get('method') or '').strip()
    show = (request.args.get('show') or 'active').strip().lower()
    payment_type = (request.args.get('payment_type') or '').strip()
    try:
        party_raw = request.args.get('party_id')
        party_id = int(party_raw) if party_raw not in (None, '', '0') else None
        if party_id == 0:
            party_id = None
    except Exception:
        party_id = None
    try:
        acc_raw = request.args.get('account_id')
        account_id = int(acc_raw) if acc_raw not in (None, '', '0') else None
        if account_id == 0:
            account_id = None
    except Exception:
        account_id = None

    return {
        'q': q,
        'method': method,
        'method_values': _normalize_method_filter(method),
        'payment_type': payment_type,
        'show': show,
        'party_id': party_id,
        'account_id': account_id,
        'per_page': _payment_page_size(),
    }


@accounts_bp.route('/payments/clients')
@login_required
def client_payments():
    """Paginated, intersecting filters and full CRUD for client payments."""
    page = max(request.args.get('page', 1, type=int) or 1, 1)
    state = _payment_filter_state()
    show_mode = state['show'] if state['show'] in ('active', 'voided', 'all') else 'active'
    date_from, date_to_excl = _parse_date_range()

    q = Payment.query.filter(Payment.date_posted >= date_from, Payment.date_posted < date_to_excl)
    if show_mode == 'active':
        q = q.filter(Payment.is_void == False)
    elif show_mode == 'voided':
        q = q.filter(Payment.is_void == True)

    if state['q']:
        like = f"%{state['q']}%"
        q = q.outerjoin(Client, Payment.client_id == Client.id).filter(or_(
            Payment.client_name.ilike(like),
            Client.code.ilike(like),
            Client.name.ilike(like),
            Payment.note.ilike(like),
            Payment.account_name.ilike(like),
            Payment.bank_name.ilike(like),
            Payment.account_no.ilike(like),
            Payment.manual_bill_no.ilike(like),
            Payment.auto_bill_no.ilike(like),
            Payment.method.ilike(like),
            Payment.discount_reason.ilike(like),
        ))

    if state['method_values']:
        q = q.filter(func.lower(Payment.method).in_(state['method_values']))

    if state['payment_type']:
        pt = state['payment_type'].strip().lower()
        if pt in ('receipt', 'payment received'):
            q = q.filter(Payment.amount >= 0, func.lower(func.coalesce(Payment.payment_type, '')) != 'refund')
        elif pt == 'refund':
            q = q.filter(or_(func.lower(Payment.payment_type) == 'refund', Payment.amount < 0))
        elif pt in ('waive-off', 'waive off', 'waive'):
            q = q.filter(or_(func.lower(Payment.payment_type).like('%waive%'), Payment.discount > 0))

    if state['party_id']:
        selected_client = db.session.get(Client, state['party_id'])
        if selected_client:
            q = q.filter(or_(
                Payment.client_id == selected_client.id,
                and_(
                    Payment.client_id.is_(None),
                    func.lower(func.trim(Payment.client_name)) == selected_client.name.strip().lower()
                )
            ))
        else:
            q = q.filter(False)

    if state['account_id']:
        q = q.filter(Payment.payment_account_id == state['account_id'])

    total_amount = q.with_entities(func.coalesce(func.sum(Payment.amount), 0)).scalar() or 0
    total_count = q.with_entities(func.count(func.distinct(Payment.id))).scalar() or 0

    ordered_q = q.order_by(Payment.date_posted.desc(), Payment.id.desc())
    payments = ordered_q.paginate(page=page, per_page=state['per_page'], error_out=False)
    if payments.pages and page > payments.pages:
        payments = ordered_q.paginate(page=payments.pages, per_page=state['per_page'], error_out=False)

    accounts = _active_accounts().filter(func.lower(func.trim(Account.category)).in_(['cash', 'bank'])).order_by(Account.name.asc(), Account.id.asc()).all()
    clients = _active_clients()
    all_clients = Client.query.order_by(Client.name.asc(), Client.id.asc()).all()
    all_accounts = Account.query.order_by(Account.name.asc(), Account.id.asc()).all()
    client_options = [{'code': c.code, 'name': c.name} for c in clients]
    account_options = [{'id': a.id, 'label': _account_option_label(a)} for a in accounts]
    payment_meta = {}
    from app.services.payments_crud import _client_payment_kind, _client_payment_source
    for p in payments.items:
        source_type, source_id = _client_payment_source(p)
        payment_meta[p.id] = {'kind': _client_payment_kind(p), 'source_type': source_type, 'source_id': source_id}
    payments_readonly = not _payments_permission_ok()

    return render_template(
        'accounts/client_payments.html', payments=payments,
        date_from=date_from, date_to=date_to_excl - timedelta(days=1),
        search=state['q'], method_f=state['method'], payment_type_f=state['payment_type'], show_mode=show_mode,
        party_id_f=state['party_id'], account_id_f=state['account_id'], per_page=state['per_page'],
        total_amount=total_amount, total_count=total_count,
        payments_readonly=payments_readonly, accounts=accounts, clients=clients,
        all_clients=all_clients, all_accounts=all_accounts,
        client_options=client_options, account_options=account_options,
        filter_party_options=[{'id': c.id, 'label': f"{c.name} ({c.code})" + ('' if c.is_active else ' [Inactive]')} for c in all_clients],
        filter_account_options=[{'id': a.id, 'label': _account_option_label(a) + ('' if a.is_active else ' [Inactive]')} for a in all_accounts],
        filter_party_name=(next((f"{c.name} ({c.code})" for c in all_clients if c.id == state['party_id']), '')),
        filter_account_name=(next((_account_option_label(a) for a in all_accounts if a.id == state['account_id']), '')),
        payment_meta=payment_meta,
    )


@accounts_bp.route('/payments/suppliers')
@login_required
def supplier_payments():
    """Paginated, intersecting filters and full CRUD for supplier payments."""
    page = max(request.args.get('page', 1, type=int) or 1, 1)
    state = _payment_filter_state()
    show_mode = state['show'] if state['show'] in ('active', 'voided', 'all') else 'active'
    date_from, date_to_excl = _parse_date_range()

    q = SupplierPayment.query.filter(SupplierPayment.date_posted >= date_from, SupplierPayment.date_posted < date_to_excl)
    if show_mode == 'active':
        q = q.filter(SupplierPayment.is_void == False)
    elif show_mode == 'voided':
        q = q.filter(SupplierPayment.is_void == True)

    if state['q']:
        like = f"%{state['q']}%"
        q = q.outerjoin(Supplier).filter(or_(
            Supplier.name.ilike(like),
            SupplierPayment.note.ilike(like),
            SupplierPayment.account_name.ilike(like),
            SupplierPayment.bank_name.ilike(like),
            SupplierPayment.account_no.ilike(like),
            SupplierPayment.manual_bill_no.ilike(like),
            SupplierPayment.auto_bill_no.ilike(like),
            SupplierPayment.method.ilike(like),
        ))

    if state['method_values']:
        q = q.filter(func.lower(SupplierPayment.method).in_(state['method_values']))

    if state['party_id']:
        q = q.filter(SupplierPayment.supplier_id == state['party_id'])

    if state['account_id']:
        q = q.filter(SupplierPayment.payment_account_id == state['account_id'])

    total_amount = q.with_entities(func.coalesce(func.sum(SupplierPayment.amount), 0)).scalar() or 0
    total_count = q.with_entities(func.count(func.distinct(SupplierPayment.id))).scalar() or 0

    ordered_q = q.order_by(SupplierPayment.date_posted.desc(), SupplierPayment.id.desc())
    payments = ordered_q.paginate(page=page, per_page=state['per_page'], error_out=False)
    if payments.pages and page > payments.pages:
        payments = ordered_q.paginate(page=payments.pages, per_page=state['per_page'], error_out=False)

    suppliers = _active_suppliers()
    accounts = _active_accounts().filter(func.lower(func.trim(Account.category)).in_(['cash', 'bank'])).order_by(Account.name.asc(), Account.id.asc()).all()
    all_suppliers = Supplier.query.order_by(Supplier.name.asc(), Supplier.id.asc()).all()
    all_accounts = Account.query.order_by(Account.name.asc(), Account.id.asc()).all()
    supplier_options = [{'id': s.id, 'name': s.name} for s in suppliers]
    account_options = [{'id': a.id, 'label': _account_option_label(a)} for a in accounts]
    payment_meta = {}
    from app.services.payments_crud import _supplier_payment_source
    for p in payments.items:
        source_type, source_id = _supplier_payment_source(p)
        payment_meta[p.id] = {'source_type': source_type, 'source_id': source_id}
    payments_readonly = not _payments_permission_ok()

    return render_template(
        'accounts/supplier_payments.html', payments=payments,
        date_from=date_from, date_to=date_to_excl - timedelta(days=1),
        search=state['q'], method_f=state['method'], show_mode=show_mode,
        party_id_f=state['party_id'], account_id_f=state['account_id'], per_page=state['per_page'],
        total_amount=total_amount, total_count=total_count,
        suppliers=suppliers, accounts=accounts,
        all_suppliers=all_suppliers, all_accounts=all_accounts,
        supplier_options=supplier_options, account_options=account_options,
        filter_party_options=[{'id': s.id, 'label': s.name + ('' if s.is_active else ' [Inactive]')} for s in all_suppliers],
        filter_account_options=[{'id': a.id, 'label': _account_option_label(a) + ('' if a.is_active else ' [Inactive]')} for a in all_accounts],
        filter_party_name=(next((s.name for s in all_suppliers if s.id == state['party_id']), '')),
        filter_account_name=(next((_account_option_label(a) for a in all_accounts if a.id == state['account_id']), '')),
        payments_readonly=payments_readonly, payment_meta=payment_meta,
    )


@accounts_bp.route('/payments/clients/void/<int:id>', methods=['POST'])
@login_required
def client_payment_void(id):
    if not _payments_void_ok():
        flash('You need the "Void / Delete / Restore Transactions" permission to delete payments.', 'danger')
        return redirect(request.referrer or url_for('accounts.client_payments'))

    payment = Payment.query.get_or_404(id)
    try:
        from app.services.payments_crud import delete_client_payment
        if delete_client_payment(payment, actor=current_user):
            db.session.commit()
            flash('Payment deleted. Balances reversed and recalculated.', 'success')
        else:
            flash('Payment already deleted.', 'warning')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception:
        db.session.rollback()
        logger.exception('Client payment delete failed')
        flash('Unable to delete payment: the payment could not be deleted. Please try again.', 'danger')

    return redirect(request.referrer or url_for('accounts.client_payments'))


@accounts_bp.route('/payments/clients/restore/<int:id>', methods=['POST'])
@login_required
def client_payment_restore(id):
    if not _payments_void_ok():
        flash('You need the "Void / Delete / Restore Transactions" permission to restore payments.', 'danger')
        return redirect(request.referrer or url_for('accounts.client_payments'))

    payment = Payment.query.get_or_404(id)
    try:
        from app.services.payments_crud import restore_client_payment
        if restore_client_payment(payment, actor=current_user):
            db.session.commit()
            flash('Payment restored. Balances re-applied.', 'success')
        else:
            flash('Payment is already active.', 'warning')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception:
        db.session.rollback()
        logger.exception('Client payment restore failed')
        flash('Unable to restore payment: the payment could not be restored. Please try again.', 'danger')

    return redirect(request.referrer or url_for('accounts.client_payments'))


@accounts_bp.route('/expenditures')
@login_required
def expenditures():
    """View and manage personal expenditures."""
    page = request.args.get('page', 1, type=int)
    per_page = 50
    search = (request.args.get('q') or '').strip()
    category_f = (request.args.get('category') or '').strip()
    subcategory_f = (request.args.get('subcategory') or '').strip()
    method_f = (request.args.get('method') or '').strip()
    date_from, date_to_excl = _parse_date_range()

    fbm_q = FbmCashDrawerEntry.query.filter(
        FbmCashDrawerEntry.entry_type == 'out',
        FbmCashDrawerEntry.is_void == False,
        FbmCashDrawerEntry.date_posted >= date_from,
        FbmCashDrawerEntry.date_posted < date_to_excl
    )
    if search:
        like = f'%{search}%'
        fbm_q = fbm_q.filter(or_(FbmCashDrawerEntry.note.ilike(like), FbmCashDrawerEntry.category.ilike(like)))
    if category_f:
        fbm_q = fbm_q.filter(FbmCashDrawerEntry.category == category_f)
    if method_f:
        fbm_q = fbm_q.filter(func.lower(FbmCashDrawerEntry.method) == method_f.lower())
    fbm_expenditures = fbm_q.all()

    tx_q = AccountTransaction.query.filter(
        AccountTransaction.date_posted >= date_from,
        AccountTransaction.date_posted < date_to_excl,
        AccountTransaction.is_void == False,
        AccountTransaction.transaction_type.in_(['Expense', 'Payment', 'Driver Payment']),
        AccountTransaction.from_account_id.isnot(None),
        # Money out to an outside party only: rows that also credit another
        # company account (loan repayments) are internal movements, tracked in
        # the account ledger and audit trail — not expenditures.
        AccountTransaction.to_account_id.is_(None)
    )
    if search:
        like = f'%{search}%'
        tx_q = tx_q.filter(or_(AccountTransaction.description.ilike(like), AccountTransaction.note.ilike(like),
                               AccountTransaction.payee.ilike(like), AccountTransaction.reference_no.ilike(like)))
    if category_f:
        tx_q = tx_q.filter(or_(AccountTransaction.category == category_f, AccountTransaction.description == category_f))
    if subcategory_f:
        # Subcategories live on the structured transaction detail; "Category /
        # Subcategory" labels come from the synced manual cash-flow row.
        tx_q = tx_q.filter(or_(AccountTransaction.subcategory == subcategory_f,
                               AccountTransaction.description == subcategory_f))
    if method_f:
        tx_q = tx_q.join(Account, AccountTransaction.from_account_id == Account.id).filter(func.lower(Account.category) == method_f.lower())
    tx_expenditures = tx_q.all()

    all_expenditures = []
    for e in fbm_expenditures:
        all_expenditures.append({
            'date_posted': e.date_posted,
            'category': e.category or '',
            'amount': float(e.amount or 0),
            'method': e.method or '',
            'note': e.note or '',
            'payee': '',
            'reference_no': '',
            'type': 'FBM Cash Drawer',
            'photo_path': '',
        })
    # Resolve accounts and synced cash-flow rows in bulk.  Doing this per row
    # was an N+1 that made the page take seconds on a busy ledger.
    account_by_id = {
        account.id: account
        for account in Account.query.filter(
            Account.id.in_({tx.from_account_id for tx in tx_expenditures if tx.from_account_id})
        ).all()
    } if tx_expenditures else {}
    cf_entry_ids = {
        tx.source_id for tx in tx_expenditures
        if (tx.source_type or '') == 'CashFlowEntry' and tx.source_id
    }
    cf_by_id = {}
    if cf_entry_ids:
        from models import CashFlowEntry
        cf_by_id = {
            entry.id: entry
            for entry in CashFlowEntry.query.filter(CashFlowEntry.id.in_(cf_entry_ids)).all()
        }
    for tx in tx_expenditures:
        acc = account_by_id.get(tx.from_account_id)
        # Manual cash-flow rows sync into the account ledger; surface their
        # richer category/subcategory instead of the plain description.
        cf_category = None
        if (tx.source_type or '') == 'CashFlowEntry' and tx.source_id:
            cf = cf_by_id.get(tx.source_id)
            if cf and cf.category:
                cf_category = cf.category.name
                if cf.subcategory:
                    cf_category = f"{cf_category} / {cf.subcategory.name}"
        category = tx.category or cf_category or (
            'Driver / Delivery Services' if tx.transaction_type == 'Driver Payment'
            else (tx.description or 'Expense'))
        all_expenditures.append({
            'date_posted': tx.date_posted,
            'category': category,
            'subcategory': (getattr(tx, 'subcategory', None) or ''),
            'amount': float(tx.amount or 0),
            'method': (tx.payment_method or (acc.category.capitalize() if acc else 'Other')),
            'note': tx.note or '',
            'payee': tx.payee or '',
            'reference_no': tx.reference_no or '',
            'type': ('Driver Payment' if tx.transaction_type == 'Driver Payment' else (tx.transaction_type or 'Expense')),
            'photo_path': tx.photo_path or '',
            'tx_id': tx.id,
        })

    all_expenditures.sort(key=lambda x: x['date_posted'], reverse=True)

    total_amount = sum(item['amount'] for item in all_expenditures)
    total_count = len(all_expenditures)

    start = (page - 1) * per_page
    end = start + per_page
    paginated_expenditures = all_expenditures[start:end]
    
    pagination_wrapper = SimpleNamespace(
        items=paginated_expenditures,
        page=page,
        per_page=per_page,
        total=total_count,
        pages=(total_count + per_page - 1) // per_page,
        has_prev=(page > 1),
        has_next=(end < total_count),
        prev_num=page - 1,
        next_num=page + 1
    )

    all_categories = set()
    for r in db.session.query(FbmCashDrawerEntry.category).filter(
        FbmCashDrawerEntry.entry_type == 'out',
        FbmCashDrawerEntry.is_void == False,
        FbmCashDrawerEntry.category.isnot(None),
        FbmCashDrawerEntry.category != ''
    ).distinct(FbmCashDrawerEntry.category).all():
        all_categories.add(r[0])
    for r in db.session.query(AccountTransaction.description).filter(
        AccountTransaction.is_void == False,
        AccountTransaction.transaction_type.in_(['Expense', 'Payment']),
        AccountTransaction.description.isnot(None),
        AccountTransaction.description != ''
    ).distinct(AccountTransaction.description).all():
        all_categories.add(r[0])
    for r in db.session.query(AccountTransaction.category).filter(
        AccountTransaction.is_void == False,
        AccountTransaction.category.isnot(None),
        AccountTransaction.category != ''
    ).distinct(AccountTransaction.category).all():
        all_categories.add(r[0])
    from models import CashFlowCategory
    for cat in CashFlowCategory.query.filter(
        CashFlowCategory.is_active == True,
        CashFlowCategory.direction.in_(['out', 'both'])
    ).all():
        all_categories.add(cat.name)
    from .transaction_tree import DEFAULT_EXPENSE_CATEGORY_TREE
    for default_cat in DEFAULT_EXPENSE_CATEGORY_TREE.keys():
        all_categories.add(default_cat)
    categories = sorted(list(all_categories))

    all_methods = set()
    for r in db.session.query(FbmCashDrawerEntry.method).filter(
        FbmCashDrawerEntry.entry_type == 'out',
        FbmCashDrawerEntry.is_void == False,
        FbmCashDrawerEntry.method.isnot(None),
        FbmCashDrawerEntry.method != ''
    ).distinct(FbmCashDrawerEntry.method).all():
        all_methods.add(r[0])
    for r in db.session.query(Account.category).filter(
        Account.is_active == True,
        Account.category.isnot(None),
        Account.category != ''
    ).distinct(Account.category).all():
        all_methods.add(r[0].capitalize())
    methods = sorted(list(all_methods))

    subcategory_options = set()
    for r in db.session.query(AccountTransaction.subcategory).filter(
        AccountTransaction.is_void == False,
        AccountTransaction.subcategory.isnot(None),
        AccountTransaction.subcategory != ''
    ).distinct(AccountTransaction.subcategory).all():
        subcategory_options.add(r[0])
    from models import CashFlowSubcategory
    for sub in CashFlowSubcategory.query.filter(CashFlowSubcategory.is_active == True).all():
        subcategory_options.add(sub.name)

    return render_template('accounts/expenditures.html', expenditures=pagination_wrapper,
                           date_from=date_from, date_to=date_to_excl - timedelta(days=1),
                           search=search, category_f=category_f, subcategory_f=subcategory_f, method_f=method_f,
                           categories=categories, methods=methods,
                           subcategory_options=sorted(subcategory_options),
                           total_amount=total_amount, total_count=total_count,
                           page=page, per_page=per_page,
                           has_prev=(page > 1), has_next=(end < total_count))


@accounts_bp.route('/receipts')
@login_required
def receipts():
    """The money-IN register for the Accounts section.

    Everything that actually moved cash/bank INTO the company in the period,
    each source shown separately with its own subtotal so the page always
    reconciles with the account ledger:

    * Client payments (``Payment`` receipts — cash/bank only)
    * Direct-sale receipts (the paid-now portion of cash sales)
    * Booking advances (paid-now on orders)
    * Other receipts (money in with no source document: owner deposits,
      refunds received, loan draws)

    GRN paid amounts are money **OUT** (paying suppliers at receiving time).
    They are listed in their own clearly-labelled section and included in the
    totals only as an outflow, so the net movement is a real in-minus-out
    figure instead of adding outgoing money to incoming.
    """
    date_from, date_to_excl = _parse_date_range(default_days=0)

    sales = DirectSale.query.filter(
        DirectSale.date_posted >= date_from,
        DirectSale.date_posted < date_to_excl,
        DirectSale.is_void == False,
        DirectSale.paid_amount > 0
    ).order_by(DirectSale.date_posted.desc()).all()

    bookings = Booking.query.filter(
        Booking.date_posted >= date_from,
        Booking.date_posted < date_to_excl,
        Booking.is_void == False,
        Booking.paid_amount > 0
    ).order_by(Booking.date_posted.desc()).all()

    # Client receipts: real money-in only.  Waive-offs carry amount 0 and
    # material returns settle goods against dues without cash, so neither is
    # a receipt; refunds are money out and are excluded here.
    client_payments = Payment.query.filter(
        Payment.date_posted >= date_from,
        Payment.date_posted < date_to_excl,
        Payment.is_void == False,
        Payment.amount > 0,
        func.lower(func.coalesce(Payment.payment_type, '')) != 'refund',
        func.lower(func.coalesce(Payment.method, '')) != 'material return',
        func.lower(func.coalesce(Payment.payment_type, '')) != 'material return'
    ).order_by(Payment.date_posted.desc()).all()

    # Other receipts: account-ledger money-in with no source document marker.
    other_receipts = AccountTransaction.query.filter(
        AccountTransaction.date_posted >= date_from,
        AccountTransaction.date_posted < date_to_excl,
        AccountTransaction.is_void == False,
        AccountTransaction.transaction_type == 'Receipt',
        AccountTransaction.to_account_id.isnot(None),
        or_(
            AccountTransaction.note.is_(None),
            ~AccountTransaction.note.ilike('%[SRC:%')
        )
    ).order_by(AccountTransaction.date_posted.desc()).all()

    grns = GRN.query.filter(
        GRN.date_posted >= date_from,
        GRN.date_posted < date_to_excl,
        GRN.is_void == False,
        GRN.paid_amount > 0
    ).order_by(GRN.date_posted.desc()).all()

    # The GRN line must show what the GRN was actually worth.  Summing only
    # the landed-cost columns (loading / freight / other / adjustment) ignored
    # every received material line, so an ordinary purchase of 100 bags showed
    # "Total Rs. 0.00" while its paid amount was 50,000.  Use the same
    # authoritative total as the GRN page, ledger and printed bill.
    from app.services.grn_svc import calculate_grn_total
    grn_totals = {grn.id: calculate_grn_total(grn) for grn in grns}

    client_receipts_total = float(sum(float(p.amount or 0) for p in client_payments))
    sales_paid_total = float(sum(float(s.paid_amount or 0) for s in sales))
    booking_paid_total = float(sum(float(b.paid_amount or 0) for b in bookings))
    other_receipts_total = float(sum(float(tx.amount or 0) for tx in other_receipts))
    total_money_in = client_receipts_total + sales_paid_total + booking_paid_total + other_receipts_total
    grn_paid_total = float(sum(float(g.paid_amount or 0) for g in grns))
    net_movement = total_money_in - grn_paid_total

    return render_template('accounts/receipts.html',
                           client_payments=client_payments,
                           sales=sales,
                           bookings=bookings,
                           other_receipts=other_receipts,
                           grns=grns,
                           grn_totals=grn_totals,
                           client_receipts_total=client_receipts_total,
                           sales_paid_total=sales_paid_total,
                           booking_paid_total=booking_paid_total,
                           other_receipts_total=other_receipts_total,
                           total_money_in=total_money_in,
                           grn_paid_total=grn_paid_total,
                           net_movement=net_movement,
                           date_from=date_from, date_to=date_to_excl - timedelta(days=1))




def _payments_permission_ok():
    return current_user.role in ('admin', 'root') or getattr(current_user, 'can_manage_payments', False)


def _payments_void_ok():
    """Void / delete / restore is its own atomic switch on top of the module
    grant: a payment clerk can record payments, but removing money history
    additionally requires "Void / Delete / Restore Transactions"."""
    if current_user.role in ('admin', 'root'):
        return True
    if not _payments_permission_ok():
        return False
    from app.services.permissions import _user_can
    return _user_can('can_void_transactions')


@accounts_bp.route('/payments/clients/save', methods=['POST'])
@login_required
def client_payment_save():
    """Create or update a client payment (single shared form for Create + Edit)."""
    if not _payments_permission_ok():
        flash('Permission denied', 'danger')
        return redirect(url_for('accounts.client_payments'))
    show_mode = (request.form.get('show') or 'active').strip().lower()
    payment_id = request.form.get('payment_id', type=int)
    try:
        from app.services.files_pdf import save_photo
        from app.services.payments_crud import save_client_payment
        uploaded_photo = save_photo(request.files.get('photo'))
        payment, created = save_client_payment(
            payment_id=payment_id,
            client_name=request.form.get('client_name', ''),
            client_code=request.form.get('client_code', ''),
            amount=request.form.get('amount', 0),
            discount=request.form.get('discount', 0),
            discount_reason=request.form.get('discount_reason', ''),
            payment_type=request.form.get('payment_type', 'Receipt'),
            method=request.form.get('method', 'Cash'),
            payment_account_id=request.form.get('payment_account_id'),
            manual_bill_no=request.form.get('manual_bill_no', ''),
            date_posted=request.form.get('date', ''),
            note=request.form.get('note', ''),
            photo_path=uploaded_photo,
            photo_url=request.form.get('photo_url', ''),
            idempotency_key=request.form.get('idempotency_key'),
            expected_revision=request.form.get('revision'),
            actor=current_user,
        )
        replay = bool(getattr(payment, '_idempotent_replay', False))
        db.session.commit()
        if replay:
            flash('This payment submission was already processed; no duplicate was created.', 'info')
        else:
            flash('Payment saved successfully.' if created else 'Payment updated. All balances recalculated.', 'success')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception:
        db.session.rollback()
        logger.exception('Client payment save failed')
        flash('Unable to save payment: the payment could not be saved. Please check the details and try again.', 'danger')
    return redirect(url_for('accounts.client_payments', show=show_mode))


@accounts_bp.route('/payments/clients/<int:id>/data')
@login_required
def client_payment_data(id):
    """JSON payload that populates the shared create/edit form for an existing payment."""
    p = Payment.query.get_or_404(id)
    c = db.session.get(Client, p.client_id) if getattr(p, 'client_id', None) else None
    if c is None:
        try:
            from app.services.lookups import get_client_by_input
            c = get_client_by_input(p.client_name or '')
        except Exception:
            c = None
    from app.services.payments_crud import _client_payment_kind, _client_payment_source
    source_type, source_id = _client_payment_source(p)
    payment_kind = _client_payment_kind(p)
    account_label = _account_option_label(p.payment_account) if p.payment_account else (p.account_name or '')
    return jsonify({
        'id': p.id,
        'revision': getattr(p, 'revision', None) or 1,
        'client_name': p.client_name or '',
        'client_code': (c.code if c else ''),
        'amount': round(abs(float(p.amount or 0)), 2),
        'discount': round(float(p.discount or 0), 2),
        'discount_reason': p.discount_reason or '',
        'payment_type': payment_kind,
        'method': ('Cash' if (p.method or '').strip().lower() == 'refund' else (p.method or 'Cash')),
        'payment_account_id': p.payment_account_id,
        'bank_name': p.bank_name or '',
        'account_name': p.account_name or '',
        'account_label': account_label,
        'account_no': p.account_no or '',
        'manual_bill_no': p.manual_bill_no or '',
        'auto_bill_no': p.auto_bill_no or '',
        'date': p.date_posted.strftime('%Y-%m-%d') if p.date_posted else '',
        'note': p.note or '',
        'photo_url': p.photo_url or '',
        'photo_path': p.photo_path or '',
        'is_void': bool(p.is_void),
        'source_type': source_type,
        'source_id': source_id,
    })


@accounts_bp.route('/payments/suppliers/save', methods=['POST'])
@login_required
def supplier_payment_save():
    """Create or update a supplier payment (single shared form for Create + Edit)."""
    if not _payments_permission_ok():
        flash('Permission denied', 'danger')
        return redirect(url_for('accounts.supplier_payments'))
    show_mode = (request.form.get('show') or 'active').strip().lower()
    payment_id = request.form.get('payment_id', type=int)
    try:
        from app.services.payments_crud import save_supplier_payment
        payment, created = save_supplier_payment(
            payment_id=payment_id,
            supplier_id=request.form.get('supplier_id'),
            amount=request.form.get('amount', 0),
            method=request.form.get('method', 'Cash'),
            payment_account_id=request.form.get('payment_account_id'),
            bank_name=request.form.get('bank_name', ''),
            account_name=request.form.get('account_name', ''),
            account_no=request.form.get('account_no', ''),
            manual_bill_no=request.form.get('manual_bill_no', ''),
            date_posted=request.form.get('date', ''),
            note=request.form.get('note', ''),
            idempotency_key=request.form.get('idempotency_key'),
            expected_revision=request.form.get('revision'),
            actor=current_user,
        )
        replay = bool(getattr(payment, '_idempotent_replay', False))
        db.session.commit()
        if replay:
            flash('This supplier-payment submission was already processed; no duplicate was created.', 'info')
        else:
            flash('Supplier payment saved successfully.' if created else 'Supplier payment updated. All balances recalculated.', 'success')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception:
        db.session.rollback()
        logger.exception('Supplier payment save failed')
        flash('Unable to save supplier payment: the payment could not be saved. Please check the details and try again.', 'danger')
    return redirect(url_for('accounts.supplier_payments', show=show_mode))


@accounts_bp.route('/payments/suppliers/<int:id>/data')
@login_required
def supplier_payment_data(id):
    """JSON payload that populates the shared create/edit form for a supplier payment."""
    p = SupplierPayment.query.get_or_404(id)
    from app.services.payments_crud import _supplier_payment_source
    source_type, source_id = _supplier_payment_source(p)
    account_label = _account_option_label(p.payment_account) if p.payment_account else (p.account_name or '')
    return jsonify({
        'id': p.id,
        'revision': getattr(p, 'revision', None) or 1,
        'supplier_id': p.supplier_id,
        'supplier_name': (p.supplier.name if p.supplier else ''),
        'amount': round(float(p.amount or 0), 2),
        'method': p.method or 'Cash',
        'payment_account_id': p.payment_account_id,
        'bank_name': p.bank_name or '',
        'account_name': p.account_name or '',
        'account_label': account_label,
        'account_no': p.account_no or '',
        'manual_bill_no': p.manual_bill_no or '',
        'auto_bill_no': p.auto_bill_no or '',
        'date': p.date_posted.strftime('%Y-%m-%d') if p.date_posted else '',
        'note': p.note or '',
        'is_void': bool(p.is_void),
        'source_type': source_type,
        'source_id': source_id,
    })


@accounts_bp.route('/payments/suppliers/<int:id>/delete', methods=['POST'])
@login_required
def supplier_payment_delete(id):
    """Soft-delete a supplier payment and reverse its accounting effects."""
    if not _payments_void_ok():
        flash('You need the "Void / Delete / Restore Transactions" permission to delete supplier payments.', 'danger')
        return redirect(url_for('accounts.supplier_payments'))
    payment = SupplierPayment.query.get_or_404(id)
    try:
        from app.services.payments_crud import delete_supplier_payment
        if delete_supplier_payment(payment, actor=current_user):
            db.session.commit()
            flash('Supplier payment deleted. Balances reversed.', 'success')
        else:
            flash('Supplier payment already deleted.', 'warning')
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
    except Exception:
        db.session.rollback()
        logger.exception('Supplier payment delete failed')
        flash('Unable to delete supplier payment: the payment could not be deleted. Please try again.', 'danger')
    return redirect(request.referrer or url_for('accounts.supplier_payments'))


@accounts_bp.route('/payments/suppliers/<int:id>/restore', methods=['POST'])
@login_required
def supplier_payment_restore(id):
    """Restore a deleted supplier payment and re-apply its accounting effects."""
    if not _payments_void_ok():
        flash('You need the "Void / Delete / Restore Transactions" permission to restore supplier payments.', 'danger')
        return redirect(url_for('accounts.supplier_payments'))
    payment = SupplierPayment.query.get_or_404(id)
    try:
        from app.services.payments_crud import restore_supplier_payment
        if restore_supplier_payment(payment, actor=current_user):
            db.session.commit()
            flash('Supplier payment restored. Balances re-applied.', 'success')
        else:
            flash('Supplier payment is already active.', 'warning')
    except Exception as exc:
        db.session.rollback()
        logger.exception('Supplier payment restore failed')
        flash(f'Unable to restore supplier payment: {exc}', 'danger')
    return redirect(request.referrer or url_for('accounts.supplier_payments'))
