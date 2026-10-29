"""cash — configuration-driven Cash Flow routes."""
from ._common import *  # noqa

logger = logging.getLogger(__name__)

from app.services.cash_flow_svc import (
    CF_DIR_IN,
    CF_DIR_OUT,
    CF_DIR_TRANSFER,
    CF_PARTY_TYPES,
    CF_SOURCE_LABELS,
    apply_running_balance,
    categories_for_direction,
    cf_used_category_ids,
    cf_used_party_ids,
    cf_used_subcategory_ids,
    collect_cash_flow_rows,
    filter_cash_flow_rows,
    subcategories_for_category,
    summarize_cash_flow_rows,
    _cf_account_label,
    _cf_company_accounts,
    _cf_ensure_indexes,
    _cf_normalize_direction,
    _cf_sort_key,
    _cf_type_label,
)


def _cf_filter_kwargs(source):
    return {
        'from_date': source.get('from_date'),
        'to_date': source.get('to_date'),
        'filter_type': source.get('filter_type') or 'all',
        'origin': source.get('origin') or 'all',
        'category': source.get('category') or '',
        'subcategory': source.get('subcategory') or '',
        'party_type': source.get('party_type') or '',
        'party': source.get('party') or '',
        'account_id': source.get('account_id') or '',
        'q': source.get('q') or '',
        'notes': source.get('notes') or '',
        'reference': source.get('reference') or '',
        'description': source.get('description') or '',
        'created_by': source.get('created_by') or '',
        'status': source.get('status') or 'active',
        'amount_min': source.get('amount_min') or '',
        'amount_max': source.get('amount_max') or '',
    }


@bp.route('/cash_flow/meta/categories', methods=['GET'])
@login_required
def cash_flow_categories_meta():
    """Read-only category choices for legacy display widgets."""
    direction = _cf_normalize_direction(request.args.get('direction') or '')
    cats = categories_for_direction(direction or 'both')
    return jsonify({
        'categories': [
            {'id': c.id, 'name': c.name, 'direction': c.direction or 'both', 'notes': c.notes or ''}
            for c in cats
        ]
    })


@bp.route('/cash_flow/meta/subcategories', methods=['GET'])
@login_required
def cash_flow_subcategories_meta():
    category_id = request.args.get('category_id', type=int)
    if not category_id:
        return jsonify({'subcategories': []})
    return jsonify({
        'subcategories': [
            {'id': s.id, 'name': s.name, 'category_id': s.category_id, 'notes': s.notes or ''}
            for s in subcategories_for_category(category_id)
        ]
    })


@bp.route('/cash_flow/meta/parties', methods=['GET'])
@login_required
def cash_flow_parties_meta():
    parties = CashFlowParty.query.filter_by(is_active=True).order_by(CashFlowParty.name).all()
    return jsonify({
        'parties': [
            {'id': p.id, 'name': p.name, 'party_type': p.party_type or 'other', 'note': p.note or ''}
            for p in parties
        ]
    })


@bp.route('/cash_flow/entry/<int:entry_id>.json')
@login_required
def cash_flow_entry_json(entry_id):
    entry = CashFlowEntry.query.get_or_404(entry_id)
    dest = getattr(entry, 'destination_account', None)
    audits = []
    for a in sorted(getattr(entry, 'audit_trail', []) or [], key=lambda x: x.id):
        audits.append({
            'action': a.action,
            'reason': a.reason or '',
            'changed_by': a.changed_by or '',
            'changed_at': a.changed_at.strftime('%Y-%m-%d %H:%M') if a.changed_at else '',
        })
    return jsonify({
        'id': entry.id,
        'direction': entry.direction,
        'amount': float(entry.amount or 0),
        'account_id': entry.account_id,
        'account_name': entry.account.name if entry.account else '',
        'destination_account_id': getattr(entry, 'destination_account_id', None),
        'destination_account_name': dest.name if dest else '',
        'category_id': entry.category_id,
        'category_name': entry.category.name if entry.category else '',
        'subcategory_id': entry.subcategory_id,
        'subcategory_name': entry.subcategory.name if entry.subcategory else '',
        'party_id': entry.party_id,
        'party_name': entry.party_name or '',
        'party_type': entry.party_type or 'other',
        'description': entry.description or '',
        'note': entry.note or '',
        'reference': getattr(entry, 'reference', None) or '',
        'date_posted': entry.date_posted.strftime('%Y-%m-%dT%H:%M') if entry.date_posted else '',
        'is_void': bool(entry.is_void),
        'void_reason': getattr(entry, 'void_reason', None) or '',
        'voided_by': getattr(entry, 'voided_by', None) or '',
        'created_by': entry.created_by or '',
        'updated_by': getattr(entry, 'updated_by', None) or '',
        'status': 'voided' if entry.is_void else 'active',
        'audit': audits,
    })


