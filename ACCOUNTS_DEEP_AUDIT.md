# Accounts Section — Deep Audit & Improvement Report

**Date:** 2026-10-29
**Branch:** `arena/5749f810-rent-copyams-git-copyvscode-re`
**Scope:** every rupee entering or leaving the app — GRN purchases, sale receipts, client payments/refunds/waive-offs, booking advances, supplier & driver payments, cash flow, expenses, transfers, opening balances, reconciliation and the general-ledger book.
**Verdict:** the Accounts section is architecturally strong (three-layer design, void/restore everywhere, idempotency, period locks, double-entry book). This review found **3 real money-integrity defects and 2 reporting defects**, which are **fixed on this branch** with 6 new regression tests. Baseline suite: **455 passed / 0 failed** before the fixes; the same suite plus the new tests passes after.

---

## 1. How the Accounts section works today

The section is not one page — it is a **three-layer money pipeline**, and every screen in the app feeds it.

### 1.1 The three layers

| Layer | Tables | What it answers | Where you see it |
|---|---|---|---|
| **1. Source documents** (why money moved) | `Payment`, `SupplierPayment`, `DirectSale`, `Booking`, `GRN`(+items), `MaterialReturn`, `WaiveOff`, `DeliveryPersonPayment`, `CashFlowEntry` | *Who/what/why* — client, bill no., method, note, proof photo | Sales, Purchases, Payments pages, client/supplier ledgers |
| **2. Cash/bank mirror** (where money sits) | `Account` + `AccountTransaction` | *How much cash/bank the company has, per account, and every movement* | **Accounts** section: Dashboard, Account Ledger, Audit Trail |
| **3. General ledger book** (double entry) | `gl_account`, `journal_entry`, `journal_line`, `accounting_period`, `ledger_version` | *The formal books*: trial balance, P&L, balance sheet, books-agree | `/ledger/*` pages (Journal, Chart, Trial Balance, P&L, BS, Books Agree) |

Key mechanics that make it trustworthy:

* **Single effect applier.** Every movement applies to a balance through `_apply_account_tx_effect` / `_reverse_account_tx_effect` (`app/services/accounting.py`), which keep the float `balance` **and** the authoritative paisa `balance_minor` in lock-step.
* **Nothing is edited — it is voided and reversed.** `AccountTransaction.is_void` + `voided_by/at`, mirror rows are voided-and-recreated when a source document changes (`_sync_linked_*_tx`).
* **Idempotency + optimistic concurrency.** `Payment`/`DirectSale` carry `idempotency_key` (+payload hash) and `revision`; retried submissions replay instead of duplicating.
* **Source linkage.** Mirror rows carry `source_type`/`source_id` (plus a `[SRC:<Kind>:<id>]` note marker) so any ledger line traces back to its document in one hop.
* **Structured audit.** `accounting_audit_log` rows are written **in the same DB transaction** as the money change (`utils/accounting_audit.py`), with before/after JSON, amounts in paisa, IP and session.
* **Period locks.** `_assert_period_open` refuses money posts into a closed/reconciliation-locked period, including the GRN auto-payment path.
* **Reconciliation chain.** Per-account reconciliation snapshots (`account_reconciliation`), the daily cash & bank board (`cash_day_*`), day locks that carry counted closing into the next opening, and a `books_agree` page that checks debits=credits, trial balance, and book-vs-subledger for receivables/payables/inventory.
* **Auto-posting journal.** SQLAlchemy `before_commit` listeners (`app/services/ledger/autopost.py`) post a balanced journal entry for **14 document types** (see matrix) — one idempotent entry per event, append-only lines, reversals reference the entry they reverse.

### 1.2 Entry points into Accounts

