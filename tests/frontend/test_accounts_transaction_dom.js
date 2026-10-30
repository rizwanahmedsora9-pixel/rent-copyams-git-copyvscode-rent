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

/* Mirrors templates/accounts/_workspace_transaction.html for the ids and data
 * attributes the workspace script drives. */
const accountOptions = ids => ids.map(([id, label, selected]) =>
  `<option value="${id}"${selected ? ' selected' : ''}>${label}</option>`).join('');
const accounts = [['1', 'Till', true], ['2', 'Bank', false]];

const picker = (kind, items, inputId, listId, valueId, value, placeholder, label) => `
<div class="aw-picker" data-picker="${kind}" data-picker-items="${items}">
  <label class="aw-label" for="${inputId}">${label} *</label>
  <div class="aw-picker-control">
    <input class="aw-input aw-picker-input" id="${inputId}" type="text" data-picker-input placeholder="${placeholder}"
           autocomplete="off" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="${listId}">
    <button type="button" class="aw-picker-toggle" data-picker-toggle tabindex="-1"></button>
  </div>
  <div class="aw-picker-list" id="${listId}" role="listbox" data-picker-list hidden></div>
  <input type="hidden" name="${kind === 'party' ? 'party_id' : 'client_code'}" id="${valueId}" data-picker-value value="${value}">
  ${kind === 'party' ? '<input type="hidden" name="party_name" id="tx-party-name" value="">' : ''}
  <p class="aw-help" data-picker-help>${kind === 'party' ? 'Pick a saved party, or type a new name.' : '2 active clients.'}</p>
</div>`;

