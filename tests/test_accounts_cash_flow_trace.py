"""Smoke-test real postings from source documents to Accounts and Cash Flow.

Each test receives pytest's private SQLite database; no live business data is
opened or changed.  The fixture posts through the same HTTP forms as an owner,
then checks source records, the account journal, the two reports and paisa.
"""
from __future__ import annotations

from collections import Counter
from uuid import uuid4

import pytest
from flask import template_rendered
from sqlalchemy import text

from app.services.account_setup import save_account
from app.services.cash_flow_svc import collect_cash_flow_rows, summarize_cash_flow_rows
from app.services.payments_crud import ledger_balance
from app.services.time_money import pk_now, pk_today
from models import (
    Account, AccountTransaction, Booking, Client, DirectSale, GRN, Material,
    Payment, Service, Supplier, SupplierPayment, db,
)
from utils.money import to_minor


def login(client):
    response = client.post('/login', data={'username': 'Admin', 'password': 'Admin@fbm12345'})
    assert response.status_code in (302, 303)


def _create_account(name, kind, opening):
    account, _ = save_account({
        'name': name, 'account_type': kind, 'opening_balance': opening,
        'account_number': {'cash': '', 'bank': 'PK00000001', 'wallet': '03001234567'}[kind],
    }, actor=None)
    return account


def _post_transaction(client, mode, amount, reference, **fields):
    payload = {
        'entry_source': 'accounts_workspace', 'tx_mode': mode,
        'idempotency_key': uuid4().hex, 'date_posted': pk_now().strftime('%Y-%m-%dT%H:%M'),
        'amount': amount, 'reference_no': reference, 'note': 'Trace smoke ' + reference,
        **fields,
    }
    response = client.post('/accounts/transactions/new', data=payload)
    assert response.status_code == 302, (reference, response.status_code, response.get_data(as_text=True)[-2000:])
    return payload


@pytest.fixture()
def scenario(app, client):
    with app.app_context():
        cash = _create_account('Trace till', 'cash', '2000.11')
        bank = _create_account('Trace bank', 'bank', '1500.22')
        wallet = _create_account('Trace wallet', 'wallet', '700.33')
        customer = Client(code='TRACE-C', name='Trace Customer', opening_balance=1000.00, is_active=True)
        supplier = Supplier(name='Trace Supplier', is_active=True)
        material = Material(code='TRACE-M', name='Trace Cement', unit='Bags', total=0)
        service = Service(name='Trace Rent', code='TRACE-S', unit='Trip')
        db.session.add_all([customer, supplier, material, service]); db.session.commit()
        ids = {'cash': cash.id, 'bank': bank.id, 'wallet': wallet.id,
               'client': customer.id, 'supplier': supplier.id, 'material': material.id}

    login(client)
    today = pk_today().isoformat()
    r = client.post('/grn', data={
        'action': 'add', 'supplier': 'Trace Supplier', 'supplier_id': str(ids['supplier']),
        'mat_name[]': 'Trace Cement', 'qty[]': '10', 'price[]': '29.99',
        'paid_amount': '37.11', 'payment_type': 'Cash', 'payment_account_id': str(ids['cash']),
        'manual_bill_no': 'TRACE-GRN', 'date': today,
    }, follow_redirects=True)
    assert b'GRN added successfully' in r.data, r.get_data(as_text=True)[-2000:]

    # Booking and partially paid credit sale both post a real receipt.  One of
    # each goes to a bank account, not only a cash-method source document.
    for amount, account, method, ref in (
        ('21.23', 'cash', 'Cash', 'TRACE-BK-C'),
        ('31.17', 'bank', 'Bank', 'TRACE-BK-B'),
    ):
        r = client.post('/add_booking', data={
            'client_code': 'TRACE-C', 'material_name[]': 'Trace Rent',
            'qty[]': '1', 'unit_rate[]': '99.99', 'amount': '99.99',
            'paid_amount': amount, 'payment_account_id': str(ids[account]),
            'payment_method': method, 'manual_bill_no': ref, 'date': today,
        }, follow_redirects=True)
        assert b'Booking added successfully' in r.data, (ref, r.get_data(as_text=True)[-2000:])

    for amount, account, method, ref in (
        ('15.91', 'cash', 'Cash', 'TRACE-SALE-C'),
        ('23.47', 'bank', 'Bank', 'TRACE-SALE-B'),
    ):
        r = client.post('/add_direct_sale', data={
            'client_name': 'Trace Customer', 'client_code': 'TRACE-C', 'driver_name': 'Trace Driver',
            'category': 'Credit Customer', 'product_name[]': 'Trace Cement',
            'qty[]': '1', 'unit_rate[]': '59.99', 'paid_amount': amount,
            'payment_account_id': str(ids[account]), 'payment_method': method,
            'manual_bill_no': ref, 'date': today,
        }, follow_redirects=True)
        assert b'Direct sale added successfully' in r.data, (ref, r.get_data(as_text=True)[-2000:])

    for amount, account, ref in (
        ('101.09', 'cash', 'TRACE-RC-C'), ('102.17', 'bank', 'TRACE-RC-B'),
        ('103.27', 'wallet', 'TRACE-RC-W'),
    ):
        _post_transaction(client, 'receive', amount, ref, receive_source='client',
                          client_code='TRACE-C', receive_account_id=str(ids[account]))
    for amount, account, ref in (('3.49', 'cash', 'TRACE-RF-C'), ('4.39', 'bank', 'TRACE-RF-B')):
        _post_transaction(client, 'pay', amount, ref, pay_target='client_refund',
                          refund_client_code='TRACE-C', pay_from_account_id=str(ids[account]))
    _post_transaction(client, 'pay', '7.53', 'TRACE-SP-B', pay_target='supplier',
                      supplier_id=str(ids['supplier']), pay_from_account_id=str(ids['bank']))
    _post_transaction(client, 'pay', '8.79', 'TRACE-EX-C', pay_target='expense', party='Trace Repair',
                      pay_from_account_id=str(ids['cash']))
    _post_transaction(client, 'pay', '9.81', 'TRACE-EX-B', pay_target='expense', party='Trace Fuel',
                      pay_from_account_id=str(ids['bank']))
    _post_transaction(client, 'receive', '11.13', 'TRACE-OTHER', receive_source='other',
                      party='Trace Other', receive_account_id=str(ids['cash']))
    _post_transaction(client, 'transfer', '12.35', 'TRACE-XFER-CB',
                      transfer_from_account_id=str(ids['cash']), transfer_to_account_id=str(ids['bank']))
    _post_transaction(client, 'transfer', '13.57', 'TRACE-XFER-BW',
                      transfer_from_account_id=str(ids['bank']), transfer_to_account_id=str(ids['wallet']))
    ids['day'] = today
    return ids


