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
    today_date = pk_today()
    opening_cutoff = datetime.combine(today_date, datetime.min.time()) - timedelta(microseconds=1)
    for account in accounts:
        account.today_opening_balance = ledger_balance(account.id, as_of=opening_cutoff)
    
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
                          submission_token=uuid4().hex,
                          default_tx_datetime=pk_now().strftime('%Y-%m-%dT%H:%M'),
                          book=book,
                          ledger_version=book['ledger_version'])


