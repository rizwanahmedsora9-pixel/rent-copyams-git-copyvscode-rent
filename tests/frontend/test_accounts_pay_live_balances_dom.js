/* Exercise live balances using actual HTML rendered by Flask.
 * Run through tests/test_accounts_pay_live_balances.py with Node + jsdom installed.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM, VirtualConsole } = require('jsdom');

const html = fs.readFileSync(process.argv[2], 'utf8');
const js = fs.readFileSync(path.join(__dirname, '../../static/accounts_workspace.js'), 'utf8');
const errors = [];
const virtualConsole = new VirtualConsole();
virtualConsole.on('jsdomError', error => errors.push(error.message));
const dom = new JSDOM(html, {
  url: 'http://localhost/accounts/transactions/new?mode=pay',
  runScripts: 'outside-only', pretendToBeVisual: true, virtualConsole
});
const w = dom.window, d = w.document;
const dataScript = html.match(/window\.AW_DATA\s*=\s*([\s\S]*?);\s*<\/script>/);
assert(dataScript, 'rendered page contains workspace data');
w.eval('window.AW_DATA = ' + dataScript[1] + ';');
w.bootstrap = { Modal: { getOrCreateInstance: () => ({ show() {} }) } };
let periodicRefresh;
w.setInterval = (callback, delay) => { assert.equal(delay, 30000); periodicRefresh = callback; return 1; };
const timers = [];
const nativeSetTimeout = w.setTimeout.bind(w), nativeClearTimeout = w.clearTimeout.bind(w);
w.setTimeout = (callback, delay, ...args) => {
  if (delay !== 10000) return nativeSetTimeout(callback, delay, ...args);
  const timer = { callback, cleared: false };
  timers.push(timer);
  return timer;
};
w.clearTimeout = timer => {
  if (timer && typeof timer === 'object') timer.cleared = true;
  else nativeClearTimeout(timer);
};
const calls = [];
w.fetch = (url, options) => new Promise((resolve, reject) => {
  assert.equal(options.cache, 'no-store');
  assert.equal(options.credentials, 'same-origin');
  const parsed = new URL(url, w.location.href);
  const call = { path: parsed.pathname, id: parsed.searchParams.get('account_id') || parsed.searchParams.get('supplier_id') || parsed.searchParams.get('client_code'), resolve, reject, done: false, timer: timers.at(-1) };
  calls.push(call);
  if (options.signal) options.signal.addEventListener('abort', () => { call.done = true; reject(new Error('aborted')); });
});

const $ = id => d.getElementById(id);
const settle = () => new Promise(resolve => setTimeout(resolve, 0));
const change = (id, value) => { $(id).value = String(value); $(id).dispatchEvent(new w.Event('change', { bubbles: true })); };
const type = (id, value) => { $(id).value = String(value); $(id).dispatchEvent(new w.Event('input', { bubbles: true })); };
const clickMode = mode => d.querySelector('[data-tx-mode="' + mode + '"]').click();
const pending = (pathPart, id) => calls.filter(call => !call.done && call.path.endsWith(pathPart) && call.id === String(id));
const respond = (call, payload) => { assert(call, 'expected a pending request'); call.done = true; call.resolve({ ok: true, json: () => Promise.resolve(payload) }); };
const fail = call => { assert(call, 'expected a pending request'); call.done = true; call.reject(new Error('offline')); };
const accountPayload = (id, balance, ledger = balance, active = true) => ({ found: true, id, is_active: active, balance_minor: balance, ledger_balance_minor: ledger });
const supplierPayload = (supplier, balance, active = true) => ({ found: true, id: Number(supplier.code), name: supplier.name, is_active: active, balance_minor: balance });
const chooseSupplier = query => {
  type('tx-supplier-search', query);
  const rows = Array.from($('tx-supplier-list').querySelectorAll('.aw-picker-item'));
  assert.equal(rows.length, 1, 'supplier search should resolve one row: ' + query);
  rows[0].dispatchEvent(new w.MouseEvent('mousedown', { bubbles: true }));
};
const supplierIdField = d.querySelector('[data-picker="supplier"] [data-picker-value]');

(async () => {
  w.eval(js);
  await settle(); await settle();
  assert.deepEqual(errors, [], 'script must initialize without exceptions');
  const [cash, bank] = w.AW_DATA.accounts;
  const [alpha, beta] = w.AW_DATA.suppliers;
  assert.equal(cash.name, 'A Till');
  assert.equal(alpha.display_code, 'SUP-0001');
  assert.equal($('tx-mode').value, 'pay');
  assert.equal(calls.length, 0, 'no lookups before selecting an account or supplier');
  assert.equal($('tx-preview-balance').textContent, '—', 'no account is not a zero balance');
  assert($('tx-funds-warning').hidden, 'no false insufficient warning without an account');

  change('tx-pay-account', cash.id);
  assert.equal(pending('/account-balance', cash.id).length, 1);
  assert(d.querySelector('[data-balance-help="pay"]').textContent.includes('Refreshing'));
  assert($('tx-submit').disabled, 'cannot use a stale balance while loading');
  change('tx-pay-target', 'supplier');
  chooseSupplier(alpha.display_code);
  assert.equal(supplierIdField.value, alpha.code, 'searchable code selects the actual supplier id');
  assert.equal(pending('/supplier-dues', alpha.code).length, 1);
  assert(!$('tx-supplier-dues').hidden);
  assert($('tx-supplier-dues-note').textContent.includes('Checking'));
  type('tx-amount', '1250.25');
  assert.equal(calls.length, 2, 'typing amount does not repeatedly fetch balances');
  respond(pending('/account-balance', cash.id)[0], accountPayload(cash.id, 500075));
  respond(pending('/supplier-dues', alpha.code)[0], supplierPayload(alpha, 275050));
  await settle(); await settle();
  assert.equal(d.querySelector('[data-balance-help="pay"]').textContent, 'Current balance: PKR 5,000.75');
  assert.equal($('tx-supplier-payable').textContent, 'PKR 2,750.50');
  assert($('tx-supplier-dues-note').textContent.includes('You owe Alpha Cement'));
  assert.equal($('tx-supplier-after').textContent, 'After this payment: you owe PKR 1,500.25.');
  assert.equal($('tx-preview-amount').textContent, 'PKR 1,250.25');
  assert.equal($('tx-preview-balance').textContent, 'PKR 3,750.50');
  assert(!$('tx-submit').disabled);

  type('tx-amount', '0.01');
  assert.equal($('tx-preview-balance').textContent, 'PKR 5,000.74', 'paisa math is exact');
  type('tx-amount', '5000.76');
  assert(!$('tx-funds-warning').hidden && $('tx-submit').disabled, 'actual insufficient funds are blocked');
  type('tx-amount', '1250.25');
  assert($('tx-funds-warning').hidden && !$('tx-submit').disabled);
  assert.equal(calls.length, 2, 'preview edits are local');

  // Out-of-order supplier responses cannot paint the wrong supplier's ledger.
  chooseSupplier(beta.name);
  const oldBeta = pending('/supplier-dues', beta.code)[0];
  chooseSupplier('alpha 0300111'); // Multiple search tokens can span name and phone.
  const newAlpha = pending('/supplier-dues', alpha.code)[0];
  respond(newAlpha, supplierPayload(alpha, 170000));
  await settle();
  respond(oldBeta, supplierPayload(beta, 900000));
  await settle();
  assert.equal(supplierIdField.value, alpha.code);
  assert.equal($('tx-supplier-payable').textContent, 'PKR 1,700.00');

  // Manual refresh can supersede even a still-pending request for the same id.
  $('tx-refresh-balances').click();
  const oldAccount = pending('/account-balance', cash.id)[0];
  const oldSupplier = pending('/supplier-dues', alpha.code)[0];
  $('tx-refresh-balances').click();
  const newAccount = pending('/account-balance', cash.id).at(-1);
  const newSupplier = pending('/supplier-dues', alpha.code).at(-1);
  respond(newAccount, accountPayload(cash.id, 550075));
  respond(newSupplier, supplierPayload(alpha, 175050));
  await settle();
  respond(oldAccount, accountPayload(cash.id, 100));
  respond(oldSupplier, supplierPayload(alpha, 100));
  await settle();
  assert.equal(d.querySelector('[data-balance-help="pay"]').textContent, 'Current balance: PKR 5,500.75');
  assert.equal($('tx-supplier-payable').textContent, 'PKR 1,750.50');
  assert.equal($('tx-preview-balance').textContent, 'PKR 4,250.50');

  // Switching accounts and resolving the old request last cannot overwrite the selection.
  change('tx-pay-account', bank.id);
  const oldBank = pending('/account-balance', bank.id)[0];
  change('tx-pay-account', cash.id);
  const selectedCash = pending('/account-balance', cash.id)[0];
  respond(selectedCash, accountPayload(cash.id, 600000));
  await settle();
  respond(oldBank, accountPayload(bank.id, 20000));
  await settle();
  assert.equal(d.querySelector('[data-balance-help="pay"]').textContent, 'Current balance: PKR 6,000.00');
  assert.equal($('tx-preview-balance').textContent, 'PKR 4,749.75');

  // Unknown data is never displayed as zero; the server remains the final validator.
  $('tx-refresh-balances').click();
  fail(pending('/account-balance', cash.id)[0]);
  respond(pending('/supplier-dues', alpha.code)[0], { found: true, id: Number(alpha.code), balance: null, balance_minor: null, is_active: true });
  await settle(); await settle();
  assert(d.querySelector('[data-balance-help="pay"]').textContent.includes('unavailable'));
  assert.equal($('tx-preview-balance').textContent, '—');
  assert.equal($('tx-supplier-payable').textContent, '—');
  assert($('tx-supplier-dues-note').textContent.includes('could not be loaded'));
  assert($('tx-funds-warning').hidden && !$('tx-submit').disabled, 'a failed lookup does not invent insufficient funds');

  // A hanging request times out instead of leaving the form disabled forever.
  $('tx-refresh-balances').click();
  const timedOut = pending('/account-balance', cash.id)[0];
  respond(pending('/supplier-dues', alpha.code)[0], supplierPayload(alpha, 200000));
  timedOut.timer.callback();
  await settle(); await settle();
  assert(d.querySelector('[data-balance-help="pay"]').textContent.includes('unavailable'));
  assert(!$('tx-submit').disabled);

  // Cached account/ledger drift is surfaced, not hidden or silently repaired.
  $('tx-refresh-balances').click();
  respond(pending('/account-balance', cash.id)[0], accountPayload(cash.id, 500000, 700000));
  respond(pending('/supplier-dues', alpha.code)[0], supplierPayload(alpha, 200000, false));
  await settle();
  assert(d.querySelector('[data-balance-help="pay"]').textContent.includes('Account ledger: PKR 7,000.00'));
  assert(d.querySelector('[data-balance-help="pay"]').textContent.includes('reconcile'));
  assert(!$('tx-funds-warning').hidden && $('tx-submit').disabled, 'a newly suspended supplier cannot be paid');

  chooseSupplier(beta.name);
  respond(pending('/supplier-dues', beta.code)[0], supplierPayload(beta, -50025));
  await settle();
  assert.equal($('tx-supplier-balance-label').textContent, 'Supplier advance');
  assert.equal($('tx-supplier-payable').textContent, 'PKR 500.25');
  assert.equal($('tx-supplier-after').textContent, 'After this payment: advance PKR 1,750.50.');
  assert(!$('tx-submit').disabled);
  type('tx-supplier-search', 'A supplier that was not selected');
  assert.equal(supplierIdField.value, '', 'typing invalidates the supplier id');
  assert($('tx-supplier-search').validity.customError, 'typed text alone cannot be submitted as a supplier');
  assert($('tx-supplier-dues').hidden);
  chooseSupplier(beta.display_code);
  respond(pending('/supplier-dues', beta.code)[0], supplierPayload(beta, 0));
  await settle();
  assert.equal($('tx-supplier-payable').textContent, 'PKR 0.00');
  assert($('tx-supplier-dues-note').textContent.includes('No amount is currently owed'));

  // Return to the tab and the timed refresh both fetch the current values.
  w.dispatchEvent(new w.Event('focus'));
  assert.equal(pending('/account-balance', cash.id).length, 1);
  assert.equal(pending('/supplier-dues', beta.code).length, 1);
  respond(pending('/account-balance', cash.id)[0], accountPayload(cash.id, 600000));
  respond(pending('/supplier-dues', beta.code)[0], supplierPayload(beta, 250000));
  await settle();
  periodicRefresh();
  assert.equal(pending('/account-balance', cash.id).length, 1);
  assert.equal(pending('/supplier-dues', beta.code).length, 1);
  respond(pending('/account-balance', cash.id)[0], accountPayload(cash.id, 600000));
  respond(pending('/supplier-dues', beta.code)[0], supplierPayload(beta, 250000));
  await settle();

  // Receive and transfer still use the same refreshed balances and exact maths.
  clickMode('transfer');
  change('tx-transfer-from', cash.id);
  change('tx-transfer-to', bank.id);
  respond(pending('/account-balance', cash.id)[0], accountPayload(cash.id, 600000));
  respond(pending('/account-balance', bank.id)[0], accountPayload(bank.id, 20000));
  await settle();
  assert.equal($('tx-preview-balance').textContent, 'PKR 4,749.75');
  change('tx-transfer-to', cash.id);
  respond(pending('/account-balance', cash.id)[0], accountPayload(cash.id, 600000));
  await settle();
  assert(!$('tx-same-account').hidden && $('tx-submit').disabled);
  change('tx-transfer-to', bank.id);
  respond(pending('/account-balance', bank.id)[0], accountPayload(bank.id, 20000));
  await settle();
  assert($('tx-same-account').hidden && !$('tx-submit').disabled);

  clickMode('receive');
  change('tx-receive-account', cash.id);
  respond(pending('/account-balance', cash.id)[0], accountPayload(cash.id, 600000));
  const client = w.AW_DATA.clients[0];
  type('tx-client-search', client.code);
  d.querySelector('#tx-client-list .aw-picker-item').dispatchEvent(new w.MouseEvent('mousedown', { bubbles: true }));
  respond(pending('/client-dues', client.code)[0], { found: true, name: client.name, balance: 1000, status: 'payable' });
  await settle();
  type('tx-amount', '200.25');
  type('tx-discount', '50.10');
  assert.equal($('tx-preview-balance').textContent, 'PKR 6,200.25');
  assert.equal($('tx-preview-remaining').textContent, 'PKR 749.65');
  type('tx-amount', '0');
  type('tx-discount-reason', 'Goodwill');
  $('tx-refresh-balances').click();
  respond(pending('/account-balance', cash.id)[0], accountPayload(cash.id, 600000, 600000, false));
  respond(pending('/client-dues', client.code)[0], { found: true, name: client.name, balance: 1000, status: 'payable' });
  await settle();
  assert(!$('tx-submit').disabled, 'a discount-only settlement does not need the selected account');
  assert($('tx-preview-help').textContent.includes('no cash moves'));

  // A background refresh completing after submit must not re-enable the button.
  clickMode('pay');
  respond(pending('/account-balance', cash.id)[0], accountPayload(cash.id, 600000));
  respond(pending('/supplier-dues', beta.code)[0], supplierPayload(beta, 250000));
  await settle();
  type('tx-amount', '100.25');
  let submits = 0;
  $('transactionForm').addEventListener('submit', event => {
    if (!event.defaultPrevented) submits += 1;
    event.preventDefault();
  });
  $('tx-submit').click();
  assert.equal(submits, 1, 'valid supplier payment can be submitted');
  assert($('tx-submit').disabled);
  assert.equal($('tx-submit').querySelector('span').textContent, 'Saving…');
  w.dispatchEvent(new w.Event('focus'));
  respond(pending('/account-balance', cash.id)[0], accountPayload(cash.id, 600000));
  respond(pending('/supplier-dues', beta.code)[0], supplierPayload(beta, 250000));
  await settle();
  assert($('tx-submit').disabled, 'live refresh cannot undo the double-submit guard');
  assert.equal($('tx-submit').querySelector('span').textContent, 'Saving…');
  assert.deepEqual(errors, [], 'no browser errors while interacting');
  dom.window.close();
  console.log('Live account and supplier balance DOM checks passed.');
})().catch(error => {
  console.error(error.stack || error);
  dom.window.close();
  process.exitCode = 1;
});