# Expected amounts are deliberately distinct so a duplicate and an omission
# cannot cancel each other in an aggregate.  Values are integer paisa.
MOVEMENTS = {
    3711: ('out', 'cash', None),  # supplier payment created by GRN
    2123: ('in', 'cash', None),   # cash booking
    3117: ('in', 'bank', None),   # bank booking
    1591: ('in', 'cash', None),   # cash credit-sale payment
    2347: ('in', 'bank', None),   # bank credit-sale payment
    10109: ('in', 'cash', None),
    10217: ('in', 'bank', None),
    10327: ('in', 'wallet', None),
    349: ('out', 'cash', None),
    439: ('out', 'bank', None),
    753: ('out', 'bank', None),
    879: ('out', 'cash', None),
    981: ('out', 'bank', None),
    1113: ('in', 'cash', None),
    1235: ('transfer', 'cash', 'bank'),
    1357: ('transfer', 'bank', 'wallet'),
}


def test_source_documents_and_independent_account_balances(scenario, app, client):
    ids = scenario
    with app.app_context():
        grn = GRN.query.filter(GRN.manual_bill_no.like('%TRACE-GRN%')).one()
        grn_payment = SupplierPayment.query.filter_by(source_type='GRN', source_id=grn.id).one()
        assert grn_payment.amount_minor == 3711 and grn_payment.payment_account_id == ids['cash']
        assert len(Booking.query.filter(Booking.manual_bill_no.like('%TRACE-BK-%')).all()) == 2
        assert len(DirectSale.query.filter(DirectSale.manual_bill_no.like('%TRACE-SALE-%')).all()) == 2
        assert Counter((p.method, p.payment_type) for p in Payment.query.all()) == {
            ('Cash', 'Receipt'): 1, ('Bank', 'Receipt'): 1, ('Online', 'Receipt'): 1,
            ('Cash', 'Refund'): 1, ('Bank', 'Refund'): 1,
        }

        # Source records must have exactly one active posting, not merely a
        # source total that happens to agree after cancellation of duplicates.
        for source in [grn_payment, *SupplierPayment.query.filter(SupplierPayment.id != grn_payment.id).all(),
                       *Booking.query.all(), *DirectSale.query.all(), *Payment.query.all()]:
            txs = AccountTransaction.query.filter_by(source_type=source.__class__.__name__,
                                                     source_id=source.id, is_void=False).all()
            assert len(txs) == 1, (source.__class__.__name__, source.id, len(txs))

        expected = {'cash': 200011, 'bank': 150022, 'wallet': 70033}
        for minor, (direction, account, destination) in MOVEMENTS.items():
            if direction == 'in': expected[account] += minor
            elif direction == 'out': expected[account] -= minor
            else:
                expected[account] -= minor
                expected[destination] += minor
        for key in ('cash', 'bank', 'wallet'):
            account_id = ids[key]
            # Raw SQL independent of the page's collector and ledger helper.
            row = db.session.execute(text('''
                SELECT a.opening_balance_minor +
                  COALESCE((SELECT SUM(COALESCE(t.amount_minor, ROUND(t.amount * 100)))
                    FROM account_transaction t WHERE t.is_void = 0 AND t.to_account_id = a.id), 0) -
                  COALESCE((SELECT SUM(COALESCE(t.amount_minor, ROUND(t.amount * 100)))
                    FROM account_transaction t WHERE t.is_void = 0 AND t.from_account_id = a.id), 0),
                  a.balance_minor
                FROM account a WHERE a.id = :id
            '''), {'id': account_id}).one()
            assert row[0] == row[1] == expected[key]
            assert to_minor(ledger_balance(account_id)) == expected[key]
            page = client.get(f"/accounts/ledger/{account_id}?from={ids['day']}&to={ids['day']}")
            assert page.status_code == 200
            assert f'{expected[key] / 100:.2f}' in page.get_data(as_text=True)


def test_every_posted_movement_appears_once_in_cash_flow(scenario, app):
    ids = scenario
    with app.app_context():
        rows = collect_cash_flow_rows(ids['day'], ids['day'], include_voided=False)
        by_amount = {}
        for row in rows:
            by_amount.setdefault(to_minor(row['amount']), []).append(row)
        errors = []
        for minor, (direction, account, destination) in MOVEMENTS.items():
            matches = by_amount.get(minor, [])
            if len(matches) != 1:
                errors.append(f'{minor} paisa: expected one row, found {len(matches)}')
                continue
            row = matches[0]
            if (row['type'], row['account_id'], row.get('account_to_id')) != (
                direction, ids[account], ids.get(destination) if destination else None
            ):
                errors.append(f'{minor} paisa: wrong direction/account {row}')
        extras = set(by_amount) - set(MOVEMENTS)
        assert not errors and not extras, f'errors={errors}; unexpected amounts={extras}'
        assert len(rows) == len(MOVEMENTS)
        summary = summarize_cash_flow_rows(rows)
        assert to_minor(summary['total_cash_in']) == sum(a for a, (d, _, _) in MOVEMENTS.items() if d == 'in')
        assert to_minor(summary['total_cash_out']) == sum(a for a, (d, _, _) in MOVEMENTS.items() if d == 'out')
        assert to_minor(summary['total_transfer_in']) == to_minor(summary['total_transfer_out']) == 2592


def test_both_cash_flow_screens_show_precise_amounts_and_sources(scenario, client):
    day = scenario['day']
    workspace = client.get(f'/accounts/?from_date={day}&to_date={day}')
    assert workspace.status_code == 200
    html = workspace.get_data(as_text=True)
    for ref in ('TRACE-BK-C', 'TRACE-BK-B', 'TRACE-SALE-C', 'TRACE-SALE-B',
                'TRACE-RC-B', 'TRACE-RC-W', 'TRACE-RF-B', 'TRACE-EX-C', 'TRACE-XFER-CB'):
        assert ref in html, ref
    for value in ('21.23', '31.17', '23.47', '102.17', '103.27', '4.39', '12.35'):
        assert value in html, value

    report = client.get(f'/cash_flow?from_date={day}&to_date={day}&ft_preset=today')
    assert report.status_code == 200
    html = report.get_data(as_text=True)
    for ref in ('TRACE-BK-C', 'TRACE-BK-B', 'TRACE-SALE-C', 'TRACE-SALE-B',
                'TRACE-RC-B', 'TRACE-RC-W', 'TRACE-RF-B', 'TRACE-EX-C', 'TRACE-XFER-CB'):
        assert ref in html, ref
    for value in ('21.23', '31.17', '23.47', '102.17', '103.27', '4.39', '12.35'):
        assert value in html, value


def test_zero_discount_only_is_not_a_cash_flow_movement(app, client):
    with app.app_context():
        customer = Client(code='DISC-C', name='Discount Customer', opening_balance=500, is_active=True)
        db.session.add(customer); db.session.commit()
    login(client)
    _post_transaction(client, 'receive', '0', 'DISC-ONLY', receive_source='client',
                      client_code='DISC-C', receive_account_id='', discount='19.87',
                      discount_reason='Discount only')
    with app.app_context():
        day = pk_today().isoformat()
        assert Payment.query.one().amount_minor == 0
        assert collect_cash_flow_rows(day, day, include_voided=False) == []


