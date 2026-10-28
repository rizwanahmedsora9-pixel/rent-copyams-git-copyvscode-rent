# Master plan — one book, many views

**Status:** §5 (S0 money/rounding) and the per-line discount part of §8 (S3) are **built and tested** — see "What is built today" below. The rest of the plan is still a proposal awaiting approval.
**Scope:** every money figure in AMS — how it is captured, stored, posted, and reported — plus a new per-line discount module and a single sync contract so no page computes its own version of the truth.
**Audience:** you (owner), plus whoever implements it.

How to read this: §1 lists your symptoms and which section fixes each one, so you can jump. §2–§4 are the design and the accounting rules. §5–§21 are the section-by-section plan with exact files. §22–§27 are schema, posting matrix, phases, tests, risks and decisions. The appendix is a file-by-file change list you can hand to a developer.

---

## What is built today (Phase A, part 1)

The point-value defect is fixed and the per-line discount engine exists. Nothing
below is a proposal any more; it is code with tests.

| Item | Where | State |
|---|---|---|
| Money in whole paisa, largest-remainder allocation | `app/services/pricing.py` | built, unit tested |
| Per-line discount columns on sale / booking / return lines | `models/sales.py`, `app/services/schema.py` | built, auto-migrated |
| One discount resolver used by every document | `pricing.resolve_line_discounts` | built |
| Sale posting stores its own split | `_direct_sales_add_direct_sale.py` | built |
| Booking posting stores its split; each delivery recognises its share | `bookings.py`, `profit_loss.py` | built |
| Return stores the discount it withdraws; report reads it | `app/services/return_discounts.py`, `returns.py` | built |
| Legacy rows backfilled once at boot | `schema._backfill_line_discounts` | built |
| Checkbox discount panel over every billed item | `static/discount_panel.js`, `templates/direct_sales.html` | built |
| Gross − Discounts = Net strip on the report | `templates/partials/profit_loss_results.html` | built |
| Regression suite | `tests/test_discount_allocation.py` (22 tests) | passing |
| Whole application suite | `tests/` | 404 passed, 6 skipped |

Still open from the full plan: the journal / chart of accounts (§6), periods and
locks (§7), and the read-model sync layer (§16). Those are the remaining phases.

---

## Table of contents

1. Symptom → fix map
2. What the app is today (diagnosis)
3. Target architecture
4. Accounting policies — the rules this design follows
5. **S0** — Money, rounding, and the end of fractions
6. **S1** — Chart of accounts and the journal (the book)
7. **S2** — Periods, locks, close, retained earnings
8. **S3** — Documents: pricing, discount engine, per-line discount wizard
9. **S4** — Inventory, GRN, landed cost, FIFO, COGS
10. **S5** — Revenue recognition: sales, bookings, dispatch, services/rent, returns
11. **S6** — Receivables: client ledger, payments, waive-offs, advances
12. **S7** — Payables: suppliers, GRN payments, opening balances
13. **S8** — Delivery persons and the cost of service
14. **S9** — Cash, bank, expenses, drawer, reconciliation
15. **S10** — Reporting: P&L, balance sheet, cash flow, trial balance, operational reports, exports
16. **S11** — Sync architecture: "no page computes from zero"
17. **S12** — Masters (clients, suppliers, materials, services, categories)
18. **S13** — Void, edit, delete, and audit
19. **S14** — Import, migration, backup, disaster recovery
20. **S15** — Permissions and controls
21. **S16** — Performance and scale
22. Data model — new and changed tables
23. Posting matrix — event to journal entry
24. Rollout phases, milestones, cut-over
25. Testing, invariants, and the "books agree" gate
26. Risk register
27. Decisions I need from you
- Appendix A — file-by-file change list
- Appendix B — glossary

---

## 1. Symptom → fix map

| What you observed | Root cause | Fixed by |
|---|---|---|
| Discount of Rs. 2,000 becomes revenue 4,756.10 on a 5,000 rent line | Bill-level discount is divided across lines at render time, never rounded, never stored | **S0 + S3** |
| "Profit 8999.999999999996" | Money kept as floating point | **S0** |
| Rate × Qty does not equal revenue on the entry screen | Same division, shown next to a stored rate | **S3** |
| Returning the rent line creates a −243.90 "loss" | Return reverses gross, original was recognized net | **S5** |
| P&L "unresolved cost" rows are excluded from profit | Report deletes what it cannot cost instead of posting suspense | **S5 + S1** |
| GRN screen and P&L can disagree about consumed stock | Two stock truths: `Entry` movements vs GRN lots | **S4** |
| Client balance changes when a new bill is entered | Payment-to-bill matching is recomputed FIFO on every page load | **S6** |
| Dashboard cash/credit differ from the ledger pages | Dashboard sums raw tables with its own formula | **S11** |
| Slow pages as history grows | Every page replays the whole document history | **S11 + S16** |
| Past months can change | No period close; edits rewrite history | **S2 + S13** |
| No balance sheet, no trial balance, cannot prove profit | There is no journal | **S1 + S10** |
| Discount on the bill is one box for everything | No per-line discount model or UI | **S3** |

---

## 2. What the app is today

The app is a **document system with report-time arithmetic**, not a book. That is the single sentence behind almost every problem in §1.

**Facts from the current tree:**

- Money is `db.Float` on almost every model (`DirectSale.amount`, `DirectSaleItem.price_at_time`, `GRN.discount`, `Booking.amount`, `Material.total`, …). Only some tables have an authoritative integer mirror (`Payment.amount_minor`, `Account.balance_minor`, `SupplierPayment.amount_minor`, `AccountTransaction.amount_minor`, `CashFlowEntry.amount_minor`). Item lines have none.
- There is **no journal, no chart of accounts, no trial balance, no balance sheet**. `Account` is a cash/bank/wallet record with a running balance, not a general-ledger account. `AccountTransaction` is a cash movement, not double entry.
- Every report re-derives:
  - P&L: `app/services/profit_loss.py` (`_Projection` at line ~376, `build_profit_loss_report` at ~2488) replays sales, bookings, allocations, entries, returns, payments and waives, and computes discount and cost shares in-flight (lines ~1312, ~1415, ~1545, ~1691).
  - Client ledger: `app/services/financial_ledgers.py` (`_client_snapshot` ~288, `_make_client_obligations` ~582, `build_client_financial_ledger` ~798, `build_current_payables` ~1063) rebuilds obligations and FIFO allocations on each request; `app/services/ledgers.py` (`_build_client_ledger_rows` ~99) is a second implementation.
  - Dashboard: `app/blueprints/core.py` `index()` sums `Payment`, `Booking`, `DirectSale` with its own formulas.
  - Stock: `Entry` IN/OUT feeds `Material.total` and stock summary; GRN lots feed `app/services/grn_consumption.py` and P&L. Two truths.
- Discount is bill-level only (`direct_sale.discount`, `booking.discount`, `payment.discount`). Line tables (`direct_sale_item`, `booking_item`) have no discount column.
- Waive-off is a third discount concept (`models/sales.py` `WaiveOff`, synced by `app/services/waive.py`), mixed with `Payment.discount`.
- Period control exists only partly: cash `CashDayLock` (`app/services/cash_day_recon.py::lock_day`) and the account-reconciliation cutoff in `payments_crud._assert_period_open`. There is no accounting period.
- `Invoice` duplicates `DirectSale` money (`total_amount`, `balance`), and `PendingBill` can be both a real source row and a derived one — duplicate truth again.

**One good precedent already in the codebase.** `DeliveryPersonPayment` documents that the authoritative movement lives in `AccountTransaction` and is kept 1:1 in sync by `_sync_delivery_person_payment_accounting`. That is exactly the pattern this plan generalises to every event: one document, one posting.

---

## 3. Target architecture

```
 ┌─────────────────────────────────────────────────────────────────────┐
 │  Documents (unchanged UX)                                           │
 │  GRN · DirectSale · Booking · Entry(dispatch) · MaterialReturn      │
 │  Payment · WaiveOff · SupplierPayment · DriverPayment · CashFlow    │
 └───────────────┬─────────────────────────────────────────────────────┘
                 │  same DB transaction: document + its posting
                 ▼
 ┌─────────────────────────────────────────────────────────────────────┐
 │  Posting engine  (app/services/ledger/posting.py)                   │
 │  post(event) → validates policy → writes journal_entry + lines      │
 │  reversal(event) → mirror entry, never an edit                      │
 └───────────────┬─────────────────────────────────────────────────────┘
                 ▼
 ┌─────────────────────────────────────────────────────────────────────┐
 │  The book                                                           │
 │  gl_account · journal_entry · journal_line (+ dimensions)           │
 │  Immutable. Balanced. Dated. Period-locked.                         │
 └───────────────┬─────────────────────────────────────────────────────┘
                 ▼
 ┌─────────────────────────────────────────────────────────────────────┐
 │  Read models (app/services/read_models/)                            │
 │  computed once, cached, versioned — the only source pages read       │
 └───────────────┬─────────────────────────────────────────────────────┘
                 ▼
 ┌─────────────────────────────────────────────────────────────────────┐
 │  Views: dashboard · ledgers · P&L · BS · CF · TB · operational      │
 │  Exports and PDFs use the same read models (no reformatting)         │
 └─────────────────────────────────────────────────────────────────────┘
```

Rules that make it work:

- A page never adds up documents. It reads a read model, which reads the book.
- A document never computes an accounting number twice. It stores its line amounts at posting.
- A read model never recomputes on every request. It caches by `(tenant, as_of, filters, ledger_version)`.
- The book never mutates. Corrections are reversing entries.

---

## 4. Accounting policies — the rules this design follows

These are the decisions. Each has a recommended default; change them before implementation if you disagree.

