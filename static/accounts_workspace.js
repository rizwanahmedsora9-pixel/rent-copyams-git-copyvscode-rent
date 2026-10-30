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

    /* ----- New Transaction: mode switching, category cascade, live preview ----- */
    const transactionForm = $('transactionForm');
    if (transactionForm) {
      const accountList = data.accounts || [];
      const txCategories = data.transactionCategories || [];
      const modeInput = $('tx-mode');
      const amountInput = $('tx-amount');
      const categoryInput = $('tx-category');
      const subcategoryInput = $('tx-subcategory');
      const submit = $('tx-submit');
      const formatter = new Intl.NumberFormat('en-PK', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      const descriptions = {
        receive: 'Record money received into a business account.',
        pay: 'Record a business expense or payment out of an account.',
        transfer: 'Move money between two of your own accounts. This is not income or an expense.'
      };
      const submitLabels = { receive: 'Receive money', pay: 'Record payment', transfer: 'Transfer money' };
      const selectedAccount = id => accountList.find(account => String(account.id) === String(id));
      const accountName = id => { const account = selectedAccount(id); return account ? account.name : '—'; };
      const cents = value => {
        const number = Number(value);
        return Number.isFinite(number) ? Math.round(number * 100) : 0;
      };
      const amountCents = () => {
        const value = amountInput.value;
        if (!value || !/^\d+(?:\.\d{1,2})?$/.test(value)) return 0;
        const [whole, fraction = ''] = value.split('.');
        const result = Number(whole) * 100 + Number(fraction.padEnd(2, '0'));
        return Number.isSafeInteger(result) && result > 0 && result <= 99999999999999 ? result : 0;
      };
      const displayMoney = value => 'PKR ' + formatter.format(value / 100);

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

      function updatePreview() {
        const mode = modeInput.value;
        const amount = amountCents();
        const sourceId = mode === 'pay' ? $('tx-pay-account').value : $('tx-transfer-from').value;
        const destinationId = mode === 'receive' ? $('tx-receive-account').value : $('tx-transfer-to').value;
        const source = selectedAccount(sourceId);
        const destination = selectedAccount(destinationId);
        const balanceAccount = mode === 'receive' ? destination : source;
        const sameAccount = mode === 'transfer' && sourceId && sourceId === destinationId;
        let remaining = null;
        if (balanceAccount && amount) {
          remaining = cents(balanceAccount.current_balance) + (mode === 'receive' ? amount : -amount);
        }
        const insufficient = remaining !== null && remaining < 0;
        $('tx-preview-amount').textContent = displayMoney(amount);
        $('tx-preview-from').textContent = mode === 'receive' ? ($('tx-party').value.trim() || '—') : accountName(sourceId);
        $('tx-preview-to').textContent = mode === 'pay' ? ($('tx-party').value.trim() || '—') : accountName(destinationId);
        $('tx-preview-from-label').textContent = mode === 'receive' ? 'Received from' : 'From';
        $('tx-preview-to-label').textContent = mode === 'pay' ? 'Paid to' : (mode === 'receive' ? 'Received into' : 'To');
        $('tx-party-label').firstChild.textContent = (mode === 'receive' ? 'Received from ' : 'Paid to ');
        $('tx-party').placeholder = mode === 'receive' ? 'Person or business' : 'Person, business, or expense';
        $('tx-preview-balance-label').textContent = balanceAccount ? balanceAccount.name + ' · balance after transaction' : 'Balance after transaction';
        $('tx-preview-balance').textContent = remaining === null ? '—' : displayMoney(remaining);
        $('tx-preview-balance').classList.toggle('aw-negative', insufficient);
        $('tx-same-account').hidden = !sameAccount;
        $('tx-transfer-to').setCustomValidity(sameAccount ? 'Choose two different accounts.' : '');
        $('tx-funds-warning').hidden = !insufficient;
        $('tx-funds-warning').textContent = insufficient ? 'Insufficient funds. Choose another account or enter a smaller amount.' : '';
        $('tx-preview-help').textContent = mode === 'transfer'
          ? 'Both account balances update together. Total business money stays the same.'
          : (balanceAccount && amount ? 'This preview includes the amount above. Your saved balance is checked again on the server.' : 'Select an account and enter an amount to preview the balance.');
        const transferUnavailable = mode === 'transfer' && accountList.filter(account => account.is_active).length < 2;
        submit.disabled = Boolean(sameAccount || insufficient || transferUnavailable);
        submit.querySelector('span').textContent = submitLabels[mode];
        document.querySelectorAll('[data-balance-help]').forEach(help => {
          const key = help.dataset.balanceHelp;
          const id = key === 'receive' ? $('tx-receive-account').value
            : key === 'pay' ? $('tx-pay-account').value
            : key === 'transfer-from' ? sourceId : destinationId;
          const account = selectedAccount(id);
          help.textContent = account ? 'Current balance: ' + displayMoney(cents(account.current_balance)) : 'Only active business accounts are available.';
        });
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

      document.querySelectorAll('[data-tx-mode]').forEach(link => {
        link.addEventListener('click', event => {
          if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
          event.preventDefault();
          setMode(link.dataset.txMode, true);
        });
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
        // Keep the server-generated key on retries. The server's unique index
        // prevents a double click or repeated POST from posting twice.
        submit.disabled = true;
        submit.querySelector('span').textContent = 'Saving…';
      });
      window.addEventListener('pageshow', () => {
        submit.querySelector('span').textContent = submitLabels[modeInput.value];
        updatePreview();
      });
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
