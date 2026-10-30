"""Owner-defined Accounts workspace and its existing posting paths."""
from models import Account, AccountCategory, AccountTransaction, CashFlowCategory, CashFlowSubcategory, db


def login(client):
    client.post('/login', data={'username': 'Admin', 'password': 'Admin@fbm12345'})


def test_workspace_creates_company_accounts_and_owner_labels(app, client):
    login(client)
    response = client.get('/accounts/')
    assert response.status_code == 200
    assert b'Accounts &amp; cash flow' in response.data
    with app.app_context():
        assert AccountCategory.query.count() == 0
        assert CashFlowCategory.query.count() == 0

    response = client.post('/accounts/company-account/add', data={
        'name': 'Main till', 'category': 'cash', 'opening_balance': '123.45',
        'opening_date': '2026-01-01',
    }, follow_redirects=True)
    assert b'Company account created.' in response.data
    assert b'123.45' in response.data
    with app.app_context():
        account = Account.query.filter_by(name='Main till').one()
        assert account.account_type == 'company'
        assert account.opening_balance_minor == 12345
        assert account.source_category is None
        assert AccountCategory.query.count() == 0

    client.post('/accounts/transaction-categories', data={'kind': 'category', 'name': 'Events', 'direction': 'in'})
    with app.app_context():
        category_id = CashFlowCategory.query.filter_by(name='Events').one().id
    client.post('/accounts/transaction-categories', data={'kind': 'subcategory', 'category_id': category_id, 'name': 'Venue'})
    page = client.get('/accounts/').get_data(as_text=True)
    assert 'Events' in page and 'Venue' in page


def test_manual_receipt_and_expense_use_parent_and_update_balance(app, client):
    login(client)
    client.post('/accounts/company-account/add', data={'name': 'Till', 'category': 'cash', 'opening_balance': '100'})
    with app.app_context():
        account_id = Account.query.filter_by(name='Till').one().id
        income = CashFlowCategory(name='Custom income', direction='in', is_active=True)
        expense = CashFlowCategory(name='Custom expense', direction='out', is_active=True)
        db.session.add_all([income, expense]); db.session.flush()
        db.session.add_all([CashFlowSubcategory(category_id=income.id, name='Income child', is_active=True),
                            CashFlowSubcategory(category_id=expense.id, name='Expense child', is_active=True)])
        db.session.commit()
    common = {'receive_account_id': account_id, 'tx_mode': 'receive', 'receive_from_category': 'other_source',
              'receive_source_label': 'Someone', 'amount': '25', 'method': 'Cash', 'receive_category': 'Custom income'}
    rejected = client.post('/accounts/transactions/new', data={**common, 'receive_subcategory': 'Expense child'}, follow_redirects=True)
    assert b'belonging to the selected category' in rejected.data
    accepted = client.post('/accounts/transactions/new', data={**common, 'receive_subcategory': 'Income child'}, follow_redirects=True)
    assert b'Receive transaction recorded successfully' in accepted.data
    paid = client.post('/accounts/transactions/new', data={'tx_mode': 'pay', 'pay_from_account_id': account_id,
                       'pay_target': 'other_expense', 'amount': '10', 'method': 'Cash', 'payee': 'Shop',
                       'expense_category': 'Custom expense', 'expense_subcategory': 'Expense child'}, follow_redirects=True)
    assert b'Outgoing payment recorded successfully' in paid.data
    with app.app_context():
        account = db.session.get(Account, account_id)
        assert account.balance_minor == 11500
        assert AccountTransaction.query.filter_by(category='Custom income', subcategory='Income child').count() == 1
        assert AccountTransaction.query.filter_by(category='Custom expense', subcategory='Expense child').count() == 1
        assert AccountCategory.query.count() == 0


def test_subcategory_cannot_be_reparented(app, client):
    login(client)
    with app.app_context():
        a = CashFlowCategory(name='Parent A', direction='both', is_active=True)
        b = CashFlowCategory(name='Parent B', direction='both', is_active=True)
        db.session.add_all([a, b]); db.session.flush()
        child = CashFlowSubcategory(name='Child', category_id=a.id, is_active=True)
        db.session.add(child); db.session.commit()
        aid, bid, cid = a.id, b.id, child.id
    response = client.post('/accounts/transaction-categories', data={'kind': 'subcategory', 'id': cid,
                           'category_id': bid, 'name': 'Child'}, follow_redirects=True)
    assert b'cannot be moved' in response.data
    with app.app_context():
        assert db.session.get(CashFlowSubcategory, cid).category_id == aid


def test_invalid_company_account_does_not_create(app, client):
    login(client)
    for data in ({'name': 'Bad', 'category': 'cash', 'opening_balance': '-1'},
                 {'name': 'Bad', 'category': 'invented'},
                 {'name': 'Bad', 'category': 'cash', 'opening_balance': 'NaN'}):
        client.post('/accounts/company-account/add', data=data)
    with app.app_context():
        assert Account.query.filter_by(name='Bad').count() == 0


def test_bank_account_receipt_and_transfer(app, client):
    login(client)
    client.post('/accounts/company-account/add', data={'name': 'Bank A', 'category': 'bank',
                'bank_name': 'Our Bank', 'account_number': '123', 'opening_balance': '50'})
    client.post('/accounts/company-account/add', data={'name': 'Cash A', 'category': 'cash', 'opening_balance': '0'})
    with app.app_context():
        bank = Account.query.filter_by(name='Bank A').one()
        cash = Account.query.filter_by(name='Cash A').one()
        bank_id, cash_id = bank.id, cash.id
        assert bank.bank_name == 'Our Bank' and bank.account_number == '123'
    response = client.post('/accounts/transactions/new', data={'tx_mode': 'receive',
        'receive_account_id': bank_id, 'receive_from_category': 'other_source',
        'receive_source_label': 'Other party', 'method': 'Bank', 'amount': '20'}, follow_redirects=True)
    assert b'Receive transaction recorded successfully' in response.data
    response = client.post('/accounts/transfers/add', data={'from_account': bank_id, 'to_account': cash_id,
                           'amount': '30'}, follow_redirects=True)
    assert b'Transfer completed successfully' in response.data
    with app.app_context():
        assert db.session.get(Account, bank_id).balance_minor == 4000
        assert db.session.get(Account, cash_id).balance_minor == 3000


def test_company_account_creation_requires_login(client):
    response = client.post('/accounts/company-account/add', data={'name': 'Nope', 'category': 'cash'})
    assert response.status_code == 302 and 'login' in response.headers['Location']