| # | Policy | Recommended | Why |
|---|---|---|---|
| P1 | Money unit | Integer **paisa** everywhere in accounting | Fractions and float drift disappear |
| P2 | Rounding | `ROUND_HALF_UP`, only at line extension, tax, discount allocation, display | One rule, predictable |
| P3 | Residual handling | Largest-remainder allocation; any bill-level residual to a **rounding** line | Bill always adds up exactly |
| P4 | Discount level | **Per line**, stored; bill-level entry is a convenience that allocates | Your requested feature; kills render-time division |
| P5 | Discount scope default | **Goods only**; services opt-in per line | Rent is a fixed resale rate you do not discount by accident |
| P6 | Trade vs settlement discount | Trade discount (bill time) = contra-revenue; waive-off / short payment = **other loss**, dated at settlement | Two different economic events |
| P7 | Revenue recognition | Goods: on delivery. Services: on service/delivery date. Never on booking, GRN or payment | Matches how the business earns |
| P8 | Booking discount | Stored per booking line as per-unit; recognized as units are delivered; remainder released on cancel | Partial delivery cannot over-recognize |
| P9 | Cost of goods | FIFO by GRN lot, landed cost included | Already the design; keep it |
| P10 | Landed cost | Freight, loading, other charges capitalized into lot cost; purchase discount reduces it | True cost per bag |
| P11 | Unknown cost | Post to **suspense**, never exclude revenue from profit | Profit stays complete and provable |
| P12 | Cost of service | Accrued from driver allocation (`SaleDeliveryPerson.rent_amount`) or recorded rent; no value-weighted guess | Exact cost, no fractions |
| P13 | Stock source of truth | GRN lots + stock movements; `Entry` becomes a dispatch record that *references* movements, not a second stock ledger | One answer to "how much is left" |
| P14 | Periods | Monthly, `open → soft-closed → locked`; corrections by reversal | Last month stops moving |
| P15 | Opening balances | Posted once, against opening-balance equity, dated at cut-over | Import cannot silently drift |
| P16 | Tax | Out of scope for phase 1; tax codes reserved on lines and accounts | Keep the door open, do not half-build it |

---

## 5. S0 — Money, rounding, and the end of fractions

**Purpose:** make it impossible to produce a number like `8999.999999999996` or `4756.0975609756`.

**Today:** `db.Float` on nearly every money column; a partial `*_minor` mirror on five tables; `utils/money.py` exists but is used only where someone remembered; the P&L sums raw floats (`profit_loss.py::_sum` at ~2117); reports format with `{:,.2f}` which hides digits but does not remove the fraction; Excel sheets in `profit_loss_exports.py` receive unrounded floats and several sheets have no number format at all.

**Target:**

- `app/services/ledger/money.py` — a `Money` type over integer minor units:
  - `Money.parse(str|Decimal|int)`, `add`, `sub`, `neg`, `mul_qty(qty: Decimal)`, `pct(rate)`
  - `allocate(total: Money, weights: list[Decimal]) -> list[Money]` using largest remainder (the single function that removes every `.09756…`)
  - `round_display(currency_precision)`
- Every financial model gets `*_minor INTEGER` authoritative columns. Float columns remain for compatibility, written **only** from `minor` (`sync_money_fields` in `utils/money.py` is the existing helper to extend).
- One formatting filter for templates (`money()` today is three different local macros) reading `Money` only.
- A `UNIT_PRICE_QUANTITY` rule: quantity may be fractional (bags, tons), money never is. `qty × rate` rounds once.

**Changes**

| File | Change |
|---|---|
| `utils/money.py` | Promote to the only money API; add `allocate()`, `Money` class, `sum_money()` |
| `app/services/ledger/money.py` (new) | Canonical import path; `utils/money.py` re-exports |
| `models/catalog.py`, `models/stock.py`, `models/sales.py`, `models/cash.py`, `models/parties.py`, `models/delivery.py` | Add `*_minor` to every money field (see §22) |
| `app/services/schema.py` | New `_ensure_money_minor_columns()` backfill helper (pattern of `_ensure_discount_columns`) |
| `app/migrations/` | `0003_add_money_minor_columns.sql` (nullable first, then backfill in Python, then `NOT NULL` where safe) |
| `app/services/profit_loss.py` | Replace `_sum()` float addition with `Money`; store `*_minor` in row dicts and expose both |
| `profit_loss_exports.py` | Write `Money`-formatted cells; apply number formats to **all** sheets |
| `templates/partials/profit_loss_results.html`, `profit_entries.html`, `profit_transaction_detail.html`, `view_bill.html`, `client_ledger.html`, `financial_ledger.html`, `supplier_ledger.html` | Use one `money()` filter |
| `tests/` | `test_money_allocate.py` (largest remainder, ties, negatives), `test_no_float_leaks.py` |

**Acceptance**

1. No money column without a `_minor` twin.
2. `sum(lines) == bill.total` exactly, for 10,000 generated bills (property test).
3. Every discount share is an integer number of paisa; no `>2` decimal appears anywhere in HTML, PDF or Excel.
4. A grep gate in CI: financial modules may not call `float()` on a stored money value.

**Size:** 5–8 days. **Risk:** low-medium (touches many files, but additive columns).

---

## 6. S1 — Chart of accounts and the journal (the book)

**Purpose:** one place that records every economic event once, with two sides, so that profit, balances, and cash can be proved.

**Today:** there is no journal. `Account` is a cash/bank/wallet with `balance`; `AccountTransaction` is a cash transfer; `AccountReconciliation` is a bank/cash check. All non-cash economics (revenue, COGS, receivables, payables, inventory) are *recomputed*, never recorded.

**Target:**

New tables (see §22 for DDL):

- `gl_account` — `code`, `name`, `class` (asset/liability/equity/revenue/cost/expense), `subcategory`, `normal_side`, `is_control`, `is_cash`, `linked_account_id` (maps a cash `Account` to its GL cash account), `is_active`.
- `journal_entry` — `entry_no`, `date`, `period_id`, `event` (see §23), `source_type`, `source_id`, `reference`, `description`, `created_by`, `created_at`, `is_reversal_of`, `idempotency_key`, `status` (`posted`/`reversed`).
- `journal_line` — `entry_id`, `gl_account_id`, `debit_minor`, `credit_minor`, dimensions `client_id`, `supplier_id`, `delivery_person_id`, `material_name`, `service_name`, `document_type`, `document_id`, `branch_id`.
- Triggers: `UPDATE`/`DELETE` on `journal_line` and `journal_entry` are rejected; only inserts.
- Constraint: `sum(debit_minor) = sum(credit_minor)` per entry, enforced in `posting.py` **and** by a `BEFORE INSERT` check on the last line (or a nightly invariant).

Posting engine `app/services/ledger/posting.py`:

```
post(event)            -> JournalEntry        # idempotent by (source_type, source_id, event)
reverse(source, reason)-> JournalEntry        # mirror image, links is_reversal_of
assert_balanced(entry)
post_opening_balances(cut_over_date)
```

Rules enforced in code:

1. Every posting carries a `source_type`/`source_id`; the unique index makes a double post impossible.
2. Posting fails if the period is locked.
3. Posting fails if an account is missing — no silent fallback.
4. Reversal is the only way to change a posted number.
5. Every document write path calls `post()` **in the same SQLAlchemy transaction** as the document save.

**Chart of accounts (initial, extensible):**

```
1000  Assets
 1010   Cash on hand              (linked to Account channel=cash)
 1020   Bank                       (linked to Account channel=bank)
 1030   Digital wallet             (linked to Account channel=digital_wallet)
 1100   Accounts receivable        (control; supported by subledger)
 1200   Inventory — goods          (control; supported by stock lots)
 1210   Inventory — in transit
 1300   Input tax receivable
 1400   Inventory suspense         (unknown cost at delivery)
2000  Liabilities
 2010   Accounts payable           (control; supported by supplier subledger)
 2020   Customer advances          (booking money not yet earned)
 2030   Driver / delivery payable  (control; supported by driver subledger)
 2100   Output tax payable
 2200   Accrued expenses
3000  Equity
 3010   Opening balance equity
 3090   Retained earnings
4000  Revenue
 4010   Sales — goods
 4020   Sales — services (rent / delivery charge)
 4030   Other operating income
 4090   Trade discounts allowed    (contra-revenue, debit balance)
 4095   Rounding gain / loss
5000  Cost of sales
 5010   COGS — goods (FIFO)
 5020   COGS — services (driver rent)
 5030   Stock write-off / shortage
 5040   Landed-cost variance
6000  Operating expenses            (one child per expense category)
7000  Other income and gains
8000  Other losses
 8010   Customer waive-off / settlement discount
 8020   Bad debt
 8030   Cash reconciliation difference
```

**Changes**

| File | Change |
|---|---|
| `app/services/ledger/__init__.py`, `accounts.py`, `posting.py`, `query.py` (new) | The book |
| `models/ledger.py` (new) + `models/__init__.py` | `GLAccount`, `JournalEntry`, `JournalLine`, `AccountingPeriod` |
| `app/migrations/NNNN_create_ledger_tables.sql` | Tables, indexes, triggers |
| `app/services/schema.py` | Seed the chart of accounts on first boot; map existing `Account` rows to `gl_account` |
| `app/services/billing.py` `get_next_bill_no` pattern | Reuse for `entry_no` sequences |
| `tools/consistency_report.py` | New check: journal balances, and every posted document has exactly one primary entry |
| `tests/test_journal_posting.py` | Balance, idempotency, immutability, lock enforcement |

**Acceptance**

1. For any date range: `Σ debit = Σ credit` (trial balance zero).
2. Posting the same document twice raises, not duplicates.
3. Editing a `journal_line` raises at the database level.
4. Deleting a document leaves a reversal pair, never a gap.
5. Every `DirectSale`, `GRN`, `Payment`, `SupplierPayment`, `MaterialReturn`, `DeliveryPersonPayment`, `CashFlowEntry` created after phase B has exactly one primary entry.

**Size:** 8–12 days. **Risk:** medium (core), but additive: nothing reads the journal yet.

---

## 7. S2 — Periods, locks, close, retained earnings

**Purpose:** stop history from moving, and give you a monthly ritual that ends with numbers you can sign.

**Today:** `CashDayLock` locks a cash day (`app/services/cash_day_recon.py::lock_day`); `payments_crud._assert_period_open` blocks changes inside a reconciled account period. Nothing stops a sale, a return or a GRN from being backdated into last year.

**Target:**

- `accounting_period` — `year`, `month`, `start`, `end`, `status` (`open` / `soft_closed` / `locked`), `locked_by`, `locked_at`, `closed_result_minor`.
- **Soft-closed:** edits allowed only by an `accountant` role, only as reversal + repost, and the entry is flagged `adjustment`.
- **Locked:** nothing posts; reversal into an open period only.
- **Close checklist** (a script, `app/services/ledger/close.py`), all must pass:
  1. Trial balance = 0.
  2. AR control = AR subledger total.
  3. AP control = AP subledger total.
  4. Inventory control = Σ open stock lots.
  5. Cash GL = Σ reconciled cash/bank balances.
  6. No journal entry without source document; no source document without journal entry (within the period).
  7. Suspense accounts either cleared or explicitly accepted with a note.
  8. Prior-period result rolled to retained earnings.
- Close produces a signed **period close report** (PDF) that becomes the evidence of what the month said.

**Changes**

