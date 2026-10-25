# GRN stock-consumption indicator & enhanced Profit & Loss — implementation report

Date: 2026-10-25
Scope: enhancement of the existing FBM Flask application. No rebuild, no route
renames, no schema change, no data rewrite.

---

## 1. What was delivered

Two features, both additive:

1. **GRN screen — stock-consumption indicator per GRN lot** (server-rendered,
   authoritative) with a horizontal bar, the percentage label at the right of
   the bar, received / consumed / remaining quantities and the five colour
   bands required by the specification. A refresh button repaints from a JSON
   endpoint without reloading the page; rows inserted by any existing workflow
   are repainted automatically.
   *Layout pass (§ 3.1):* the row actions are one uniform icon strip and the
   screen-level summary is a single slim strip.  *Compact pass (§ 3.2):* every
   GRN is one line; an expand arrow opens a details row that lists each lot's
   own received / consumed / remaining figures and bar.  There is deliberately
   no per-GRN roll-up column — several items on one GRN report separately.
2. **Profit & Loss Control Center — automatic filters and a full breakdown**
   on top of the existing report engine: no Apply button, debounced text
   fields, searchable dropdowns (white background, black text, scrollable),
   summary cards, item / service / customer / date / transaction breakdowns,
   expandable drill-down to each transaction, a visually separated
   "estimate / balance" block, and Net Profit / Loss as the single headline.

All existing screens, routes, permissions, ledger maths and exports keep
working; the original plain-language summary on the P&L page is preserved
(Total Profit / Total Loss tiles, "Read the result in three steps",
"Sales by Item" / "Sales by Customer", the same wording the existing test suite
pins).

---

## 2. Files changed

### New

| File | Purpose |
|---|---|
| `app/services/grn_consumption.py` | Read-only FIFO replay that turns GRN lots + `GRNAllocation` + physical issues + returns into received / consumed / remaining per lot. |
| `app/services/profit_loss_breakdown.py` | Cards, reconciliation, by-item / by-service / by-customer / by-day tables, document settlement (payment status), booking status, outstanding credit, advances, estimated undelivered margin, GRN & inventory section. |
| `templates/partials/profit_loss_results.html` | The results region. Rendered inside the page **and** re-rendered alone for `?partial=1`. |
| `static/profit_report.js` | Automatic filters, searchable comboboxes, debounce, in-flight indicator, partial fetch, URL sync, error banner. |
| `static/grn_consumption.js` | Bar / percentage / colour painting, ARIA progressbar, mutation observer, JSON refresh; repaints the read-outs beside each bar through `data-grn-scope` and the whole-screen overall meter. |
| `tests/test_grn_consumption_indicator.py` | 7 tests: FIFO parity, spec scenario, bands, recorded vs reconstructed, page/API agreement, filter invariance, returns. |
| `tests/test_grn_screen_layout.py` | 6 tests: the collapsed row is one line with an expand button over a hidden details row, every read-out sits in a repaint scope, the slim overview strip aggregates the service exactly, the row actions are one uniform icon strip, the refresh button is wired to CSS that exists, the toolbar keeps its controls. |
| `tests/frontend/test_grn_consumption_dom.js` | 43 jsdom checks: the compact row / hidden details row, the expand-collapse state machine (`aria-expanded`, icon, title, row class), repaint of the bars **and** the figures beside them, scope isolation between lots, the whole-screen meter (only the lots on the page, no per-GRN meter), clamping. Runs offline (`npm i jsdom`). |
| `tests/test_profit_loss_automatic_filters.py` | 12 tests: every filter (including the GRN lot filter), sorting stability, reconciliation, partial rendering, drill-down links, realized vs estimate separation. |

### Modified

