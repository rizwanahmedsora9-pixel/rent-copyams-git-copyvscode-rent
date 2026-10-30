/* Accounts workspace interactions. Runs after Bootstrap is available. */
(function () {
  'use strict';

  function init() {
    const data = window.AW_DATA || { accounts: [], categories: [], formValues: {} };
    const accounts = new Map((data.accounts || []).map(a => [String(a.id), a]));
    const categories = new Map((data.categories || []).map(c => [String(c.id), c]));
    const values = data.formValues || {};
    const $ = id => document.getElementById(id);

    function money(minor) { return (Number(minor || 0) / 100).toFixed(2); }
    function modal(id) { return bootstrap.Modal.getOrCreateInstance($(id)); }

    document.querySelectorAll('[data-auto-submit]').forEach(el => {
      el.addEventListener('change', () => el.form && el.form.requestSubmit());
    });

    /* ----- Account create/edit ----- */
    const accountForm = $('accountForm');
    function accountTypeFields(type) {
      const number = $('account-number');
      const label = $('account-number-label');
      const help = $('account-number-help');
      const opening = $('account-opening');
      if (!number) return;
      if (type === 'bank') {
        label.textContent = 'Account number / IBAN *';
        number.required = true; number.placeholder = 'e.g. PK00 BANK 0000 0000 0000';
        help.textContent = 'Required. Leading zeros are kept.';
        opening.removeAttribute('min');
        $('account-opening-help').textContent = 'Use a negative amount only for an overdraft.';
      } else if (type === 'wallet') {
        label.textContent = 'Wallet number *';
        number.required = true; number.placeholder = 'e.g. 0300 1234567';
        help.textContent = 'Required. The mobile number registered to the wallet.';
        opening.min = '0';
        $('account-opening-help').textContent = 'The amount in this wallet when you start. Enter 0 if it is empty.';
      } else {
        label.textContent = 'Account number';
        number.required = false; number.placeholder = 'Optional internal reference';
        help.textContent = 'Optional for cash accounts.';
        opening.min = '0';
        $('account-opening-help').textContent = 'The amount in this account when you start. Enter 0 if it is empty.';
      }
    }

    function openAccount(account) {
      if (!accountForm) return;
      accountForm.reset();
      const editing = Boolean(account);
      const locked = editing && account.is_used;
      $('accountModalTitle').textContent = editing ? 'Edit account' : 'New account';
      $('account-save').textContent = editing ? 'Save changes' : 'Create account';
      $('account-id').value = editing ? account.id : '';
      $('account-revision').value = editing ? account.revision : '';
      $('account-name').value = editing ? account.name : (values.name || '');
      $('account-number').value = editing ? (account.account_number || '') : (values.account_number || '');
      const type = editing ? account.account_type : (values.account_type || 'cash');
      $('account-type').value = type;
      $('account-type').disabled = locked;
      $('account-type-locked').disabled = !locked;
      $('account-type-locked').value = locked ? type : '';
      const opening = editing ? money(account.opening_balance_minor) : (values.opening_balance != null ? values.opening_balance : '0');
      $('account-opening').value = opening;
      $('account-opening').disabled = locked;
      $('account-opening-locked').disabled = !locked;
      $('account-opening-locked').value = locked ? opening : '';
      $('account-locked-note').hidden = !locked;
      accountTypeFields(type);
      modal('accountModal').show();
    }

    document.querySelectorAll('[data-account-create]').forEach(btn => btn.addEventListener('click', () => openAccount(null)));
    document.querySelectorAll('[data-account-edit]').forEach(btn => btn.addEventListener('click', () => openAccount(accounts.get(btn.dataset.accountEdit))));
    if ($('account-type')) $('account-type').addEventListener('change', e => accountTypeFields(e.target.value));

    /* ----- Category create/edit ----- */
    const categoryForm = $('categoryForm');
    function openCategory(category) {
      if (!categoryForm) return;
      categoryForm.reset();
      const editing = Boolean(category);
      const locked = editing && category.usage_count > 0;
      $('categoryModalTitle').textContent = editing ? 'Edit category' : 'New category';
      $('category-save').textContent = editing ? 'Save changes' : 'Create category';
      $('category-id').value = editing ? category.id : '';
      $('category-name').value = editing ? category.name : (values.name || '');
      $('category-notes').value = editing ? (category.notes || '') : (values.notes || '');
      const direction = editing ? category.direction : (values.direction || 'both');
      categoryForm.querySelectorAll('input[name="direction"][type="radio"]').forEach(radio => {
        radio.checked = radio.value === direction;
        radio.disabled = locked;
      });
      $('category-direction-locked').disabled = !locked;
      $('category-direction-locked').value = locked ? direction : '';
      $('category-locked-note').hidden = !locked;
      modal('categoryModal').show();
    }

    document.querySelectorAll('[data-category-create]').forEach(btn => btn.addEventListener('click', () => openCategory(null)));
    document.querySelectorAll('[data-category-edit]').forEach(btn => btn.addEventListener('click', () => openCategory(categories.get(btn.dataset.categoryEdit))));

    /* ----- New Transaction: mode + receive source, pickers, live preview ----- */
    const transactionForm = $('transactionForm');
    if (transactionForm) {
      const accountList = data.accounts || [];
      const txCategories = data.transactionCategories || [];
      const clientList = data.clients || [];
      const partyList = data.parties || [];
      const receiveSources = data.receiveSources || {};
      const duesUrl = data.clientDuesUrl || '';
      const modeInput = $('tx-mode');
      const sourceInput = $('tx-receive-source');
      const amountInput = $('tx-amount');
      const discountInput = $('tx-discount');
      const discountReasonInput = $('tx-discount-reason');
      const categoryInput = $('tx-category');
      const subcategoryInput = $('tx-subcategory');
      const categorySection = $('tx-category-section');
      const receiveAccountInput = $('tx-receive-account');
      const submit = $('tx-submit');
      const formatter = new Intl.NumberFormat('en-PK', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      const descriptions = {
        receive: 'Record money received into a business account.',
        pay: 'Record a business expense or payment out of an account.',
        transfer: 'Move money between two of your own accounts. This is not income or an expense.'
      };
      const submitLabels = { receive: 'Receive money', pay: 'Record payment', transfer: 'Transfer money' };
      const sourceKeys = ['client', 'party', 'other'];
      const selectedAccount = id => accountList.find(account => String(account.id) === String(id));
      const accountName = id => { const account = selectedAccount(id); return account ? account.name : '—'; };
      const cents = value => {
        const number = Number(value);
        return Number.isFinite(number) ? Math.round(number * 100) : 0;
      };
      // Exact paisa from a decimal string: never let binary float rounding
      // decide what the preview shows or what the submit gate accepts.
      const moneyCents = input => {
        const value = (input.value || '').trim().replace(/,/g, '');
        if (!value || !/^\d+(?:\.\d{1,2})?$/.test(value)) return 0;
        const [whole, fraction = ''] = value.split('.');
        const result = Number(whole) * 100 + Number(fraction.padEnd(2, '0'));
        return Number.isSafeInteger(result) && result > 0 && result <= 99999999999999 ? result : 0;
      };
      const amountCents = () => moneyCents(amountInput);
      const discountCents = () => moneyCents(discountInput);
      const displayMoney = value => 'PKR ' + formatter.format(value / 100);
      // Dues are signed (negative = the client is in advance); the words next to
      // the number say which it is, so the figure itself stays positive.
      const displayDues = value => 'PKR ' + formatter.format(Math.abs(value) / 100);
      const sourceHint = key => (receiveSources[key] && receiveSources[key].hint) || '';

      const isReceive = () => modeInput.value === 'receive';
      const isClientSource = () => isReceive() && sourceInput.value === 'client';
      const pickedClientCode = () => $('tx-client-code').value.trim();
      const pickedClient = () => clientList.find(item => item.code === pickedClientCode());
      // Signed paisa: positive means the client still owes us.
      let duesCents = null;

      /* ----- Searchable, data-driven picker (clients and parties) ----- */
      function createPicker(root) {
        const kind = root.dataset.pickerItems === 'parties' ? 'party' : 'client';
        const items = kind === 'party' ? partyList : clientList;
        const input = root.querySelector('[data-picker-input]');
        const list = root.querySelector('[data-picker-list]');
        const valueInput = root.querySelector('[data-picker-value]');
        const help = root.querySelector('[data-picker-help]');
        const toggle = root.querySelector('[data-picker-toggle]');
        const maxRows = 50;
        const defaultHelp = help ? help.textContent : '';
        let matches = [];
        let cursor = -1;
        let suppressInput = false;
        // The visible text of the current pick. Reopening the list must not
        // filter by it: "Ali Traders (C-001)" would match nothing.
        let selectedText = '';

        const haystack = item => [item.name, item.code, item.phone, item.category, item.type_label, item.note]
          .filter(Boolean).join(' ').toLowerCase();
        const displayText = item => (kind === 'party' ? item.name : item.name + (item.code ? ' (' + item.code + ')' : ''));

        function close() {
          list.hidden = true;
          input.setAttribute('aria-expanded', 'false');
          cursor = -1;
        }

        // Rows are built from text nodes only: client and party names are user
        // data and must never reach the DOM as markup.
        function row(item, index) {
          const option = document.createElement('div');
          option.className = 'aw-picker-item';
          option.setAttribute('role', 'option');
          option.id = list.id + 'Option' + index;
          option.dataset.index = String(index);
          const name = document.createElement('span');
          name.className = 'aw-picker-name';
          name.textContent = item.name;
          const meta = document.createElement('span');
          meta.className = 'aw-picker-meta';
          if (kind === 'party') {
            const badge = document.createElement('span');
            badge.className = 'aw-picker-badge' + (item.party_type === 'loan' ? ' is-loan' : '');
            badge.textContent = item.type_label || 'Other';
            meta.append(badge);
            option.append(name, meta);
            return option;
          }
          const code = document.createElement('span');
          code.className = 'aw-picker-code';
          code.textContent = item.code || '';
          option.append(code, name);
          if (item.phone) {
            meta.textContent = item.phone;
            option.append(meta);
          }
          return option;
        }

        function render() {
          const typed = input.value.trim();
          const tokens = (typed && typed !== selectedText ? typed.toLowerCase() : '').split(/\s+/).filter(Boolean);
          matches = items.filter(item => tokens.every(token => haystack(item).includes(token)));
          list.replaceChildren();
          cursor = -1;
          const current = valueInput.value.trim();
          const shown = matches.slice(0, maxRows);
          shown.forEach((item, index) => {
            const option = row(item, index);
            const value = kind === 'party' ? String(item.id) : (item.code || '');
            option.setAttribute('aria-selected', String(Boolean(current) && value === current));
            list.append(option);
          });
          const foot = document.createElement('div');
          foot.className = 'aw-picker-foot' + (matches.length ? '' : ' is-empty');
          if (!items.length) {
            foot.textContent = kind === 'party'
              ? 'No saved parties yet — type a name to add one.'
              : 'No active clients yet. Add one from the Clients page.';
          } else if (!matches.length) {
            foot.textContent = kind === 'party'
              ? 'No party matches “' + typed + '”. Press Enter to add it as a new party.'
              : 'No client matches “' + typed + '”.';
          } else if (matches.length > shown.length) {
            foot.textContent = 'Showing ' + shown.length + ' of ' + matches.length + ' — keep typing to narrow the list.';
          } else {
            foot.textContent = matches.length + (kind === 'party' ? ' parties' : ' clients') + ' shown.';
          }
          list.append(foot);
          list.hidden = false;
          input.setAttribute('aria-expanded', 'true');
        }

        function moveCursor(step) {
          const rows = Array.from(list.querySelectorAll('.aw-picker-item'));
          if (!rows.length) return;
          cursor = (cursor + step + rows.length) % rows.length;
          rows.forEach((row, index) => row.classList.toggle('is-cursor', index === cursor));
          // Guarded: not every DOM implementation (or test harness) lays out.
          if (rows[cursor].scrollIntoView) rows[cursor].scrollIntoView({ block: 'nearest' });
          input.setAttribute('aria-activedescendant', rows[cursor].id);
        }

        // Selecting writes the hidden value the server reads and keeps the
        // visible text human; typing again clears the value so a stale
        // selection can never be submitted behind an edited name.
        function select(item) {
          suppressInput = true;
          input.value = displayText(item);
          suppressInput = false;
          selectedText = input.value;
          valueInput.value = kind === 'party' ? String(item.id) : (item.code || '');
          if (kind === 'party') $('tx-party-name').value = item.name || '';
          close();
          onPick(item);
        }

        function acceptTyped() {
          if (kind !== 'party') return false;
          const name = input.value.trim();
          if (!name) return false;
          suppressInput = true;
          input.value = name;
          suppressInput = false;
          selectedText = name;
          valueInput.value = '';
          $('tx-party-name').value = name;
          close();
          onPick(null);
          return true;
        }

        function onPick(item) {
          if (kind === 'party') {
            const typeSelect = $('tx-party-type');
            typeSelect.disabled = Boolean(item);
            if (item) typeSelect.value = item.party_type || 'other';
            help.textContent = item
              ? (item.name + ' is saved as “' + (item.type_label || 'Other') + '”.')
              : defaultHelp;
          } else {
            loadDues(valueInput.value.trim(), item);
          }
          updatePreview();
        }

        input.addEventListener('focus', render);
        input.addEventListener('input', () => {
          if (suppressInput) return;
          // Manual editing invalidates the hidden selection immediately.
          selectedText = '';
          valueInput.value = '';
          if (kind === 'party') {
            $('tx-party-name').value = input.value.trim();
            $('tx-party-type').disabled = false;
          } else {
            loadDues('', null);
          }
          render();
          updatePreview();
        });
        input.addEventListener('keydown', event => {
          if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            if (list.hidden) render();
            moveCursor(event.key === 'ArrowDown' ? 1 : -1);
            return;
          }
          if (event.key === 'Enter') {
            if (list.hidden) return;
            event.preventDefault();
            if (cursor >= 0 && matches[cursor]) select(matches[cursor]);
            else acceptTyped();
            return;
          }
          if (event.key === 'Escape') { event.preventDefault(); close(); }
        });
        list.addEventListener('mousedown', event => {
          // mousedown (not click) so the input has not blurred yet.
          const option = event.target.closest('.aw-picker-item');
          if (!option) return;
          event.preventDefault();
          select(matches[Number(option.dataset.index)]);
        });
        if (toggle) toggle.addEventListener('mousedown', event => {
          event.preventDefault();
          if (list.hidden) { input.focus(); render(); } else close();
        });
        document.addEventListener('click', event => {
          if (!root.contains(event.target)) close();
        });
        return {
          close,
          // Restore a server-rendered selection after a failed submit.
          restore(value) {
            if (!value) return;
            const item = items.find(candidate => String(kind === 'party' ? candidate.id : candidate.code) === String(value));
            if (item) select(item);
            else if (kind === 'party') acceptTyped();
          },
          setText(text) {
            suppressInput = true;
            input.value = text || '';
            suppressInput = false;
            selectedText = input.value;
          }
        };
      }

      const pickers = {};
      document.querySelectorAll('[data-picker]').forEach(root => {
        pickers[root.dataset.picker] = createPicker(root);
      });

      /* ----- Payable amount for the picked client ----- */
      let duesToken = 0;
      function duesUnavailable(message) {
        duesCents = null;
        const panel = $('tx-client-dues');
        panel.hidden = false;
        panel.classList.remove('is-loading', 'is-payable', 'is-advance', 'is-settled');
        $('tx-client-payable').textContent = '—';
        $('tx-client-dues-note').textContent = message;
        updatePreview();
      }

      function loadDues(code, client) {
        const panel = $('tx-client-dues');
        if (!code) {
          duesToken += 1;
          duesCents = null;
          panel.hidden = true;
          updatePreview();
          return;
        }
        const token = ++duesToken;
        panel.hidden = false;
        panel.classList.add('is-loading');
        panel.classList.remove('is-payable', 'is-advance', 'is-settled');
        $('tx-client-payable').textContent = '…';
        $('tx-client-dues-note').textContent = 'Checking what ' + ((client && client.name) || 'this client') + ' owes…';
        if (!duesUrl || typeof window.fetch !== 'function') {
          duesUnavailable('The payable amount could not be checked in this browser. The receipt still saves against the client ledger.');
          return;
        }
        window.fetch(duesUrl + '?client_code=' + encodeURIComponent(code), {
          headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store'
        })
          .then(response => (response.ok ? response.json() : Promise.reject(new Error(response.status))))
          .then(payload => {
            // A newer pick supersedes this answer; never paint a stale balance.
            if (token !== duesToken) return;
            panel.classList.remove('is-loading');
            if (!payload || !payload.found || payload.balance === null || payload.balance === undefined) {
              duesUnavailable('This client has no ledger balance yet, so nothing is payable.');
              return;
            }
            duesCents = cents(payload.balance);
            const name = payload.name || (client && client.name) || 'This client';
            const status = payload.status || 'settled';
            panel.classList.toggle('is-payable', status === 'payable');
            panel.classList.toggle('is-advance', status === 'advance');
            panel.classList.toggle('is-settled', status === 'settled');
            $('tx-client-payable').textContent = displayDues(duesCents);
            $('tx-client-dues-note').textContent = status === 'payable'
              ? name + ' owes ' + displayDues(duesCents) + '. What you receive here reduces that balance.'
              : (status === 'advance'
                ? name + ' already has an advance of ' + displayDues(duesCents) + '. A receipt adds to it.'
                : name + ' has no outstanding dues. A receipt is kept as an advance.');
            updatePreview();
          })
          .catch(() => {
            if (token !== duesToken) return;
            panel.classList.remove('is-loading');
            duesUnavailable('The payable amount could not be loaded right now. The receipt still saves against the client ledger.');
          });
      }

      function updateSubcategories(preserve) {
        const previous = preserve ? subcategoryInput.value : '';
        const category = txCategories.find(item => item.name === categoryInput.value);
        const children = category ? category.subcategories : [];
        subcategoryInput.replaceChildren(new Option('No subcategory', ''));
        children.forEach(name => subcategoryInput.add(new Option(name, name)));
        if (children.includes(previous)) subcategoryInput.value = previous;
        $('tx-subcategory-field').hidden = !children.length;
        subcategoryInput.disabled = !children.length;
      }

      function updateCategories(preserve) {
        const previous = preserve ? categoryInput.value : '';
        const direction = modeInput.value === 'receive' ? 'in' : 'out';
        let hasPrevious = false;
        Array.from(categoryInput.options).forEach(option => {
          if (!option.value) return;
          const category = txCategories.find(item => item.name === option.value);
          const visible = Boolean(category && ['both', direction].includes(category.direction));
          option.hidden = !visible;
          option.disabled = !visible;
          if (visible && option.value === previous) hasPrevious = true;
        });
        categoryInput.value = hasPrevious ? previous : '';
        $('tx-category-help').textContent = 'Only active ' + (direction === 'in' ? 'money-in' : 'money-out') + ' categories are shown.';
        updateSubcategories(preserve && hasPrevious);
      }

      /* ----- Who the money came from, and what that enables ----- */
      function receivedFromLabel() {
        if (!isReceive()) return '';
        if (sourceInput.value === 'client') {
          const client = pickedClient();
          return client ? client.name : (pickedClientCode() || ($('tx-client-search').value.trim() || '—'));
        }
        if (sourceInput.value === 'party') {
          const id = $('tx-party-id').value;
          const party = partyList.find(item => String(item.id) === String(id));
          return party ? party.name : ($('tx-party-search').value.trim() || '—');
        }
        return $('tx-receive-other').value.trim() || '—';
      }

      // A discount settles part of a client's dues without cash arriving, so a
      // discount-only entry is the one case where the amount may stay 0 and no
      // destination account is needed.
      const isDiscountOnly = () => isClientSource() && amountCents() === 0 && discountCents() > 0;

      function applyReceiveSource() {
        const receive = isReceive();
        const source = receive ? sourceInput.value : 'other';
        document.querySelectorAll('[data-tx-source-panel]').forEach(panel => {
          const active = receive && panel.dataset.txSourcePanel === source;
          panel.hidden = !active;
          panel.disabled = !active;
        });
        document.querySelectorAll('[data-tx-source]').forEach(tab => {
          const active = receive && tab.dataset.txSource === source;
          tab.classList.toggle('is-active', active);
          tab.setAttribute('aria-pressed', active ? 'true' : 'false');
        });
        $('tx-source-hint').textContent = receive ? sourceHint(source) : '';

        // A client receipt is posted to the client ledger, which carries no
        // cash-flow category, so the category block only applies otherwise.
        const clientSource = isClientSource();
        if (categorySection) {
          const hiddenByMode = modeInput.value === 'transfer';
          categorySection.hidden = hiddenByMode || clientSource;
          categorySection.disabled = hiddenByMode || clientSource;
        }
        $('tx-category-client-note').hidden = !clientSource;

        const discountAllowed = clientSource;
        $('tx-discount-field').hidden = !discountAllowed;
        $('tx-discount-reason-field').hidden = !discountAllowed;
        discountInput.disabled = !discountAllowed;
        discountReasonInput.disabled = !discountAllowed;
        if (!discountAllowed) { discountInput.value = ''; discountReasonInput.value = ''; }

        const discountOnly = isDiscountOnly();
        amountInput.required = !discountOnly;
        amountInput.min = discountOnly ? '0' : '0.01';
        if (receiveAccountInput) receiveAccountInput.required = !discountOnly;
        $('tx-amount-help').textContent = discountOnly
          ? 'Discount only: no cash is received, so 0 is allowed and no account is needed.'
          : (clientSource ? 'Leave 0 and enter a discount to waive part of the dues.' : '');
        const needsMoney = clientSource && amountCents() === 0 && discountCents() === 0;
        amountInput.setCustomValidity(needsMoney ? 'Enter an amount to receive, or a discount.' : '');
        discountReasonInput.required = discountCents() > 0;
        $('tx-discount-reason-label').innerHTML = 'Discount reason' + (discountCents() > 0 ? ' <span aria-hidden="true">*</span>' : '');
        $('tx-reference-help').hidden = !clientSource;
        $('tx-amount-label').innerHTML = (discountOnly ? 'Amount received (PKR)' : 'Amount (PKR)') + (discountOnly ? '' : ' <span aria-hidden="true">*</span>');
      }

      function updatePreview() {
        const mode = modeInput.value;
        const amount = amountCents();
        const discount = discountCents();
        const sourceId = mode === 'pay' ? $('tx-pay-account').value : $('tx-transfer-from').value;
        const destinationId = mode === 'receive' ? $('tx-receive-account').value : $('tx-transfer-to').value;
        const source = selectedAccount(sourceId);
        const destination = selectedAccount(destinationId);
        const balanceAccount = mode === 'receive' ? destination : source;
        const sameAccount = mode === 'transfer' && sourceId && sourceId === destinationId;
        let remaining = null;
        if (balanceAccount) {
          remaining = cents(balanceAccount.current_balance) + (mode === 'receive' ? amount : -amount);
        }
        const insufficient = remaining !== null && remaining < 0;
        $('tx-preview-amount').textContent = displayMoney(amount);
        $('tx-preview-from').textContent = mode === 'receive' ? receivedFromLabel() : accountName(sourceId);
        $('tx-preview-to').textContent = mode === 'pay' ? ($('tx-party').value.trim() || '—') : accountName(destinationId);
        $('tx-preview-from-label').textContent = mode === 'receive' ? 'Received from' : 'From';
        $('tx-preview-to-label').textContent = mode === 'pay' ? 'Paid to' : (mode === 'receive' ? 'Received into' : 'To');
        $('tx-preview-balance-label').textContent = balanceAccount ? balanceAccount.name + ' · balance after transaction' : 'Balance after transaction';
        $('tx-preview-balance').textContent = remaining === null ? '—' : displayMoney(remaining);
        $('tx-preview-balance').classList.toggle('aw-negative', insufficient);
        $('tx-same-account').hidden = !sameAccount;
        $('tx-transfer-to').setCustomValidity(sameAccount ? 'Choose two different accounts.' : '');
        $('tx-funds-warning').hidden = !insufficient;
        $('tx-funds-warning').textContent = insufficient ? 'Insufficient funds. Choose another account or enter a smaller amount.' : '';

        const showDues = isClientSource() && Boolean(pickedClientCode());
        $('tx-preview-dues').hidden = !showDues;
        if (showDues) {
          const known = duesCents !== null;
          const after = known ? duesCents - amount - discount : null;
          $('tx-preview-payable').textContent = known ? displayDues(duesCents) : '—';
          $('tx-preview-received').textContent = displayMoney(amount);
          $('tx-preview-discount').textContent = displayMoney(discount);
          $('tx-preview-remaining').textContent = after === null ? '—' : (after < 0 ? 'Advance ' + displayDues(after) : displayMoney(after));
          $('tx-preview-remaining').classList.toggle('aw-negative', after !== null && after > 0);
        }

        const discountOnly = isDiscountOnly();
        $('tx-preview-help').textContent = mode === 'transfer'
          ? 'Both account balances update together. Total business money stays the same.'
          : (discountOnly
            ? 'Discount only: no cash moves, so account balances stay the same and the client owes less.'
            : (balanceAccount && amount ? 'This preview includes the amount above. Your saved balance is checked again on the server.' : 'Select an account and enter an amount to preview the balance.'));
        const transferUnavailable = mode === 'transfer' && accountList.filter(account => account.is_active).length < 2;
        submit.disabled = Boolean(sameAccount || insufficient || transferUnavailable);
        submit.querySelector('span').textContent = discountOnly ? 'Save discount' : submitLabels[mode];
        document.querySelectorAll('[data-balance-help]').forEach(help => {
          const key = help.dataset.balanceHelp;
          const id = key === 'receive' ? $('tx-receive-account').value
            : key === 'pay' ? $('tx-pay-account').value
            : key === 'transfer-from' ? sourceId : destinationId;
          const account = selectedAccount(id);
          help.textContent = account ? 'Current balance: ' + displayMoney(cents(account.current_balance)) : 'Only active business accounts are available.';
        });
        applyReceiveSource();
      }

      function setMode(mode, updateUrl) {
        if (!Object.prototype.hasOwnProperty.call(descriptions, mode)) return;
        modeInput.value = mode;
        document.querySelectorAll('[data-tx-mode]').forEach(link => {
          const active = link.dataset.txMode === mode;
          link.classList.toggle('is-active', active);
          if (active) link.setAttribute('aria-current', 'true');
          else link.removeAttribute('aria-current');
        });
        document.querySelectorAll('[data-tx-panel]').forEach(panel => {
          const active = panel.dataset.txPanel.split(' ').includes(mode);
          panel.hidden = !active;
          panel.disabled = !active;
        });
        $('tx-intro').textContent = descriptions[mode];
        updateCategories(true);
        updatePreview();
        if (updateUrl) {
          const url = new URL(window.location.href);
          url.searchParams.set('mode', mode);
          history.replaceState(null, '', url);
        }
      }

      function setSource(source) {
        if (!sourceKeys.includes(source)) source = 'client';
        sourceInput.value = source;
        updatePreview();
      }

      document.querySelectorAll('[data-tx-mode]').forEach(link => {
        link.addEventListener('click', event => {
          if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
          event.preventDefault();
          setMode(link.dataset.txMode, true);
        });
      });
      document.querySelectorAll('[data-tx-source]').forEach(tab => {
        tab.addEventListener('click', () => setSource(tab.dataset.txSource));
      });
      transactionForm.addEventListener('input', updatePreview);
      transactionForm.addEventListener('change', updatePreview);
      categoryInput.addEventListener('change', () => updateSubcategories(false));
      transactionForm.addEventListener('submit', event => {
        updatePreview();
        if (submit.disabled || !transactionForm.checkValidity()) {
          event.preventDefault();
          transactionForm.reportValidity();
          return;
        }
        if (isClientSource() && !pickedClientCode()) {
          event.preventDefault();
          $('tx-client-search').setCustomValidity('Select a client from the list.');
          $('tx-client-search').reportValidity();
          $('tx-client-search').focus();
          return;
        }
        $('tx-client-search').setCustomValidity('');
        // Keep the server-generated key on retries. The server's unique index
        // prevents a double click or repeated POST from posting twice.
        submit.disabled = true;
        submit.querySelector('span').textContent = 'Saving…';
      });
      window.addEventListener('pageshow', () => {
        submit.querySelector('span').textContent = isDiscountOnly() ? 'Save discount' : submitLabels[modeInput.value];
        updatePreview();
      });

      // Restore a picked client/party after a server-side validation failure:
      // the hidden value survives the round trip, the visible text does not.
      if (pickers.client && $('tx-client-code').value) pickers.client.restore($('tx-client-code').value);
      if (pickers.party) {
        if ($('tx-party-id').value) pickers.party.restore($('tx-party-id').value);
        else if ($('tx-party-name').value) {
          pickers.party.setText($('tx-party-name').value);
          $('tx-party-type').disabled = false;
        }
      }
      setMode(modeInput.value, false);
    }

    /* ----- Shared confirm dialog ----- */
    const confirmForm = $('confirmForm');
    document.querySelectorAll('[data-confirm-url]').forEach(btn => {
      btn.addEventListener('click', () => {
        if (!confirmForm) return;
        confirmForm.action = btn.dataset.confirmUrl;
        $('confirmModalTitle').textContent = btn.dataset.confirmTitle || 'Confirm';
        $('confirmModalBody').textContent = btn.dataset.confirmBody || '';
        $('confirm-revision').value = btn.dataset.revision || '';
        $('confirm-active').value = btn.dataset.active || '';
        const submit = $('confirmSubmit');
        submit.textContent = btn.dataset.confirmLabel || 'Confirm';
        submit.className = 'aw-btn aw-btn-' + (btn.dataset.confirmTone || 'primary');
        modal('confirmModal').show();
      });
    });

    [accountForm, categoryForm, confirmForm].forEach(form => {
      if (!form) return;
      form.addEventListener('submit', () => {
        const btn = form.querySelector('button[type="submit"]');
        if (btn) { btn.disabled = true; btn.dataset.originalText = btn.textContent; btn.textContent = 'Saving…'; }
      });
    });

    ['accountModal', 'categoryModal', 'confirmModal'].forEach(id => {
      const el = $(id);
      if (!el) return;
      el.addEventListener('shown.bs.modal', () => {
        const input = el.querySelector('input:not([type="hidden"]):not([disabled]), select:not([disabled]), textarea:not([disabled])');
        if (input) input.focus();
      });
      el.addEventListener('hidden.bs.modal', () => {
        const btn = el.querySelector('button[type="submit"]');
        if (btn && btn.dataset.originalText) { btn.disabled = false; btn.textContent = btn.dataset.originalText; }
      });
    });

    /* ----- Deep links and server-side validation returns ----- */
    const params = new URLSearchParams(window.location.search);
    if (data.modalOpen === 'account') {
      openAccount(data.editAccountId ? accounts.get(String(data.editAccountId)) : null);
    } else if (data.modalOpen === 'category') {
      openCategory(data.editCategoryId ? categories.get(String(data.editCategoryId)) : null);
    } else if (accountForm && params.get('new') === '1') {
      openAccount(null);
    } else if (accountForm && params.get('edit')) {
      openAccount(accounts.get(params.get('edit')));
    } else if (categoryForm && params.get('new') === '1') {
      openCategory(null);
    } else if (categoryForm && params.get('edit')) {
      openCategory(categories.get(params.get('edit')));
    }
    if (params.has('new') || params.has('edit')) {
      params.delete('new'); params.delete('edit');
      const qs = params.toString();
      history.replaceState(null, '', window.location.pathname + (qs ? '?' + qs : ''));
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