def _render_context(app, client, url, name):
    captured = []
    def record(sender, template, context, **extra):
        if template.name == name:
            captured.append(context.copy())
    template_rendered.connect(record, app)
    try:
        response = client.get(url)
    finally:
        template_rendered.disconnect(record, app)
    assert response.status_code == 200
    assert captured, name
    return captured[-1]


def test_report_opening_closing_and_account_filter_reconcile(scenario, app, client):
    ids = scenario
    day = ids['day']
    expected = {'cash': 200011, 'bank': 150022, 'wallet': 70033}
    for amount, (direction, account, destination) in MOVEMENTS.items():
        if direction == 'in': expected[account] += amount
        elif direction == 'out': expected[account] -= amount
        else:
            expected[account] -= amount
            expected[destination] += amount
    context = _render_context(app, client, f'/cash_flow?from_date={day}&to_date={day}', 'cash_flow.html')
    assert to_minor(context['opening_balance']) == 420066
    assert to_minor(context['closing_balance']) == sum(expected.values())
    assert to_minor(context['total_cash_in']) == sum(a for a, (d, _, _) in MOVEMENTS.items() if d == 'in')
    assert to_minor(context['total_cash_out']) == sum(a for a, (d, _, _) in MOVEMENTS.items() if d == 'out')
    assert to_minor(context['account_activity'][ids['bank']]['in']) == 16916
    assert to_minor(context['account_activity'][ids['bank']]['out']) == 3530
    assert to_minor(context['account_activity'][ids['cash']]['in']) == 14936
    assert to_minor(context['account_activity'][ids['cash']]['out']) == 6174
    bank_breakdown = context['breakdown_account']['Trace bank']
    assert to_minor(bank_breakdown['in']) == 15681
    assert to_minor(bank_breakdown['transfer_in']) == 1235
    assert to_minor(bank_breakdown['out']) == 2173
    assert to_minor(bank_breakdown['transfer_out']) == 1357
    assert to_minor(context['ft']['total_in']) == to_minor(context['total_cash_in'])
    assert to_minor(context['ft']['total_out']) == to_minor(context['total_cash_out'])
    bank_tracker = _render_context(app, client,
        f"/cash_flow?from_date={day}&to_date={day}&ft_preset=today&ft_account={ids['bank']}", 'cash_flow.html')['ft']
    assert to_minor(bank_tracker['total_in']) == 16916
    assert to_minor(bank_tracker['total_out']) == 3530
    assert list(bank_tracker['breakdown_account']) == ['Trace bank']
    filtered = _render_context(app, client,
        f"/cash_flow?from_date={day}&to_date={day}&account_id={ids['bank']}", 'cash_flow.html')
    assert to_minor(filtered['opening_balance']) == 150022
    assert to_minor(filtered['closing_balance']) == expected['bank']


def test_external_loan_receipt_counts_as_company_money_in(app, client):
    with app.app_context():
        cash = _create_account('Loan-receiving till', 'cash', '10.11')
        source = Account(name='Private lender', account_type='loan', source_category='Loan', balance=0, is_active=True)
        db.session.add(source); db.session.commit()
        cash_id, source_id = cash.id, source.id
    login(client)
    _post_transaction(client, 'receive', '7.29', 'TRACE-LOAN', receive_source='account',
                      receive_group='Loan', receive_from_account_id=str(source_id), receive_account_id=str(cash_id))
    with app.app_context():
        day = pk_today().isoformat()
        rows = collect_cash_flow_rows(day, day, include_voided=False)
        assert len(rows) == 1
        assert (rows[0]['type'], rows[0]['account_id'], to_minor(rows[0]['amount'])) == ('in', cash_id, 729)
        assert to_minor(summarize_cash_flow_rows(rows)['total_cash_in']) == 729
        assert db.session.get(Account, cash_id).balance_minor == 1740


def test_reconciliation_adjustment_is_visible_in_cash_flow(app, client):
    from app.services.payments_crud import reconcile_account
    with app.app_context():
        cash = _create_account('Reconciled till', 'cash', '10.11')
        db.session.commit()
        rec = reconcile_account(account_id=cash.id, actual_balance='9.87', note='Count shortage')
        db.session.commit()
        tx = db.session.get(AccountTransaction, rec.adjustment_transaction_id)
        day = pk_today().isoformat()
        rows = [r for r in collect_cash_flow_rows(day, day, include_voided=False) if r['tx_id'] == tx.id]
        assert len(rows) == 1
        assert (rows[0]['type'], to_minor(rows[0]['amount']), rows[0]['source_id']) == ('out', 24, rec.id)
        assert cash.balance_minor == 987
    login(client)
    assert '0.24' in client.get(f'/accounts/?from_date={day}&to_date={day}').get_data(as_text=True)


def test_source_receipts_cannot_be_edited_or_deleted_separately(scenario, app, client):
    # A document receipt cannot be reversed on its own while its Booking/Sale
    # remains active; that would split the source record from the account.
    with app.app_context():
        txs = AccountTransaction.query.filter(AccountTransaction.source_type.in_(['Booking', 'DirectSale'])).all()
        tx_ids = [tx.id for tx in txs]
        before = {a.id: a.balance_minor for a in Account.query.filter(Account.id.in_([
            scenario['cash'], scenario['bank'], scenario['wallet']])).all()}
    for tx_id in tx_ids:
        response = client.post(f'/accounts/transactions/{tx_id}/delete', follow_redirects=True)
        assert b'source' in response.data.lower() or b'booking' in response.data.lower() or b'sale' in response.data.lower()
        response = client.post(f'/accounts/transactions/{tx_id}/edit', data={'amount': '0.01'}, follow_redirects=True)
        assert b'source' in response.data.lower() or b'booking' in response.data.lower() or b'sale' in response.data.lower()
    with app.app_context():
        assert all(not db.session.get(AccountTransaction, tx_id).is_void for tx_id in tx_ids)
        assert {a.id: a.balance_minor for a in Account.query.filter(Account.id.in_(list(before))).all()} == before