| File | Change |
|---|---|
| `app/services/profit_loss.py` | Exposes final lot balances (`grn_lot_balances`); adds `payment_status`, `booking_status`, `transaction_group`, `entity_type`, `client_category`, `sort`, `order`, `include_breakdown`; wires the breakdown and inventory view into the report dict; adds `services` / `client_categories` / `grn_choices` to the filter options; two pure performance indexes (`@lru_cache` material key, service-candidate and return-candidate indexes) that do not change any value. |
| `app/blueprints/reports/_profit_profit_reports.py` | Adds drill-down URLs (`entries_url`, `profit_url`, `loss_url`, `unknown_url`, per-card links, estimate anchor), a `sort_url` helper that stays on the current view, and returns the results partial for `?partial=1`. |
| `app/blueprints/ops/grn.py` | Builds the per-lot consumption view for the listed GRNs and passes it to the template (fail-soft: a projection error never breaks the page). |
| `app/blueprints/api.py` | New read-only endpoint `GET /api/grn_consumption` (login required, optional `grn_ids`). |
| `app/services/profit_loss_exports.py` | Eight additional Excel sheets (`P&L Cards`, `By Item`, `By Service`, `By Customer`, `By Day`, `Outstanding Credit`, `Customer Advances`, `GRN & Inventory`). Existing six sheets unchanged in name and order. |
| `templates/profit_reports.html` | Rebuilt filter bar + results region + JS bootstrap. Same URL, same route, same header actions (View All Transactions, Excel, PDF, Print). |
| `templates/grn_wizard.html` | Indicator markup and CSS (theme-aware), consumption refresh button, script tag. Every existing control left in place. Layout pass (§ 3.1): uniform action strip, slim screen-level meter, wrapping toolbar. Compact pass (§ 3.2): one line per GRN with an expand arrow; per-lot detail strips are server-rendered into a hidden `grn-details-row`; no per-GRN roll-up. |
| `templates/profit_report_pdf.html` | Adds the cards, reconciliation, item / service / customer tables, outstanding credit, estimate table and inventory section to the existing PDF. |

**No database migrations, no backfills, no data changes.** The feature is a
projection over tables that already exist (`GRN`, `GRNItem`, `GRNAllocation`,
`DirectSale`, `DirectSaleItem`, `Booking`, `BookingAllocation`, `Entry`,
`MaterialReturn`, `Payment`, `Obligation`-style ledger projections).

---

## 3. How the GRN consumption indicator works

```
GRNItem.qty            -> received
Σ GRNAllocation.qty    -> recorded consumption (authoritative)
FIFO replay of issues  -> reconstructed consumption for everything not allocated
returns                -> give quantity back to the lots the dispatch used
consumed = received − remaining
percent  = clamp(consumed / received × 100, 0, 100)       (0 when received = 0)
```

* **One GRN item = one FIFO cost lot.** Lots are ordered by GRN date, then GRN
  id, then lot id; a lot is available from the calendar day its GRN was posted
  (the same rule the posting screen uses, so same-day lots are not hidden).
* **Recorded allocations win.** `GRNAllocation` rows written by the sale
  posting path are consumed first, exactly as the P&L costs them. Only the
  remainder of a dispatch is replayed oldest-lot-first.
* **Physical issues** considered by the replay: direct/credit sale lines, the
  booking-supply portion of a booking delivery (`BookingAllocation`), the
  chargeable remainder of a mixed document, and standalone dispatch `Entry`
  OUT rows. Service/rent lines are never stock.
* **Returns** (normal and booked) restore quantity proportionally to the cost
  layers of the dispatch they are matched to; a return that cannot be traced to
  a surviving lot never inflates a GRN.
* **Status is disclosed per row**: `recorded` (evidenced by posted
  allocations), `reconstructed` (replay only), `untouched`, `unknown`
  (no received quantity). The GRN screen shows a small badge for the first two.
* **The replay always covers the whole ledger**, even when the screen is
  filtered by search or dates; only the *reported* rows narrow. This is what
  keeps a lot's percentage identical after typing in the search box (covered by
  `test_05`). Filtering the replay itself would push an old dispatch onto a
  newer lot and change the answer.
