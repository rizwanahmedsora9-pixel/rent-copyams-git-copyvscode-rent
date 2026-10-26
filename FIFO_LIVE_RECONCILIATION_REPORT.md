# FIFO / GRN / Profit & Loss live reconciliation — fix report

Date: 2026-10-26  
Scope: one FIFO truth for sale posting, GRN consumption and Profit & Loss. No
schema change, no data rewrite, no second costing algorithm.

---

## 1. What was wrong

The application had three different pictures of the same stock movements:

1. **P&L** ran a global chronological replay of sales, booking supplies,
   legacy dispatches and returns. It filled any uncovered balance from the next
   real GRN lots, even when those lots were dated after the dispatch.
2. **GRN consumption** ran a second replay that skipped that forward
   consumption whenever another GRN was already dated before the dispatch.
   On bundled 2026 data this left **19 of 152 lots / 6,438 units** looking
   unconsumed on the GRN screen while P&L had charged them to COGS: the GRN
   screen said 25,057 units remained, while P&L said 18,619.
3. **Sale posting** chose lots from recorded `GRNAllocation` rows only. Legacy
   and booking dispatches that had consumed stock in the P&L replay were still
   “available” to a new sale, so the new sale could be allocated to the wrong
   lot.

A fourth issue hid the mismatch from P&L: when a sale line carried a frozen
single cost rate, that scalar was charged over the whole unallocated remainder
even when the global replay consumed several real lots at different rates.
The GRN page showed exact lots; P&L showed one `FrozenRate` line.

---

## 2. What changed

### One authoritative replay

* `app/services/profit_loss.py::_Projection` is now the single global FIFO and
  COGS replay. Its `grn_lot_balances` are the source of truth for remaining
  quantities.
* `app/services/grn_consumption.py::_ProjectionLedger` adapts those same
  balances to the GRN indicator. The independent `_LotLedger` replay is no
  longer used by the GRN page, `/api/grn_consumption`, the P&L breakdown or the
  inventory view. It remains only for callers that explicitly pass a prepared
  ledger.
* `build_profit_loss_report()` passes its existing context through
  `fifo_context=context` to the GRN/inventory view, so a report and its stock
  panel cannot disagree.
* `build_inventory_view(context=...)` forwards the same context.
* `/api/grn_consumption` now builds from that authoritative replay. Refreshing
  the browser cannot produce a different answer than a fresh page load.

### Same FIFO at sale-posting time

* `_Projection(exclude_sale_id=..., through_date=...)` builds a posting-date
  snapshot that excludes the sale being edited and ignores later dispatches and
  returns.
* `build_fifo_snapshot()` exposes that replay to
  `_expand_chargeable_items_fifo()`.
* Sale lines now reserve the remaining quantities from the same lot queue that
  P&L and GRN consume. Earlier lines of one document still reserve their own
  quantities, so no lot is promised twice.
* Recorded `GRNAllocation` rows remain authoritative for their own lines.

### No rate mixing

* For every unrecorded unit, P&L costs the exact FIFO lot the replay consumed,
  at **that lot’s own rate**. A frozen scalar is used only for quantity that no
  real GRN lot can cover; a pure master-price placeholder is still labelled
  `estimated`.
* Every lot-based cost source now carries `grn_item_id`, `lot_rate`, qty,
  amount, GRN reference, supplier and date. `_merge_sources()` keys by
  `(type, grn_id, grn_item_id, reference, rate)`, so two same-rate lots under
  one GRN are not silently merged.
* `Lot #<id>` appears in the P&L results layers, the transaction detail, the
  PDF and the Excel `GRN Cost Layers` sheet.

### Real-time screen updates

* `GET /api/ledger_revision` returns a cheap token derived from the SQLite
  database, WAL and SHM file size/mtime. It does not use the per-connection
  `PRAGMA data_version`, which would cause false refreshes when the connection
  pool changed.
* `static/ledger_live.js` polls every 12 seconds and on window focus,
  visibility restoration and field focus-out. It coalesces concurrent checks,
  pauses while a user is editing, and refreshes only when the token changes.
* On a change, the GRN screen repaints from `/api/grn_consumption` and the
  P&L screen reuses its existing `?partial=1` fetch. The watcher waits for the
  P&L partial response before calling the refresh successful. Both endpoints
  read the same FIFO replay. This is polling, not SSE/WebSocket event push.
* `/grn` and `/profit_reports` load the watcher and show a live status element.

---

## 3. Required acceptance scenario

