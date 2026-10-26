# Fixing plan — what to do next, in priority order

Written after the sequential audit (PR #13). §1 lists what is **already fixed
and merged on this branch**; §2 is the remaining work, ordered by risk.

Rule used for ordering: a defect earns a higher rank the more it **silently
changes a money figure**. Anything that can make profit, stock or a balance
wrong without anyone noticing comes before things that merely look bad or are
slow.

---

## 1. Already fixed on this branch (PR #13)

| # | Defect | Impact before the fix | Where |
|---|---|---|---|
| 1 | "Data 0" never cleared the **Services master** | Stale rent charges survived a Check-all wipe and polluted every dropdown | wipe route, registry, Settings checkbox |
| 2 | A service we sell was **"unknown cost"** in P&L | 4,000 of real rent income was excluded from net profit + false "missing cost" alarm | `profit_loss.py` |
| 3 | **`'rent' in name`** substring rule | `TRANSPARENT SHEET`, `CURRENT STOCK`, `PARENT BOARD`, `DIFFERENT SAND` … were treated as services: **no stock, no GRN consumption, no cost of goods** | `lookups.py` + 3 call sites now share one word-boundary rule |
| 4 | Over-sold bills were **silent** | Once a frozen rate existed the row looked "actual", so selling stock you never bought produced no visible warning | new `short_stock_cost` warning + red block on the P&L page |

Each one has a regression test and is re-checked by
`tools/audit_sequence_smoke.py` (254 assertions) on every run.

---

## 2. Next fixes, highest risk first

### P0 — a physical issue can use a lot bought *after* it was issued

`consumed_qty` (GRN screen) only counts lots available on/before the issue date,
but the replay still **removes quantity from later lots**. Open
`/api/grn_consumption` and compare a lot's `consumed_qty` with
`received − remaining`: for a pre-GRN delivery they disagree.

* **Why it matters:** the manager sees a lot that looks untouched while P&L
  already costed goods against it — the exact "GRN and P&L disagree" complaint
  PR #12 was meant to end. It is invisible today only because the seeded data
  always buys before it sells.
* **Fix:** report the later-lot draw as its own layer (`consumed_forward_qty`,
  badge *"covered by a later GRN"*, and remaining reduced accordingly), and make
  `received − consumed − remaining = 0` an invariant asserted in
  `tools/consistency_report.py`.
* **Size:** ~1 day. **Risk:** medium (touches the shared replay) — but the audit
  tool already guards the invariant from four directions.

### P0 — `Material.total` and the lot ledger can disagree on legacy data

`_rebuild_material_totals()` is the only writer of `Material.total` and it
aggregates the **`Entry`** table, while P&L and the GRN screen replay **GRN
lots**. Any dataset where `Entry` and `GRN` were imported separately (the legacy
snapshots are exactly that) can show stock in one place and cost in another.

* **Fix:** add a `consistency_report` check that compares, per material,
  `Entry` net vs FIFO lot remaining, and a dated reconciliation note on the
  stock summary when they differ. Do not silently "correct" either side.
* **Size:** ½ day. **Risk:** low (read-only).

### P1 — void / edit after a lot is consumed

Editing a GRN rate after sales consumed its lot silently rewrites historical
COGS (the frozen `cost_rate_at_sale` and `GRNAllocation.cost_rate` keep the old
rate, the report prefers the allocation, so the two views can drift). Sales
edits that reduce quantity leave orphan allocation rows.

* **Fix:** a single `reconcile_allocations()` service — after any GRN/sale edit,
  re-run the replay and either re-price or flag the affected documents; expose
  "3 bills were affected by this edit" in the flash message.
* **Size:** 2 days. **Risk:** high — needs a careful test matrix.

### P1 — very large `.amsdb` snapshots

Export is in memory. A multi-year DB will exhaust RAM on PythonAnywhere.

* **Fix:** stream the export (`sqlite3` backup API + `send_file` of a temp file).
* **Size:** ½ day. **Risk:** low.

### P2 — the rest of the quality pass

| Item | Why | Size |
|---|---|---|
| Ledger/payment unit tests are thin | money math is only covered through pages | 2 days |
| Page-set smoke inside pytest (not only the external tool) | catches BuildErrors on pages no test renders | ½ day |
| PDF: WeasyPrint is currently falling back to ReportLab on this box | verify the deployed install has pango/cairo | ¼ day |
| `tools/consistency_report.py` → 4 new checks (above) | makes drift visible before a customer finds it | ½ day |
| Frontend DOM tests run with `node` + jsdom in CI | `tests/frontend/*` are never executed today | ½ day |
| README: add this audit + the fixing plan to the "what is solid" table | keep the story current | ¼ day |

---

## 3. What I would do in the next session

1. **P0-1 and P0-2 together** (½–1½ days): they are the same question — *does the
   screen agree with the costed number?* Fix the forward-lot reporting, add the
   reconciliation check, extend `tools/audit_sequence_smoke.py` with a
   pre-GRN-delivery scenario so the fix is proven end to end.
2. **Re-run the whole gate**: sequence audit (must stay 254/254), full pytest
   (378+), `tools/audit_full_cycle.py` (76/76), and a fresh-data sweep of every
   page.
3. **Then P1 edits** with the allocation reconciler, because that is the next
   thing that can silently move money.

## 4. Standing gate (keep green on every change)

```bash
python tools/audit_sequence_smoke.py   # 254/254, exit 0 — FIFO == GRN == P&L
python tools/audit_full_cycle.py       # 76/76 — wipe + one business cycle
pytest                                # 388 tests
python tools/consistency_report.py     # raw-SQL integrity, 9 → 13 checks after P0-2
```