* **The bar/percentage is server-rendered.** JavaScript repaints the same
  numbers from `data-received` / `data-consumed` (and from
  `/api/grn_consumption` on refresh); it never computes inventory truth. If the
  endpoint fails, the screen keeps the server values and says nothing changed.
* **Colour bands** (server and JS agree):

  | consumption | band | colour |
  |---|---|---|
  | 0 – 24.99% | low | green |
  | 25 – 49.99% | moderate | light green |
  | 50 – 74.99% | high | amber |
  | 75 – 99.99% | critical | orange |
  | 100% | complete | red |

* Accessibility: each track is `role="progressbar"` with
  `aria-valuemin/max/now/valuetext` and a title showing *consumed of received*.
  Layout is flex-based and collapses under 768 px; no fixed pixel width depends
  on the number of rows.
* Survivability: page load, server-side filter/search/date changes, sort
  changes and pagination all re-render from the server; rows injected by an
  existing workflow are repainted by a `MutationObserver`. No element ids are
  generated per row, so duplicate ids are impossible (asserted in
  `test_04`).

### 3.1 Layout pass (what the screen looked like before, and why it changed)

The first cut put the numbers on the page but not the space to read them. Four
problems were fixed in `templates/grn_wizard.html` + `static/grn_consumption.js`
with **no change to any projection, route or number**:

| Problem | Fix |
|---|---|
| The per-GRN roll-up meter was inside the **Total Qty** column: a `min-width: 190px` box whose bar was clamped to `min-width: 90px` next to a 46 px label, so the one figure that says *is this GRN still in the warehouse?* was the least readable thing on the row. | The roll-up moved to its own **Overall consumption** column (bar 14 px tall, 190–250 px wide, 1 rem percentage) with *consumed / left* and the stock value still in hand under it. **Total Qty** is now just a right-aligned, tabular quantity plus a line count. *(Superseded in § 3.2: the roll-up column was removed — each lot reports separately in the expanded row.)* |
| Received / consumed / remaining sat **outside** the element the repaint script searches (`el.querySelector` inside `[data-grn-consume]`), so *Recalculate* moved the bars and left the figures stale. | Each block is wrapped in `data-grn-scope`; `paint()` falls back to that scope. The jsdom test asserts the figures move *and* that lot 2 does not inherit lot 1's numbers. |
| The Actions cell mixed three icon buttons with a wide labelled **Delete** and an even wider **Shift** button (plus a `d-inline` form), so the strip wrapped unevenly and the buttons had five different widths. | One `.grn-row-actions` flex strip: five 34 px icon-only outline buttons (38 px under 768 px), each with `title` **and** `aria-label`, delete still a confirmed POST, shift still a modal trigger. |
| The ⟳ button toggled a `.spin` class that **no stylesheet defines**, and added `btn-danger` / `btn-success` on top of `btn-outline-info`, which `theme.css` defines *later* — so the busy and the success/failure states were invisible. | `.grn-refresh-spin` is defined in the page stylesheet, and the base `btn-outline-info` is removed before the state class is applied, so the button really spins and really turns green/red. |

Additions that came with the space:

* **Overall consumption panel** above the table *(replaced in § 3.2 by a one-line strip)* — one big bar for the whole
  screen, measured as *consumed value ÷ received value* at lot cost (unit-safe,
  unlike summing bags + tons + CFT), with lots on screen, fully consumed,
  FIFO-replay lots and the stock value still in hand. It is server-rendered from
  the same projection and repainted by the refresh button from the same JSON
  payload, so it can only ever reproduce what a page load would print.
* Each lot is a bordered card (name, @rate, `recorded` / `reconstructed` badge,
  the three figures, then the bar), instead of one dashed rule between lots.