| Evidence | Result |
|---|---|
| GRN-old 10 @ Rs 1,000 + GRN-new 30 @ Rs 1,200 | two separate lots |
| Sale 40 @ Rs 1,300 | two `GRNAllocation` lines: `(10, 1000)` and `(30, 1200)` |
| COGS | `10 × 1000 + 30 × 1200 = Rs 46,000` |
| Profit | `52,000 − 46,000 = Rs 6,000` |
| GRN balances | both lots fully consumed; remaining `0` |
| P&L cost layers | lot IDs and rates `1000`, `1200`; no blended `1150` |
| GRN API / detail / PDF / Excel | same quantities, same lot IDs, same rates |

The new `tests/test_fifo_grn_profit_realtime.py` also verifies the harder
historical-data case: an older dated lot is exhausted and a later GRN covers
the balance. P&L, the GRN screen and `/api/grn_consumption` all report both
lots fully consumed.

---

## 4. Bundled legacy data verification

Read-only run against a copy of `LEGACY DATA/AMS_SMOOTH_IMPORT.amsdb`
(2026-01-01 to 2026-12-31):

| Measure | Before | After |
|---|---:|---:|
| GRN lots checked | 152 | 152 |
| GRN/P&L lot balance differences | 19 | **0** |
| Difference in units | 6,438 | **0** |
| GRN-screen remaining quantity | 25,057 | **18,619** |
| Remaining stock value | — | **Rs 11,324,917.10** |
| COGS | — | **Rs 101,977,664.85** |
| Net profit | — | **Rs -716,477.51** |
| Goods rows with cost sources | — | **2,502** |
| Rows whose source amounts do not equal row cost | — | **0** |
| Rows with more than one purchase rate | — | **46** |
| Rows tied to explicit GRN lot IDs | — | **2,484** |
| Rows still shown as `FrozenRate` | 2 | **0** |

COGS and net profit are unchanged on this dataset, so the fix reconciles GRN
and P&L without rewriting historical profit. Eighteen rows still use a clearly
labelled estimated rate because no real GRN lot exists for their quantity; they
are not hidden inside a mixed-rate line.

---

## 5. Tests

* Full Python suite: **372 passed, 6 skipped, 11 subtests passed** in 973.66 s.
* New end-to-end regression: `tests/test_fifo_grn_profit_realtime.py` — 4 tests
  covering shared lot balances, exact cost layers, sale posting FIFO, the live
  revision token, and the wiring of both screens.
* Updated `tests/test_profit_loss_multi_grn_costing.py` and
  `tests/test_profit_loss_control_center.py` to assert that real lot rates beat
  a frozen scalar, instead of expecting the old mixed-cost behaviour.
* Frontend DOM suites (jsdom):
  * `test_grn_consumption_dom.js`: **43 checks passed**
  * `test_pan_view_dom.js`: **33 checks passed**
  * `test_ledger_live_dom.js`: **15 checks passed**
* `node --check` passed for `ledger_live.js`, `profit_report.js` and
  `grn_consumption.js`.

Browser-visual verification was not performed in the headless sandbox; the DOM
suites verify the live watcher’s request/coalescing/refresh behaviour and the
server routes verify the data it receives.

---

## 6. Files touched

* Core: `app/services/profit_loss.py`, `app/services/grn_consumption.py`,
  `app/services/profit_loss_breakdown.py`, `app/services/sales_core.py`,
  `app/services/profit_loss_exports.py`.
* Routes/UI: `app/blueprints/api.py`, `static/ledger_live.js`,
  `static/profit_report.js`,
  `templates/grn_wizard.html`, `templates/profit_reports.html`,
  `templates/partials/profit_loss_results.html`,
  `templates/profit_transaction_detail.html`,
  `templates/profit_report_pdf.html`.
* Tests/docs: `tests/test_fifo_grn_profit_realtime.py`,
  `tests/frontend/test_ledger_live_dom.js`, two updated regression suites,
  `README.md`, `MULTI_GRN_COSTING_FIX_REPORT.md`, this report.

---

## 7. Known limits

* Live refresh is a 12-second polling loop plus focus/visibility checks, not
  server push. It is intentionally lightweight on the bundled dataset, where a
  full projection takes roughly 0.4–1.8 s.
* GRN loading, freight and tax are still not allocated into FIFO unit costs;
  they are not silently expensed or added to COGS.
* A quantity that genuinely exceeds every real GRN lot is still costed from the
  frozen sale rate or material master price and is labelled accordingly.