| File | Change |
|---|---|
| `app/services/ledger/periods.py`, `close.py` (new) | Period state machine + checklist |
| `blueprints/accounts/` | New page `/accounts/periods` (list, close, reopen with reason) |
| `templates/accounts/periods.html` (new) | UI |
| `app/services/payments_crud.py::_assert_period_open` | Delegate to `periods.assert_open()` so one rule governs all modules |
| `app/services/cash_day_recon.py::lock_day` | On lock, also assert the day's journal entries exist |
| `app/blueprints/sales/*`, `ops/*`, `misc/*` | Every save path calls `periods.assert_open(posting_date)` |
| `tests/test_period_close.py` | Lock blocks posting; reversal into open period works; checklist fails loudly |

**Acceptance**

1. A backdated sale into a locked month is refused with a clear message naming the period.
2. Closing a month with an unbalanced trial balance is impossible.
3. After close, last month's P&L is byte-identical on re-render.
4. Reopening requires a reason and is audited.

**Size:** 5–7 days. **Risk:** medium (users will hit the lock; needs good messages).

---

## 8. S3 — Documents: pricing, discount engine, per-line discount wizard

**Purpose:** capture the price, the discount, the tax and the totals **once**, at posting, per line — and give you the discount screen you asked for.

**Today:** one discount box per bill (`templates/direct_sales.html` line ~818, `Booking.discount`, `Payment.discount`), no line column, and the report divides it later (`profit_loss.py` ~1312, ~1415, ~1545). Editing a bill rewrites the stored discount with no history. Reason is a free-text field.

### 8.1 Pricing contract (compute once, store)

For every document line, at save time:

```
gross_minor      = round(qty × rate)
discount_minor   = from the line (typed), or allocated from a bill-level amount
net_minor        = gross_minor − discount_minor
tax_minor        = round(net_minor × tax_rate)          # reserved, phase 1 zero
line_total_minor = net_minor + tax_minor
document.total   = Σ line_total + round_off
round_off        = document.total − Σ line_total        # posted to 4095
```

Stored on the line: `gross_minor`, `discount_minor`, `net_minor`, `tax_minor`, `line_total_minor`, `discount_kind` (`amount` / `percent` / `allocated`), `discount_source` (`manual` / `bill_allocation`), `discount_reason`.

Stored on the document: `discount_total_minor` (must equal Σ line discounts — invariant), `round_off_minor`, `total_minor`, and the legacy floats for compatibility.

### 8.2 The discount wizard (your requested flow)

**Entry point:** a `Discount` button on the bill form, next to the totals.

**Step 1 — "Which items?"**
The modal lists **every billed line** in a table:

| ☑ | Item | Type | Qty | Rate | Gross | Already discounted | New discount | Line net |
|---|---|---|---|---|---|---|---|---|
| ☑ | CEMENT-OPC | Goods | 30 | 1,200.00 | 36,000.00 | 0.00 | [ 1,500.00 ] | 34,500.00 |
| ☐ | RENT-MIXER | Service | 1 | 5,000.00 | 5,000.00 | 0.00 | disabled | 5,000.00 |
| ☑ | STEEL-10MM | Goods | 10 | 300.00 | 3,000.00 | 0.00 | [ 500.00 ] | 2,500.00 |

- Ticking a line enables its discount input. Unticked lines keep zero.
- Quick buttons: **Select all**, **Select all goods**, **Clear**, **Invert**.
- Service lines are unticked/disabled by default (policy P5); the tooltip says "services are billed at their fixed rate — enable in Settings to allow".
- Each ticked line accepts **amount (Rs.)** or **percent (%)** — a per-line toggle. Percent is converted to an amount immediately in the browser *and re-computed on the server*; the server is the only authority.
- Validation as you type: `0 ≤ line discount ≤ line gross`; percent `0–100`; a line already discounted shows the existing value and its source.
- A running footer shows: **Gross, Discounts, Net, Paid, Balance** for the whole bill, live.

**Step 2 — "Or enter a total for the ticked lines"** (optional convenience)
If you prefer to type one number (e.g. "give 2,000 off these two items"), type it and choose a distribution:
- **By value** (proportional to line gross)
- **Equally**
- **To the largest line**
- **Manual** (pre-fills the rows for you to adjust)

The server performs the split with **largest-remainder allocation** (S0), so the shares are whole paisa and always add up to exactly 2,000.00. The UI shows the exact per-line shares before you apply; nothing is applied silently.

**Step 3 — Reason**
A reason is mandatory (dropdown of common reasons + free text), because your current audit already expects a `discount_reason` and waive-offs require one.

**Step 4 — Apply**
- The modal writes hidden per-line fields into the bill form and updates the visible totals; the bill is **not saved yet**.
- On submit, the server re-validates everything (never trust the browser), converts to minor units, checks `Σ line discounts = header discount`, and stores line + header.
- Any residual paisa from a bill-level allocation goes to the **largest line**; any residual from rounding of the document total goes to the rounding line 4095.

**Editing a saved bill:**
- Open period → the same wizard pre-loads stored line discounts; saving produces a **reversal + repost**, with both journal entries visible in the audit trail.
- Locked period → the wizard is read-only and offers "Post an adjustment in the current period".

**Returns:** the return screen shows the original line's `discount_per_unit`; the reversal is `returned_qty × discount_per_unit`, so the discount comes back exactly, never more, never less.

**Bookings:** the wizard runs on the booking lines and stores `discount_per_unit_minor` on `booking_item`. On each delivery the recognized discount is `delivered_qty × discount_per_unit_minor`; the remainder stays unearned and is released on cancel or void. A partially delivered booking can therefore never over-recognize the discount.

**What this removes:** the four render-time divisions in `profit_loss.py` (~1312, ~1415, ~1545, ~1691). Revenue becomes a stored number. The P&L line for the rent example becomes:

```
Gross sales — goods        36,000.00
Gross sales — services      5,000.00
Trade discounts            −2,000.00
Net revenue                39,000.00
```

**Changes**

| File | Change |
|---|---|
| `models/sales.py` | `DirectSaleItem`: `gross_minor`, `discount_minor`, `discount_kind`, `discount_source`, `discount_reason`, `net_minor`, `tax_minor`, `line_total_minor`. `DirectSale`: `discount_total_minor`, `round_off_minor`, `total_minor`. `BookingItem`: `discount_per_unit_minor`, `discount_reason`. `MaterialReturnItem`: `discount_reversed_minor`. `Payment`: `discount_minor` (exists) + `discount_kind` |
| `models/stock.py` | `GRNItem`: `discount_minor` (line discount on purchase), `landed_share_minor`, `line_total_minor` |
| `app/services/pricing.py` (new) | `price_line()`, `allocate_discount()`, `apply_wizard_payload()`, `validate_discount()` — the only discount arithmetic in the app |
| `app/blueprints/sales/_direct_sales_add_direct_sale.py` (~236–470 amount build, ~545 pending) | Read per-line discounts, drop `calculated_amount` float sum, store line + header |
| `app/blueprints/sales/_direct_sales_edit_direct_sale.py` | Reversal + repost path |
| `app/blueprints/sales/bookings.py` | Booking discount per line |
| `app/blueprints/sales/returns.py` | Reverse stored per-unit discount |
| `app/blueprints/api.py` | `POST /api/sales/discount/preview` (allocation preview, no write), `GET /api/sales/<id>/discount` |
| `templates/direct_sales.html` (line ~818, JS 2217–2260, 2449, 2681) | Replace the single discount box with the wizard; keep the box as a shortcut that opens it |
| `templates/_direct_sale_edit_modal.html` | Same wizard for edits |
| `templates/bookings.html`, `templates/_booking_edit_modal.html` | Booking wizard |
| `static/discount_wizard.js` (new, loaded by the bill pages) | Modal, table, allocation preview call, validation |
| `templates/partials/profit_loss_results.html` | New "Gross → Discounts → Net" block; allocations labelled as allocated |
| `app/services/profit_loss.py` | Delete the division sites; read stored `discount_minor`/`net_minor` |
| `tests/test_discount_wizard.py`, `tests/frontend/test_discount_wizard_dom.js` | Server rules + DOM behaviour |

**Acceptance**

1. Discount on one line, several lines, or all lines — each saved exactly as typed.
2. A bill-level total of 2,000 distributed over ticked lines sums to exactly 2,000.00, always.
3. A service line cannot be discounted unless policy enables it; the API refuses it too.
4. Percent and amount entry produce identical stored values for equivalent input.
5. On the P&L, `Gross − Discounts = Net` for every bill, and the by-item table's allocated discounts sum to the header discount.
6. Returning a discounted line reverses exactly the discount that was recognized.

**Size:** 10–14 days. **Risk:** medium-high (touches the busiest screen; needs the DOM test and a careful edit/void path).

---

## 9. S4 — Inventory, GRN, landed cost, FIFO, COGS

**Purpose:** one answer to "how much do we have and what did it cost".

**Today:** two stock truths.
- `Entry` (IN/OUT) feeds `Material.total` (`_rebuild_material_totals`) and the stock summary and the dashboard.
- GRN lots (`GRNItem`, `GRNAllocation`, `grn_consumption._LotLedger`) feed P&L cost and the GRN screen.
- `FIXING_PLAN.md` already records this: `Material.total` and the lot ledger can disagree on legacy data ("P0 — `Material.total` and the lot ledger can disagree").
- Landed costs (`GRN.loading_cost`, `freight_cost`, `other_expense`, `discount`, `tax_amount`) are computed into a GRN total (`grn_svc.calculate_grn_total`) but are **not** in the lot cost the P&L uses.

**Target:**

- `stock_lot` — one row per GRN line: `grn_item_id`, `material_name`, `received_qty`, `remaining_qty`, `unit_cost_minor` (includes landed share), `received_at`.
- `stock_movement` — `lot_id`, `date`, `direction`, `qty`, `unit_cost_minor`, `source_type`, `source_id`, `kind` (`purchase`, `sale`, `return`, `adjustment`, `transfer`, `shortage`).
- FIFO consumption always writes `stock_movement` rows and `GRNAllocation` rows (sales) — the allocation table stays, but it is derived from and checked against movements.
- Landed cost allocation: freight/loading/other charges are allocated to lots **by weight or value** (policy choice, default value, largest remainder) and stored as `unit_cost_minor`. Purchase discount reduces lot cost. Recoverable tax goes to 1300, not inventory.
- `Entry` stops being a stock ledger: it becomes the dispatch record, and dispatch writes stock movements. `Material.total` becomes a **cached read model**, not an independent truth.
- Deliveries without a cost layer are blocked (preferred) or posted to suspense 1400 via 1200 — never quietly excluded from profit.

**Changes**