def test_client_and_supplier_payment_void_restore_follow_source(scenario, app, client):
    ids = scenario
    with app.app_context():
        receipt = Payment.query.filter(Payment.manual_bill_no.like('%TRACE-RC-B%')).one()
        supplier = SupplierPayment.query.filter(SupplierPayment.manual_bill_no.like('%TRACE-SP-B%')).one()
        receipt_id, supplier_id = receipt.id, supplier.id
        original = db.session.get(Account, ids['bank']).balance_minor
    cases = [
        (receipt_id, f'/accounts/payments/clients/void/{receipt_id}',
         f'/accounts/payments/clients/restore/{receipt_id}', 10217, -10217, Payment),
        (supplier_id, f'/accounts/payments/suppliers/{supplier_id}/delete',
         f'/accounts/payments/suppliers/{supplier_id}/restore', 753, 753, SupplierPayment),
    ]
    for source_id, delete_url, restore_url, amount, balance_delta, model in cases:
        response = client.post(delete_url, follow_redirects=True)
        assert response.status_code == 200
        with app.app_context():
            assert db.session.get(model, source_id).is_void is True
            assert db.session.get(Account, ids['bank']).balance_minor == original + balance_delta
            rows = collect_cash_flow_rows(ids['day'], ids['day'], include_voided=False)
            assert not [r for r in rows if to_minor(r['amount']) == amount]
            voided = [r for r in collect_cash_flow_rows(ids['day'], ids['day'], include_voided=True)
                      if to_minor(r['amount']) == amount]
            assert len(voided) == 1 and voided[0]['status'] == 'voided' and voided[0]['source_id'] == source_id
            assert to_minor(summarize_cash_flow_rows(voided)['total_cash_in']) == 0
            assert to_minor(summarize_cash_flow_rows(voided)['total_cash_out']) == 0
        report = client.get(f"/cash_flow?from_date={ids['day']}&to_date={ids['day']}&status=voided&export_csv=1")
        import csv, io
        exported = list(csv.DictReader(io.StringIO(report.get_data(as_text=True))))
        assert exported and all(r['Status'] == 'voided' for r in exported)
        assert all(not r['Received'] and not r['Spent'] and not r['Transfer'] for r in exported)
        response = client.post(restore_url, follow_redirects=True)
        assert response.status_code == 200
        with app.app_context():
            assert db.session.get(model, source_id).is_void is False
            assert db.session.get(Account, ids['bank']).balance_minor == original
            rows = [r for r in collect_cash_flow_rows(ids['day'], ids['day'], include_voided=False)
                    if to_minor(r['amount']) == amount]
            assert len(rows) == 1 and rows[0]['source_id'] == source_id and rows[0]['tx_id']


def test_legacy_booking_void_restore_follows_the_receipt(scenario, app, client):
    from app.services.void_rebuild import _set_booking_void_state
    ids = scenario
    with app.app_context():
        booking = Booking.query.filter(Booking.manual_bill_no.like('%TRACE-BK-B%')).one()
        booking_id = booking.id
        before = db.session.get(Account, ids['bank']).balance_minor
        assert _set_booking_void_state(booking, True)
        db.session.commit()
        assert db.session.get(Account, ids['bank']).balance_minor == before - 3117
        assert not [r for r in collect_cash_flow_rows(ids['day'], ids['day'], include_voided=False)
                    if r['source_type'] == 'BOOKING' and r['source_id'] == booking_id]
        voided = [r for r in collect_cash_flow_rows(ids['day'], ids['day'], include_voided=True)
                  if r['source_type'] == 'BOOKING' and r['source_id'] == booking_id]
        assert len(voided) == 1 and voided[0]['status'] == 'voided' and voided[0]['tx_id']
    response = client.post(f'/unvoid_transaction/Booking/{booking_id}', follow_redirects=True)
    assert response.status_code == 200
    with app.app_context():
        assert db.session.get(Account, ids['bank']).balance_minor == before
        rows = [r for r in collect_cash_flow_rows(ids['day'], ids['day'], include_voided=False)
                if r['source_type'] == 'BOOKING' and r['source_id'] == booking_id]
        assert len(rows) == 1 and to_minor(rows[0]['amount']) == 3117 and rows[0]['tx_id']


def test_account_ledger_uses_authoritative_paisa_if_float_columns_drift(app, client):
    with app.app_context():
        account = _create_account('Precise till', 'cash', '10.01')
        db.session.commit()
        account_id = account.id
    login(client)
    _post_transaction(client, 'receive', '3.07', 'TRACE-DRIFT', receive_source='other',
                      party='Precision test', receive_account_id=str(account_id))
    with app.app_context():
        tx = AccountTransaction.query.filter_by(reference_no='TRACE-DRIFT').one()
        # Simulate legacy/import drift without the model's synchronising hooks.
        db.session.execute(text('UPDATE account_transaction SET amount = 999.99 WHERE id = :id'), {'id': tx.id})
        db.session.execute(text('UPDATE account SET balance = 888.88 WHERE id = :id'), {'id': account_id})
        db.session.commit()
    day = pk_today().isoformat()
    page = client.get(f'/accounts/ledger/{account_id}?from={day}&to={day}').get_data(as_text=True)
    assert '3.07' in page and '13.08' in page
    assert '999.99' not in page and '888.88' not in page
    with app.app_context():
        rows = collect_cash_flow_rows(day, day, include_voided=False)
        assert len(rows) == 1 and to_minor(rows[0]['amount']) == 307
    context = _render_context(app, client, f'/cash_flow?from_date={day}&to_date={day}', 'cash_flow.html')
    assert '13.08' in context['account_options'][0]['label']
    assert '888.88' not in context['account_options'][0]['label']


def test_filtered_workspace_net_includes_transfer_and_selected_balance(scenario, app, client):
    ids = scenario
    context = _render_context(app, client,
        f"/accounts/?from_date={ids['day']}&to_date={ids['day']}&account_id={ids['bank']}",
        'accounts/workspace.html')
    with app.app_context():
        balance = db.session.get(Account, ids['bank']).balance_minor
    assert to_minor(context['cf_active_balance']) == balance
    assert to_minor(context['cf_net']) == balance - 150022
    assert to_minor(context['cf_summary']['total_transfer_in']) == 1235
    assert to_minor(context['cf_summary']['total_transfer_out']) == 1357


def test_physical_count_rolls_cash_and_bank_forward_separately(app, client):
    from datetime import datetime, timedelta
    from models import CashFlowDifferenceAdjustment
    from app.services.cash_flow_svc import save_manual_cash_flow_entry
    today = pk_today()
    opening_day = today - timedelta(days=4)
    count_day = today - timedelta(days=2)
    movement_day = today - timedelta(days=1)
    with app.app_context():
        cash = _create_account('Historical till', 'cash', '100.11')
        bank = _create_account('Historical bank', 'bank', '200.22')
        cash.opening_balance_date = bank.opening_balance_date = datetime.combine(opening_day, datetime.min.time())
        db.session.commit()
        for direction, amount, source, dest in (
            ('in', '5.01', cash, None), ('in', '3.02', bank, None),
            ('transfer', '2.03', cash, bank),
        ):
            save_manual_cash_flow_entry(
                direction=direction, amount=amount, account_id=source.id,
                destination_account_id=dest.id if dest else None,
                date_posted=datetime.combine(movement_day, datetime.min.time()).replace(hour=12),
                category_name='Historical', create_missing=True,
            )
        db.session.add(CashFlowDifferenceAdjustment(
            adjustment_date=count_day, amount=0, difference=0,
            physical_cash_available=111.11, calculated_closing=100.11,
        ))
        db.session.commit()
        bank_id = bank.id
    login(client)
    context = _render_context(app, client,
        f'/cash_flow?from_date={today}&to_date={today}', 'cash_flow.html')
    assert to_minor(context['opening_balance']) == 31936   # count + bank at count + net after count
    assert to_minor(context['closing_balance']) == 31936
    assert to_minor(context['reconciliation_closing']) == 11409  # count + cash in - cash→bank
    # A report spanning the count must disclose its baseline reset and end
    # at the same amount as a report starting after the count.
    spanning = _render_context(app, client,
        f'/cash_flow?from_date={opening_day}&to_date={today}', 'cash_flow.html')
    assert to_minor(spanning['closing_balance']) == 31936
    assert to_minor(spanning['reconciliation_closing']) == 11409
    assert [(entry['date'], entry['delta_minor']) for entry in spanning['count_carry_adjustments']] == [(count_day, 1100)]
    selected = _render_context(app, client,
        f'/cash_flow?from_date={today}&to_date={today}&account_id={bank_id}', 'cash_flow.html')
    assert to_minor(selected['opening_balance']) == to_minor(selected['closing_balance']) == 20527