* The toolbar is a wrapping `.grn-toolbar` (dates, search, **Filter**, **Clear**,
  **Excel**, **Recalculate**) with labelled buttons instead of seven loose icon
  controls, and the sort links use `.grn-sort-link` — `text-dark` was invisible
  on the dark card header.

Pinned by `tests/test_grn_screen_layout.py` (6 tests: compact row + expand
button, one repaint scope per lot, overview aggregation against the service, the
action strip, the refresh wiring, the toolbar) and
`tests/frontend/test_grn_consumption_dom.js` (43 jsdom checks, including the
expand / collapse state machine).

### 3.2 Compact pass — vertical space, an arrow, no per-GRN roll-up

A list of GRNs was still tall: every lot was a full card inside the row, and a
separate column repeated an aggregate figure for the whole GRN.  The screen now
reads as a list first and a report second:

| Before | Now |
|---|---|
| Each row showed one card per lot (name, rate, badge, three figures, bar) plus a **GRN roll-up** column. A two-lot GRN was roughly three stacked blocks tall. | The row is a single line: arrow, star, GRN #, date, supplier, the lot names + quantities, total qty, discount, actions. No bar and no percentage are in the collapsed row at all. |
| The roll-up repeated what the lots already said. With several items in one GRN it was a second, aggregate answer to a question the operator was asking per item. | The **Overall consumption** column is gone. Expanding the row shows `Consumption by lot`: one compact strip per item with *its own* received / consumed / remaining, bar, percentage, rate, `recorded` / `reconstructed` badge and stock value in hand. No aggregate is rendered — not in the row, not in the panel. |
| The whole-screen overall meter was a two-column panel (headline, caption, 18 px bar, legend row + a 2×2 card grid). | One slim strip: title, percentage, 8 px bar, received / consumed / in hand, and the lot counts + stock value in a single muted line. Same server projection and same JSON refresh contract. |

The details row is **server-rendered** (`<tr class="grn-details-row" id="grnDetails<id>" hidden>`)
and the arrow is a real `<button aria-expanded aria-controls>`; the toggle only
flips `hidden` and the icon, so every figure is on the page with or without
JavaScript. `static/grn_consumption.js` exposes `initExpanders`, `setExpanded`
and `toggleRow`, and the expander is idempotent for rows inserted later.

### Worked example from the specification (asserted end to end in `test_01`)

| | |
|---|---|
| Purchases | 2,000 @ 1,200 (GRN-A) + 2,000 @ 1,300 (GRN-B) |
| Dispatch | 2,100 @ 1,500 |
| Allocations written | 2,000 from lot A @ 1,200, 100 from lot B @ 1,300 |
| Revenue | 2,100 × 1,500 = **3,150,000** |
| COGS (FIFO) | 2,400,000 + 130,000 = **2,530,000** |
| Gross profit | **620,000** |
| Lot A indicator | received 2,000 / consumed 2,000 / remaining 0 → **100% · complete · recorded** |
| Lot B indicator | received 2,000 / consumed 100 / remaining 1,900 → **5% · low · recorded** |
| Remaining stock | 1,900 × 1,300 = **2,470,000** |

The P&L replay ends on exactly the same lot balances (`test_01`, `test_06`,
`test_07`), so the indicator and the reported FIFO cost can never disagree.

---

## 4. How the P&L page works now

### 4.1 Automatic filters (`static/profit_report.js`)

`period`, `start_date`, `end_date`, entity type (customer ↔ client category),
searchable customer / category / item-service combobox, transaction type,
payment status, booking status, GRN lot, activity category, supplier, free-text
reference search and rows-per-page.

* No Apply button. Selects and dates apply on `change`; text fields are
  debounced 500 ms; combobox selection applies immediately.
* Editing a date switches the period to *Custom* automatically, so typed dates
  are never ignored by a preset.
* While a request is in flight the results region is dimmed, a top progress bar
  runs and the state label reads *Updating…* — stale figures are never
  presented as current.
