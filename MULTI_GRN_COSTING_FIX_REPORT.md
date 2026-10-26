# Multi-GRN material costing in Profit & Loss — fix report

> **Partly superseded (2026-10-26).** See
> `FIFO_LIVE_RECONCILIATION_REPORT.md`.  Recorded `GRNAllocation` rows still
> stay authoritative, but a frozen scalar rate no longer masks real GRN layers:
> every unrecorded unit is costed at the exact lot rate the FIFO replay
> consumes.  A later/addendum GRN therefore updates both P&L and GRN in real
> time instead of leaving the delivery on its posting-time placeholder rate.

Date: 2026-10-25
Scope: costing correction inside the existing FBM Flask application. No schema
change, no data rewrite, no costing-policy replacement.

---

## 1. Reported problem

The Profit & Loss report charged a delivery from **one** GRN purchase rate even
when the delivered quantity was physically covered by **several** GRNs with
different rates. Required case:

| Purchase | Qty | Rate | Value |
|---|---|---|---|
| Old GRN | 10 bags | Rs. 1,000 | Rs. 10,000 |
| New GRN | 30 bags | Rs. 1,200 | Rs. 36,000 |
| **Delivered** | **40 bags** | weighted average **Rs. 1,150** | **Rs. 46,000** |

The report had to stop producing Rs. 48,000 (40 × 1,200) or Rs. 40,000
(40 × 1,000) and show, on every delivery line, which GRN/lot supplied which
quantity at which purchase rate.

---

## 2. What the application actually stores (investigation result)

| Layer | Table / field | Meaning |
|---|---|---|
| Purchase lot | `GRNItem.qty`, `GRNItem.price_at_time` | One GRN item = one FIFO lot with its own purchase rate |
| Lot → delivery link | `GRNAllocation.sale_item_id`, `grn_item_id`, `qty`, `cost_rate` | Recorded FIFO consumption written at posting |
| Frozen line cost | `DirectSaleItem.grn_item_id`, `cost_rate_at_sale` | The rate posting recorded on that sale line |
| Physical issue | `Entry` (`type='OUT'`), `DirectSaleItem`, `BookingAllocation` | Quantity that left the yard |
| Delivery rent | `DirectSale.delivery_rent_cost`, driver allocations | Direct delivery cost, separate from material |

So the schema **does** preserve per-lot costing. Posting
(`_expand_chargeable_items_fifo` → `_apply_grn_allocations_for_sale`) already
splits a priced sale line into one line per FIFO lot when stock is on hand, and
the report already reads those allocations first. The defect was in the
**reconstruction path used for every quantity that posting could not allocate**
(the delivery was posted before the receipts were entered, was imported, or ran
past the lots that existed at posting time):

* posting froze **one** material-wide rate on the unallocated remainder
  (`_cost_rate_for_material`: newest GRN on/before the sale, else nearest later
  GRN, else `Material.unit_price`), and
* the P&L replay (`_build_fifo_cost_plan` → `plan_event`) then charged that
  single scalar to the whole remainder and discarded the lot layers it had just
  consumed, so the cost no longer matched the lots the same report showed as
  consumed. `cost_sources` for those rows had no quantity/amount at all.

That is exactly the observed symptom: 40 bags delivered → 40 × one rate.

---

## 3. Costing method (unchanged)

**FIFO, one lot per GRN item, oldest available lot first.** The fix does not
introduce weighted average, LIFO or any new policy:

1. **Recorded allocations are authoritative and never re-costed**
   (`GRNAllocation.qty × cost_rate`).
2. **A frozen sale-line cost stays authoritative when purchase history existed**
   when the line was posted — a later/addendum GRN cannot silently re-cost a
   completed delivery.
3. **Everything else is reconstructed from the lot queue**: dated lots on/before
   the supply date first, then the next real lots in FIFO order (negative
   stock). Each consumed lot contributes `qty × that lot's own rate`.
4. **Only quantities no lot can cover** fall back to the labelled single rate
   (latest GRN on/before, else nearest later GRN, else Material master). Those
   rows are `estimated`, never `actual`.
5. Services/rent are never stock; GRN loading/freight/tax/adjustment continue to
   affect the supplier payable only (already disclosed in the report's policy
   block) — they are not silently folded into lot rates.

---

## 4. Files changed