def test_account_opened_mid_period_is_separate_from_income(app, client):
    from datetime import datetime, timedelta
    from app.services.cash_flow_svc import save_manual_cash_flow_entry
    today = pk_today()
    first = today - timedelta(days=3)
    opened = today - timedelta(days=1)
    with app.app_context():
        cash = _create_account('Old till', 'cash', '100.11')
        bank = _create_account('New bank', 'bank', '200.22')
        cash.opening_balance_date = datetime.combine(first, datetime.min.time())
        bank.opening_balance_date = datetime.combine(opened, datetime.min.time())
        db.session.commit()
        save_manual_cash_flow_entry(direction='in', amount='4.05', account_id=bank.id,
            date_posted=datetime.combine(opened, datetime.min.time()).replace(hour=12),
            category_name='New account receipt', create_missing=True)
        db.session.commit()
        bank_id = bank.id
    login(client)
    context = _render_context(app, client,
        f'/cash_flow?from_date={first}&to_date={today}', 'cash_flow.html')
    assert to_minor(context['opening_balance']) == 10011
    assert to_minor(context['total_cash_in']) == 405
    assert to_minor(context['closing_balance']) == 30438
    assert [(r['account_name'], r['amount_minor']) for r in context['opening_additions']] == [('New bank', 20022)]
    assert to_minor(context['reconciliation_closing']) == 10011
    selected = _render_context(app, client,
        f'/cash_flow?from_date={first}&to_date={today}&account_id={bank_id}', 'cash_flow.html')
    assert to_minor(selected['opening_balance']) == 0
    assert to_minor(selected['closing_balance']) == 20427
    ledger = _render_context(app, client,
        f'/accounts/ledger/{bank_id}?from={first}&to={today}', 'accounts/account_ledger.html')
    assert to_minor(ledger['opening_balance']) == 0
    assert to_minor(ledger['page_rows'][0]['running']) == 20427


def test_source_document_hard_deletion_reverses_receipt_and_cash_flow(scenario, app, client):
    ids = scenario
    with app.app_context():
        booking_id = Booking.query.filter(Booking.manual_bill_no.like('%TRACE-BK-C%')).one().id
        sale_id = DirectSale.query.filter(DirectSale.manual_bill_no.like('%TRACE-SALE-C%')).one().id
        before = db.session.get(Account, ids['cash']).balance_minor
    for kind, source_id, amount in (('Booking', booking_id, 2123), ('DirectSale', sale_id, 1591)):
        response = client.post(f'/delete_transaction/{kind}/{source_id}', follow_redirects=True)
        assert response.status_code == 200 and f'{kind} deleted'.encode() in response.data
        before -= amount
        with app.app_context():
            assert db.session.get(Account, ids['cash']).balance_minor == before
            assert not [r for r in collect_cash_flow_rows(ids['day'], ids['day'], include_voided=False)
                        if to_minor(r['amount']) == amount]


def test_driver_payment_trace_void_and_restore(app, client):
    from models import DeliveryPerson, DeliveryPersonPayment
    with app.app_context():
        bank = _create_account('Driver bank', 'bank', '100.09')
        person = DeliveryPerson(name='Trace Driver', opening_balance=88.88, is_active=True)
        db.session.add(person); db.session.commit()
        bank_id, driver_id = bank.id, person.id
    login(client)
    day = pk_today().isoformat()
    response = client.post(f'/delivery_person_ledger/{driver_id}/pay', data={
        'paid_amount': '17.31', 'method': 'Bank', 'payment_account_id': str(bank_id),
        'reference': 'TRACE-DRIVER', 'date': day, 'idempotency_key': uuid4().hex,
    }, follow_redirects=True)
    assert b'Driver payment recorded' in response.data
    with app.app_context():
        source = DeliveryPersonPayment.query.one()
        source_id = source.id
        tx = AccountTransaction.query.filter_by(source_type='DeliveryPersonPayment', source_id=source_id,
                                                is_void=False).one()
        tx_id = tx.id
        assert tx.amount_minor == 1731 and db.session.get(Account, bank_id).balance_minor == 8278
        rows = [r for r in collect_cash_flow_rows(day, day, include_voided=False) if r['tx_id'] == tx_id]
        assert len(rows) == 1
        assert (rows[0]['type'], rows[0]['source_id'], rows[0]['party_name'], rows[0]['reference']) == (
            'out', source_id, 'Trace Driver', 'TRACE-DRIVER')
    # It is unsafe to edit or reverse only the mirror from an account ledger.
    client.post(f'/accounts/transactions/{tx_id}/delete', follow_redirects=True)
    client.post(f'/accounts/transactions/{tx_id}/edit', data={'amount': '0.01'}, follow_redirects=True)
    with app.app_context():
        assert not db.session.get(AccountTransaction, tx_id).is_void
        assert db.session.get(Account, bank_id).balance_minor == 8278
    response = client.post(f'/delivery_person_payments/{source_id}/void', follow_redirects=True)
    assert b'payment reversed' in response.data.lower()
    with app.app_context():
        assert db.session.get(Account, bank_id).balance_minor == 10009
        assert not [r for r in collect_cash_flow_rows(day, day, include_voided=False) if r['source_id'] == source_id]
    response = client.post(f'/delivery_person_payments/{source_id}/restore', follow_redirects=True)
    assert b'payment restored' in response.data.lower()
    with app.app_context():
        assert db.session.get(Account, bank_id).balance_minor == 8278
        rows = [r for r in collect_cash_flow_rows(day, day, include_voided=False)
                if r['source_type'] == 'DRIVER_PAYMENT' and r['source_id'] == source_id]
        assert len(rows) == 1 and to_minor(rows[0]['amount']) == 1731


def test_independent_truth_engine_reconciles_all_three_accounts(scenario, app):
    from tools.predator_truth_engine import accounts_section, connect
    uri = app.config['SQLALCHEMY_DATABASE_URI']
    assert uri.startswith('sqlite:///')
    con = connect(uri.removeprefix('sqlite:///'))
    try:
        truth = accounts_section(con)
    finally:
        con.close()
    assert truth['ok'], truth['findings']
    assert truth['summary']['account_count'] == 3
    assert truth['summary']['diverged_accounts'] == 0