* The fetch targets `?partial=1`, so only the results region is replaced; the
  filter bar keeps focus, scroll and values. Failures show a banner with a
  link to the same report as a full page.
* The address bar is updated with `history.replaceState` (the transport flag is
  stripped), so the view is bookmarkable and back/forward reloads it.
* `noscript` falls back to the normal GET submit.
* Sorting and pagination links inside the region are intercepted
  (`a[data-pl-nav]`) and fetched as partials; sorting keeps the current view and
  never changes any total.

### 4.2 Summary cards

Realized: Delivered Goods Sales, Earned Service Revenue, COGS (FIFO), Direct
Service Costs, Gross Profit, Service Margin, Operating Expenses, **Net Profit /
Loss** (hero, the single headline figure).
Position / estimate (visual `estimate` / `balance` badges, dashed cards, never
added): Credit / Outstanding Receivables, Customer Advances, Estimated Profit on
Undelivered Bookings.

Every realized card links to the filtered transaction list; the estimate card
anchors to `#pl-undelivered-estimate`.

### 4.3 Breakdowns

* **By item** — delivered qty, goods sales, FIFO COGS, gross profit, margin,
  remaining stock (qty and FIFO value).
* **By service** — revenue earned, direct cost, margin (Rs. and %).
* **Delivery-person / direct service cost** rows.
* **Operating expenses by category.**
* **By customer** — revenue, cost, profit, client category (the application has
  no separate company-site dimension; sites are clients / client categories —
  stated on the page).
* **By delivery date** — chart plus table.
* **By sales transaction** — sortable, paginated; each reference opens
  `profit_transaction_detail` which already shows the GRN cost layers
  (qty × unit cost).
* **Reconciliation line** — gross profit + service margin + other income + gains
  − expenses − losses = net, flagged when it differs from the ledger by more
  than rounding (it does not: see §6).
* **Collections & credit**, **Bills with a balance** (top 40 by balance),
  **undelivered bookings / advances**, **GRN purchases & remaining inventory**,
  **prior-equivalent period**, **accounting coverage & data quality**.

### 4.4 Filters are selection only

`payment_status`, `booking_status`, `transaction_group`, `entity_type` /
`client_category` only decide which rows are included. The totals are always
computed from every matching row, and every filtered view still reconciles
(`test_07` iterates all filters and asserts `reconcile.matches is True`).

---

## 5. Accounting rules enforced

| Rule | Where |
|---|---|
| Revenue and COGS recognised on delivery, never on booking | existing projection (unchanged); asserted by existing + new tests |
| Credit sales are revenue in full; unpaid credit is a receivable | `credit_outstanding` card + "Bills with a balance"; never subtracted from profit (`test_01`, `test_02`) |
| Collections never create profit | `Money Received` tile / financial position only |
| Booking prices are never retro-updated | existing engine; existing tests unchanged |
| Service margin = service revenue − delivery-person cost (5,000 − 3,500 = **1,500**) | `by_service`, service-margin card (`test_03`) |
| Unpaid service charge → receivable; unpaid driver cost → payable | document settlement + `driver_outstanding` (existing) |
| Earnings-date filtering for P&L, payment-date for collections, GRN-date for purchases | report period vs `payment_received_total` vs `grn_section` |
| No duplicate revenue/cost | reconciliation must match; returns reverse on the return date |
| Cancelled / reversed / void excluded | existing `_active()` guards, `voided_source_count` shown |
| Internal transfers are not sales | existing projection (Entries are not revenue) |
| Advances are not earned revenue | Customer Advances card, excluded from net |
| Estimated margin is not profit | separate block, own total, never added |

---

## 6. Verification on real data (legacy database, 2026 full year)

`LEGACY DATA` production copy, read-only:

| Measure | Value (Rs.) |
|---|---|
| Delivered goods sales | 100,322,455.48 |
| FIFO cost of goods sold | 97,734,906.10 |
| Gross profit | 2,587,549.38 |
| Earned service revenue | 1,435,674.12 |
| Direct service costs | 1,437,528.75 |
| Service margin | −1,854.63 |
| Operating expenses | 1,420.00 |
| Other losses | 495,522.26 |
| **Net profit** | **2,088,752.49** |
| Credit / outstanding receivables | 6,060,285.77 |
| Customer advances (open bookings, current position) | 57,864,050.79 |
| Estimated profit on undelivered bookings (known-cost lines only) | −5,628,100.60 |
| GRN purchases in period | 92,256,645.00 (152 lots) |
| Remaining inventory | 40,326,974.10 over 43,186.0 units |

Reconciliation difference: **−5.8 × 10⁻⁸** (floating-point noise) — `matches = True`.
The same numbers appear on the page, in the `?partial=1` response, in the PDF
and in the Excel export.

GRN indicator on the same data: 152 lots → 73 low, 1 moderate, 5 high,
4 critical, 69 complete; 82 lots reconstructed, 1 recorded, 69 untouched
(legacy imports predate `GRNAllocation`, see §9).

---

## 7. Tests executed

Commands and results (Python 3.11, SQLite fixtures created by `tests/conftest.py`,
no production database touched):

```
python -m pytest tests/test_grn_consumption_indicator.py -q
  7 passed

python -m pytest tests/test_profit_loss_automatic_filters.py -q
  12 passed

python -m pytest tests/test_profit_loss_fifo.py tests/test_profit_loss_control_center.py \
    tests/test_profit_simple_summary.py tests/test_grn_datetime_override.py \
    tests/test_row_star_highlight.py tests/test_predator_regressions.py \
    tests/test_grn_consumption_indicator.py tests/test_profit_loss_automatic_filters.py -q
  71 passed

python -m pytest -q          # whole suite, final code
  352 passed, 6 skipped, 2 warnings, 11 subtests passed in 1028.31s (0:17:08)
```

Baseline before this change: 333 passed, 6 skipped (881.77 s). The 19 added
tests are the 7 GRN-consumption tests and the 12 filter/reporting tests; no
existing test was modified, skipped or deleted.

Browser-level checks (jsdom against the real rendered HTML, script executed as a
browser would):

* one `fetch` per filter change, always with `partial=1`, address bar cleaned;
* no fetch inside the 500 ms debounce window, exactly one after it;
* editing a date flips the period to `custom` and sends the typed date;
* sorting from inside the result region is fetched as a partial and does not
  navigate;
* combobox opens on focus (200 options), typing `abdul` filters to 10, selecting
  writes the hidden field and refreshes; switching to *client category* offers
  `General / Open Khata / Walking-Customer`;
* a failed request shows the error banner with a full-page link;
* the loading class is applied during the request and cleared after it;
* GRN indicator: every lot bar painted, 0 width/aria/level mismatches against
  the data attributes, 0 duplicate ids, 100% clamped from 140%, divide-by-zero
  returns 0%, injected row repainted, refresh moves the bar. No per-GRN summary
  element is rendered (removed in § 3.2).
* GRN layout (jsdom, `tests/frontend/test_grn_consumption_dom.js`): the arrow
  opens and closes the hidden details row (`aria-expanded`, icon, title and
  `is-expanded` stay in agreement), a refresh repaints the bar *and* the
  received / consumed / remaining figures beside it, lot 2 keeps its own figures
  (scope isolation), the whole-screen meter follows the payload for the lots on
  the page, percentages stay clamped — 43 checks.
  The same script was also run against the real rendered `/grn` page with a real
  `/api/grn_consumption` payload: the per-lot bars and the overall meter matched
  the payload, and the first row’s arrow toggled its details row (43 checks).

---

## 8. Performance (legacy data, 2026)