| File | Change |
|---|---|
| `models/stock.py` | `GRNItem.landed_share_minor`, `unit_cost_minor`; `Entry` gains `stock_movement_id` |
| `models/inventory.py` (new) | `StockLot`, `StockMovement` |
| `app/services/ledger/stock.py` (new) | `receive_lot()`, `consume_fifo()`, `return_to_lot()`, `adjust()`, `value_open_lots()` |
| `app/services/grn_svc.py` | `calculate_grn_total` returns a `Money` breakdown; landed allocation on save |
| `app/services/sales_core.py` (`_allocate_grn_fifo_plan` ~271, `_expand_chargeable_items_fifo` ~300, `_apply_grn_allocations_for_sale` ~405, `_refresh_grn_item_locks` ~363) | Delegate to `ledger/stock.py` |
| `app/services/grn_consumption.py` (`_LotLedger` ~170, `_ProjectionLedger` ~703, `build_grn_consumption` ~786) | Read stored movements + lots; keep the replay only for pre-migration data |
| `app/blueprints/ops/grn.py`, `_dispatch_*.py` | Post receipt/issue to the ledger |
| `templates/grn.html`, `templates/stock_summary.html`, `templates/material_ledger.html` | Read from the read model |
| `tools/consistency_report.py` | `check_stock_truths`: `Entry` net vs lot remaining, per material, must match or be explained |
| `tests/test_stock_lots.py` | Landed cost, FIFO across multiple GRNs, return to original lot, shortage |

**Acceptance**

1. `received − consumed − remaining = 0` per lot, always.
2. Each material: `Entry` net = Σ lot remaining (or a dated, explained difference).
3. Landed cost per lot is visible and included in COGS.
4. No sale can consume a lot dated after the sale.
5. Stock value in the ledger = Σ open lot values.

**Size:** 10–14 days. **Risk:** high (deepest existing logic) — but the audit tools already assert much of this; reuse them.

---

## 10. S5 — Revenue recognition: sales, bookings, dispatch, services/rent, returns

**Purpose:** earn revenue at the right moment, once.

**Today:** `app/services/profit_loss.py` decides recognition at report time, with `include_in_net` switches. Unknown cost → the whole row is excluded from net. Services (rent) are recognized through `add_direct_chargeable`, `add_booking_supply`, `add_delivery_rows` — three code paths that can each produce a service row, with different rules (`service_no_cost` vs `delivery_include_in_net`). `DeliveryRent` and `SaleDeliveryPerson` costs are merged into a value-weighted share.

**Target — recognition events, not report rules:**

| Business event | Recognizes |
|---|---|
| Goods dispatched on a sale | Revenue (net of stored line discount), COGS by FIFO lot |
| Goods dispatched against a booking | Revenue at booking line rate, less the stored per-unit discount; COGS by FIFO |
| Service/rent billed on a sale | Service revenue at the line's net amount |
| Service/rent delivered against a booking | Service revenue per delivered unit |
| Return of goods | Reverse revenue and reverse COGS, back into the **original lot** |
| Return of a service | Reverse service revenue only |
| Waive-off / settlement discount | Other loss 8010, dated at settlement (never edits the bill) |
| Unknown cost at dispatch | Revenue posts, COGS goes to suspense 1400 |

Rules:

- **One row per economic event.** A dispatch produces one goods row and, if present, one service row. No double counting between `Entry` dispatch and `DirectSale` — the existing `chargeable_qty` and `BookingAllocation` logic is retained but expressed as "the allocation is the document".
- **No exclusion from profit.** `include_in_net=False` disappears from the report; instead the journal carries the suspense amount and the P&L shows "COGS recognized (including X in suspense)". The old warnings survive as a management panel.
- **Estimated cost** is allowed only when explicitly labelled and posted to a variance account (5040), never mixed into COGS silently.

**Changes**

| File | Change |
|---|---|
| `app/services/profit_loss.py` | Reduce from 2,781 lines of replay to a query over `journal_line` + dimensions; keep `build_profit_loss_report()` signature so templates/URLs do not change |
| `app/services/ledger/recognize.py` (new) | `recognize_sale()`, `recognize_booking_delivery()`, `recognize_service()`, `recognize_return()`, `recognize_waive()` |
| `app/blueprints/sales/*`, `app/blueprints/ops/_dispatch_*.py` | Call recognition inside the save transaction |
| `app/services/profit_loss_breakdown.py` | Read stored revenue/cost; drop `_margin` recomputation of money (keep % only) |
| `templates/partials/profit_loss_results.html` | Rows show stored values; unresolved rows show suspense amount instead of being hidden |
| `tests/test_recognition.py` | Directive cases: partial booking delivery, rent service, return, waive |

**Acceptance**

1. A booking delivered in three parts recognizes exactly the booked revenue and discount on the delivered quantity, no more.
2. A rent line earns exactly its net amount on the delivery date, once.
3. A return of a rent line produces zero net effect on that service over the two documents.
4. No revenue row without a document; no document without revenue rows.
5. Profit includes every delivered sale, including those with unresolved cost (suspense shown separately).

**Size:** 12–16 days. **Risk:** high (this is the heart of the accounting), but testable against the old report month by month.

---

## 11. S6 — Receivables: client ledger, payments, waive-offs, advances

**Purpose:** a client balance that is stored, explainable, and stable.

**Today:** `_make_client_obligations` (`financial_ledgers.py` ~582) rebuilds every bill and applies payments FIFO on **every page load**; `_build_client_ledger_rows` (`ledgers.py` ~99) is a second builder; `finance_clients._compute_client_financial_summary` (~103) a third; `_client_balance_as_of` (~406) a fourth. Consequences: a balance can change when a new bill is added (FIFO re-settles), pages slow with history, and there is no "this payment belongs to that bill".

**Target:**

- `ar_document` — one row per bill/booking obligation: `client_id`, `document_type`, `document_id`, `date`, `gross_minor`, `discount_minor`, `net_minor`, `paid_minor`, `waived_minor`, `outstanding_minor`, `due_date`, `status`.
- `ar_allocation` — `payment_id|credit_id`, `ar_document_id`, `amount_minor`, `date`, `is_void`, `method` (`auto_fifo` / `manual`).
- Advance receipts are `ar_allocation` with `ar_document_id = NULL` → shown as an advance liability (2020), not negative AR.
- Waive-off is its own document (`WaiveOff` stays) and posts to 8010; `Payment.discount` becomes a *source* for a WaiveOff row, not a second discount.
- The client ledger page becomes a **read** of `ar_document` + `ar_allocation` + journal lines; all four builders collapse into one (`app/services/ledger/subledger_ar.py`).
- The page gains: apply payment to specific bill(s), split a payment, unapply, and an ageing view.

**Changes**

| File | Change |
|---|---|
| `models/ledger.py` | `ARDocument`, `ARAllocation` |
| `app/services/ledger/subledger_ar.py` (new) | `rebuild_ar_documents()`, `apply_payment()`, `unapply()`, `balance(client)`, `ageing()` |
| `app/services/financial_ledgers.py` | `_make_client_obligations`, `_client_snapshot`, `build_client_financial_ledger`, `build_current_payables` read stored rows |
| `app/services/ledgers.py` | `_build_client_ledger_rows` deleted or reduced to a formatter |
| `app/services/finance_clients.py` | Summary and `_client_balance_as_of` read stored rows |
| `app/services/payments_crud.py` (`save_client_payment` ~266, `delete` ~500, `restore` ~531) | Write allocation rows + journal inside the same transaction |
| `app/services/waive.py` | Waive posts 8010; sync from `Payment.discount` becomes a one-time migration |
| `app/blueprints/ledgers/*` | Read models; new `/ledger/<id>/apply` endpoint |
| `templates/client_ledger.html`, `financial_ledger.html`, `financial_details.html`, `client_clearance.html` | Same data, stored; new apply/unapply controls |
| `static/ledger_live.js` | Refresh on `ledger_version` change instead of polling revisions |
| `tests/test_ar_subledger.py` | FIFO default, manual apply, over-payment, advance, waive, void |

**Acceptance**

1. A client balance does not change when an unrelated bill is entered.
2. Σ outstanding across clients = AR control account balance (to the paisa).
3. Every payment is either allocated or explicitly an advance.
4. Applying and unapplying leaves no orphan journal line.
5. Ledger page load time is flat in history size (measured by `tools/benchmark_pages.py`).

**Size:** 12–15 days. **Risk:** medium-high (this is the second most used screen after sales).

---

## 12. S7 — Payables: suppliers, GRN payments, opening balances

**Purpose:** supplier balances that tie to the ledger and to GRNs.

**Today:** `build_current_payables` (`financial_ledgers.py` ~1063) replays; supplier ledger built in `ledgers.py::_build_supplier_ledger_rows` (~616) and `app/blueprints/misc/suppliers.py`; `grn_svc._sync_grn_auto_supplier_payment` (~136) creates an auto `SupplierPayment` per GRN; `supplier_ledger_shift.py` moves a whole ledger and rewrites history visibly.

**Target:**

- `ap_document` — one row per GRN: `supplier_id`, `grn_id`, `date`, `gross_minor`, `discount_minor`, `net_minor`, `paid_minor`, `outstanding_minor`, `due_date`.
- `ap_allocation` — supplier payment → document; advances supported.
- GRN posts: inventory debit, AP credit; auto payment posts AP debit, cash credit (no double count — the existing auto-sync pattern is correct, it just needs to post instead of only writing a row).
- Supplier ledger shift becomes: reverse the old ledger entries in a new document + post the new, both visible in one audit report. No silent rewrite.
- Opening balances per supplier posted to 3010 at cut-over.

**Changes**

| File | Change |
|---|---|
| `models/ledger.py` | `APDocument`, `APAllocation` |
| `app/services/ledger/subledger_ap.py` (new) | Build, apply, balance, ageing |
| `app/services/grn_svc.py` | Post on save; auto payment posts settlement |
| `app/services/payments_crud.py::save_supplier_payment` (~562) | Write allocation + journal |
| `app/services/supplier_ledger_shift.py`, `app/blueprints/masters/shift_supplier_ledger.py` | Reversal-based shift with a diff report |
| `templates/supplier_ledger.html`, `pay_supplier.html`, `suppliers.html` | Stored data |
| `tests/test_ap_subledger.py` | GRN + part payment + discount + shift |

**Acceptance**

1. Σ supplier outstanding = AP control.
2. GRN total (with landed costs, discount, tax) equals the payable created.
3. A ledger shift leaves both old and new visible and produces zero net change for the company as a whole.
4. Auto-payment cannot double-count with a manual payment.