| Entry point | Route(s) | Writes |
|---|---|---|
| Accounts Dashboard → Receive/Pay modal | `/accounts/transactions/new` | Client receipt/refund/waive-off (via `save_client_payment`), other-source receipt, account-category transfer, company transfer, supplier payment, driver payment (FIFO), client refund, loan repayment, expense |
| Client payments page | `/accounts/payments/clients` (+ legacy `/add_payment`) | `save_client_payment` → Payment + mirror Receipt/Refund/Loss |
| Supplier payments page | `/accounts/payments/suppliers`, `/pay_supplier` | `save_supplier_payment` → SupplierPayment + mirror |
| Transfers | `/accounts/transfers/add` | AccountTransaction Transfer (minor-safe) |
| GRN wizard | `/grn` | GRN.paid_amount → **auto SupplierPayment** (`[AUTO_GRN_PAY:n]`) → mirror Supplier Payment |
| Direct sale / Booking forms | sales module | paid-now → mirror Receipt (`[SRC:DirectSale:n]` / `[SRC:Booking:n]`) |
| Driver settlement | driver module & Accounts pay→driver | `DeliveryPersonPayment` → mirror Driver Payment (+waive-off Loss) |
| Material return | returns module | non-cash credit `Payment` (method `Material Return`) — settles dues without cash |
| Cash flow page | `/cash_flow` (read-only report) | fed by the same `AccountTransaction` rows; manual rows originate in Accounts |
| Openings | client/supplier/account edit forms | opening balance + adjustment ledger entries (audited, idempotent) |

### 1.3 KPI cards and drill-downs

Dashboard KPIs come from the **read-model book** (`app/services/read_models/dashboard.py`, cached against `ledger_version`), so the dashboard, ledgers and API share one definition of "received today". Drill-downs (`/accounts/api/kpi/*` and `/accounts/kpi/*` pages) list the underlying rows per card.

---

## 2. Money-flow coverage matrix (verified in code)