const fixture = `<!doctype html><html><body>
<nav class="aw-tabs"><a class="aw-tab">Cash Flow</a><a class="aw-tab aw-tab-new">New Transaction</a></nav>
<form id="transactionForm">
<input name="idempotency_key" value="browser-test-key-123456"><input type="hidden" id="tx-mode" name="tx_mode" value="receive">
<a data-tx-mode="receive" class="is-active">Receive</a><a data-tx-mode="pay">Pay</a><a data-tx-mode="transfer">Transfer</a>
<p id="tx-intro"></p>
<fieldset data-tx-panel="receive"><select id="tx-receive-account" name="receive_account_id" required>${accountOptions([['', 'Select an account', false], ...accounts])}</select><p data-balance-help="receive"></p></fieldset>
<fieldset data-tx-panel="pay" disabled hidden><select id="tx-pay-account" name="pay_from_account_id" required>${accountOptions([['', 'Select', false], ...accounts])}</select><p data-balance-help="pay"></p></fieldset>
<fieldset data-tx-panel="transfer" disabled hidden><select id="tx-transfer-from" name="transfer_from_account_id" required>${accountOptions([['', 'Select', false], ...accounts])}</select><select id="tx-transfer-to" name="transfer_to_account_id" required>${accountOptions([['', 'Select', false], ['1', 'Till', false], ['2', 'Bank', true]])}</select><p data-balance-help="transfer-from"></p><p data-balance-help="transfer-to"></p><p id="tx-same-account" hidden></p></fieldset>
<fieldset data-tx-panel="receive">
  <input type="hidden" name="receive_source" id="tx-receive-source" value="client">
  <button type="button" data-tx-source="client" class="aw-source-tab is-active">Client</button>
  <button type="button" data-tx-source="party" class="aw-source-tab">Party</button>
  <button type="button" data-tx-source="other" class="aw-source-tab">Other</button>
  <p id="tx-source-hint"></p>
  <fieldset data-tx-source-panel="client">
    ${picker('client', 'clients', 'tx-client-search', 'tx-client-list', 'tx-client-code', '', 'Search clients…', 'Client')}
    <div class="aw-dues" id="tx-client-dues" hidden>
      <span class="aw-tx-eyebrow">Payable amount</span><strong id="tx-client-payable">—</strong>
      <p id="tx-client-dues-note"></p>
    </div>
  </fieldset>
  <fieldset data-tx-source-panel="party" disabled hidden>
    ${picker('party', 'parties', 'tx-party-search', 'tx-party-list', 'tx-party-id', '', 'Search parties…', 'Party')}
    <select id="tx-party-type" name="party_type"><option value="loan">Loan</option><option value="outsider">Outsider</option><option value="person">Person</option><option value="other" selected>Other</option></select>
  </fieldset>
  <fieldset data-tx-source-panel="other" disabled hidden>
    <label id="tx-receive-other-label" for="tx-receive-other">Received from *</label>
    <input id="tx-receive-other" name="party" value="">
  </fieldset>
</fieldset>
<fieldset data-tx-panel="pay" disabled hidden><label id="tx-party-label" for="tx-party">Paid to </label><input id="tx-party" name="party" value=""></fieldset>
<fieldset data-tx-panel="receive pay" id="tx-category-section"><select id="tx-category" name="category"><option value="">Uncategorized</option><option value="Income">Income</option><option value="Expense">Expense</option><option value="Both">Both</option></select><span id="tx-category-help"></span><div id="tx-subcategory-field"><select id="tx-subcategory" name="subcategory"><option value="">None</option></select></div><p id="tx-category-client-note" hidden></p></fieldset>
<label id="tx-amount-label" for="tx-amount">Amount (PKR) *</label><input id="tx-amount" name="amount" type="number" min="0" step="0.01" required value="125.25"><p id="tx-amount-help"></p>
<div id="tx-discount-field" hidden><label id="tx-discount-label" for="tx-discount">Discount (PKR)</label><input id="tx-discount" name="discount" type="number" min="0" step="0.01" disabled value=""></div>
<div id="tx-discount-reason-field" hidden><label id="tx-discount-reason-label" for="tx-discount-reason">Discount reason</label><input id="tx-discount-reason" name="discount_reason" disabled value=""></div>
<input id="tx-date" name="date_posted" value="2026-10-30T12:00"><input id="tx-reference" name="reference_no"><p id="tx-reference-help" hidden></p><input id="tx-note" name="note">
<strong id="tx-preview-amount"></strong><span id="tx-preview-from-label"></span><strong id="tx-preview-from"></strong><span id="tx-preview-to-label"></span><strong id="tx-preview-to"></strong>
<div id="tx-preview-dues" hidden><strong id="tx-preview-payable"></strong><strong id="tx-preview-received"></strong><strong id="tx-preview-discount"></strong><strong id="tx-preview-remaining"></strong></div>
<span id="tx-preview-balance-label"></span><strong id="tx-preview-balance"></strong><p id="tx-funds-warning" hidden></p><p id="tx-preview-help"></p>
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

/* The dues lookup is stubbed so the picker can be exercised offline. */
const duesCalls = [];
let duesPayload = { found: true, name: 'Ali Traders', code: 'C-001', balance: 12500, payable: 12500, advance: 0, status: 'payable' };
w.fetch = url => {
  duesCalls.push(String(url));
  return Promise.resolve({ ok: true, json: () => Promise.resolve(duesPayload) });
};
const settle = () => new Promise(resolve => setTimeout(resolve, 0));

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
    ],
    clients: [
      { code: 'C-001', name: 'Ali Traders', phone: '0300111', category: 'General' },
      { code: 'C-002', name: 'Bilal Steel', phone: '0300222', category: 'Dealer' }
    ],
    parties: [
      { id: 7, name: 'Haji Lender', party_type: 'loan', type_label: 'Loan (person or outside lender)', phone: '', note: '' },
      { id: 8, name: 'Outside Partner', party_type: 'outsider', type_label: 'Outsider / other party', phone: '', note: '' }
    ],
    receiveSources: {
      client: { label: 'Client', hint: 'Search your client list.' },
      party: { label: 'Party', hint: 'Loan givers and other parties.' },
      other: { label: 'Other', hint: 'Type a name.' }
    },
    clientDuesUrl: '/accounts/transactions/client-dues',
    accountsList: [], formValues: {}
  };
}

let pass = 0, fail = 0;
function check(name, condition, extra) {
  if (condition) { pass++; console.log('  ok  -', name); }
  else { fail++; console.log('  FAIL-', name, extra === undefined ? '' : extra); }
}
const $ = id => d.getElementById(id);
const clickMode = mode => d.querySelector('[data-tx-mode="' + mode + '"]').click();
const clickSource = source => d.querySelector('[data-tx-source="' + source + '"]').click();
const change = (id, value) => { $(id).value = value; $(id).dispatchEvent(new w.Event('change', { bubbles: true })); };
const type = (id, value) => { $(id).value = value; $(id).dispatchEvent(new w.Event('input', { bubbles: true })); };
const panelOf = source => d.querySelector('[data-tx-source-panel="' + source + '"]');
const pickerRows = id => Array.from($(id).querySelectorAll('.aw-picker-item'));

(async function run() {
if (process.env.AW_PAGE_HTML) {
  // The real form intentionally starts without a selected account or amount.
  // Normalize only test input, keeping the rendered controls and server data.
  const accounts = w.AW_DATA.accounts;
  for (const id of ['tx-receive-account', 'tx-pay-account', 'tx-transfer-from']) $(id).value = String(accounts[0].id);
  $('tx-transfer-to').value = String(accounts[1].id);
  $('tx-party').value = 'Ali';
  $('tx-receive-other').value = 'Ali';
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
  // Point the dues stub at whatever client the rendered page actually has.
  const stubClient = (w.AW_DATA.clients || [])[0];
  if (stubClient) {
    duesPayload = { found: true, name: stubClient.name, code: stubClient.code, balance: 12500, payable: 12500, advance: 0, status: 'payable' };
  }
}
w.eval(code);
d.dispatchEvent(new w.Event('DOMContentLoaded'));

/* ----- mode + source defaults ----- */
check('receive is the initial mode', $('tx-mode').value === 'receive');
check('client is the default receive source', $('tx-receive-source').value === 'client');
check('only the client source panel is enabled', !panelOf('client').disabled && panelOf('party').disabled && panelOf('other').disabled);
check('source hint is server driven', $('tx-source-hint').textContent.length > 0, $('tx-source-hint').textContent);
check('discount is enabled for a client receive', $('tx-discount-field').hidden === false && $('tx-discount').disabled === false);
check('a client receive hides the category block', $('tx-category-section').disabled && !$('tx-category-client-note').hidden);

/* ----- searchable client picker ----- */
$('tx-client-search').dispatchEvent(new w.Event('focus'));
check('the client list is data driven', pickerRows('tx-client-list').length === w.AW_DATA.clients.length, pickerRows('tx-client-list').length);
check('opening the list expands the combobox', $('tx-client-search').getAttribute('aria-expanded') === 'true' && !$('tx-client-list').hidden);
/* Tokens and expectations are read from AW_DATA so these checks hold both for
   the fixture and for a rendered page with different clients and parties. */
const firstClient = w.AW_DATA.clients[0];
const secondClient = w.AW_DATA.clients[1] || firstClient;
type('tx-client-search', secondClient.code);
check('typing filters the list', pickerRows('tx-client-list').length === 1
  && pickerRows('tx-client-list')[0].textContent.includes(secondClient.name),
  pickerRows('tx-client-list').map(row => row.textContent));
check('typing clears a stale hidden selection', $('tx-client-code').value === '');
type('tx-client-search', 'zzz');
check('no match is reported, not silently empty', $('tx-client-list').textContent.includes('No client matches'));
type('tx-client-search', firstClient.code);
pickerRows('tx-client-list')[0].dispatchEvent(new w.MouseEvent('mousedown', { bubbles: true }));
check('picking a client writes the hidden code', $('tx-client-code').value === firstClient.code, $('tx-client-code').value);
check('picking a client shows its display text', $('tx-client-search').value.includes(firstClient.name) && $('tx-client-search').value.includes(firstClient.code), $('tx-client-search').value);
check('picking closes the list', $('tx-client-list').hidden && $('tx-client-search').getAttribute('aria-expanded') === 'false');
check('the payable lookup is requested once', duesCalls.length === 1 && duesCalls[0].includes('client_code=' + firstClient.code), duesCalls);
await settle(); await settle();
check('the payable amount shows under the client', $('tx-client-dues').hidden === false && $('tx-client-payable').textContent === 'PKR 12,500.00', $('tx-client-payable').textContent);
check('the payable note explains the effect', $('tx-client-dues-note').textContent.includes('reduces that balance'), $('tx-client-dues-note').textContent);
check('the preview shows the picked client', $('tx-preview-from').textContent === firstClient.name, $('tx-preview-from').textContent);

/* ----- settlement preview + discount ----- */
change('tx-amount', '2500');
change('tx-discount', '500');
check('the settlement preview is shown', $('tx-preview-dues').hidden === false);
check('settlement maths is exact paisa', $('tx-preview-payable').textContent === 'PKR 12,500.00'
  && $('tx-preview-received').textContent === 'PKR 2,500.00'
  && $('tx-preview-discount').textContent === 'PKR 500.00'
  && $('tx-preview-remaining').textContent === 'PKR 9,500.00', $('tx-preview-remaining').textContent);
check('a discount demands a reason', $('tx-discount-reason').required === true);
type('tx-discount-reason', 'Good customer');
check('the account balance preview ignores the discount', $('tx-preview-balance').textContent.endsWith('3,500.00'), $('tx-preview-balance').textContent);

/* ----- discount-only receive ----- */
change('tx-amount', '0');
check('a discount-only entry allows amount 0', $('tx-amount').required === false && $('tx-amount').min === '0');
check('a discount-only entry needs no account', $('tx-receive-account').required === false);
check('the button says what it will do', $('tx-submit').querySelector('span').textContent === 'Save discount', $('tx-submit').textContent);
check('the preview explains that no cash moves', $('tx-preview-help').textContent.includes('no cash moves'), $('tx-preview-help').textContent);
check('remaining dues equal the discount', $('tx-preview-remaining').textContent === 'PKR 12,000.00', $('tx-preview-remaining').textContent);
change('tx-discount', '0');
check('zero amount and zero discount is blocked', $('tx-amount').validity.customError === true && $('tx-amount').required === true);
change('tx-discount', '500');
check('a discount unblocks the zero amount', $('tx-amount').validity.customError === false);

/* ----- a client in advance is labelled, never shown as a negative payable ----- */
duesPayload = { found: true, name: firstClient.name, code: firstClient.code, balance: -2500, payable: 0, advance: 2500, status: 'advance' };
$('tx-client-search').dispatchEvent(new w.Event('focus'));
pickerRows('tx-client-list')[0].dispatchEvent(new w.MouseEvent('mousedown', { bubbles: true }));
await settle(); await settle();
check('an advance reads as an advance', $('tx-client-payable').textContent === 'PKR 2,500.00'
  && $('tx-client-dues-note').textContent.includes('already has an advance'), $('tx-client-dues-note').textContent);
check('the settlement preview carries the advance through', $('tx-preview-remaining').textContent === 'Advance PKR 3,000.00', $('tx-preview-remaining').textContent);
duesPayload = { found: true, name: firstClient.name, code: firstClient.code, balance: 12500, payable: 12500, advance: 0, status: 'payable' };

/* ----- party source (loan givers and other parties) ----- */
clickSource('party');
check('switching source enables only that panel', panelOf('party').disabled === false && panelOf('client').disabled && panelOf('other').disabled);
check('the hidden client code cannot be submitted', $('tx-client-code').closest('fieldset').disabled);
check('a party receive may be categorised', $('tx-category-section').disabled === false && $('tx-category-client-note').hidden);
check('discounts are client-only', $('tx-discount').disabled && $('tx-discount').value === '' && $('tx-discount-field').hidden);
check('the amount is required again', $('tx-amount').required === true && $('tx-receive-account').required === true);
check('the submit label follows the mode', $('tx-submit').querySelector('span').textContent === 'Receive money');
const firstParty = w.AW_DATA.parties[0];
$('tx-party-search').dispatchEvent(new w.Event('focus'));
check('the party list is data driven', pickerRows('tx-party-list').length === w.AW_DATA.parties.length);
check('loan parties are badged', $('tx-party-list').textContent.includes('Loan'));
type('tx-party-search', firstParty.name);
pickerRows('tx-party-list')[0].dispatchEvent(new w.MouseEvent('mousedown', { bubbles: true }));
check('picking a party writes the hidden id', $('tx-party-id').value === String(firstParty.id), $('tx-party-id').value);
check('a picked party locks its saved type', $('tx-party-type').disabled && $('tx-party-type').value === firstParty.party_type);
check('the party shows in the preview', $('tx-preview-from').textContent === firstParty.name, $('tx-preview-from').textContent);
type('tx-party-search', 'New Lender');
check('editing clears the picked party', $('tx-party-id').value === '');
check('a typed party name is submitted', $('tx-party-name').value === 'New Lender');
check('a typed party may be given a type', $('tx-party-type').disabled === false);
change('tx-party-type', 'loan');
change('tx-amount', '20000');
check('a party receive previews the account balance', $('tx-preview-balance').textContent.endsWith('21,000.00'), $('tx-preview-balance').textContent);

/* ----- other source ----- */
clickSource('other');
check('the other panel takes a free-text name', panelOf('other').disabled === false && $('tx-receive-other').closest('fieldset').disabled === false);
type('tx-receive-other', 'Walk-in customer');
check('the typed name reaches the preview', $('tx-preview-from').textContent === 'Walk-in customer');
check('the settlement block is client-only', $('tx-preview-dues').hidden);

/* ----- keyboard support ----- */
clickSource('client');
$('tx-client-search').focus();
$('tx-client-search').dispatchEvent(new w.KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
check('arrow down opens and moves the cursor', !$('tx-client-list').hidden && pickerRows('tx-client-list')[0].classList.contains('is-cursor'));
$('tx-client-search').dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
check('enter picks the highlighted client', $('tx-client-code').value === w.AW_DATA.clients[0].code, $('tx-client-code').value);
$('tx-client-search').dispatchEvent(new w.Event('focus'));
$('tx-client-search').dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
check('escape closes the list', $('tx-client-list').hidden);

/* ----- pay / transfer behaviour is unchanged ----- */
clickMode('pay');
check('pay switches panels and URL', $('tx-mode').value === 'pay' && !$('tx-pay-account').closest('fieldset').disabled && $('tx-receive-account').closest('fieldset').disabled && w.location.search.includes('mode=pay'));
check('pay hides every receive source panel', ['client', 'party', 'other'].every(source => panelOf(source).disabled));
check('pay keeps the discount closed', $('tx-discount').disabled && $('tx-discount-field').hidden);
check('pay filters categories and clears incompatible child', $('tx-category').value === '' && $('tx-subcategory').disabled && !$('tx-category').querySelector('[value="Expense"]').disabled);
change('tx-category', 'Expense');
change('tx-subcategory', 'Fuel');
check('expense children available', $('tx-subcategory').value === 'Fuel');
type('tx-party', 'Supplier');
change('tx-amount', '100');
check('pay subtracts from balance', $('tx-preview-balance').textContent.endsWith('900.00'), $('tx-preview-balance').textContent);
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

/* ----- submit ----- */
clickMode('receive');
clickSource('client');
$('tx-client-search').dispatchEvent(new w.Event('focus'));
pickerRows('tx-client-list')[0].dispatchEvent(new w.MouseEvent('mousedown', { bubbles: true }));
await settle(); await settle();
change('tx-amount', '125.25');
change('tx-discount', '0');
// The workspace handler runs first, so a refusal shows up as defaultPrevented.
let posts = 0, blocked = 0;
$('transactionForm').addEventListener('submit', event => {
  if (event.defaultPrevented) blocked++; else posts++;
  event.preventDefault();
});
$('tx-submit').click();
check('submit retains idempotency key and prevents double click', posts === 1 && blocked === 0 && $('tx-submit').disabled && d.querySelector('[name="idempotency_key"]').value.length >= 16, [posts, blocked]);
$('tx-submit').click();
check('second click does not submit', posts === 1 && blocked === 0, [posts, blocked]);
w.dispatchEvent(new w.Event('pageshow'));
check('back-forward cache restores button', !$('tx-submit').disabled);

/* ----- an unpicked client cannot be submitted ----- */
type('tx-client-search', 'someone not in the list');
check('editing the name drops the selection', $('tx-client-code').value === '');
$('tx-submit').click();
check('submit without a picked client is refused', posts === 1 && blocked === 1, [posts, blocked]);
check('the refusal tells the user what to do', $('tx-client-search').validationMessage.includes('Select a client'), $('tx-client-search').validationMessage);

console.log(fail === 0 ? '\nALL ' + pass + ' CHECKS PASSED' : '\n' + fail + ' FAILED / ' + pass + ' passed');
process.exit(fail ? 1 : 0);
})();