def test_cash_flow_csv_and_pdf_preserve_each_source_and_paisa(scenario, client):
    import csv
    import io
    from pypdf import PdfReader
    day = scenario['day']
    csv_response = client.get(f'/cash_flow?from_date={day}&to_date={day}&export_csv=1')
    assert csv_response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(csv_response.get_data(as_text=True))))
    assert len(rows) == len(MOVEMENTS)
    assert Counter(to_minor(row['Amount']) for row in rows) == Counter(MOVEMENTS.keys())
    for reference in ('TRACE-BK-B', 'TRACE-SALE-B', 'TRACE-RC-B', 'TRACE-RF-B', 'TRACE-XFER-CB'):
        assert any(reference in row['Reference'] for row in rows), reference
    tracker = client.get('/financial_tracker?preset=today&export_csv=1')
    tracker_rows = list(csv.DictReader(io.StringIO(tracker.get_data(as_text=True))))
    bank_booking = next(row for row in tracker_rows if 'TRACE-BK-B' in row['Reference'])
    assert bank_booking['In'] == '31.17' and bank_booking['Source']
    pdf = client.get(f'/cash_flow?from_date={day}&to_date={day}&export_pdf=1')
    assert pdf.status_code == 200 and pdf.data[:4] == b'%PDF'
    text_content = '\n'.join(page.extract_text() or '' for page in PdfReader(io.BytesIO(pdf.data)).pages)
    for ref in ('TRACE-BK-B', 'TRACE-SALE-B', 'TRACE-RC-B', 'TRACE-RF-B'):
        assert ref in text_content, ref
    for amount in ('31.17', '23.47', '102.17', '4.39'):
        assert amount in text_content, amount


def test_financial_tracker_payment_method_filters_follow_the_source(scenario, client, app):
    day = scenario['day']
    context = _render_context(app, client, f'/cash_flow?from_date={day}&to_date={day}&ft_preset=today&ft_method=bank', 'cash_flow.html')
    bank_amounts = {to_minor(row['amount']) for row in context['ft']['rows']}
    assert {3117, 2347, 10217, 439, 753, 981} <= bank_amounts
    assert 10327 not in bank_amounts and 10109 not in bank_amounts
    context = _render_context(app, client, f'/cash_flow?from_date={day}&to_date={day}&ft_preset=today&ft_method=online', 'cash_flow.html')
    online_amounts = {to_minor(row['amount']) for row in context['ft']['rows']}
    assert 10327 in online_amounts
    assert 10217 not in online_amounts and 10109 not in online_amounts


def test_multiple_counts_and_midperiod_cash_opening_are_disclosed(app, client):
    from datetime import datetime, timedelta
    from models import CashFlowDifferenceAdjustment
    from app.services.cash_flow_svc import save_manual_cash_flow_entry
    today = pk_today()
    first, count1, opened, legacy, count2 = [today - timedelta(days=n) for n in (5, 4, 3, 2, 1)]
    with app.app_context():
        cash = _create_account('Old counted till', 'cash', '100.11')
        bank = _create_account('Counted bank', 'bank', '50.00')
        new_cash = _create_account('New counted till', 'cash', '20.19')
        cash.opening_balance_date = bank.opening_balance_date = datetime.combine(first, datetime.min.time())
        new_cash.opening_balance_date = datetime.combine(opened, datetime.min.time())
        db.session.commit()
        save_manual_cash_flow_entry(direction='in', amount='3.07', account_id=new_cash.id,
            date_posted=datetime.combine(opened, datetime.min.time()).replace(hour=12),
            category_name='Period receipt', reference='COUNT-RECEIPT', create_missing=True)
        db.session.add_all([
            CashFlowDifferenceAdjustment(adjustment_date=count1, physical_cash_available=101.12),
            CashFlowDifferenceAdjustment(adjustment_date=legacy, amount=2.05),
            CashFlowDifferenceAdjustment(adjustment_date=count2, physical_cash_available=145.50),
        ])
        db.session.commit()
    login(client)
    spanning = _render_context(app, client,
        f'/cash_flow?from_date={first}&to_date={today}', 'cash_flow.html')
    assert to_minor(spanning['opening_balance']) == 15011
    assert to_minor(spanning['closing_balance']) == 19550
    assert to_minor(spanning['reconciliation_closing']) == 14550
    assert [e['delta_minor'] for e in spanning['count_carry_adjustments']] == [101, -205, 2317]
    assert to_minor(spanning['rows'][0]['running_balance']) == 17438
    after = _render_context(app, client, f'/cash_flow?from_date={today}&to_date={today}', 'cash_flow.html')
    assert to_minor(after['opening_balance']) == to_minor(spanning['closing_balance'])


def test_legacy_manual_transfer_void_and_restore_stays_in_sync(app, client):
    from app.services.cash_flow_svc import save_manual_cash_flow_entry
    from models import CashFlowEntry
    day = pk_today().isoformat()
    with app.app_context():
        cash = _create_account('Legacy transfer till', 'cash', '100.11')
        bank = _create_account('Legacy transfer bank', 'bank', '50.22')
        entry, _ = save_manual_cash_flow_entry(direction='transfer', amount='7.19',
            account_id=cash.id, destination_account_id=bank.id,
            reference='LEGACY-TRANSFER', create_missing=True)
        db.session.commit()
        cash_id, bank_id, entry_id, tx_id = cash.id, bank.id, entry.id, entry.account_tx_id
        assert (cash.balance_minor, bank.balance_minor) == (9292, 5741)
        assert len(collect_cash_flow_rows(day, day, include_voided=False)) == 1
    login(client)
    response = client.post(f'/accounts/transactions/{tx_id}/delete', follow_redirects=True)
    assert b'reversed' in response.data.lower()
    with app.app_context():
        assert db.session.get(CashFlowEntry, entry_id).is_void
        assert db.session.get(AccountTransaction, tx_id).is_void
        assert (db.session.get(Account, cash_id).balance_minor, db.session.get(Account, bank_id).balance_minor) == (10011, 5022)
        assert collect_cash_flow_rows(day, day, include_voided=False) == []
        voided = collect_cash_flow_rows(day, day, include_voided=True)
        assert len(voided) == 1 and voided[0]['status'] == 'voided'
    response = client.post(f'/accounts/transactions/{tx_id}/restore', follow_redirects=True)
    assert b'restored' in response.data.lower()
    with app.app_context():
        assert not db.session.get(CashFlowEntry, entry_id).is_void
        assert not db.session.get(AccountTransaction, tx_id).is_void
        assert (db.session.get(Account, cash_id).balance_minor, db.session.get(Account, bank_id).balance_minor) == (9292, 5741)
        rows = collect_cash_flow_rows(day, day, include_voided=False)
        assert len(rows) == 1 and to_minor(rows[0]['amount']) == 719


def test_daily_reconciliation_shows_paisa_and_only_counted_differences(scenario, client):
    day, bank_id = scenario['day'], scenario['bank']
    response = client.post('/daily_reconciliation', data={
        'action': 'save_count', 'day': day, 'account_id': bank_id, 'counted': '1634.07',
    }, headers={'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json'})
    assert response.status_code == 200
    payload = response.get_json()
    bank = next(p for p in payload['positions'] if p['account_id'] == bank_id)
    assert to_minor(bank['expected_closing']) == 163408
    assert to_minor(bank['difference']) == -1
    assert to_minor(payload['net_difference']) == -1  # other accounts are not counted yet
    page = client.get(f'/daily_reconciliation?day={day}').get_data(as_text=True)
    assert '1,634.08' in page and '-0.01' in page
    assert 'minimumFractionDigits: 2, maximumFractionDigits: 2' in page