def _book_cash_context(day=None):
    """Cash per the journal, for the reconciliation strip.

    Never raises: the cash-flow page must render even if the book is being
    rebuilt, and the strip says so.
    """
    try:
        from app.services.read_models import cash_view

        view = cash_view.load(as_of=day or pk_today())
        return {
            'total_book': view['total_book'],
            'total_master': view['total_master'],
            'difference': view['difference'],
            'matches': view['matches'],
            'rows': view['rows'],
            'ledger_version': view['ledger_version'],
        }
    except Exception:  # pragma: no cover - defensive
        return None



@bp.route('/cash_flow', methods=['GET'])
@login_required
def cash_flow():
    # Standalone cash movements are entered via Accounts. Linked business
    # events remain attached to their source records and feed this view.
    _cf_ensure_indexes()
    source = request.args
    fresh_start_dt = pk_today()
    fresh_start_date = fresh_start_dt.strftime('%Y-%m-%d')
    from_date = source.get('from_date', fresh_start_date)
    to_date = source.get('to_date', fresh_start_date)
    filter_kwargs = _cf_filter_kwargs(source)
    filter_type = filter_kwargs['filter_type']
    opening_balance_input = source.get('opening_balance', '').strip()
    export_pdf = request.args.get('export_pdf', '')
    export_csv = request.args.get('export_csv', '')
    export_scope = (request.args.get('export_scope') or 'filtered').strip().lower()

    adjustment_date_input = to_date

    try:
        from_date_dt = datetime.strptime(from_date, '%Y-%m-%d').date()
    except Exception:
        from_date_dt = fresh_start_dt
        from_date = fresh_start_date
    try:
        to_date_dt = datetime.strptime(to_date, '%Y-%m-%d').date()
    except Exception:
        to_date_dt = fresh_start_dt
        to_date = fresh_start_date

    fresh_start_clamped = False
    if to_date_dt < from_date_dt:
        to_date_dt = from_date_dt
        to_date = from_date_dt.strftime('%Y-%m-%d')

    today_opening_override = _cash_flow_today_opening_override(fresh_start_date)
    fresh_start_cutoff = _cash_flow_fresh_start_cutoff(fresh_start_date)
    # "Fresh start" (hide existing entries, opening Rs. 0) is opt-in only:
    # it activates when the user explicitly clicks the reset button today.
    # By default, TODAY behaves like any other date — automatic opening
    # carry-forward and all entries visible.
    _cutoff_info = session.get('cash_flow_fresh_start_cutoff') or {}
    hide_existing_today_entries = (
        from_date_dt == fresh_start_dt and bool(_cutoff_info.get('user_set'))
    )
    posted_after = fresh_start_cutoff if hide_existing_today_entries else None

    if opening_balance_input:
        try:
            opening_balance = float(opening_balance_input)
        except ValueError:
            opening_balance = 0.0
    elif from_date_dt == fresh_start_dt and today_opening_override is not None:
        opening_balance = today_opening_override
    elif hide_existing_today_entries:
        opening_balance = 0.0
    else:
        opening_balance = _automatic_cash_opening_balance(from_date_dt)

    filter_account_id = source.get('account_id', type=int)
    filter_status = (filter_kwargs.get('status') or 'active').strip().lower()
    all_rows = collect_cash_flow_rows(
        from_date, to_date,
        posted_after=posted_after,
        include_voided=(filter_status in ('voided', 'all')),
    )
    all_rows.sort(key=_cf_sort_key)

    display_rows = filter_cash_flow_rows(all_rows, {
        **filter_kwargs,
        'account_id': filter_account_id,
        'filter_type': filter_type,
        'status': filter_status,
    })
    if export_scope == 'all' and (export_csv == '1' or export_pdf == '1'):
        display_rows = list(all_rows)
        if filter_status in ('active', 'voided'):
            display_rows = [r for r in display_rows if (r.get('status') or 'active') == filter_status]

    closing_balance = apply_running_balance(display_rows, opening_balance, account_id=filter_account_id)
    summary = summarize_cash_flow_rows(display_rows, account_id=filter_account_id)
    total_cash_in = summary['total_cash_in']
    total_cash_out = summary['total_cash_out']

    # Reconciliation must always compare physical cash against the FULL
    # (unfiltered, company-wide) closing for the period — never against a
    # closing narrowed by type/category/party/account display filters.
    active_all_rows = [r for r in all_rows if (r.get('status') or 'active') == 'active']
    reconciliation_closing = apply_running_balance(
        [dict(r) for r in active_all_rows], opening_balance
    )

    try:
        adjustment_date_dt = datetime.strptime(adjustment_date_input, '%Y-%m-%d').date()
    except Exception:
        adjustment_date_dt = datetime.strptime(to_date, '%Y-%m-%d').date()

    adjustment_entry = CashFlowDifferenceAdjustment.query.filter_by(adjustment_date=adjustment_date_dt).first()
    physical_cash_available = adjustment_entry.physical_cash_available if adjustment_entry and adjustment_entry.physical_cash_available is not None else None
    adjustment_amount = float((adjustment_entry.difference if adjustment_entry and adjustment_entry.difference is not None else adjustment_entry.amount) or 0) if adjustment_entry else 0.0
    reconciliation_reason = (adjustment_entry.reason or adjustment_entry.note or '') if adjustment_entry else ''
    adjusted_closing_balance = physical_cash_available if physical_cash_available is not None else reconciliation_closing

    if export_csv == '1':
        buf = io.StringIO()
        import csv
        writer = csv.writer(buf)
        writer.writerow([
            'Date', 'Type', 'Account', 'Category', 'Subcategory', 'Party',
            'Amount', 'Received', 'Spent', 'Transfer', 'Description', 'Notes',
            'Reference', 'Source', 'Created By', 'Status',
        ])
        for r in display_rows:
            writer.writerow([
                r.get('date'), r.get('tx_type_label'), r.get('account_display'),
                r.get('category'), r.get('subcategory'), r.get('party_name'),
                r.get('amount'), r.get('cash_in') or '', r.get('cash_out') or '',
                r.get('transfer_amount') or '', r.get('description'), r.get('note'),
                r.get('reference'), r.get('origin_label'), r.get('created_by'),
                r.get('status'),
            ])
        resp = make_response(buf.getvalue())
        resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
        resp.headers['Content-Disposition'] = f'attachment; filename=cash_flow_{from_date}_{to_date}.csv'
        return resp

    cash_accounts = _cf_company_accounts(active_only=True)
    cash_accounts_list = [a for a in cash_accounts if (a.category or 'cash').lower() == 'cash']
    bank_accounts_list = [a for a in cash_accounts if (a.category or '').lower() == 'bank']
    cash_total = sum(_money_round(a.balance or 0) for a in cash_accounts_list)
    bank_total = sum(_money_round(a.balance or 0) for a in bank_accounts_list)

    # Per-account activity within the currently filtered rows (for cash/bank cards).
    account_activity = {}
    for r in display_rows:
        if (r.get('status') or 'active') != 'active':
            continue
        rtype = r.get('type')
        aid = r.get('account_id')
        if aid:
            act = account_activity.setdefault(aid, {'in': 0.0, 'out': 0.0, 'transfer_in': 0.0, 'transfer_out': 0.0})
            if rtype == CF_DIR_IN:
                act['in'] += float(r.get('cash_in') or 0)
            elif rtype == CF_DIR_OUT:
                act['out'] += float(r.get('cash_out') or 0)
            elif rtype == CF_DIR_TRANSFER:
                act['transfer_out'] += float(r.get('transfer_amount') or 0)
        aid2 = r.get('account_to_id')
        if aid2 and rtype == CF_DIR_TRANSFER:
            act2 = account_activity.setdefault(aid2, {'in': 0.0, 'out': 0.0, 'transfer_in': 0.0, 'transfer_out': 0.0})
            act2['transfer_in'] += float(r.get('transfer_amount') or 0)

    cf_categories = CashFlowCategory.query.order_by(CashFlowCategory.sort_order, CashFlowCategory.name).all()
    cf_subcategories = CashFlowSubcategory.query.order_by(CashFlowSubcategory.name).all()
    cf_parties = CashFlowParty.query.filter_by(is_active=True).order_by(CashFlowParty.name).all()
    cf_parties_all = CashFlowParty.query.order_by(CashFlowParty.name).all()
    created_by_options = sorted({
        (r.get('created_by') or '').strip()
        for r in all_rows
        if (r.get('created_by') or '').strip()
    })
    account_options = [{'id': a.id, 'label': _cf_account_label(a)} for a in cash_accounts]
    source_options = [('all', 'All'), ('manual', 'Manual'), ('system', 'System-generated')]
    for key, label in CF_SOURCE_LABELS.items():
        if key == 'MANUAL_CASH_FLOW':
            continue
        source_options.append((key.lower(), label))

    today_str = fresh_start_date
    yesterday_str = (fresh_start_dt - timedelta(days=1)).strftime('%Y-%m-%d')
    this_week_str = (fresh_start_dt - timedelta(days=fresh_start_dt.weekday())).strftime('%Y-%m-%d')
    this_month_str = fresh_start_dt.replace(day=1).strftime('%Y-%m-%d')
    last_30_days_str = (fresh_start_dt - timedelta(days=30)).strftime('%Y-%m-%d')

    recent_reconciliations = (
        CashFlowDifferenceAdjustment.query
        .order_by(CashFlowDifferenceAdjustment.adjustment_date.desc())
        .limit(10)
        .all()
    )

    common = dict(
        rows=display_rows,
        cash_accounts=cash_accounts,
        cash_accounts_list=cash_accounts_list,
        bank_accounts_list=bank_accounts_list,
        cash_total=cash_total,
        bank_total=bank_total,
        account_activity=account_activity,
        account_options=account_options,
        cf_categories=cf_categories,
        cf_subcategories=cf_subcategories,
        cf_parties=cf_parties,
        cf_parties_all=cf_parties_all,
        used_category_ids=cf_used_category_ids(),
        used_subcategory_ids=cf_used_subcategory_ids(),
        used_party_ids=cf_used_party_ids(),
        default_movement_datetime=pk_now().strftime('%Y-%m-%dT%H:%M'),
        party_types=CF_PARTY_TYPES,
        source_options=source_options,
        created_by_options=created_by_options,
        today_str=today_str,
        yesterday_str=yesterday_str,
        this_week_str=this_week_str,
        this_month_str=this_month_str,
        last_30_days_str=last_30_days_str,
        breakdown_cat=summary['breakdown_cat'],
        breakdown_party=summary['breakdown_party'],
        breakdown_account=summary['breakdown_account'],
        filter_origin=filter_kwargs['origin'],
        filter_category=filter_kwargs['category'],
        filter_subcategory=filter_kwargs['subcategory'],
        filter_party_type=filter_kwargs['party_type'],
        filter_party=filter_kwargs['party'],
        filter_account_id=filter_account_id,
        filter_q=filter_kwargs['q'],
        filter_notes=filter_kwargs['notes'],
        filter_reference=filter_kwargs['reference'],
        filter_description=filter_kwargs['description'],
        filter_created_by=filter_kwargs['created_by'],
        filter_status=filter_status,
        filter_amount_min=filter_kwargs['amount_min'],
        filter_amount_max=filter_kwargs['amount_max'],
        from_date=from_date, to_date=to_date,
        filter_type=filter_type,
        opening_balance=opening_balance,
        opening_balance_input=opening_balance_input,
        adjustment_amount=adjustment_amount,
        physical_cash_available=physical_cash_available,
        reconciliation_reason=reconciliation_reason,
        recent_reconciliations=recent_reconciliations,
        show_delete_button=bool(adjustment_entry and adjustment_entry.physical_cash_available is not None),
        adjusted_closing_balance=adjusted_closing_balance,
        adjustment_date_input=adjustment_date_input,
        closing_balance=closing_balance,
        reconciliation_closing=reconciliation_closing,
        total_cash_in=total_cash_in,
        total_cash_out=total_cash_out,
        total_transfer_in=summary['total_transfer_in'],
        total_transfer_out=summary['total_transfer_out'],
        generated_at=pk_now().strftime('%Y-%m-%d %H:%M'),
        settings=None,
        fresh_start_date=fresh_start_date,
        fresh_start_cutoff=fresh_start_cutoff,
        fresh_start_clamped=fresh_start_clamped,
        today_opening_override=today_opening_override,
        is_fresh_start_view=hide_existing_today_entries,
        # The book's answer, from read_models.cash_view (master plan §16 S11).
        # This page keeps its document-level detail; the reconciliation strip
        # uses the ledger so a difference between the two is visible instead of
        # being buried in a second formula.
        book_cash=_book_cash_context(),
    )

    if export_pdf == '1':
        rendered = render_template('cash_flow.html', pdf_mode=True, **common)
        pdf_resp = _try_render_weasy_pdf(rendered, f'cash_flow_{from_date}_{to_date}.pdf')
        if pdf_resp:
            return pdf_resp
        return Response(rendered, content_type='text/html')

    # Embedded reference layouts (references-images/):
    #   - "Transactions" tab   -> Financial Tracking Filter Matrix (layout #2)
    #   - "Daily Reconciliations" tab -> Daily Cash & Bank Reconciliation (layout #1)
    recon_day_arg = (request.args.get('recon_day') or '').strip()
    dr_ctx = _dr_build_context(recon_day_arg, embed=True)
    ft_ctx = _ft_build_context(request.args, prefix='ft_', embed=True, posted_after=posted_after)
    ft_ctx['read_only'] = True
    initial_tab = ''
    if recon_day_arg:
        initial_tab = 'reconcile'
    elif any(k.startswith('ft_') for k in request.args):
        initial_tab = 'transactions'

    return render_template(
        'cash_flow.html', pdf_mode=False, read_only=True,
        dr=dr_ctx, ft=ft_ctx, initial_tab=initial_tab,
        **common,
    )