| File | Change |
|---|---|
| `app/services/profit_loss.py` | `_lot_available()` helper (date rule shared by both replays); `plan_event` reconstruction rewritten: allocate recorded lots, then dated FIFO layers at their own rates, then `estimate_from_next_lots()` which splits the shortage over the next real lots (each at its own purchase rate) and consumes them so a later delivery cannot reuse them; `add_frozen_source()`/`single_rate_sources()` record `qty`/`rate`/`amount` so evidence reconciles to the row cost; `_merge_sources()` (one printed line per lot/rate), `_reversed_sources()` (returns carry the reversed lots). Master-price placeholder detection keeps a pure material-master rate labelled `estimated`. |
| `app/services/grn_consumption.py` | `_consume_forward()` + `_lot_history_exists()` mirror the new P&L rule, so the GRN consumption indicator's remaining quantity agrees with the costed quantity for negative-stock dispatches. |
| `app/services/profit_loss_exports.py` | Excel `Transactions` sheet gains **Source GRNs** and **GRN Cost Layers** (`REF: qty × rate = amount`), copied from the same report rows. |
| `templates/partials/profit_loss_results.html` | Each cost cell lists its source GRNs / qty × rate. |
| `templates/profit_report_pdf.html` | PDF transaction detail lists each source lot, quantity and rate. |
| `tests/test_profit_loss_multi_grn_costing.py` | New regression suite (9 tests, see §6). |
| `tests/test_profit_loss_fifo.py` | Pinned the corrected cross-period FIFO total (220 units delivered against 300 purchased now cost exactly `100×70 + 100×90 + 20×200`) and the reconciled cost evidence. |

No database migration, backfill, GRN edit or allocation rewrite was performed.

---

## 5. Behavioural rules that fall out of the fix

* **Multiple GRNs are summed per delivery line.** A delivery that ran past the
  first lot shows two evidence lines, e.g. `GRN-OLD 10 × 1,000 = 10,000;
  GRN-NEW 30 × 1,200 = 36,000`, and `material_cost = 46,000`.
* **No duplicate consumption.** Forward-drawn lots are deducted, so the same
  purchased units cannot be costed twice; the row total is exactly the FIFO
  value of the units dispatched.
* **Frozen costs stay frozen.** Once history existed at posting, the recorded
  rate is kept (`test_addendum_...`) and a later purchase cannot rewrite it.
* **Selling prices are untouched** — a purchase rate never changes a sale or
  booking rate (`test_new_purchase_rate_...`).
* **Returns reverse the original layers** at the original unit cost and print the
  source GRN(s) they came from.
* **Every evidence line reconciles**: `Σ source.amount = material_cost` and
  `Σ source.qty = row qty` (legacy mismatch count 1,140 → 0).
* Dashboard, breakdown, PDF and Excel all read the same report rows, so they
  cannot disagree.

---

## 6. Tests

`tests/test_profit_loss_multi_grn_costing.py` (isolated DB per test):

| Test | Pins |
|---|---|
| `test_required_10_plus_30_bag_delivery_costs_rs_46000` | exact 10 @ 1,000 + 30 @ 1,200 → rows 10,000/36,000, COGS 46,000 (≠48,000, ≠40,000), weighted avg 1,150, allocations 10/30 at each rate, both source GRNs shown, all lots fully consumed, remaining stock 0 |
| `test_detail_dashboard_and_excel_exports_reconcile` | repeated report generation is identical; dashboard renders the bill; transaction detail shows both GRNs and `10.00 × Rs. 1,000.00` / `30.00 × Rs. 1,200.00`; Excel `Material Cost` total 46,000, `Net Profit` 6,000, summary COGS 46,000, layer columns contain both GRNs |
| `test_delivery_posted_before_receipts_is_valued_from_both_lots` | delivery posted before the receipts: master-price placeholder while no GRN exists, then 10 × 1,000 + 30 × 1,200 = 46,000 after the receipts, `estimated`, no invented `GRNAllocation` |
| `test_partial_deliveries_split_the_lots_correctly` | 15 + 25 bags → 10 @ 1,000 + 5 @ 1,200, then 25 @ 1,200; total 46,000 |
| `test_return_reverses_the_original_cost_layers` | return of 10 reverses the oldest lot at −10,000 with the source GRN; COGS 36,000 |
| `test_cancelled_delivery_releases_its_lots` | deletion restores both lots to full, COGS 0, then re-delivery costs 46,000 again (no double count) |
| `test_new_purchase_rate_does_not_rewrite_price_or_completed_delivery` | a later, more expensive GRN leaves the delivery at 46,000 and the selling price at 1,300 |
| `test_addendum_grn_does_not_re_cost_a_delivery_that_had_history` | a backdated addendum GRN cannot rewrite a delivery whose rate was frozen with history |
| `test_booking_delivery_supplied_from_two_lots` | booking fulfilment split 10/30 costs 10,000 + 36,000 |

Existing suites re-run green: `test_profit_loss_control_center.py` (14),
`test_profit_loss_fifo.py` (3), `test_profit_loss_automatic_filters.py` (12),
`test_grn_consumption_indicator.py` (7), `test_grn_screen_layout.py` (6).
Full suite: see §8.

---

## 7. Historical data that cannot be reconstructed exactly

Read-only audit of `LEGACY DATA/AMS_SMOOTH_IMPORT.amsdb` (2026, 3,364 sales,
6,274 sale items, 152 GRN lots, 219 allocations):

