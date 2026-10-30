# Day Close, Daily Reconciliation & Atomic Permissions — Audit & Upgrade

**Date:** 2026-10-30
**Branch:** `arena/5749f810-rent-copyams-git-copyvscode-re`
**Scope:** (1) the day-close / daily reconciliation flow — why "editing and adding a custom amount to settle" only worked sometimes; (2) a study of the whole app's permission system and an atomic-permission upgrade so the owner can choose access per action as the app grows.

---

## Part 1 — Day close & daily reconciliation

### How it works

Two linked surfaces:

| Surface | Where | What it does |
|---|---|---|
| **Daily Cash & Bank Reconciliation board** | `/daily_reconciliation` (also embedded on `/cash_flow` → Reconcile tab) | Per money account: OPENING / IN / OUT / TRANSFER IN / OUT / EXPECTED CLOSING, the operator's **Actual Counted** figure and the DIFFERENCE. **Verify & Lock** freezes every account's counted figure and carries it to the next day's opening. Unlock reverts (counts survive as drafts). |
| **Per-account settlement** | Accounts → account ledger → Reconcile (`/accounts/<id>/reconcile`) | Compares the live ledger against a physically entered actual balance, writes an immutable `AccountReconciliation` snapshot and posts the difference as an **Adjustment** ledger entry (audited, minor-unit exact, refuses dates earlier than an existing later reconciliation). |

The old `/cash_flow_differences` page is now a read-only history of the legacy day-difference records.

### Why "sometimes the edit / custom amount worked and sometimes not" — root causes found & fixed

1. **Comma-formatted numbers were silently rejected.** The board's save used a bare `float("9,500")` in the route, so pasting a thousands-separated figure (the app accepts those on GRN/expense forms) failed with a generic "Enter a valid counted amount" — while plain digits worked. **Fix:** a shared `_parse_counted` parser (commas/spaces tolerated, NaN/inf rejected, paisa-rounded) with the real reason surfaced to the row and the flash message.
2. **Editing behind a lock "worked" — and corrupted the chain.** `save_count`/`clear_count` never checked the lock: on a locked day you could overwrite (or delete!) the frozen count that feeds the next day's opening, silently rewriting closed history. The UI only hid the little ✕ button — the server accepted everything. **Fix:** both the service and the route now refuse with *"Day YYYY-MM-DD is locked. Unlock it first…"*; the board disables the input and buttons on locked rows; the correct path is Unlock → correct → Lock again.
3. **Back-dated entries into a locked day drifted invisibly.** Locking does not block a new transaction dated into the locked day (by design — monthly period locks do that), but the recomputed EXPECTED then diverged from the frozen snapshot with nothing on screen telling you. **Fix:** each locked row now carries `expected_at_lock` and a `drift_after_lock` figure; the board shows a **drift badge** on the expected closing so the mismatch is visible at a glance.
4. **No permission on the board at all.** Any user who could open the page could save counts and lock/unlock financial days. **Fix:** all POST actions (save/lock/unlock/clear) now require the new **`can_close_cash_day`** permission (admin/root bypass; GET stays behind `can_view_cash_flow`).
5. **No audit trail.** Save/lock/unlock/clear now write `cash_day_reconciliation` events to the activity log with day, account and amount.
6. **Difference could not be settled from the board.** When counted ≠ expected the row just stayed red. The board now links **Settle** straight to the account's reconciliation page, which posts the difference as a proper ledger Adjustment (that is the "custom amount to settle" done correctly — with an immutable audit snapshot, not a silent edit).

### Files changed (Part 1)

- `app/services/cash_day_recon.py` — `_parse_counted`, lock guards on `save_count`/`delete_count`, `expected_at_lock`/`drift_after_lock` in positions.
- `app/blueprints/reports/cash.py` — permission gate, real error messages, audit log.
- `templates/_daily_recon_board.html` — disabled controls on locked rows, drift badge, short error text in the row state, **Settle** link for unmatched rows.

---

## Part 2 — Atomic permission management

### What the app had

