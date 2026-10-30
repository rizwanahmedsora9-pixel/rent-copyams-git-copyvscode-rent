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