| # | Money event | Cash/bank effect | Client/Supplier/Driver ledger | Journal entry |
|---|---|---|---|---|
| 1 | Client payment (receipt) | IN ✔ mirror `Receipt` | ✔ | ✔ `payment` |
| 2 | Client refund | OUT ✔ mirror `Refund` | ✔ | ✔ |
| 3 | Waive-off / discount settlement | non-cash ✔ mirror `Loss` | ✔ `WaiveOff` | ✔ `8010` |
| 4 | Direct sale — paid now | IN ✔ | ✔ receivable | ✔ `direct_sale` |
| 5 | Direct sale — credit | — | ✔ due | ✔ |
| 6 | Booking advance | IN ✔ | ✔ advance | ✔ `booking_advance` |
| 7 | GRN purchase — paid at receive | OUT ✔ via auto `SupplierPayment` | ✔ payable | ✔ `grn` + `supplier_payment` |
| 8 | Supplier payment | OUT ✔ | ✔ | ✔ |
| 9 | Driver service payment | OUT ✔ | ✔ driver ledger FIFO | ✔ |
| 10 | Driver waive-off | non-cash ✔ `Loss` | ✔ | ✔ |
| 11 | Delivery rent | accrual only (correct — paid via #9) | ✔ driver payable | ✔ `delivery_rent` |
| 12 | Material return (goods back) | correctly **no cash** | ✔ negative settlement | ✔ |
| 13 | Manual cash-flow in / out / transfer | IN/OUT/INT ✔ | cash-flow report | ✔ `cash_flow_entry` |
| 14 | Other-source receipt / loan draw | IN ✔ | audit | ✔ `account_transaction` |
| 15 | Expense / other payment | OUT ✔ | expenditures page | ✔ |
| 16 | Loan repayment | INT ✔ (cash → loan account) | account ledger | ✔ |
| 17 | Openings (client/supplier/account) | baseline + adjustment entry | ✔ | ✔ `*_opening` |
| 18 | Reconciliation difference | signed adjustment entry (immutable) | ✔ | ✔ |

**Coverage conclusion:** every money in/out path in the app posts to all three layers. The gaps found were in *correctness of two report surfaces* and *detail captured per transaction*, not in missing coverage — with the one serious exception in §3.1.

---

## 3. Defects found in this review (all fixed on this branch)

### 3.1 🔴 CRITICAL — silent account-balance drift (authoritative minor-unit violation)

**Where:** `/accounts/transactions/new` (Dashboard Receive/Pay): *Receive → other source*, *Receive → from account*, *Pay → company transfer*, *Pay → loan*, *Pay → expense/other*.
**Bug:** these paths updated only the float `Account.balance` and **never `Account.balance_minor`**. Every other money path (client payments, supplier payments, transfers page, refunds, reconciliation) treats `balance_minor` as authoritative when present (`_account_minor()` prefers it). Consequence: after any manual receipt/expense, the **next minor-unit write silently re-computed the balance from the stale paisa value and erased the manual change** — real money disappearing from an account without any ledger trace.
**Fix:** all five paths now create the `AccountTransaction` first (with `amount_minor`), flush, and post the effect through the single `_apply_account_tx_effect()` — exactly like the transfers page and payment services already did. `created_by` is now stamped on these rows too.
**Regression test:** `test_accounts_money_controls.py::test_expense_entry_moves_balance_and_minor_together`, `::test_receive_other_source_...`, `::test_company_transfer_and_loan_repayment_...` — assert `balance` and `balance_minor` move together.

### 3.2 🟠 HIGH — Expenditures KPI drill-down showed the wrong rows

**Where:** `/accounts/api/kpi/expenditures` (Dashboard "Expenditures Today" drill-down).
**Bug:** the account-ledger part filtered `to_account_id IS NOT NULL` — that selects **internal movements** (transfers, loan repayments) and **misses every actual expense** (which has no destination account). Rows were labelled `Other Receipt`. The drill-down disagreed with the Expenditures page and with the KPI itself.
**Fix:** filter is now `from_account_id IS NOT NULL AND to_account_id IS NULL`; rows use the `category` key the drill-down table renders, real expense categories, and the method snapshot.

### 3.3 🟠 HIGH — Loan repayments were counted as expenditures

**Where:** `/accounts/expenditures` page + `/accounts/kpi/expenditures` page.
**Bug:** `Payment`-type rows with a destination account (loan repayments — money moving between two company accounts) were listed as spending, overstating expenditures.
**Fix:** both pages now require `to_account_id IS NULL` (money out to an outside party). Internal movements remain fully visible on the account ledger and audit trail.

### 3.4 🟠 HIGH — Receipts page mixed money IN with money OUT

**Where:** `/accounts/receipts`.
**Bugs:** (a) the only "receipts" listed were direct sales and **GRN paid amounts — which are payments to suppliers (money out)**; (b) the "Total Cash Movement" KPI **added** GRN outflows to sale receipts; (c) client payments — the largest money-in stream — were missing entirely, as were booking advances and other receipts.
**Fix:** rebuilt as a true money-in register with per-source subtotals: Client Payments, Direct Sale Receipts, Booking Advances, Other Receipts, **Total Money In**, plus GRN purchase payments in a clearly-labelled **Money Out** section; the header now shows **Net Movement = In − Out**. (The earlier fix that shows a GRN's full landed total is retained and still regression-tested.)

### 3.5 🟡 MEDIUM — expenses carried almost no structure

**Where:** Dashboard Pay → expense stored only a free-text description + note.
**Fix (additive, auto-migrated):** `AccountTransaction` gained six columns — `category`, `payee`, `reference_no`, `payment_method`, `photo_path`, `photo_url` (SQLite `ALTER TABLE` boot migration added in `app/services/schema.py`).
* The Pay → Expense form now captures **Expense Category** (shared book with Cash Flow's spending categories), **Paid To**, **Voucher/Bill No.**, and an optional **proof photo**; the category is validated against the managed category list.
* The **Account Ledger**, **Expenditures page** and **KPI drill-down** now display category / paid-to / voucher / proof.
* Search on the expenditures page covers payee & voucher; the category filter also matches the new category column and includes Cash Flow spending categories.

### 3.6 🟡 LOW — latent float-only void helper

`_reverse_balance_effect` (accounts helpers) was float-only like §3.1. It now delegates to the authoritative `_reverse_account_tx_effect`.

---

## 4. Files changed on this branch

| File | Change |
|---|---|
| `models/cash.py` | 6 new detail columns on `AccountTransaction` |
| `app/services/schema.py` | additive boot migration for the new columns |
| `blueprints/accounts/transactions.py` | §3.1 fix on all five manual paths; detail capture; `created_by` |
| `blueprints/accounts/helpers.py` | §3.6 fix |
| `blueprints/accounts/kpis.py` | §3.2 + §3.3 (API + KPI page), richer row payload |
| `blueprints/accounts/payments.py` | §3.3 expenditures page, §3.4 receipts rebuild, category options |
| `blueprints/accounts/dashboard.py` | expense categories passed to the Pay form |
| `templates/accounts/dashboard.html` | multipart form, Expense Details block, JS toggle |
| `templates/accounts/receipts.html` | rebuilt money-in register + Net Movement |
| `templates/accounts/expenditures.html` | Type / Paid To / Voucher / proof columns |
| `templates/accounts/account_ledger.html` | category / paid-to / voucher / proof in ledger lines |
| `templates/accounts/audit.html` | same detail badges on audit-trail lines |
| `tests/test_accounts_money_controls.py` | **6 new regression tests** (all passing) |

---

## 5. Recommended next improvements (prioritized, not yet implemented)

1. **Backfill one-time repair for drifted accounts.** Any account that received a manual entry *after* its last minor-unit write may still carry a stale `balance_minor`. Add a boot/consistency check: `|to_minor(balance) − balance_minor| > 0` → fix minor from the ledger replay (`opening_balance + Σ movements`) and log a `LedgerSyncEvent`. The consistency report already detects divergence — make it heal with a `--fix` flag.
2. **Expense approval / limits.** Now that expenses are structured, add per-role daily expense limits and an approval flag for entries above a threshold (rows already carry actor + audit trail, so this is a small policy layer).
3. **Recurring expenses.** A "repeat monthly" flag on structured expenses with a daily job that drafts the next occurrence (keeps one-posting-per-event idempotency by using `idempotency_key = <template>-<YYYY-MM>`).
4. **Bank-statement matching for bank accounts.** Import a CSV per bank account and auto-match to mirror rows by amount+date±2d, flagging unmatched rows on the reconciliation page — closes the loop between bank statements and the books.
5. **Attach proof photos to GRN/supplier payments too** (client payments already support photos) so every money-out has the same evidence trail as expenses now do.
6. **Single "Money Map" page.** One Accounts screen listing every source type with today/MTD inflow, outflow and net, linking to each register (client receipts, sale receipts, booking advances, GRN payments, supplier payments, driver payments, expenses, transfers, loans, refunds). The data all exists in `AccountTransaction`; this is a presentation win for the owner.
7. **Soft-lock the dashboard Receive/Pay modal per account** while a reconciliation for that account/day is locked (currently only the period gate blocks posting into closed periods).

---

## 6. How to verify

```bash
# full suite (≈21 min)
.venv/bin/python -m pytest tests/ -q

# just the new money-control regressions (≈1 min)
.venv/bin/python -m pytest tests/test_accounts_money_controls.py -q
```

Manual smoke: Accounts → *New Transaction* → Pay → *Personal/Other Expense* now reveals Category / Paid To / Voucher / Proof; after saving, the entry appears with those details under **Expenditures**, on the **Account Ledger** line, and the account balance stays consistent with its paisa baseline.
