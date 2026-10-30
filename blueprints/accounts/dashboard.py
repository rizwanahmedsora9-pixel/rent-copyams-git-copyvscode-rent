"""dashboard — split from accounts.py."""
from ._common import *  # noqa

from uuid import uuid4

@accounts_bp.route('/')
@login_required
def dashboard():
    """Accounts dashboard with KPI cards and financial overview."""
    _ensure_default_account_categories()
    _backfill_legacy_account_groups()
    today = pk_today()
    
    # KPIs come from the book (read_models.dashboard, cached against
    # ledger_version), not from a local sum of Payment/Booking/DirectSale rows.
    # One definition of "received today" for the dashboard, the ledger and the
    # API (master plan §16 S11).
    from app.services.read_models import dashboard as dashboard_model

    book = dashboard_model.load(today)
    client_payments_today = book['receivable_collected_today'] / 100.0
    supplier_payments_today = book['payable_paid_today'] / 100.0
    expenditures_today = book['cash_out_today'] / 100.0
    receipts_today = book['cash_in_today'] / 100.0

    # Account balances + due clients + company liquidity KPI
    accounts = _active_accounts().order_by(Account.name.asc()).all()
    from app.services.payments_crud import ledger_balance
    from .transaction_tree import (
        build_live_transaction_tree,
        enrich_accounts_with_period_flows,
        transaction_tree_registry_json,
    )
    today_date = pk_today()
    today_start = datetime.combine(today_date, datetime.min.time())
    today_end_excl = today_start + timedelta(days=1)
    opening_cutoff = today_start - timedelta(microseconds=1)
    for account in accounts:
        account.today_opening_balance = ledger_balance(account.id, as_of=opening_cutoff)
    enrich_accounts_with_period_flows(accounts, today_start, today_end_excl)

    # Period selector for the live data-driven Transaction Tree explorer
    tree_range = (request.args.get('tree_range') or 'month').strip().lower()
    tree_account_id = request.args.get('tree_account_id', type=int)
    if tree_range == 'today':
        tree_from, tree_to_excl = today_start, today_end_excl
        tree_range_label = 'Today'
    elif tree_range == 'week':
        tree_from = today_start - timedelta(days=6)
        tree_to_excl = today_end_excl
        tree_range_label = 'Last 7 Days'
    elif tree_range == 'all':
        tree_from, tree_to_excl = None, None
        tree_range_label = 'All Time'
    else:
        tree_range = 'month'
        tree_from = datetime(today_date.year, today_date.month, 1)
        tree_to_excl = today_end_excl
        tree_range_label = 'This Month'

    live_tx_tree = build_live_transaction_tree(
        date_from=tree_from,
        date_to_excl=tree_to_excl,
        account_id=tree_account_id,
    )
    tx_tree_registry = transaction_tree_registry_json()
    category_catalog = tx_tree_registry['catalog']
    loan_accounts = [
        a for a in accounts
        if (a.source_category or '').strip().lower() == 'loan'
        or (a.class_subcategory or '').strip().lower() == 'loans'
        or (a.type or '').strip().lower() == 'loan'
    ]
    
    due_clients = _client_due_summary()
    supplier_payables = _supplier_payable_summary()
    company_accounts = _company_accounts()
    account_categories = _account_categories()
    total_company_money = sum(float(a.balance or 0) for a in company_accounts)
    total_cash_money = sum(float(a.balance or 0) for a in company_accounts if (a.category or '').lower() == 'cash')
    receive_source_accounts = [{
        'id': a.id,
        'name': a.name,
        'source_category': (a.source_category or '').strip()
    } for a in accounts if (a.source_category or '').strip()]
    
    suppliers = Supplier.query.filter_by(is_active=True).order_by(Supplier.name.asc()).all()
    clients = Client.query.filter_by(is_active=True).order_by(Client.name.asc()).all()
    # Expense categories for the Pay → Expense form: one shared category book
    # with the Cash Flow page (out/both categories only).
    from models import CashFlowCategory
    expense_categories = CashFlowCategory.query.filter(
        CashFlowCategory.is_active == True,
        CashFlowCategory.direction.in_(['out', 'both']),
    ).order_by(CashFlowCategory.sort_order.asc(), CashFlowCategory.name.asc()).all()
    # Drivers are payables like suppliers: the Accounts section can pay them
    # directly, and the resulting transaction appears in the driver ledger.
    from app.services.driver_payments import driver_outstanding
    delivery_persons = DeliveryPerson.query.filter_by(is_active=True).order_by(DeliveryPerson.name.asc()).all()
    driver_outstanding_map = {}
    for person in delivery_persons:
        try:
            driver_outstanding_map[person.id] = driver_outstanding(person.id)
        except Exception:
            driver_outstanding_map[person.id] = 0.0
    return render_template('accounts/dashboard.html',
                          client_payments_today=client_payments_today,
                          supplier_payments_today=supplier_payments_today,
                          expenditures_today=expenditures_today,
                          receipts_today=receipts_today,
                         accounts=accounts,
                         due_clients=due_clients,
                         supplier_payables=supplier_payables,
                         company_accounts=company_accounts,
                          account_categories=account_categories,
                          receive_source_accounts=receive_source_accounts,
                          total_company_money=total_company_money,
                          total_cash_money=total_cash_money,
                          suppliers=suppliers,
                          clients=clients,
                          delivery_persons=delivery_persons,
                          driver_outstanding=driver_outstanding_map,
                          expense_categories=expense_categories,
                          tx_tree_registry=tx_tree_registry,
                          category_catalog=category_catalog,
                          live_tx_tree=live_tx_tree,
                          tree_range=tree_range,
                          tree_range_label=tree_range_label,
                          tree_account_id=tree_account_id,
                          loan_accounts=loan_accounts,
                          submission_token=uuid4().hex,
                          default_tx_datetime=pk_now().strftime('%Y-%m-%dT%H:%M'),
                          book=book,
                          ledger_version=book['ledger_version'])