* **219 allocations exist and every one of them is single-lot** (0 lines with
  2+ allocations), so no legacy delivery can be shown as a *recorded* multi-lot
  consumption. Their cost is used exactly as recorded and did not change.
* **6,055 of 6,274 sale items have no allocation and no frozen lot link.** Their
  cost is reconstructed by the FIFO replay and labelled `estimated`.
* **1,289 material rows still have no resolvable cost** (e.g. `WIRE`, `6MM STEEL`
  lots without a `price_at_time`, materials with no purchase at all). They stay
  visible and excluded from net profit — the report does not guess a rate.
* **79 material rows (4,808.7 units) consumed a GRN lot that has no purchase
  rate** (`GRNItem.price_at_time = 0`, all `missing`). They are disclosed as
  `Dated GRN lot without a purchase rate` instead of silently costing Rs. 0.
  `GRNItem` stores no second price or line total, and the only other money on
  the receipt is the GRN header (loading, freight, other, tax, adjustment,
  discount). Deriving a rate from that header would fold landed costs into the
  lot rate — a different costing policy — so the fix deliberately leaves these
  rows unresolved rather than guess.
* Materials whose sale name only *similar* to a GRN name (e.g.
  `ISM 12MM STEEL` vs `12MM STEEL`) keep a **single labelled estimate**: taking
  another material's lots would move stock between items, so it is deliberately
  not done.
* Legacy `Entry`/import rows that lost the GRN link cannot be re-linked without
  guessing; the replay derives their cost from the dated lot queue instead, and
  marks it estimated.

### Measured impact on the legacy database (calendar 2026)

| Metric | Before | After |
|---|---|---|
| Revenue | 110,556,132.82 | 110,556,132.82 (unchanged) |
| COGS | 99,172,434.85 | 101,977,664.85 (+2,804,313.90) |
| Recorded ("actual") material cost | 4,184,591.40 | 4,184,591.40 (unchanged) |
| Estimated material cost | 93,550,314.70 | 96,355,544.70 |
| Net result | 2,088,752.49 | −716,477.51 |
| Material rows incl. returns | 3,848 (224 actual / 2,335 estimated / 1,289 unresolved) | unchanged split |
| Rows whose evidence amount ≠ row cost | 1,140 | **0** |
| Rows whose evidence quantity ≠ row qty | 1,140 | **0** (79 unresolved rows carry no evidence at all) |
| Goods rows showing 2+ source GRNs | — | 95 |

All 1,482 changed rows remain **labelled estimates** (1,473 estimated→estimated,
9 unresolved→estimated). The movement is the correction itself: shortages that
were valued at one GRN rate are now split across the real lots that covered
them, and the lots those dispatches consumed are no longer available to later
unallocated rows. No recorded allocation, GRN, stock quantity or selling price
was modified.

---

## 8. Verification

```
.venv/bin/python -m pytest tests/test_profit_loss_multi_grn_costing.py -q
    → 9 passed
.venv/bin/python -m pytest tests/test_profit_loss_control_center.py \
      tests/test_profit_loss_fifo.py tests/test_profit_loss_automatic_filters.py \
      tests/test_grn_consumption_indicator.py tests/test_grn_screen_layout.py -q
    → 51 passed
.venv/bin/python -m pytest tests -q
    → 367 passed, 6 skipped, 11 subtests passed in 845 s
```

---

## 9. Remaining, explicitly disclosed limits

1. A delivery posted while stock was on hand keeps the rate frozen at posting;
   a *backdated* addendum GRN does not retroactively re-cost it (requirement:
   later receipts must not silently change completed deliveries).
2. GRN landed costs (loading, freight, tax, adjustment) still sit in the
   supplier payable, not in the lot rate — an existing, documented policy.
3. Cost for the 1,289 unresolved legacy rows (including the 79 zero-rate GRN
   lots above) cannot be established from the stored data and remains excluded
   from net profit.
4. Similar-but-not-identical legacy material names are not merged for costing.

### Why posting itself was not changed

Posting already does the right thing while stock is on hand:
`_expand_chargeable_items_fifo` splits a priced line into one slice per open
lot, reserves each lot inside the document, and `_apply_grn_allocations_for_sale`
writes a `GRNAllocation` per slice; only the uncovered remainder is frozen at a
single rate (`_allocate_grn_fifo_plan` emits `grn_item_id=None` for it).

For a delivery posted while stock was negative the covering lots **do not exist
at posting time**, so no allocation row can be written without inventing a
reference into the future. Options were to (a) rewrite history when the GRNs
later arrive, (b) write speculative lots/allocations, or (c) reconstruct the
cost read-only in the report. (a) and (b) would mutate live stock and GRN
history, so (c) was chosen; the report labels those rows `estimated` and shows
the exact lots it used. Posting behaviour is therefore unchanged, and the
frozen rate remains the audit record of what the operator saw at posting time.
