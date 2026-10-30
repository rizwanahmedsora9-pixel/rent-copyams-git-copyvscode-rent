/* Offline DOM coverage for the New Transaction tab.
 * Run: node tests/frontend/test_accounts_transaction_dom.js
 * Optional: AW_PAGE_HTML=/path/to/rendered-page.html to test the real template.
 */
const fs = require('fs');
const path = require('path');
let JSDOM;
try { JSDOM = require('jsdom').JSDOM; }
catch (_) { JSDOM = require('/tmp/node_modules/jsdom').JSDOM; }
const root = path.join(__dirname, '..', '..');
const code = fs.readFileSync(path.join(root, 'static/accounts_workspace.js'), 'utf8');
const fixture = `<!doctype html><html><body>
<nav class="aw-tabs"><a class="aw-tab">Cash Flow</a><a class="aw-tab aw-tab-new">New Transaction</a></nav>
<form id="transactionForm">
<input name="idempotency_key" value="browser-test-key-123456"><input type="hidden" id="tx-mode" name="tx_mode" value="receive">
<a data-tx-mode="receive" class="is-active">Receive</a><a data-tx-mode="pay">Pay</a><a data-tx-mode="transfer">Transfer</a>
<p id="tx-intro"></p>
<fieldset data-tx-panel="receive"><select id="tx-receive-account"><option value="">Select</option><option value="1" selected>Till</option><option value="2">Bank</option></select><p data-balance-help="receive"></p></fieldset>
<fieldset data-tx-panel="pay" disabled hidden><select id="tx-pay-account"><option value="">Select</option><option value="1" selected>Till</option><option value="2">Bank</option></select><p data-balance-help="pay"></p></fieldset>
<fieldset data-tx-panel="transfer" disabled hidden><select id="tx-transfer-from"><option value="">Select</option><option value="1" selected>Till</option><option value="2">Bank</option></select><select id="tx-transfer-to"><option value="">Select</option><option value="1">Till</option><option value="2" selected>Bank</option></select><p data-balance-help="transfer-from"></p><p data-balance-help="transfer-to"></p><p id="tx-same-account" hidden></p></fieldset>
<fieldset data-tx-panel="receive pay"><label id="tx-party-label" for="tx-party">Received from </label><input id="tx-party" value="Ali"><select id="tx-category"><option value="">Uncategorized</option><option value="Income">Income</option><option value="Expense">Expense</option><option value="Both">Both</option></select><span id="tx-category-help"></span><div id="tx-subcategory-field"><select id="tx-subcategory"><option value="">None</option></select></div></fieldset>
<input id="tx-amount" type="number" min="0.01" step="0.01" value="125.25"><input id="tx-date" value="2026-10-30T12:00"><input id="tx-reference"><input id="tx-note">
<strong id="tx-preview-amount"></strong><span id="tx-preview-from-label"></span><strong id="tx-preview-from"></strong><span id="tx-preview-to-label"></span><strong id="tx-preview-to"></strong><span id="tx-preview-balance-label"></span><strong id="tx-preview-balance"></strong><p id="tx-funds-warning" hidden></p><p id="tx-preview-help"></p>
<button id="tx-submit" type="submit"><span>Receive money</span></button>
</form></body></html>`;
const html = process.env.AW_PAGE_HTML ? fs.readFileSync(process.env.AW_PAGE_HTML, 'utf8') : fixture;
const dom = new JSDOM(html, { url: 'http://localhost/accounts/transactions/new', runScripts: 'outside-only' });
const w = dom.window, d = w.document;
if (process.env.AW_PAGE_HTML) {
  const dataScript = html.match(/window\.AW_DATA\s*=\s*([\s\S]*?);\s*<\/script>/);
  if (dataScript) w.eval('window.AW_DATA = ' + dataScript[1] + ';');
}
w.bootstrap = { Modal: { getOrCreateInstance: () => ({ show() {} }) } };
if (!process.env.AW_PAGE_HTML) {
  w.AW_DATA = {
    accounts: [
      { id: 1, name: 'Till', current_balance: 1000, is_active: true },
      { id: 2, name: 'Bank', current_balance: 500, is_active: true }
    ],
    transactionCategories: [
      { name: 'Income', direction: 'in', subcategories: ['Hire'] },
      { name: 'Expense', direction: 'out', subcategories: ['Fuel', 'Repairs'] },
      { name: 'Both', direction: 'both', subcategories: [] }
    ], accountsList: [], formValues: {}
  };
}
let pass = 0, fail = 0;
function check(name, condition, extra) {
  if (condition) { pass++; console.log('  ok  -', name); }
  else { fail++; console.log('  FAIL-', name, extra || ''); }
}
const $ = id => d.getElementById(id);
const clickMode = mode => d.querySelector('[data-tx-mode="' + mode + '"]').click();
const change = (id, value) => { $(id).value = value; $(id).dispatchEvent(new w.Event('change', { bubbles: true })); };
if (process.env.AW_PAGE_HTML) {
  // The real form intentionally starts without a selected account or amount.
  // Normalize only test input, keeping the rendered controls and server data.
  const accounts = w.AW_DATA.accounts;
  for (const id of ['tx-receive-account', 'tx-pay-account', 'tx-transfer-from']) $(id).value = String(accounts[0].id);
  $('tx-transfer-to').value = String(accounts[1].id);
  $('tx-party').value = 'Ali';
  $('tx-amount').value = '125.25';
  const categories = w.AW_DATA.transactionCategories;
  const names = { Income: categories.find(c => c.direction === 'in').name, Expense: categories.find(c => c.direction === 'out').name };
  const both = categories.find(c => c.direction === 'both');
  if (both) names.Both = both.name;
  else {
    names.Both = 'Both';
    categories.push({ name: 'Both', direction: 'both', subcategories: [] });
    $('tx-category').add(new w.Option('Both', 'Both'));
  }
  for (const [alias, name] of Object.entries(names)) $('tx-category').querySelector('[value="' + name + '"]').value = alias;
  const aliases = Object.fromEntries(Object.entries(names).map(([alias, name]) => [name, alias]));
  categories.forEach(c => { c.name = aliases[c.name] || c.name; });
  accounts[0].name = 'Till'; accounts[1].name = 'Bank';
  accounts[0].current_balance = 1000; accounts[1].current_balance = 500;
  categories.find(c => c.name === 'Income').subcategories = ['Hire'];
  categories.find(c => c.name === 'Expense').subcategories = ['Fuel', 'Repairs'];
}
w.eval(code);
d.dispatchEvent(new w.Event('DOMContentLoaded'));
check('receive is the initial mode', $('tx-mode').value === 'receive');
check('receive preview uses exact paisa', $('tx-preview-balance').textContent.endsWith('1,125.25'), $('tx-preview-balance').textContent);
check('inactive pay fields are disabled', $('tx-pay-account').closest('fieldset').disabled);
check('only income and both categories are enabled', !$('tx-category').querySelector('[value="Income"]').disabled && $('tx-category').querySelector('[value="Expense"]').disabled);
change('tx-category', 'Income');
check('subcategory follows parent', $('tx-subcategory').options[1].value === 'Hire' && !$('tx-subcategory').disabled);
clickMode('pay');
check('pay switches panels and URL', $('tx-mode').value === 'pay' && !$('tx-pay-account').closest('fieldset').disabled && $('tx-receive-account').closest('fieldset').disabled && w.location.search.includes('mode=pay'));
check('pay filters categories and clears incompatible child', $('tx-category').value === '' && $('tx-subcategory').disabled && !$('tx-category').querySelector('[value="Expense"]').disabled);
change('tx-category', 'Expense');
change('tx-subcategory', 'Fuel');
check('expense children available', $('tx-subcategory').value === 'Fuel');
check('pay subtracts from balance', $('tx-preview-balance').textContent.endsWith('874.75'), $('tx-preview-balance').textContent);
change('tx-amount', '1000.01');
check('insufficient funds blocks submit', !$('tx-funds-warning').hidden && $('tx-submit').disabled);
change('tx-amount', '100');
check('valid amount re-enables submit', !$('tx-submit').disabled);
clickMode('transfer');
check('transfer disables party and category fields', $('tx-party').closest('fieldset').disabled && $('tx-category').closest('fieldset').disabled);
check('transfer preview shows both accounts', $('tx-preview-from').textContent === 'Till' && $('tx-preview-to').textContent === 'Bank');
change('tx-transfer-to', $('tx-transfer-from').value);
check('same-account transfer is blocked', !$('tx-same-account').hidden && $('tx-submit').disabled && $('tx-transfer-to').validity.customError);
change('tx-transfer-to', Array.from($('tx-transfer-to').options).find(option => option.value && option.value !== $('tx-transfer-from').value).value);
check('different destination restores submit', !$('tx-submit').disabled);
let posts = 0;
$('transactionForm').addEventListener('submit', event => { event.preventDefault(); posts++; });
$('tx-submit').click();
check('submit retains idempotency key and prevents double click', posts === 1 && $('tx-submit').disabled && d.querySelector('[name="idempotency_key"]').value.length >= 16);
$('tx-submit').click();
check('second click does not submit', posts === 1);
w.dispatchEvent(new w.Event('pageshow'));
check('back-forward cache restores button', !$('tx-submit').disabled);
console.log(fail === 0 ? '\nALL ' + pass + ' CHECKS PASSED' : '\n' + fail + ' FAILED / ' + pass + ' passed');
process.exit(fail ? 1 : 0);