@bp.route('/cash_flow_differences')
@login_required
def cash_flow_differences():
    from_date = request.args.get('from_date', '').strip()
    to_date = request.args.get('to_date', '').strip()
    workflow_filter = request.args.get('workflow_filter', 'all').strip()
    page = max(1, int(request.args.get('page', 1) or 1))
    per_page = 25

    query = CashFlowDifferenceAdjustment.query
    if from_date:
        try:
            query = query.filter(CashFlowDifferenceAdjustment.adjustment_date >= datetime.strptime(from_date, '%Y-%m-%d').date())
        except Exception:
            from_date = ''
    if to_date:
        try:
            query = query.filter(CashFlowDifferenceAdjustment.adjustment_date <= datetime.strptime(to_date, '%Y-%m-%d').date())
        except Exception:
            to_date = ''
    if workflow_filter == 'new':
        query = query.filter(CashFlowDifferenceAdjustment.physical_cash_available.isnot(None))
    elif workflow_filter == 'legacy':
        query = query.filter(CashFlowDifferenceAdjustment.physical_cash_available.is_(None))
    else:
        workflow_filter = 'all'

    total_count = CashFlowDifferenceAdjustment.query.count()
    new_workflow_count = CashFlowDifferenceAdjustment.query.filter(CashFlowDifferenceAdjustment.physical_cash_available.isnot(None)).count()
    legacy_count = CashFlowDifferenceAdjustment.query.filter(CashFlowDifferenceAdjustment.physical_cash_available.is_(None)).count()
    total_audit_events = CashFlowReconciliationAudit.query.count()

    filtered_count = query.count()
    pages = max(1, (filtered_count + per_page - 1) // per_page)
    page = min(page, pages)
    reconciliations = query.order_by(
        CashFlowDifferenceAdjustment.adjustment_date.desc(),
        CashFlowDifferenceAdjustment.id.desc()
    ).offset((page - 1) * per_page).limit(per_page).all()
    for rec in reconciliations:
        rec.display_opening_balance = _automatic_cash_opening_balance(rec.adjustment_date)
        rec.display_cash_in, rec.display_cash_out = _cash_flow_in_out_between(rec.adjustment_date, rec.adjustment_date)
        if rec.calculated_closing is None:
            rec.display_calculated_closing = rec.display_opening_balance + rec.display_cash_in - rec.display_cash_out
        else:
            rec.display_calculated_closing = rec.calculated_closing

    return render_template(
        'cash_flow_differences.html',
        reconciliations=reconciliations,
        total_count=total_count,
        new_workflow_count=new_workflow_count,
        legacy_count=legacy_count,
        total_audit_events=total_audit_events,
        from_date=from_date,
        to_date=to_date,
        workflow_filter=workflow_filter,
        page=page,
        pages=pages,
    )


def _dr_build_context(day_value, embed=False):
    """Build the Daily Cash & Bank Reconciliation board context (layout #1).

    Shared by the standalone /daily_reconciliation page and the embedded
    "Daily Reconciliations" tab on /cash_flow.
    """
    from app.services import cash_day_recon as recon

    today_str = pk_today().strftime('%Y-%m-%d')
    day_str = (day_value or today_str).strip() or today_str
    try:
        datetime.strptime(day_str, '%Y-%m-%d')
    except ValueError:
        day_str = today_str

    positions = recon.account_positions_for_date(day_str)
    day_lock = recon.get_day_lock(day_str)
    total_expected = _money_round(sum(p['expected_closing'] for p in positions))
    counted_values = [p['effective_counted'] for p in positions if p['effective_counted'] is not None]
    total_counted = _money_round(sum(counted_values)) if counted_values else None
    net_difference = (
        _money_round(total_counted - total_expected) if total_counted is not None else 0.0
    )
    prev_str = (datetime.strptime(day_str, '%Y-%m-%d') - timedelta(days=1)).strftime('%Y-%m-%d')
    next_str = (datetime.strptime(day_str, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')

    return dict(
        day_str=day_str,
        prev_str=prev_str,
        next_str=next_str,
        positions=positions,
        day_lock=day_lock,
        total_expected=total_expected,
        total_counted=total_counted,
        net_difference=net_difference,
        embed=embed,
    )


def _safe_return_to():
    """Relative-only return_to target posted by embedded reconciliation forms."""
    rt = (request.form.get('return_to') or '').strip()
    if rt.startswith('/') and not rt.startswith('//'):
        return rt
    return None


def _dr_wants_json():
    return (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or request.accept_mimetypes.best == 'application/json'
    )


def _dr_json_context(day_str):
    ctx = _dr_build_context(day_str)
    return {
        'day_str': ctx['day_str'],
        'total_expected': ctx['total_expected'],
        'total_counted': ctx['total_counted'],
        'net_difference': ctx['net_difference'],
        'positions': ctx['positions'],
    }


@bp.route('/daily_reconciliation', methods=['GET', 'POST'])
@login_required
def daily_reconciliation():
    """Daily Cash & Bank Reconciliation board (reference layout #1)."""
    from app.services import cash_day_recon as recon

    today_str = pk_today().strftime('%Y-%m-%d')
    day_str = (request.form.get('day') or request.args.get('day') or today_str).strip() or today_str
    try:
        datetime.strptime(day_str, '%Y-%m-%d')
    except ValueError:
        day_str = today_str

    if request.method == 'POST':
        action = (request.form.get('action') or '').strip()
        counted_raw = (request.form.get('counted') or '').strip()
        # fetch(FormData) omits the submit button. Treat a counted amount +
        # account as save_count so Enter / unfocused Save still persist.
        if not action and counted_raw and request.form.get('account_id'):
            action = 'save_count'
        wants_json = _dr_wants_json()
        ok = True
        message = ''
        status = 'success'
        try:
            if action == 'save_count':
                if counted_raw == '':
                    raise ValueError('empty counted amount')
                recon.save_count(
                    day_str,
                    int(request.form.get('account_id')),
                    float(counted_raw),
                    actor=current_user,
                )
                message = 'Counted balance saved.'
                status = 'success'
            elif action == 'lock':
                recon.lock_day(day_str, actor=current_user, note=request.form.get('note') or '')
                message = f'Day {day_str} verified & locked. Locked totals become the next day opening.'
                status = 'success'
            elif action == 'unlock':
                recon.unlock_day(day_str)
                message = f'Day {day_str} unlocked.'
                status = 'warning'
            elif action == 'clear_count':
                recon.delete_count(day_str, int(request.form.get('account_id')))
                message = 'Counted balance cleared.'
                status = 'warning'
            else:
                raise ValueError('Unsupported reconciliation action.')
        except (TypeError, ValueError):
            db.session.rollback()
            ok = False
            message = 'Enter a valid counted amount.' if action == 'save_count' else 'Unable to update reconciliation.'
            status = 'danger'
        except Exception:
            db.session.rollback()
            logger.exception('Daily reconciliation save failed')
            ok = False
            message = 'Unable to save reconciliation. Reload the page and try again.'
            status = 'danger'
        if wants_json:
            payload = _dr_json_context(day_str)
            payload.update({'ok': ok, 'message': message, 'error': None if ok else message, 'status': status})
            return jsonify(payload), (200 if ok else 400)
        flash(message, status)
        rt = _safe_return_to()
        if rt:
            return redirect(rt)
        return redirect(url_for('daily_reconciliation', day=day_str))

    return render_template(
        'daily_reconciliation.html',
        dr=_dr_build_context(day_str),
    )


def _ft_date_preset(preset, today):
    if preset == 'today':
        return today, today
    if preset == 'yesterday':
        y = today - timedelta(days=1)
        return y, y
    if preset == 'this_week':
        return today - timedelta(days=today.weekday()), today
    if preset == 'last_30':
        return today - timedelta(days=30), today
    if preset == 'all':
        return date(2000, 1, 1), today
    return today.replace(day=1), today  # this_month default


def _ft_build_context(args, prefix='', embed=False, posted_after=None):
    """Build the Financial Tracking Filter Matrix context (reference layout #2).

    Shared by the standalone /financial_tracker page (prefix='') and the
    embedded "Transactions" tab on /cash_flow (prefix='ft_', to avoid
    clashing with the cash flow page's own query params).
    """
    today = pk_today()
    preset = (args.get(prefix + 'preset') or 'this_month').strip().lower()
    from_d, to_d = _ft_date_preset(preset, today)
    f_direction = (args.get(prefix + 'direction') or 'all').strip().lower()
    f_category = (args.get(prefix + 'category') or '').strip()
    f_account = (args.get(prefix + 'account') or '').strip()
    f_client = (args.get(prefix + 'client') or '').strip()
    f_supplier = (args.get(prefix + 'supplier') or '').strip()
    f_partner = (args.get(prefix + 'partner') or '').strip()
    f_worker = (args.get(prefix + 'worker') or '').strip()
    f_vehicle = (args.get(prefix + 'vehicle') or '').strip().lower()
    f_method = (args.get(prefix + 'method') or 'all').strip().lower()
    f_q = (args.get(prefix + 'q') or '').strip().lower()

    rows = collect_cash_flow_rows(
        from_d.strftime('%Y-%m-%d'), to_d.strftime('%Y-%m-%d'),
        posted_after=posted_after,
    )

    role_filters = {
        'client': f_client, 'supplier': f_supplier,
        'partner': f_partner, 'worker': f_worker,
    }
    active_roles = {k: v for k, v in role_filters.items() if v and v != 'all'}

    filtered = []
    for r in rows:
        if (r.get('status') or 'active') != 'active':
            continue
        if f_direction in ('in', 'out') and r.get('type') != f_direction:
            continue
        if f_direction == 'transfer' and r.get('type') != CF_DIR_TRANSFER:
            continue
        if f_category and f_category.lower() not in (r.get('category') or '').lower():
            continue
        if f_account:
            try:
                aid = int(f_account)
            except ValueError:
                aid = -1
            if r.get('account_id') != aid and r.get('account_to_id') != aid:
                continue
        if active_roles:
            ptype = (r.get('party_type') or '').lower()
            pname = (r.get('party_name') or '')
            ok = False
            for role, val in active_roles.items():
                if ptype == role and (val.lower() == pname.lower() or val.lower() in pname.lower()):
                    ok = True
            if not ok:
                continue
        if f_vehicle and f_vehicle not in ' '.join([
            r.get('description') or '', r.get('note') or '', r.get('reference') or ''
        ]).lower():
            continue
        if f_method != 'all' and f_method not in (r.get('method') or r.get('origin_label') or '').lower():
            continue
        if f_q:
            blob = ' '.join([
                str(r.get('reference') or ''), str(r.get('description') or ''),
                str(r.get('note') or ''), str(r.get('party_name') or ''),
                str(r.get('category') or ''), str(r.get('account_display') or ''),
            ]).lower()
            if f_q not in blob:
                continue
        filtered.append(r)

    summary = summarize_cash_flow_rows(filtered)
    total_in = _money_round(summary['total_cash_in'] + summary['total_transfer_in'])
    total_out = _money_round(summary['total_cash_out'] + summary['total_transfer_out'])
    net = _money_round(total_in - total_out)

    # Dropdown options sourced from the data.
    all_rows_active = [r for r in rows if (r.get('status') or 'active') == 'active']
    cat_options = sorted({(r.get('category') or '').strip() for r in all_rows_active if (r.get('category') or '').strip()})
    account_options = [{'id': a.id, 'label': _cf_account_label(a)} for a in _cf_company_accounts(active_only=True)]

    def _party_opts(ptype):
        return sorted({
            (r.get('party_name') or '').strip()
            for r in all_rows_active
            if (r.get('party_type') or '').lower() == ptype and (r.get('party_name') or '').strip()
        })

    ctx = dict(
        preset=preset, from_d=from_d, to_d=to_d,
        f_direction=f_direction, f_category=f_category, f_account=f_account,
        f_client=f_client, f_supplier=f_supplier, f_partner=f_partner,
        f_worker=f_worker, f_vehicle=f_vehicle, f_method=f_method, f_q=f_q,
        rows=filtered, total_in=total_in, total_out=total_out, net=net,
        record_count=len(filtered),
        cat_options=cat_options, account_options=account_options,
        client_opts=_party_opts('client'), supplier_opts=_party_opts('supplier'),
        partner_opts=_party_opts('partner'), worker_opts=_party_opts('worker'),
        vehicle_opts=sorted({(r.get('vehicle') or '').strip() for r in all_rows_active if (r.get('vehicle') or '').strip()}),
        breakdown_cat=summary['breakdown_cat'],
        breakdown_account=summary['breakdown_account'],
        prefix=prefix,
        embed=embed,
    )
    return ctx


@bp.route('/financial_tracker', methods=['GET'])
@login_required
def financial_tracker():
    """Financial Tracking Filter Matrix (reference layout #2)."""
    ctx = _ft_build_context(request.args)
    from_d, to_d = ctx['from_d'], ctx['to_d']
    filtered = ctx['rows']

    if request.args.get('export_csv') == '1':
        buf = io.StringIO()
        import csv
        w = csv.writer(buf)
        w.writerow(['Date', 'Type', 'Account', 'Category', 'Party', 'In', 'Out', 'Transfer', 'Reference', 'Description'])
        for r in filtered:
            w.writerow([
                r.get('date'), r.get('tx_type_label'), r.get('account_display'),
                r.get('category'), r.get('party_name'), r.get('cash_in') or '',
                r.get('cash_out') or '', r.get('transfer_amount') or '',
                r.get('reference'), r.get('description'),
            ])
        resp = make_response(buf.getvalue())
        resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
        resp.headers['Content-Disposition'] = f'attachment; filename=financial_tracker_{from_d}_{to_d}.csv'
        return resp

    return render_template('financial_tracker.html', ft=ctx)


@bp.route('/cash_flow_differences/<int:rec_id>')
@login_required
def cash_flow_reconciliation_detail(rec_id):
    reconciliation = CashFlowDifferenceAdjustment.query.get_or_404(rec_id)
    audit_trail = CashFlowReconciliationAudit.query.filter_by(
        adjustment_date=reconciliation.adjustment_date
    ).order_by(CashFlowReconciliationAudit.changed_at.asc(), CashFlowReconciliationAudit.id.asc()).all()
    return render_template(
        'cash_flow_reconciliation_detail.html',
        reconciliation=reconciliation,
        audit_trail=audit_trail,
    )