| Operation | Time |
|---|---|
| Projection build | 1.39 s |
| GRN consumption replay (152 lots) | 0.44 s |
| P&L report, full year | 1.81 s (2.00 s with position) |
| P&L report, one month | 1.57 s |
| `GET /profit_reports` (full page) | 2.0 – 2.6 s |
| `GET /profit_reports?partial=1` | 2.0 – 2.4 s |
| `GET /grn` (152 GRNs, 152 lots) | ~0.95 s |
| `GET /api/grn_consumption` | ~0.45 s |
| Excel export (quarter) | 0.54 s |

Two value-neutral optimisations were added to the projection (memoised material
key, indexed service/return candidate lookups); they cut several seconds from
the full-year build without changing a single number (the full suite, including
the FIFO parity tests, still passes).

---

## 9. Legacy data limitations (disclosed, not hidden)

1. **`GRNAllocation` coverage is thin in the imported history** — 1 of 152 lots
   has recorded allocations; 82 are consumed by the replay. Those lots are
   labelled `reconstructed` on the GRN screen, and the report says so. No
   allocation row was invented or written.
2. **1,390 replay warnings** (all surfaced, first 20 in the API/Excel):
   dispatches that predate every dated GRN lot of their material. They consume
   no GRN lot; revenue is shown, cost is resolved by the existing P&L rules
   (nearest later lot / master fallback) exactly as before. The indicator never
   silently drops stock.
3. **358 undelivered booking lines have no defensible GRN cost** (materials
   never received in this database). Their estimated margin is reported as
   "cost unknown" and excluded from the estimate total; no cost was fabricated.
   This is why the estimate card shows a negative total: stale legacy bookings
   were priced below today's recorded GRN cost.
4. **Customer advances (57.9 M) and receivables (6.1 M) are current balances**,
   not period figures; they are labelled as such and are never added to profit.
5. **No company-site entity exists** in this schema. The customer selector
   therefore covers clients, and the "entity type" switch offers
   client / client category. This is stated on the page.
6. **A mixed document** (booking supply + excess sold directly on the same
   sale) is settled against the booking bill when the booking obligation exists,
   otherwise against the sale bill. Splitting a single bill across two
   obligations is not modelled by the ledger and is noted here rather than
   approximated per row.
7. **Advances and the balance list are capped for display** (top 40 balances,
   100 advance rows in the report, 200 document rows in Excel); the totals are
   complete.

---

## 10. Manual verification recipe

1. Open **GRN**. Above the table one slim **Overall consumption** strip shows
   the whole screen (consumed value ÷ received value at cost) with received /
   consumed / in hand, lot counts and the stock value still in hand.
   Every record is one line. Click the **chevron** on the left of a row: it
   turns down and a `Consumption by lot` panel opens listing each item on that
   GRN with its own name, @rate, `recorded`/`reconstructed` badge, received /
   consumed / remaining, bar and percentage, and the value still in hand for
   that lot. There is no per-GRN aggregate column. Collapse it with the same
   arrow. The Actions column is five equal icon buttons — view, print, edit,
   shift, delete. Click **Recalculate** next to Excel to pull a fresh replay:
   the bars, the figures beside them, the hidden detail rows and the slim
   overall strip all move, and the icon spins while the request is in flight.
2. Filter or search the GRN screen: the same lot keeps the same percentage.
3. Open **Profit & Loss**. Change *Payment status* to *Credit outstanding*:
   the cards and tables update without a page reload and without an Apply
   button; the top bar shows the request in flight.
4. Type in *Customer* (white dropdown, scrollable) — options filter as you type.
5. Click a summary card: it opens the entries page filtered to that card, with
   no `partial` flag in the URL.
6. Click a transaction reference: the detail page lists the GRN cost layers
   (quantity × unit cost) that produced the profit.
7. Excel / PDF: the new sheets/sections are appended; the original six sheets
   and the authoritative summary are unchanged.