def test_legacy_transfer_mutations_respect_reconciled_periods(app, client):
    from app.services.cash_flow_svc import save_manual_cash_flow_entry
    from models import AccountReconciliation, CashFlowEntry
    with app.app_context():
        cash = _create_account('Locked legacy cash', 'cash', '100.00')
        bank = _create_account('Locked legacy bank', 'bank', '50.00')
        a, _ = save_manual_cash_flow_entry(direction='transfer', amount='3.11',
            account_id=cash.id, destination_account_id=bank.id, create_missing=True)
        b, _ = save_manual_cash_flow_entry(direction='transfer', amount='4.22',
            account_id=cash.id, destination_account_id=bank.id, create_missing=True)
        db.session.commit()
        ids = cash.id, a.id, a.account_tx_id, b.id, b.account_tx_id
    login(client)
    assert b'reversed' in client.post(f'/accounts/transactions/{ids[4]}/delete', follow_redirects=True).data.lower()
    with app.app_context():
        db.session.add(AccountReconciliation(account_id=ids[0], reconciliation_date=pk_today()))
        db.session.commit()
    for tx_id, action in ((ids[2], 'delete'), (ids[4], 'restore')):
        response = client.post(f'/accounts/transactions/{tx_id}/{action}', follow_redirects=True)
        assert b'reconciled period' in response.data.lower()
    with app.app_context():
        assert not db.session.get(CashFlowEntry, ids[1]).is_void
        assert db.session.get(CashFlowEntry, ids[3]).is_void
        assert not db.session.get(AccountTransaction, ids[2]).is_void
        assert db.session.get(AccountTransaction, ids[4]).is_void


def test_unassigned_legacy_sources_are_visible_but_never_invent_account_money(app, client):
    import csv
    import io
    from pypdf import PdfReader
    from models import CashFlowEntry, FbmCashDrawerEntry
    from app.services.accounting import _apply_account_tx_effect
    day = pk_today().isoformat()
    with app.app_context():
        cash = _create_account('Legacy review till', 'cash', '10.01')
        supplier = Supplier(name='Unassigned supplier', is_active=True)
        db.session.add(supplier); db.session.flush()
        booking = Booking(client_name='Legacy client', paid_amount=0.41, manual_bill_no='U-BOOK')
        sale = DirectSale(client_name='Legacy client', category='Cash', paid_amount=0.52,
                          payment_method='Cash', manual_bill_no='U-SALE')
        payment = Payment(client_name='Legacy client', amount=0.63, method='Cash',
                          payment_type='Receipt', manual_bill_no='U-PAY')
        return_credit = Payment(client_name='Legacy client', amount=0.96, method='Cash',
                                payment_type='Material Return', manual_bill_no='U-RETURN')
        supplier_pay = SupplierPayment(supplier_id=supplier.id, amount=0.74, method='Cash', manual_bill_no='U-SUP')
        drawer = FbmCashDrawerEntry(entry_type='out', amount=0.85, method='Cash', category='Old drawer')
        unlinked_cf = CashFlowEntry(direction='in', amount=0.68, reference='U-CF', is_void=False)
        # Although this source field is empty, its actual structured journal
        # mirror identifies an account; trace it there, not as unassigned.
        linked = Payment(client_name='Legacy client', amount=0.97, method='Cash',
                         payment_type='Receipt', manual_bill_no='U-LINK')
        db.session.add_all([booking, sale, payment, return_credit, supplier_pay, drawer, unlinked_cf, linked]); db.session.flush()
        tx = AccountTransaction(to_account_id=cash.id, amount=0.97, transaction_type='Receipt',
                                source_type='Payment', source_id=linked.id, date_posted=linked.date_posted)
        db.session.add(tx); db.session.flush(); _apply_account_tx_effect(tx); db.session.commit()
        cash_id = cash.id
    login(client)
    context = _render_context(app, client, f'/cash_flow?from_date={day}&to_date={day}', 'cash_flow.html')
    assert to_minor(context['opening_balance']) == 1001
    assert to_minor(context['total_cash_in']) == 97
    assert to_minor(context['total_cash_out']) == 0
    assert to_minor(context['closing_balance']) == 1098
    assert len(context['rows']) == 1 and context['rows'][0]['account_id'] == cash_id
    assert {to_minor(r['amount']) for r in context['unassigned_rows']} == {41, 52, 63, 68, 74, 85}
    page = client.get(f'/cash_flow?from_date={day}&to_date={day}').get_data(as_text=True)
    assert 'Unassigned legacy sources' in page and 'not included' in page
    for ref in ('U-BOOK', 'U-SALE', 'U-PAY', 'U-SUP', 'U-CF', 'DRAWER-'):
        assert ref in page
    csv_response = client.get(f'/cash_flow?from_date={day}&to_date={day}&export_csv=1')
    csv_rows = list(csv.DictReader(io.StringIO(csv_response.get_data(as_text=True))))
    assert Counter(r['Status'] for r in csv_rows) == {'active': 1, 'unassigned': 6}
    assert all(not r['Received'] and not r['Spent'] and not r['Transfer']
               for r in csv_rows if r['Status'] == 'unassigned')
    pdf = client.get(f'/cash_flow?from_date={day}&to_date={day}&export_pdf=1')
    text_content = '\n'.join(p.extract_text() or '' for p in PdfReader(io.BytesIO(pdf.data)).pages)
    assert 'Unassigned legacy source records' in text_content and 'U-BOOK' in text_content


def test_source_mismatches_and_orphan_postings_are_not_silently_hidden(app, client):
    from app.services.accounting import _apply_account_tx_effect
    day = pk_today().isoformat()
    with app.app_context():
        cash = _create_account('Mismatch cash', 'cash', '10.00')
        bank = _create_account('Mismatch bank', 'bank', '5.00')
        mismatched = Payment(client_name='Mismatch client', amount=1.01, method='Bank',
            payment_type='Receipt', payment_account_id=bank.id, manual_bill_no='MISMATCH-PAY')
        unposted = Booking(client_name='Unposted booking', paid_amount=0.35,
            receive_in_account_id=bank.id, manual_bill_no='NO-MIRROR')
        db.session.add_all([mismatched, unposted]); db.session.flush()
        actual = AccountTransaction(to_account_id=cash.id, amount=0.99, transaction_type='Receipt',
            source_type='Payment', source_id=mismatched.id, date_posted=mismatched.date_posted)
        orphan = AccountTransaction(to_account_id=cash.id, amount=0.25, transaction_type='Receipt',
            source_type='Booking', source_id=987654, date_posted=pk_now())
        db.session.add_all([actual, orphan]); db.session.flush()
        _apply_account_tx_effect(actual); _apply_account_tx_effect(orphan); db.session.commit()
        cash_id = cash.id
    login(client)
    context = _render_context(app, client, f'/cash_flow?from_date={day}&to_date={day}', 'cash_flow.html')
    assert to_minor(context['opening_balance']) == 1500
    assert to_minor(context['total_cash_in']) == 124
    assert to_minor(context['closing_balance']) == 1624
    mismatch = next(row for row in context['rows'] if 'MISMATCH-PAY' in row['reference'])
    assert mismatch['account_id'] == cash_id and to_minor(mismatch['amount']) == 99
    assert 'Source amount 1.01' in mismatch['warning'] and 'Source account differs' in mismatch['warning']
    orphan_row = next(row for row in context['rows'] if to_minor(row['amount']) == 25)
    assert 'booking #987654' in orphan_row['warning'].lower()
    assert len(context['unassigned_rows']) == 1 and 'NO-MIRROR' in context['unassigned_rows'][0]['reference']
    page = client.get(f'/cash_flow?from_date={day}&to_date={day}').get_data(as_text=True)
    assert 'Source amount 1.01' in page and 'Unassigned legacy sources' in page


