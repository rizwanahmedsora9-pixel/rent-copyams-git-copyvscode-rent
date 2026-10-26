# Sequential audit — PRs, data 0, and the full FIFO / GRN / P&L smoke cycle

Date: 2026-10-26 · Branch `arena/6bab9cb4-rent-copyams-git-copyvscode-re`
(base: merge of PR #12, `374b12c`)

This is the record of a full audit of the repository: every merged PR was read,
the application was taken to **data 0**, and one complete business cycle was
then entered **in the order a yard actually works**, verifying GRN consumption
and Profit & Loss after every single step.

Everything below is reproducible:

```bash
python tools/audit_sequence_smoke.py              # fresh temp DB, 220 checks
python tools/audit_sequence_smoke.py --keep       # keep the DB to inspect
pytest tests/test_sequence_smoke.py               # same cycle inside CI
```

Result of the final run: **220 / 220 checks passed** (exit 0).
Full test suite on this branch: **378 passed, 6 skipped, 0 failed**.

---

## 1. PR review (all 12 merged PRs read)

| PR | Title | What it establishes | Still true today |
|---|---|---|---|
| #1 | Services & Charges master + one-click rent conversion | `Service` rows bill like materials but never hold stock (`total` is always 0, no GRN, no stock reports) | ✅ |
| #2 | Profit section in plain language | The report is readable by a non-accountant | ✅ |
| #3 | Profit & Loss Control Center | One server-side projection, unresolved cost exposed instead of zero | ✅ |
| #4 | Full-app audit + data cleanup + smoke cycle | Wipe keeps identity/settings/audit, wipes business tables | ⚠️ **gap found — Services master survived the wipe** (fixed, see §3) |
| #5 | FIFO valuation in P&L | Same-day GRNs visible, no "newest purchase" fallback, one line ≠ two drains | ✅ |
| #6 | Clarify profit reports & FIFO tracing | Lot layers visible on transaction detail | ✅ |
| #7 | GRN consumption indicator + P&L auto filters | Per-lot consumed/remaining meter | ✅ |
| #8 | GRN layout pass | Meters readable, one action strip | ✅ |
| #9 | Compact GRN rows with expandable per-lot panel | Server-rendered expandable panel, no JS maths | ✅ |
| #10 | Cost P&L from every GRN lot a delivery consumed | `1,000 × 10 + 1,200 × 30`, never one blended rate | ✅ |
| #11 | No alternate material for services in Direct Sales | Service rows never get an alternate | ✅ |
| #12 | Unify sale, GRN and P&L FIFO with live reconciliation | One authoritative replay feeds P&L, GRN page, `/api/grn_consumption`, sale posting | ✅ |

No PR needed reverting. Two defects were still open **after** #12 and are fixed
here (§3). Both were found only because this audit replayed the whole lifecycle
in sequence and compared every screen to an independent FIFO replay.

---

## 2. The dataset that was entered, in order

Nothing was seeded through the ORM: every line below went through the real HTTP
route with a real session and CSRF token, exactly as a user would.

**Phase 0 — data 0.** Junk seeded across the modules, then
`POST /delete_selected_data` with *Check all* + hard-delete confirmation.
Proved: every business table at 0 rows, identity survives, and the empty-state
pages (`/`, `/materials`, `/clients`, `/suppliers`, `/grn`, `/direct_sales`,
`/profit_reports`, `/services`) still render.

**Phase 1 — material category.** `Cement`, `Sand & Gravel`. A duplicate is
refused.

**Phase 2 — materials.** `DG Cement` (Bags → Cement), `Pioneer Cement`
(Bags → Cement), `Chenab Sand` (CFT → Sand & Gravel). Category, unit and
0 stock stored per material; duplicate refused.

**Phase 3 — client.** `Bilal Construction` / `SEQ-CL-1`, Credit Customer.

**Phase 4 — suppliers.** `Ravi Cement Supplier`, `Kohat Traders`; duplicate
refused.

**Phase 5 — rent service.** `CEMENT RENT` (Trips) — exists as a Service, does
**not** exist as a material, carries no stock.

**Phase 6 — six GRNs, same cement at different price groups, two suppliers:**

| # | Supplier | Material | Qty | Rate | Date / time |
|---|---|---|---|---|---|
| G1 | Ravi | DG Cement | 100 | 1,200 | 2026-09-01 09:00 |
| G2 | Ravi | DG Cement | 50 | 1,350 | 2026-09-03 10:30 |
| G3 | Kohat | DG Cement **+** Chenab Sand | 80 @ 1,400 + 200 @ 55 | — | 2026-09-05 11:15 |
| G4 | Ravi | Pioneer Cement | 60 | 1,600 | 2026-09-07 09:45 |
| G5 | Kohat | DG Cement | 40 | 1,350 | 2026-09-09 14:20 |
| G6 | Ravi | DG Cement | 25 | 1,500 | 2026-09-11 16:05 |

Each GRN was checked before the next was posted: hand-typed date **and** time
stored verbatim, lots stored exactly as typed, `Material.total` equal to
purchased quantity, no lot consumed by a GRN alone, supplier ledger equal to
what that supplier delivered.

**Phase 7 — nine sales:**

| # | Date | Lines | Expected FIFO cost |
|---|---|---|---|
| S1 | 09-12 | DG 30 @ 1,500 | 30 × 1,200 |
| S2 | 09-13 | DG 80 @ 1,550 | 70 × 1,200 + 10 × 1,350 |
| S3 | 09-14 | DG 25 + DG 30 @ 1,600 (two lines, one material) | 40 × 1,350 + 15 × 1,400 |
| S4 | 09-15 | Sand 50 @ 90 | 50 × 55 |
| S5 | 09-16 | DG 5 @ 1,600 **+ CEMENT RENT 2 @ 2,000** | 5 × 1,400, service = no lot |
| S6 | 09-17 | Pioneer 20 @ 1,900 | 20 × 1,600 |
| S7 | 09-18 | DG 60 @ 1,700 | 60 × 1,400 |
| S8 | 09-19 | DG 65 @ 1,750 | 40 × 1,350 + 25 × 1,500 |
| S9 | 09-20 | DG 10 @ 1,800 (over-sold, override ticked) | none — no lot left |

---

## 3. What the audit found, and what was fixed

### 3.1 "Data 0" was not data 0 — the Services master survived the wipe

`POST /delete_selected_data` deleted materials, categories, clients, suppliers,
GRNs, sales… but never `service`. After a *Check all* hard wipe the Services
master still held its rows: stale rent charges kept appearing in every dropdown
of a supposedly empty company, and any subsequent "add service" collided with
the ghost row.

Fixed in:

* `app/blueprints/misc/_wipe_delete_selected_data.py` — `Service` is deleted in
  the full-wipe path (before the category cascade) and is now a selectable
  target (`services`) on its own.
* `app/services/wipe.py` — `services` added to the wipe registry so the preview
  and row counts include it.
* `templates/settings.html` — **Rent & Service Charges** checkbox, so *Check
  all* covers it.
* `tools/audit_full_cycle.py` — target list updated.

Pinned by `tests/test_sequence_smoke.py::test_full_wipe_clears_the_services_master`
and `::test_services_wipe_target_can_run_on_its_own`.

### 3.2 A rent service we sell was reported as "unknown cost" in P&L

Sale S5 bills 5 bags of cement **plus** 2 rent trips @ 2,000. The cement line was
costed correctly from lot G3 (5 × 1,400). The rent line was classified as a
*delivery service* and then required a *recorded delivery cost*; a fixed rent
charge has none, so the row became `cost_status = "missing"` and was **excluded
from net profit** — 4,000 of income silently disappeared from P&L, and the page
raised "some sales have no recorded purchase cost".

That rule is right for a delivery charge (the truck has a cost, and guessing
zero would overstate profit). It is wrong for a **registered Service**: a
service is a charge we sell, not goods, and its cost of goods is zero *by
definition*. The distinction now comes from the data, not from a heuristic:

* `app/services/profit_loss.py` — `_registered_service_keys()` reads the
  Services master once per projection (`projection.is_registered_service`);
  every service component is tagged `registered_service`.
* A registered service row is recognised (`cost_status = not_applicable`,
  `include_in_net = True`, zero cost, no cost sources, explicit basis text).
* A rent-like/delivery label with **no** Service master row keeps the previous
  conservative behaviour (excluded, `missing`), so legacy data cannot silently
  gain a free pass. A registered service **with** a real recorded delivery cost
  is still netted against that cost — unchanged.

Pinned by three tests: recognised-with-zero-cost, unregistered-stays-conservative,
registered-with-recorded-cost-still-netted.

### 3.3 Observations that are correct by design (documented, not changed)

* **S9 over-sold 10 bags.** Posting consumed every real lot and froze a rate;
  P&L then disclosed 10 × 1,500 = 15,000 as a `FrozenRate` cost source with the
  basis *"…quantity beyond every GRN lot"*. Lot-backed cost for that bill is
*zero*, so no real lot is invented. Total COGS = real lots 425,750 + disclosed
15,000 = 440,750.
* **`Material.total` went to −10** for DG Cement. The lot ledger can never go
  negative (declared remaining never below 0); the master counter follows the
  physical ± of every posting, so it carries the over-sold units. The two views
  reconcile exactly (lots in hand 0 − over-sold 10 = −10).
* **Manual bill numbers are stored normalised** (`MB NO.<typed>`), so lookups
  match on `LIKE %typed%`, as the UI does.
* **Material ledger (`/client_ledger/<id>`) shows quantity only**; money sits on
  the financial ledger (`/ledger/<id>`), which correctly shows the full
  545,250 receivable.

---

## 4. How the verification works (why the numbers can be trusted)

`tools/audit_sequence_smoke.py` never reuses the application's costing code to
check the application's costing. `FifoOracle` opens the SQLite file with raw
SQL, rebuilds every lot, and replays all material issues independently in
Python; it also re-adds the billed amounts from `direct_sale_item`. After **every
single GRN and every single sale** it asserts:

* the lot queue is consumed **oldest first, at that lot's own purchase rate**
  (`G5` in September is exhausted before the newer identical-rate `G6` lot);
* the recorded `GRNAllocation` rows equal the oracle's layers
  (`1,350 × 40 + 1,400 × 15`, not a blended rate);
* `/api/grn_consumption` and the **rendered `/grn` page** (`data-consumed`
  attributes parsed out of the HTML) show the same per-lot consumed / remaining
  as the oracle — this is the "real-time effect in GRN consumption" check;
* P&L lot-backed cost for every bill equals the oracle's layers, price band by
  price band, and P&L revenue equals the billed amount;
* the transaction detail page lists the real lot ids (`Lot #…`) it costed from;
* no lot goes below zero, and no cost source ever points at a service name.

Then, across the whole run:

* 295 DG bags bought = 295 sold → lots in hand 0, one 10-bag shortfall disclosed;
* Sand 200 − 50 = 150 CFT left; Pioneer 60 − 20 = 40 bags left;
* `Material.total` equals lots-in-hand minus over-sold units for every material;
* the rent service never became stock and never appears in a GRN line;
* the material filter selects rows only — DG-filtered cost (391,000 lot cost)
  equals the DG FIFO layers, and no other material leaks in;
* **P&L is a pure report**: re-reading it twice produces an identical ledger, and
  its total equals GRN + sales evidence exactly
  (revenue 545,250 · COGS 440,750 · profit 100,500 · formula check 0);
* PDF and Excel exports download (`%PDF` / `PK`).

---

## 5. Test evidence

| Check | Result |
|---|---|
| `python tools/audit_sequence_smoke.py` | **220 / 220 passed**, exit 0 |
| `pytest tests/test_sequence_smoke.py` | **6 passed** (cycle + 5 defect regressions) |
| `pytest` targeted (profit ×4, simple summary, wipe, services master) | **70 passed** |
| Full `pytest` suite | **378 passed, 6 skipped, 11 subtests, 0 failed** (17m25s) |
| `python tools/audit_full_cycle.py` (pre-existing cycle) | **76 / 76 passed** |

---

## 6. Files changed

| File | Change |
|---|---|
| `tools/audit_sequence_smoke.py` | **new** — the sequential audit (data 0 → … → P&L) with the independent FIFO oracle |
| `tests/test_sequence_smoke.py` | **new** — the cycle in CI + 5 regressions for the two fixes |
| `app/services/profit_loss.py` | registered services are income with zero COGS (`not_applicable`), unregistered rent labels unchanged |
| `app/blueprints/misc/_wipe_delete_selected_data.py` | Services master is wiped (full wipe + standalone `services` target) |
| `app/services/wipe.py` | `services` in the wipe registry/preview |
| `templates/settings.html` | **Rent & Service Charges** wipe checkbox |
| `tools/audit_full_cycle.py` | `services` in the target list |