**Size:** 8–10 days. **Risk:** medium.

---

## 13. S8 — Delivery persons and the cost of service

**Purpose:** make the rent you pay the driver an exact, per-sale cost, not a value-weighted guess.

**Today:** `SaleDeliveryPerson.rent_amount` per (sale, driver, bags); `DeliveryRent` legacy rows; `DeliveryPersonPayment` with the good 1:1 accounting sync; `driver_payments.py::settle_driver_fifo` (~436) and `reconcile_driver_payments` (~553); P&L splits `DirectSale.delivery_rent_cost` across service lines **by value** (`profit_loss.py` ~1691), which is where fractional service costs come from.

**Target:**

- Cost of service = the driver allocation(s) attached to that sale, or the recorded `delivery_rent_cost` when it is the only evidence. Allocated to service lines **from the allocation rows**, not by value share. If a sale has one service line, it gets 100%.
- Driver payable subledger: each allocation debits `5020 COGS — services` and credits `2030 Driver payable`; each driver payment debits 2030 and credits cash. Waive-off by driver posts 8010.
- The driver ledger page reads the subledger, not a replay.
- If a sale has a service charge but no driver cost, the cost is **accrued** at the recorded tariff (if configured) or flagged in a "service cost missing" panel — it is not silently treated as 100% margin.

**Changes**

| File | Change |
|---|---|
| `models/delivery.py` | `SaleDeliveryPerson` gains `cost_minor`; `DeliveryRent` gains `cost_minor` |
| `app/services/ledger/subledger_driver.py` (new) | Allocation, settlement, balance |
| `app/services/driver_payments.py` | Write journal + allocation rows; `driver_outstanding` reads stored |
| `app/blueprints/ledgers/delivery_person.py` | Read model; apply-payment endpoint |
| `templates/delivery_persons.html`, `delivery_rents.html` | Stored data |
| `tests/test_driver_subledger.py` | Multi-sale allocation, partial payment, waiver |

**Acceptance**

1. A sale with one service line shows that line's cost = the driver rent, to the paisa.
2. Two service lines on one sale split the driver cost by allocation, not by revenue value.
3. Σ driver payable = 2030 balance.
4. Driver payments cannot exceed outstanding without an explicit advance flag.

**Size:** 6–8 days. **Risk:** medium.

---

## 14. S9 — Cash, bank, expenses, drawer, reconciliation

**Purpose:** cash is a *settlement*, and the ledger must know it without touching revenue.

**Today:** `Account` + `AccountTransaction` + `CashFlowEntry`; `app/services/drawer.py` builds unified drawer rows from several sources; `cash_day_recon.py` computes positions and locks days; `reports/cash.py` renders cash flow and daily reconciliation; `AccountReconciliation` holds the cutoff.

**Target:**

- Every `Account` maps to a GL cash/bank account. Cash movements post: `Dr cash / Cr AR` (receipt), `Dr AP / Cr cash` (supplier payment), `Dr expense / Cr cash`, `Dr cash A / Cr cash B` (transfer).
- `CashFlowEntry` becomes a document that posts; the cash-flow report reads the ledger's cash accounts plus operating/investing/financing classification.
- Drawer stays the operational view over cash accounts, reading the ledger.
- Daily reconciliation compares **counted** vs **ledger**; the difference posts to 8030 on lock (with a reason), so the cash book always equals reality after a lock.
- Expenses get categories mapped to 6000 children; no free-text expense postings without an account.

**Changes**

| File | Change |
|---|---|
| `models/cash.py` | `CashFlowEntry.journal_entry_id`; `Account.gl_account_id` |
| `app/services/ledger/cash.py` (new) | `post_receipt()`, `post_payment()`, `post_transfer()`, `post_expense()`, `post_recon_difference()` |
| `blueprints/accounts/*` (`accounts_crud.py`, `transactions.py`, `payments.py`, `kpis.py`) | Read ledger balances; KPI endpoints use read models |
| `app/services/drawer.py` | Read model |
| `app/services/cash_day_recon.py` | Lock posts the difference |
| `app/blueprints/reports/cash.py` | Cash-flow statement from the ledger |
| `tests/test_cash_posting.py`, `tests/test_reconciliation_posting.py` | Receipt, payment, transfer, difference |

**Acceptance**

1. Σ cash GL = Σ `Account.balance` after backfill.
2. A receipt never changes revenue.
3. A day cannot be locked with an unexplained difference (or the difference is posted with a reason).
4. Cash-flow statement reconciles opening cash + movements = closing cash.

**Size:** 10–12 days. **Risk:** medium.

---

## 15. S10 — Reporting

**Purpose:** reports become readers, not calculators.

**Today:** `profit_loss.py` (2,781 lines), `profit_loss_breakdown.py` (791), `profit_loss_exports.py` (~450) each format differently; `profit_report_pdf.html` is a fourth rendering; Excel applies number formats to some sheets and not others; there is no balance sheet, cash-flow statement, or trial balance anywhere; `templates/stock_summary.html` and `templates/dashboard` compute their own aggregates.

**Target reports, all from the ledger:**

| Report | Source | Notes |
|---|---|---|
| Trial balance | `journal_line` grouped by account | The proof report; must be zero |
| Profit & loss | revenue/cost/expense accounts, with dimensions | Gross → discounts → net; COGS; service margin; expenses |
| Balance sheet | asset/liability/equity accounts | Current result = period net |
| Cash-flow statement | cash accounts + classification | Direct method from cash postings |
| AR ageing | `ar_document` | 0–30/31–60/61–90/90+ |
| AP ageing | `ap_document` | Same buckets |
| Sales by item/service/customer/day | `journal_line` + dimensions | Replaces the report-time grouping |
| Stock valuation | lots + movements | FIFO value |
| Driver statement | driver subledger | Charges vs settlements |
| Books-agree dashboard | invariant checks (§25) | Replaces the pile of audit markdown |

Rules:

- **One formatter, one exporter.** `app/services/reporting/format.py` + `export.py`; HTML, PDF and Excel consume the same view model (today they do not).
- Every cell carries `drill` metadata: account, document type/id, journal entry id. The UI makes them links.
- Every report states its `as_of`, `ledger_version`, and period status.

**Changes**

| File | Change |
|---|---|
| `app/services/reporting/` (new) | `trial_balance.py`, `balance_sheet.py`, `cash_flow.py`, `pnl.py`, `ageing.py`, `format.py`, `export.py` |
| `app/services/profit_loss.py`, `profit_loss_breakdown.py`, `profit_loss_exports.py` | Thin adapters over the new modules; keep endpoints and template contracts |
| `app/blueprints/reports/*` | Add `/reports/trial_balance`, `/reports/balance_sheet`, `/reports/cash_flow`, `/reports/books_agree` |
| `templates/` | New pages: `trial_balance.html`, `balance_sheet.html`, `cash_flow_statement.html`, `books_agree.html`; existing profit templates point at the new view model |
| `templates/profit_report_pdf.html` | Same view model |
| `tests/test_reports_*.py` | Golden files per report; cross-report tie-outs |

**Acceptance**

1. Trial balance is zero for every closed month.
2. Balance sheet balances every day, not only at close.
3. P&L net = balance sheet movement in retained earnings/current result.
4. Cash-flow closing cash = balance sheet cash.
5. HTML, PDF and Excel show identical figures for the same filters.
6. Every reported figure links back to a journal entry and a document.

**Size:** 12–16 days. **Risk:** medium.

---

## 16. S11 — Sync architecture: "no page computes from zero"

**Purpose:** your explicit requirement — pages must take numbers from the modules that own them and stay in sync, not each rebuild the world.

**Today, fifteen places compute money independently:**

| Page / feature | Where it computes today |
|---|---|
| Dashboard | `app/blueprints/core.py::index` (own SQL sums for cash, credit, sales breakdown, stock) |
| Profit & loss | `profit_loss.py::build_profit_loss_report` |
| Profit entries / detail | `_explorer_rows`, `profit_transaction_detail` |
| Client ledger | `financial_ledgers.build_client_financial_ledger` + `ledgers._build_client_ledger_rows` |
| Client clearance / history / PDFs | `ledgers.py` receipts + `client_full_history_pdf.html` |
| Current payables | `financial_ledgers.build_current_payables` |
| Financial tracker / matrix | `templates/_financial_tracker_matrix.html` + services |
| Supplier ledger / payables | `ledgers._build_supplier_ledger_rows`, `misc/suppliers.py` |
| Delivery-person ledger | `driver_payments.driver_outstanding` |
| Stock summary / material ledger | `Entry` sums, `Material.total` |
| GRN screen consumption | `grn_consumption.build_grn_consumption` |
| Cash flow / daily recon / drawer | `reports/cash.py`, `cash_day_recon.py`, `drawer.py` |
| Accounts KPIs | `blueprints/accounts/kpis.py` |
| Excel/PDF exports | `profit_loss_exports.py`, PDF templates |
| API | `app/services/api.py` |

**Target: three layers, one direction of dependency.**

```
documents  ──►  journal (the book)  ──►  read models  ──►  pages / exports / API
                                        (cached, versioned)
```

1. **Documents own the event.** Only the owning module writes its document and posts its journal entry.
2. **The book owns the numbers.** Only `ledger/query.py` aggregates.
3. **Read models own the page shapes.** `app/services/read_models/`:

| Read model | Returns | Used by |
|---|---|---|
| `dashboard.py` | today's cash, credit, sales by category, stock summary, KPIs | `/` |
| `client_account.py` | ledger rows, balance, ageing, advances, statements | `/ledger*`, `/financial_ledger*`, PDFs |
| `supplier_account.py` | supplier rows, balance, ageing | `/suppliers`, `/supplier_ledger` |
| `stock_view.py` | per-material in/out/remaining/value, lots, movements | `/stock_summary`, `/material_ledger`, `/grn` |
| `pnl_view.py` | P&L cards, breakdown, transactions page, exports | `/profit_reports*` |
| `cash_view.py` | cash positions, flow, drawer, recon | `/cash_flow*`, `/daily_reconciliation` |
| `payables_view.py` | outstanding bills, ageing | `/payables`, dashboard |
| `driver_view.py` | charges, settlements, balance | delivery person pages |

Each read model:

- takes `(tenant, as_of, filters)` and returns plain dicts plus `ledger_version` and `source_documents`;
- is cached in `app/services/cache.py` keyed by those values plus `ledger_version`;
- is invalidated automatically because `ledger_version` increments on every posting (and on reversal);
- is the **only** thing a template, export or API endpoint reads.

Consequences:

- The dashboard and the P&L cannot disagree; both read the same journal.
- A page refresh after a sale shows updated numbers because the version changed, not because the page recomputed.
- `static/ledger_live.js` can watch `ledger_version` and refresh only what changed.

**Changes**

| File | Change |
|---|---|
| `app/services/read_models/*.py` (new, 8 modules) | The read layer |
| `app/services/cache.py` (new) | Version-keyed cache with explicit invalidation |
| `app/blueprints/core.py::index` | Replace ~15 inline aggregates with `read_models.dashboard.load()` |
| `blueprints/accounts/kpis.py` | Same |
| `app/blueprints/reports/cash.py` | Same |
| `templates/_financial_tracker_matrix.html`, `_daily_recon_board.html` | Context from read models |
| `app/services/api.py` | Same models; API becomes a thin serializer |
| `static/ledger_live.js` | Watch version, refresh section |
| `tools/benchmark_pages.py` | Baselines per page before/after |
| `tests/test_read_model_consistency.py` | Every page-shaped model ties to the journal |

**Acceptance**

1. For any date range: dashboard totals = P&L totals = ledger pages = API (asserted in tests).
2. Grep gate: templates receive no raw SQL aggregates; `app/blueprints/**` contains no `func.sum(` on financial tables outside read models.
3. Adding a historical document changes `ledger_version` and every affected page updates without a restart.
4. No page loads the full document history to render.

**Size:** 10–12 days (after the book exists). **Risk:** medium.

---

## 17. S12 — Masters

**Purpose:** identities and defaults that the book can rely on.

**Today:** `Client`, `Supplier`, `DeliveryPerson`, `Material`, `Service`, `MaterialCategory` are name-keyed in documents (`BookingItem.material_name`, `DirectSaleItem.product_name`, `Entry.material` are strings). `Client` has duplicate-name handling (`_same_name_id_groups`) and `transferred_to_id`; suppliers have `transferred_to_id`; materials have `rename_material_label` that cascades into history.

**Target:**

- IDs in new documents (`client_id`, `supplier_id`, `material_id`, `service_id`, `delivery_person_id`), names kept as display snapshots.
- A rename is a master change that does not rewrite history; the ledger keeps the name it posted with, and reports can show "current name" via join.
- `Service` gets `revenue_account_id` and optional `cost_account_id`; `Material` gets `inventory_account_id` and `cogs_account_id` (defaults from the chart).
- Opening balances (`Client.opening_balance`, `Supplier.opening_balance`, `DeliveryPerson` opening) are posted once to 3010 with a date, and become read-only afterwards.
- Merge/transfer operations become documented ledger operations (reverse + repost under the new identity) with a diff report.

**Changes**

| File | Change |
|---|---|
| `models/catalog.py`, `models/parties.py` | `revenue_account_id`, `cost_account_id`, `inventory_account_id`, `cogs_account_id`; `opening_balance_minor`, `opening_balance_posted_at` |
| `app/blueprints/masters/*` (`rename_material_label.py`, `transfer_client.py`, `shift_supplier_ledger.py`, `services.py`) | Ledger-aware operations + diff report |
| `app/services/schema.py` | Backfill account mappings |
| `tests/test_master_ledger_ops.py` | Rename, merge, transfer keep totals |

**Acceptance**

1. Renaming a material does not change any historical figure.
2. Merging two clients preserves the combined balance exactly.
3. Opening balances appear once, with a date, on the balance sheet.

**Size:** 5–7 days. **Risk:** low-medium.

---

## 18. S13 — Void, edit, delete, and audit

**Purpose:** history is append-only; mistakes are corrected visibly.

**Today:** `is_void` flags everywhere; `_purge_voided_rows_at_boot` (`schema.py` ~802) physically deletes voided rows at boot; `tools/purge_voided.py`; `unvoid` exists; edits rewrite documents (`_direct_sales_edit_direct_sale.py`); audit is `AuditLog` + `AccountingAuditLog` written by hand in places.

**Target:**

- `void` = reversal entry + document status `voided` (row never deleted; no boot purge of financial rows).
- `edit` on a posted document = reversal + repost, both linked, both visible; the document carries `revision` and `superseded_by`.
- `delete` is not offered for posted financial documents; it is offered for drafts only.
- Every posting, reversal, discount, waive, period action writes an `AccountingAuditLog` row with before/after and the actor.
- The void audit page shows pairs (entry ↔ reversal) rather than orphan flags.

**Changes**

| File | Change |
|---|---|
| `app/services/ledger/reversal.py` (new) | Reversal API used by every module |
| `app/blueprints/sales/_bills_void_transaction.py`, `_bills_unvoid_transaction.py`, `_bills_edit_ledger_transaction.py`, `_direct_sales_edit_direct_sale.py`, `ops/_dispatch_delete_entry.py`, `misc/_wipe_*` | Use reversal; no destructive delete of posted rows |
| `app/services/schema.py::_purge_voided_rows_at_boot` | Purge drafts and orphan rows only; never journal-backed rows |
| `tools/purge_voided.py` | Guarded, refuses journal-backed rows |
| `templates/void_audit.html` | Entry ↔ reversal pairs |
| `tests/test_reversal_policy.py` | Void, edit, unvoid, purge guard |

**Acceptance**