def test_booking_void_checks_the_actual_legacy_receipt_period_and_account(app):
    from datetime import timedelta
    from models import AccountReconciliation
    from app.services.accounting import _apply_account_tx_effect
    from app.services.void_rebuild import _set_booking_void_state
    with app.app_context():
        old_bank = _create_account('Reconciled legacy bank', 'bank', '5.00')
        new_cash = _create_account('Source cash reference', 'cash', '10.00')
        booking = Booking(client_name='Legacy mismatch', paid_amount=1.37,
            receive_in_account_id=new_cash.id, date_posted=pk_now())
        db.session.add(booking); db.session.flush()
        receipt = AccountTransaction(transaction_type='Receipt', amount=1.37,
            to_account_id=old_bank.id, date_posted=pk_now() - timedelta(days=1),
            source_type='booking', source_id=booking.id)
        db.session.add(receipt); db.session.flush(); _apply_account_tx_effect(receipt)
        db.session.add(AccountReconciliation(account_id=old_bank.id, reconciliation_date=pk_today()))
        db.session.commit()
        with pytest.raises(ValueError, match='reconciled period'):
            _set_booking_void_state(booking, True)
        assert not booking.is_void and not receipt.is_void
        assert old_bank.balance_minor == 637 and new_cash.balance_minor == 1000


def test_party_accounts_named_cash_are_not_counted_as_company_cash(app):
    from app.services.accounting import _apply_account_tx_effect
    with app.app_context():
        personal = Account(name='Personal wallet', account_type='loan', category='cash',
                           balance=0, is_active=True)
        db.session.add(personal); db.session.flush()
        payment = Payment(client_name='Legacy client', amount=1.11, method='Cash',
            payment_type='Receipt', payment_account_id=personal.id, manual_bill_no='PERSONAL-PAY')
        db.session.add(payment); db.session.flush()
        tx = AccountTransaction(to_account_id=personal.id, amount=1.11, transaction_type='Receipt',
            source_type='Payment', source_id=payment.id, date_posted=payment.date_posted)
        db.session.add(tx); db.session.flush(); _apply_account_tx_effect(tx); db.session.commit()
        day = pk_today().isoformat()
        rows = collect_cash_flow_rows(day, day, include_voided=False)
        assert not rows
        from app.services.cash_flow_svc import collect_unassigned_cash_flow_rows
        unassigned = collect_unassigned_cash_flow_rows(day, day, assigned_rows=rows)
        assert len(unassigned) == 1 and 'PERSONAL-PAY' in unassigned[0]['reference']


@pytest.mark.parametrize('incoming', [False, True])
def test_legacy_transfer_with_party_uses_the_company_side(app, incoming):
    from models import CashFlowEntry
    from app.services.accounting import _apply_account_tx_effect
    with app.app_context():
        cash = _create_account('Company cash for legacy transfer', 'cash', '10.00')
        personal = Account(name='Party cash ledger', account_type='loan', category='cash',
                           balance=0, is_active=True)
        db.session.add(personal); db.session.flush()
        source, destination = (personal, cash) if incoming else (cash, personal)
        entry = CashFlowEntry(direction='transfer', amount=1.11, account_id=source.id,
                              destination_account_id=destination.id, reference='EXTERNAL-LEGACY')
        db.session.add(entry); db.session.flush()
        tx = AccountTransaction(from_account_id=source.id, to_account_id=destination.id,
            amount=1.11, transaction_type='Transfer', source_type='CashFlowEntry',
            source_id=entry.id, date_posted=entry.date_posted)
        db.session.add(tx); db.session.flush(); entry.account_tx_id = tx.id
        _apply_account_tx_effect(tx); db.session.commit()
        day = pk_today().isoformat()
        rows = collect_cash_flow_rows(day, day, include_voided=False)
        assert len(rows) == 1 and rows[0]['type'] == ('in' if incoming else 'out')
        assert rows[0]['account_id'] == cash.id
        assert to_minor(rows[0]['cash_in' if incoming else 'cash_out']) == 111
        assert cash.balance_minor == (1111 if incoming else 889)
        from app.services.cash_flow_svc import collect_unassigned_cash_flow_rows
        assert not collect_unassigned_cash_flow_rows(day, day, assigned_rows=rows)


def test_booking_void_restore_preserves_mismatched_and_historical_receipts(app):
    from app.services.accounting import _apply_account_tx_effect
    from app.services.void_rebuild import _set_booking_void_state
    with app.app_context():
        actual_cash = _create_account('Actual legacy receipt cash', 'cash', '10.00')
        recorded_bank = _create_account('Source field bank', 'bank', '20.00')
        booking = Booking(client_name='Mismatch', paid_amount=2.00, receive_in_account_id=recorded_bank.id)
        missing = Booking(client_name='Unposted', paid_amount=3.00, receive_in_account_id=recorded_bank.id)
        db.session.add_all([booking, missing]); db.session.flush()
        posted = AccountTransaction(to_account_id=actual_cash.id, amount=1.37, transaction_type='Receipt',
            source_type='Booking', source_id=booking.id, date_posted=pk_now())
        already_void = AccountTransaction(to_account_id=actual_cash.id, amount=0.50, transaction_type='Receipt',
            source_type='Booking', source_id=booking.id, date_posted=pk_now(), is_void=True)
        db.session.add_all([posted, already_void]); db.session.flush(); _apply_account_tx_effect(posted)
        db.session.commit()
        assert _set_booking_void_state(booking, True) and _set_booking_void_state(missing, True)
        db.session.commit()
        assert posted.is_void and already_void.is_void and actual_cash.balance_minor == 1000
        assert _set_booking_void_state(booking, False) and _set_booking_void_state(missing, False)
        db.session.commit()
        assert not posted.is_void and already_void.is_void
        assert actual_cash.balance_minor == 1137 and recorded_bank.balance_minor == 2000
        assert posted.amount_minor == 137 and posted.to_account_id == actual_cash.id
        assert AccountTransaction.query.filter_by(source_type='Booking', source_id=booking.id).count() == 2
        assert AccountTransaction.query.filter_by(source_type='Booking', source_id=missing.id).count() == 0