* `User.role` ∈ {admin, root, user} — admin/root bypass every check.
* 23 module-level boolean columns (`can_manage_sales`, `can_manage_accounts`, …) rendered as grouped checkboxes from `PERMISSION_GROUPS` in Settings → Users.
* `ENDPOINT_PERMISSION_MAP` (~190 endpoints) enforced by a `before_request` hook, plus inline checks inside ~36 views, plus `access_mode` (global read-only) and `restrict_backdated_edit`.

**Gap:** one switch covered whole modules — *view* and *act* were the same key. `can_manage_accounts` meant "see Accounts" **and** "void payments, close days, settle accounts, create accounts". As the app grew, there was no way to say "this clerk records payments but can never delete history" or "this accountant settles accounts but cannot edit account masters".

### What was added (additive, auto-migrated, backwards compatible)

Four **atomic money-control switches** in a new Settings group *Money Controls (atomic)*:

| Permission | Default | Controls |
|---|---|---|
| `can_void_transactions` | **ON** (preserves today's behaviour) | Void / delete / restore of client payments, supplier payments and account-ledger rows — now requires this **on top of** the module grant |
| `can_close_cash_day` | OFF | Save counted balances, Verify & Lock, Unlock, Clear on the daily reconciliation board |
| `can_reconcile_accounts` | OFF | Per-account settlement (reconcile + Adjustment posting) |
| `can_manage_account_masters` | OFF | Create/edit/archive accounts & account categories (was hard-wired to admin/root) |

Mechanics that keep existing databases safe:

* Columns are added by the boot migration (`_ensure_model_columns`); **NULL on existing users resolves through `USER_PERMISSION_DEFAULTS`**, so nobody gains or loses access until the admin changes a switch.
* New switches flow through the existing form saver (`EDITABLE_USER_PERMISSION_FIELDS`), the Feature × User matrix and the add/edit-user checkboxes automatically.

### Role presets — "make it easy to choose"

The Create User modal now shows one-click **role presets** rendered from a new `ROLE_PRESETS` registry (data, not code):

**Cashier** (payments + day close) · **Salesman** (bookings/sales views) · **Accountant** (full finance: accounts, cash flow, day close, settlement, masters, ledgers, reports) · **Store Keeper** (GRN + stock) · **Manager** (everything default-on) · **Viewer (read only)** (view-only set). Apply a preset, then fine-tune the atomic switches underneath — combined with the existing *Access Mode: Read Only* toggle this gives store-level (all actions) and record-level (void) control.

### Files changed (Part 2)

- `models/core.py` — 4 new User columns.
- `app/services/constants.py` — defaults, editable fields, new permission group, `ROLE_PRESETS`.
- `blueprints/accounts/accounts_crud.py` — `_account_master_permission_ok` (grantable), `_reconcile_permission_ok` + reconcile POST gate.
- `blueprints/accounts/payments.py` — `_payments_void_ok`; gates on client void/restore, supplier delete/restore.
- `blueprints/accounts/transactions.py` — `_void_permission_ok`; gates on ledger delete/restore.
- `templates/settings.html` — preset bar + engine, badges for the four switches, preset data.
- `app/blueprints/system/settings.py` — presets passed to the page.

---

## Tests

`tests/test_day_close_and_permissions.py` — **9 new regression tests** (all passing):

* locked day refuses save/clear (service + route, readable message)
* '9,500.50' style counts save correctly
* locked count carries to the next day's opening
* back-dated entry into a locked day shows `drift_after_lock`
* `can_close_cash_day` gate (denied → granted)
* `can_reconcile_accounts` gate (denied → granted, adjustment row created)
* `can_manage_account_masters` gate (403 → 200)
* void gate on payments (default-allowed, then blocked when switched off)
* settings page renders presets + all four switches

Full suite: **470 passed, 6 skipped** (461 before + 9 new).

## Verification

```bash
.venv/bin/python -m pytest tests/test_day_close_and_permissions.py tests/test_cash_day_reconciliation_page.py -q
.venv/bin/python -m pytest tests/ -q            # full suite
```