1. Voiding changes no total except by adding a visible reversal.
2. A boot never deletes a financial row.
3. Every edit shows before/after and both journal entries.
4. Audit trail is complete for money changes (checked by a test that walks a document's lifecycle).

**Size:** 6–8 days. **Risk:** medium (data policy change; communicate).

---

## 19. S14 — Import, migration, backup, disaster recovery

**Purpose:** bring history into the new book safely, and be able to prove it.

**Today:** `blueprints/import_export/*` (full DB snapshot/restore, raw import, transfer import, app upgrade), `app/services/legacy_migration.py`, `tools/migrate`, `tools/restore_legacy_snapshot.py`, `tools/roundtrip_compare.py`, backup settings in `models/ops_meta.py`, `app/services/backup.py`.

**Target migration (this is the risky part — treat it as its own project):**

1. **Freeze** a cut-over date. Take a full DB snapshot (existing tooling).
2. **Add columns** (nullable) — no behaviour change.
3. **Backfill money** — fill `*_minor` from floats; reconcile count and sum; report exceptions (nulls, NaN, negative-money).
4. **Seed chart of accounts** and map masters.
5. **Replay history** date-ordered into the journal using the *new* posting rules, but with old documents as the source. Every document gets its entries.
6. **Diff** old report vs new report per month: revenue, COGS, discount, expenses, profit, AR, AP, stock. Every difference must be explained (policy change vs bug). Produce `MIGRATION_DIFF.md` with the table.
7. **Dual-run**: `ACCOUNTING_MODE=docs|gl` switch; production reads documents until the diff is zero or accepted; the new report is visible side by side (read-only) before the switch.
8. **Switch** on a period boundary; lock everything before it.
9. **Keep the document system** as the operational UI; only the numbers change source.

Rules: never import into a live database without a snapshot; every migration step idempotent; every step writes a row to `migration_history` (already exists in `auto_migrate.py`); imports that create postings must use `posting.post()` (no hand-written journals from CSVs).

**Backup/DR:** snapshot before every migration, weekly automated snapshot (existing `backup.py`), and a documented restore drill using `restore_legacy_snapshot.py`; the books-agree gate runs immediately after any restore.

**Size:** 10–15 days (plus the dual-run waiting time). **Risk:** high if rushed; low if the diff step is honest.

---

## 20. S15 — Permissions and controls

**Purpose:** money changes get the right person, and they are visible.

**Today:** `ENDPOINT_PERMISSION_MAP` in `app/services/permissions.py` + `app/services/constants.py`; `tools/audit_permissions.py` checks coverage; roles via `models/core.py::User`.

**Target:**

- New permissions: `can_discount` (with a maximum % or amount), `can_waive_off`, `can_edit_posted_document`, `can_post_adjustment`, `can_close_period`, `can_view_books` (trial balance/BS), `can_export_books`.
- Approval thresholds: discount above X requires an approver; waive-off above Y requires an approver; both recorded in the document and in the journal description.
- Segregation: the person who posts a sale cannot close the period that contains it (configurable).
- Every new endpoint is added to `ENDPOINT_PERMISSION_MAP`; `tools/audit_permissions.py` must pass with zero unmapped routes.
- The books-agree dashboard is permission-gated to `can_view_books`.

**Changes**

| File | Change |
|---|---|
| `app/services/permissions.py`, `app/services/constants.py` | New permissions and map entries |
| `models/core.py::User` | New boolean columns (via migration) |
| `templates/admin*, settings.html` | Permission UI |
| `app/services/ledger/posting.py` | Enforce threshold permissions at post time |
| `tools/audit_permissions.py`, `tests/test_audit_permissions_fixes.py` | Coverage gate |

**Acceptance**

1. A user without `can_discount` cannot post a discount, via UI or API.
2. A discount over the threshold requires an approver and is recorded.
3. Zero unmapped endpoints.

**Size:** 4–6 days. **Risk:** low.

---

## 21. S16 — Performance and scale

**Purpose:** keep it fast as history grows.

**Today:** report-time replay is the bottleneck (documented in code comments as "O(returns × candidates)", "dominated the report"); `tools/benchmark_pages.py` exists for measurement.

**Target:**

- The book is small relative to documents: one entry per event, lines indexed by `(date, gl_account_id)`, dimensions indexed.
- Read models cached by `ledger_version`; invalidated on post.
- Closed periods: report snapshots (per account per month) for instant statement rendering; invalidated by any reversal into that period.
- Pagination at the query level, never in Python lists.
- Target budgets (measured in `benchmark_pages.py`): P&L < 400 ms, client ledger < 250 ms, dashboard < 300 ms, trial balance < 600 ms on a 5-year dataset.
- A synthetic dataset generator (extend `dummy_data/generate_dummy_data.py`) with 5 years of documents, used in a performance test.

**Size:** 5–7 days, spread. **Risk:** low.

---

## 22. Data model — new and changed tables

### New tables (ledger)

```sql
CREATE TABLE gl_account (
  id INTEGER PRIMARY KEY,
  tenant_id INTEGER,
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  class TEXT NOT NULL,              -- asset|liability|equity|revenue|cost|expense
  subcategory TEXT,
  normal_side TEXT NOT NULL,        -- debit|credit
  is_control INTEGER DEFAULT 0,
  is_cash INTEGER DEFAULT 0,
  linked_account_id INTEGER,        -- -> account.id for cash/bank/wallet
  is_active INTEGER DEFAULT 1,
  UNIQUE (tenant_id, code)
);

CREATE TABLE accounting_period (
  id INTEGER PRIMARY KEY,
  tenant_id INTEGER,
  year INTEGER NOT NULL,
  month INTEGER NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',   -- open|soft_closed|locked
  closed_result_minor INTEGER,
  locked_by TEXT, locked_at TEXT, note TEXT,
  UNIQUE (tenant_id, year, month)
);

CREATE TABLE journal_entry (
  id INTEGER PRIMARY KEY,
  tenant_id INTEGER,
  entry_no TEXT NOT NULL,
  entry_date TEXT NOT NULL,
  period_id INTEGER REFERENCES accounting_period(id),
  event TEXT NOT NULL,                    -- see §23
  source_type TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  reference TEXT,
  description TEXT,
  is_reversal_of INTEGER REFERENCES journal_entry(id),
  is_adjustment INTEGER DEFAULT 0,
  idempotency_key TEXT,
  created_by TEXT, created_at TEXT,
  UNIQUE (tenant_id, source_type, source_id, event, is_reversal_of)
);

CREATE TABLE journal_line (
  id INTEGER PRIMARY KEY,
  entry_id INTEGER NOT NULL REFERENCES journal_entry(id),
  line_no INTEGER NOT NULL,
  gl_account_id INTEGER NOT NULL REFERENCES gl_account(id),
  debit_minor INTEGER NOT NULL DEFAULT 0,
  credit_minor INTEGER NOT NULL DEFAULT 0,
  client_id INTEGER, supplier_id INTEGER, delivery_person_id INTEGER,
  material_name TEXT, service_name TEXT,
  document_type TEXT, document_id INTEGER,
  branch_id INTEGER,
  memo TEXT,
  CHECK (debit_minor = 0 OR credit_minor = 0),
  CHECK (debit_minor >= 0 AND credit_minor >= 0)
);
CREATE INDEX ix_jl_entry ON journal_line(entry_id);
CREATE INDEX ix_jl_account_date ON journal_line(gl_account_id, entry_id);
CREATE INDEX ix_jl_client ON journal_line(client_id);
CREATE INDEX ix_jl_doc ON journal_line(document_type, document_id);

CREATE TRIGGER trg_jl_no_update BEFORE UPDATE ON journal_line
BEGIN SELECT RAISE(ABORT, 'journal_line is immutable'); END;
CREATE TRIGGER trg_jl_no_delete BEFORE DELETE ON journal_line
BEGIN SELECT RAISE(ABORT, 'journal_line is immutable'); END;
```

### New tables (subledgers, stock, versions)

```sql
CREATE TABLE ar_document (id, tenant_id, client_id, document_type, document_id,
  doc_date, due_date, gross_minor, discount_minor, net_minor,
  paid_minor, waived_minor, outstanding_minor, status, journal_entry_id);
CREATE TABLE ar_allocation (id, tenant_id, client_id, ar_document_id,
  payment_id, credit_document_type, credit_document_id, amount_minor,
  allocated_at, is_void, method);

CREATE TABLE ap_document (id, tenant_id, supplier_id, grn_id, doc_date, due_date,
  gross_minor, discount_minor, net_minor, paid_minor, outstanding_minor,
  status, journal_entry_id);
CREATE TABLE ap_allocation (id, tenant_id, supplier_id, ap_document_id,
  payment_id, amount_minor, allocated_at, is_void, method);

CREATE TABLE stock_lot (id, tenant_id, grn_item_id, material_name, received_qty,
  remaining_qty, unit_cost_minor, received_at);
CREATE TABLE stock_movement (id, tenant_id, lot_id, moved_at, direction,
  qty, unit_cost_minor, kind, source_type, source_id);

CREATE TABLE ledger_version (tenant_id PRIMARY KEY, version INTEGER NOT NULL);
```

### Changed tables (money + discount + linkage)

| Table | Add |
|---|---|
| `direct_sale` | `total_minor`, `discount_total_minor`, `round_off_minor`, `journal_entry_id`, `revision`, `superseded_by` |
| `direct_sale_item` | `gross_minor`, `discount_minor`, `discount_kind`, `discount_source`, `discount_reason`, `net_minor`, `tax_minor`, `line_total_minor`, `material_id` |
| `booking` | `total_minor`, `discount_total_minor`, `journal_entry_id` |
| `booking_item` | `gross_minor`, `discount_per_unit_minor`, `net_minor`, `material_id` |
| `grn` | `gross_minor`, `discount_minor`, `tax_minor`, `total_minor`, `journal_entry_id` |
| `grn_item` | `line_total_minor`, `discount_minor`, `landed_share_minor`, `unit_cost_minor` |
| `material_return` / `_item` | `total_minor`, `discount_reversed_minor` |
| `payment` | `allocated_minor`, `unallocated_minor`, `journal_entry_id` |
| `supplier_payment` | `allocated_minor`, `journal_entry_id` |
| `waive_off` | `amount_minor`, `journal_entry_id`, `approved_by` |
| `delivery_person_payment` | `amount_minor`, `journal_entry_id` |
| `sale_delivery_persons` | `cost_minor` |
| `delivery_rent` | `cost_minor` |
| `cash_flow_entry` | `journal_entry_id`, `classification` |
| `account` | `gl_account_id`, `opening_balance_minor`, `opening_balance_posted_at` |
| `client` / `supplier` | `opening_balance_minor`, `opening_balance_posted_at` |
| `invoice` | `total_minor`, `balance_minor` (or deprecate: it duplicates `direct_sale`) |
| `pending_bill` | `amount_minor`, `is_derived` |

---

## 23. Posting matrix — event to journal entry

Abbreviations: **Dr** debit, **Cr** credit. Rent/delivery charge = "service".

| # | Event | Dr | Cr | Dimensions |
|---|---|---|---|---|
| 1 | GRN received (goods) | 1200 Inventory | 2010 AP | supplier, material, GRN |
| 2 | GRN landed cost (freight/loading) | 1200 Inventory | 2010 AP / 1010 Cash | supplier, GRN |
| 3 | GRN purchase discount | 2010 AP | 1200 Inventory | supplier, GRN |
| 4 | GRN recoverable tax | 1300 Input tax | 2010 AP | supplier, GRN |
| 5 | Supplier payment | 2010 AP | 1010/1020 Cash/Bank | supplier, payment |
| 6 | Goods sold — revenue | 1100 AR | 4010 Sales goods | client, material, sale |
| 6a | Goods sold — discount | 4090 Trade discounts | 1100 AR | client, material, sale |
| 7 | Goods sold — COGS | 5010 COGS goods | 1200 Inventory | material, sale, lot |
| 8 | Sale with unknown cost | 5010 COGS goods | 1400 Inventory suspense | material, sale |
| 9 | Service billed (rent) | 1100 AR | 4020 Sales services | client, service, sale |
| 9a | Service discount (if allowed) | 4090 Trade discounts | 1100 AR | client, service, sale |
| 10 | Driver rent accrued | 5020 COGS services | 2030 Driver payable | driver, sale |
| 11 | Driver payment | 2030 Driver payable | 1010/1020 Cash/Bank | driver, payment |
| 12 | Booking advance received | 1010/1020 Cash/Bank | 2020 Customer advances | client, booking |
| 13 | Advance applied on delivery | 2020 Customer advances | 1100 AR | client, booking, sale |
| 14 | Customer receipt | 1010/1020 Cash/Bank | 1100 AR | client, payment |
| 15 | Customer receipt (unapplied) | 1010/1020 Cash/Bank | 2020 Customer advances | client, payment |
| 16 | Waive-off / settlement discount | 8010 Waive-off | 1100 AR | client, bill |
| 17 | Sales return — goods revenue | 4010 Sales goods | 1100 AR | client, material, return |
| 17a | Sales return — discount reversal | 1100 AR | 4090 Trade discounts | client, material, return |
| 18 | Sales return — goods back to stock | 1200 Inventory | 5010 COGS goods | material, return, lot |
| 19 | Service return | 4020 Sales services | 1100 AR | client, service, return |
| 20 | Expense paid | 6xxx Expense | 1010/1020 Cash/Bank | category |
| 21 | Expense accrued | 6xxx Expense | 2200 Accrued expenses | category |
| 22 | Cash transfer | 1010/1020 destination | 1010/1020 source | — |
| 23 | Cash reconciliation difference | 8030 / 1010 | 1010 / 8030 | day, account |
| 24 | Stock shortage / write-off | 5030 Stock write-off | 1200 Inventory | material |
| 25 | Rounding residual | 4095 or account | account or 4095 | document |
| 26 | Opening balances | as per balance | 3010 Opening equity | — |
| 27 | Period close result | 3xxx Current result | 3090 Retained earnings | period |

Every entry also carries `source_type`/`source_id` and is idempotent per `(source, event)`.

---

## 24. Rollout phases, milestones, cut-over

Each phase ships working software and a demo.

**Phase A — Money + discounts (2–3 weeks)**
- S0, S3 (wizard, per-line storage), S13 partially.
- Demo: a bill with rent + a 2,000 discount split across two ticked items shows whole rupees everywhere; P&L shows Gross → Discounts → Net; returns reverse exactly.
- Exit criteria: no `>2` decimals anywhere; property tests green; the four division sites in `profit_loss.py` deleted.

**Phase B — The book (3–4 weeks)**
- S1, S2 (basic), posting for all documents; trial balance page; `ACCOUNTING_MODE` dual-run.
- Exit criteria: every new document posts; trial balance zero; diff report between document-mode and book-mode is empty or explained.

**Phase C — Subledgers (3 weeks)**
- S4 (stock lots), S6 (AR), S7 (AP), S8 (driver).
- Exit criteria: AR/AP/driver/stock controls tie to the book; ledger pages no longer replay; page timings improved.

**Phase D — Reports + sync (3–4 weeks)**
- S5, S10, S11.
- Exit criteria: P&L, balance sheet, cash flow, trial balance all tie; dashboard = reports; HTML/PDF/Excel identical.

**Phase E — Governance (2 weeks)**
- S2 full close, S12, S15, S16.
- Exit criteria: close checklist passes; permissions enforced; performance budgets met.

**Phase F — History and cut-over (2–3 weeks + waiting)**
- S14 replay, diff, switch on a period boundary, documentation, training.
- Exit criteria: `MIGRATION_DIFF.md` explains every difference; books-agree green for all closed months; backups verified by a restore drill.

Total for one experienced developer: roughly 4–5 months of focused work, with phases A–C being the non-negotiable core. It can be parallelised: A is independent; B must precede C and D; E and F overlap.

**Rollback:** each phase is additive; `ACCOUNTING_MODE=docs` restores old behaviour instantly; migrations are nullable-first so an older binary still boots.

---

## 25. Testing, invariants, and the "books agree" gate

**Invariants (asserted in `tools/consistency_report.py` and in pytest):**

1. `Σ debit = Σ credit` for every entry **and** for every period.
2. Every posted document has exactly one primary entry; every entry has a document.
3. `Σ line discount = header discount` per document.
4. `Σ line net + round_off = document total` per document.
5. AR control = Σ `ar_document.outstanding`; AP control = Σ `ap_document.outstanding`.
6. Inventory control = Σ open `stock_lot.remaining_qty × unit_cost_minor`.
7. Cash GL = Σ `account.balance` (mapped accounts).
8. Driver payable = Σ driver subledger.
9. No journal entry in a locked period created after the lock.
10. Every lot: `received − consumed − remaining = 0`.
11. Every material: `Entry` net vs lot remaining — equal or explained.
12. Returns never exceed original quantity or original discount.
13. No money value with more than 2 decimals exists in a `_minor` conversion.
14. Trial balance zero for every closed month.

**Test layers**

- Unit: money, allocation, pricing, posting, reversal.
- Property-based (`hypothesis` or a simple generator): random documents → invariants hold.
- Integration: a full business cycle (GRN → sale → rent service → discount → return → payment → waive → close) with golden totals — extend `tests/test_full_cycle_smoke.py`.
- Golden report files: P&L, BS, CF, TB for a fixed dataset.
- Cross-page consistency: dashboard = P&L = ledger = API.
- Front-end DOM tests for the discount wizard and live refresh (pattern of `tests/frontend/*`).
- Performance: `tools/benchmark_pages.py` budgets.
- Migration: `tools/roundtrip_compare.py` and the month-by-month diff.

**The "books agree" page** (`/reports/books_agree`) runs all invariants and shows green/red with a link to the first break. It replaces the pile of audit markdown and is the gate before any period close or deployment.

---

## 26. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Discount wizard breaks the busiest screen | Medium | High | Behind a settings flag for one release; DOM tests; server re-validation; keep the old single box as a shortcut that fills the wizard |
| Historical data cannot be replayed cleanly | High | High | Dual-run + month-by-month diff; accept documented differences; never silently "fix" |
| Backfill of `*_minor` finds bad floats | High | Medium | Report exceptions; quarantine; correct via master/opening entries, not by editing history |
| Users dislike period locks | Medium | Medium | Soft-close first; clear messages; adjustment path; training |
| Performance regression from journal joins | Medium | Medium | Indexes, read models, snapshots; measure every phase |
| Reports disagree during dual-run | High | Medium | Expected; the diff report is the deliverable, not a surprise |
| Scope creep (tax, multi-currency, branches) | High | Medium | Explicitly deferred in P16; dimensions reserved in schema |
| Someone deletes a document after posting | Medium | High | Reversal-only policy; DB triggers; purge guard |
| Migration done on live data without snapshot | Low | Critical | Freeze + snapshot + restore drill as a hard gate |
| Too much at once | High | High | Phase gates with exit criteria; ship A alone if needed |

---

## 27. Decisions I need from you

| # | Decision | Recommended | Effect if changed |
|---|---|---|---|
| D1 | Discount default scope | Goods only; services opt-in | If "bill" is default, rent lines will be discounted |
| D2 | Discount rounding | Largest remainder; residual to largest line | "Residual to rounding line" is also valid |
| D3 | Money precision | Paisa (2 dp) in accounting; display whole rupees if you prefer | Rupee-only rounding would make billing coarse |
| D4 | Unknown cost policy | Suspense account, profit complete | Current "exclude from profit" keeps profit understated |
| D5 | Waive-off classification | Other loss 8010 (settlement event) | Could be contra-revenue; accounting differs |
| D6 | Cost of service | From driver allocation rows | Value-weighted split reintroduces fractions |
| D7 | Period close cadence | Monthly, soft then locked | Weekly is possible but heavy |
| D8 | Cut-over date | First day of a month, after a clean diff | Mid-month cut-over complicates opening balances |
| D9 | Tax/VAT | Deferred | If needed now, add tax codes to S3 and S1 |
| D10 | Branches | Deferred, dimension reserved | If needed now, dimension is already in the schema |
| D11 | Void policy | Reversal only; no boot purge of financial rows | Keeping purge means history can disappear |
| D12 | Who can discount | Role + threshold approval | Open discounting defeats the control |

---

## Appendix A — file-by-file change list

**New**

```
app/services/ledger/__init__.py
app/services/ledger/money.py
app/services/ledger/accounts.py          chart of accounts registry + seeding
app/services/ledger/posting.py           post / reverse / assert_balanced
app/services/ledger/query.py             balances, movements, dimensions
app/services/ledger/periods.py           period state machine
app/services/ledger/close.py             close checklist + retained earnings
app/services/ledger/stock.py             lots, FIFO, landed cost, movements
app/services/ledger/recognize.py         revenue/COGS recognition per event
app/services/ledger/subledger_ar.py
app/services/ledger/subledger_ap.py
app/services/ledger/subledger_driver.py
app/services/ledger/cash.py
app/services/ledger/reversal.py
app/services/pricing.py                  line pricing + discount allocation
app/services/read_models/dashboard.py
app/services/read_models/client_account.py
app/services/read_models/supplier_account.py
app/services/read_models/stock_view.py
app/services/read_models/pnl_view.py
app/services/read_models/cash_view.py
app/services/read_models/payables_view.py
app/services/read_models/driver_view.py
app/services/reporting/trial_balance.py
app/services/reporting/balance_sheet.py
app/services/reporting/cash_flow.py
app/services/reporting/pnl.py
app/services/reporting/ageing.py
app/services/reporting/format.py
app/services/reporting/export.py
app/services/cache.py                    version-keyed cache
models/ledger.py                         GLAccount, JournalEntry, JournalLine,
                                         AccountingPeriod, ARDocument, ARAllocation,
                                         APDocument, APAllocation, StockLot,
                                         StockMovement, LedgerVersion
static/discount_wizard.js
templates/accounts/periods.html
templates/trial_balance.html
templates/balance_sheet.html
templates/cash_flow_statement.html
templates/books_agree.html
app/migrations/0003_add_money_minor_columns.sql
app/migrations/0004_create_ledger_tables.sql
app/migrations/0005_create_subledgers.sql
app/migrations/0006_create_stock_lots.sql
app/migrations/0007_add_discount_line_columns.sql
tests/test_money_allocate.py
tests/test_pricing_and_discount.py
tests/test_journal_posting.py
tests/test_period_close.py
tests/test_recognition.py
tests/test_ar_subledger.py
tests/test_ap_subledger.py
tests/test_driver_subledger.py
tests/test_stock_lots.py
tests/test_read_model_consistency.py
tests/test_reports_tie_out.py
tests/test_reversal_policy.py
tests/frontend/test_discount_wizard_dom.js
tools/migrate_ledger_replay.py           history replay + month-by-month diff
docs/MIGRATION_DIFF.md                   generated
```

**Modified (major)**

```
models/catalog.py, models/stock.py, models/sales.py, models/cash.py,
models/parties.py, models/delivery.py, models/__init__.py
app/services/schema.py                   seed accounts, mapping, backfills
app/services/auto_migrate.py             (no change needed; new SQL files only)
app/services/profit_loss.py              delete divisions; read stored + journal
app/services/profit_loss_breakdown.py    read stored; % only
app/services/profit_loss_exports.py      Money + formats on all sheets
app/services/financial_ledgers.py        read subledger instead of replay
app/services/ledgers.py                  collapse duplicate builders
app/services/finance_clients.py          read subledger
app/services/payments_crud.py            allocations + journal in transaction
app/services/waive.py                    post 8010; migration from Payment.discount
app/services/grn_svc.py                  landed cost + posting + auto payment
app/services/sales_core.py               FIFO via ledger/stock
app/services/grn_consumption.py          read stored movements
app/services/driver_payments.py          subledger + journal
app/services/cash_day_recon.py           lock posts difference
app/services/drawer.py                   read model
app/services/billing.py                  entry_no sequence reuse
blueprints/accounts/*                    ledger-backed balances, periods page
blueprints/import_export/*               import posts via posting.post()
app/blueprints/core.py                   dashboard read model
app/blueprints/reports/*                 new statement routes
app/blueprints/sales/*                   per-line discount, reversal edits
app/blueprints/ops/*                     GRN/dispatch posting
app/blueprints/ledgers/*                 subledger reads, apply/unapply
app/blueprints/masters/*                 ledger-aware merges/renames
app/blueprints/misc/*                    wipe/purge guards
templates/*                              money filter, wizard, new report pages
static/profit_report.js                  read new blocks
static/ledger_live.js                    version watcher
tools/consistency_report.py              new invariants + books-agree
tools/audit_sequence_smoke.py            extended end-to-end assertions
tools/benchmark_pages.py                 budgets
setup_Deploy.py                          post-install books-agree check
README.md                                architecture section
```

**Deleted / retired (after cut-over)**

```
profit_loss.py replay internals (_Projection recognition paths)   → ledger queries
ledgers._build_client_ledger_rows                                  → subledger read
finance_clients._compute_client_financial_summary                  → subledger read
schema._purge_voided_rows_at_boot (financial rows)                 → reversal policy
```

---

## Appendix B — glossary

- **Journal / book** — the double-entry record of every economic event: equal debits and credits.
- **Control account** — a ledger account whose balance must equal a supporting list (AR, AP, inventory, driver payable).
- **Subledger** — that supporting list: one row per bill or GRN with paid/outstanding amounts.
- **Posting** — writing the journal entry for a document, once.
- **Reversal** — a mirror entry that cancels a posted one; the only way to change history.
- **Period** — a month; open, soft-closed, or locked.
- **Suspense** — a holding account for an amount whose true classification is not yet known (e.g. cost of goods that has no GRN lot).
- **Landed cost** — purchase price plus freight/loading/other charges, minus purchase discount.
- **FIFO** — oldest lot consumed first.
- **Largest remainder** — the standard way to split an amount into whole units so the parts add back to the total exactly.
- **Read model** — a prepared, cached page-shaped answer computed from the book, so pages never recompute.
- **Dual-run** — running the old report and the new book side by side and comparing before switching.
- **Trade discount** — a reduction agreed at billing time (contra-revenue).
- **Settlement discount / waive-off** — a reduction granted when the customer pays (an other loss, dated at settlement).

---

*Prepared as a study document. Nothing in this file changes application behaviour. When you approve the policies in §27 and the order in §24, implementation starts with Phase A, which is self-contained and fixes the discount and rounding problems you reported.*
