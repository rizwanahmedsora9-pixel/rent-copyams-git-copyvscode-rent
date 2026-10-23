# SERVICES PLAN — rent items become a `Service` master (Option B)

> Status: **PLAN ONLY, COMPLETE.** Every problem identified during design is
> resolved below. No `.py`, `.html`, model, migration or template has been
> written or modified.
> Branch for the eventual work: `arena/ae223204-rent-copyams-git-copyvscode-re`.
> Supersedes the `Material.is_stock_tracked` flag approach in `rent_solution.md` §5.
>
> Evidence base: live code in this repo + **read-only** SQL over
> `LEGACY DATA/AMS_SMOOTH_IMPORT.amsdb` (production snapshot, 2026-10-20,
> `integrity ok`). All counts in this document are measured, not estimated.

---

## 1. Decision (owner, this session)

> Take the rent items **out of inventory** and make them a **Service** master.
>
> - New sidebar page where services are managed.
> - No fixed price — the rate is typed per line, exactly as it already is today.
> - No GRN, no stock, ever.
> - Services remain in the **same item picker** in Bookings and Direct Sales.
> - Bill, print/PDF, client ledger and financial ledger stay **exactly** as today.
> - Implemented with **no discrepancy**: §12 defines what must not change and
>   how that is proven.

### Scope

- **Become services:** `RENT-CEMENT`, `RENT-GENERAL`, `RENT-STEEL`, `RENT STEEL`.
- **Not now:** `LABOUR`, `CHAUGATH-LABOUR`, `MISC`, `BACK FILLING`.
- **Rent is charged per unit** (per bag / per kg) — `qty × rate` stays, so the
  booking pool stays exact. Bill-level charge lines are not needed.
- **Cutover is a separate later step** (§11, S5).
- **Collisions are blocked**, not warned (§8).

---

## 2. Why Option B is affordable here

The usual objection to a new master — migrate every line, teach ~40 files to read
two worlds — does not apply, because **nothing is FK-keyed**:

```
BookingItem.material_name        → String(100)   # models/sales.py:52
DirectSaleItem.product_name      → String(100)   # models/sales.py:234
Entry.material                   → String(100)   # models/stock.py:10
MaterialReturnItem.material_name → String(100)   # models/sales.py:279
```

The only real FK on a sale line is `DirectSaleItem.grn_item_id`
(`models/sales.py:237`). Everything else resolves by **free-text name**.

1. **No item-row migration.** The 1,607 historical rent sale lines keep
   `product_name = 'RENT-CEMENT'` and simply resolve against `Service` instead of
   `Material` after cutover.
2. **No dual-FK plumbing.** We do not add `service_id` to `DirectSaleItem`; we
   resolve the name, as today.
3. **Most stock code is already null-safe.** The 42 sites doing
   `Material.query.filter_by(name=…).first()` are written as
   `if mat: mat.total += qty`. Once the name is not a Material they skip it with
   no edit.

Real cost: **1 model + 1 page + 10 guarded sites**.

---

## 3. Data inventory (measured from the snapshot)

### The four rows to move

| id | code | name | unit | cat | `total` | `unit_price` |
|---|---|---|---|---|---|---|
| 10 | `FBMREN-000002` | `RENT-CEMENT` | Pcs | 5 (RENT) | −32,076.0 | 0 |
| 9 | `FBMREN-000001` | `RENT-GENERAL` | Pcs | 5 (RENT) | −3,438.0 | 0 |
| 11 | `FBMREN-000003` | `RENT-STEEL` | Pcs | 5 (RENT) | −84,090.6 | 0 |
| 43 | `tmpm-00004` | `RENT STEEL` | Bags | 11 (General) | −22,707.9 | **1.7857** |

- **Units differ** (`Pcs` ×3, `Bags` ×1) → `Service.unit` is required.
- **`RENT STEEL` sits in category `General`, not `RENT`** — proof that a category
  rule can never identify these; only an explicit master can.
- **`RENT STEEL` has `unit_price = 1.7857`**, which currently feeds
  `material_unit_cost_index`. See §15, accepted discrepancy #2.

### Rows referencing these names

| Table | Rows | Detail |
|---|---|---|
| `entry` (not void) | **1,608** | CEMENT 1,198 · GENERAL 242 · STEEL 115 · `RENT STEEL` 53 |
| `direct_sale_item` | **1,607** | CEMENT 1,196 · STEEL 117 · GENERAL 241 · `RENT STEEL` 53 |
| `booking_item` | **301** | CEMENT 197 (31,952 u) · STEEL 68 (117,733.8 u) · GENERAL 19 (1,820 u) · `RENT STEEL` 17 (24,712 u) |
| `material_return_item` | **0** | *(see §10)* |
| `grn_item` | **0** | no rent GRN has ever existed |

`grn_item = 0` confirms the pressure for a "rent GRN" is purely an artefact of
rent being stock-tracked. There is nothing historical to clean up.

---

## 4. The `Service` master

```python
class Service(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(100), nullable=False)
    code       = db.Column(db.String(50), nullable=False)
    unit       = db.Column(db.String(20), default='Pcs')
    # Duck-typing aids — see §6, Problem 6. Never a real price: rates are typed
    # per line. Present only so existing readers of `.unit_price` keep working.
    unit_price = db.Column(db.Float, default=0)
    is_active  = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)

    @property
    def total(self):
        """Services are never stock. Read-only: a write raises loudly instead of
        silently accumulating a number no report should ever show."""
        return 0
```

**No `total` column, no `category_id`, no GRN relationship.**

**Justification for the two duck-typing members** — this is the single highest-
leverage decision in the whole plan. Existing code reads `.unit_price` off a
resolved object at `returns.py:151`, `_dispatch_add_record.py:122` and
`_direct_sales_edit_direct_sale.py:339`. If `Service` lacks the attribute, those
paths raise `AttributeError`. Adding a `unit_price` column defaulting to 0 and a
read-only `total` property means **zero call-site changes** on every read path,
while any *write* to `.total` fails loudly in tests instead of corrupting a
number in silence.

**No migration file is needed for the table.** Per `app/migrations/README.md`,
`db.create_all()` creates ORM tables on boot and `_ensure_model_columns()`
(`app/services/schema.py:109`, called at `:847`) adds missing columns.
Migrations exist only for what those cannot express. A migration is needed only
for the cutover (§11, S5).

---

## 5. The ten guarded sites

### 5.1 Free — no edit required

| Site | Why it is free |
|---|---|
| `_rebuild_material_totals()` `void_rebuild.py:1876` | Aggregates `Entry` by name, then assigns **only** to `Material.query.all()`. No Material row ⇒ no total. The −84k holes simply stop existing. |
| Stock gate `_direct_sales_add_direct_sale.py:376-383` | `mat_obj = Material.query.filter_by(name=mat).first()` inside `if mat_obj:`. `None` ⇒ skipped ⇒ **a rent-only sale posts with `allow_global_negative_stock` OFF**. The headline behaviour change, and it costs nothing. |
| GRN picker `ops/grn.py:322` | `Material.query.filter_by(is_active=True)` — services cannot appear in it. |
| Material ledger `ledgers/other.py:255-312`, stock category lists | Driven by `Material.query` ⇒ services absent. |
| All 42 `Material.query.filter_by(name=…)` stock writers | Null-safe `if mat: … continue`. Void, un-void, return, dispatch, GRN-void, import. |

### 5.2 The filter rule — read this before writing any filter

> 🛑 The `Entry`-grouping queries split into two classes with **opposite**
> requirements. Filtering the wrong one does not merely look wrong — it breaks
> correctness.

| Class | Requirement | Why |
|---|---|---|
| **Pool queries** — booked-vs-delivered availability | Services **must stay** | This *is* the booking pool. Filtering rent out makes booked rent unconsumable: the 31,952-booked / 32,076-delivered drift stops being drift and becomes a hard break. |
| **Stock-presentation queries** — what is in the store | Services **must go** | They were never stock. |

- **Keep:** `drafts.py:221,237,239,254,275,285,288` ·
  `_direct_sales_add_direct_sale.py:408,426` · `other.py:202`
- **Filter:** `stock.py:49-51` · `core.py:33-45` · `_dispatch_tracking.py:174,221`

Assertion #6 in §12.3 is the guard against getting this backwards.

### 5.3 Guarded sites

| # | Site | Why | Change |
|---|---|---|---|
| 1 | `resolve_transaction_material()` `lookups.py:148` | **Most dangerous site in the plan.** Rejects any non-Material name; and resolves ids with an **unqualified** `db.session.get(Material, mid)` (`:170`) on a form-posted value. | New `resolve_transaction_item(service_id, material_id, typed_text)` → `(kind, obj)`. Order: *service_id → material_id → typed text*. `resolve_transaction_material()` becomes a wrapper that **raises** if the result is a service, so no existing caller silently accepts one. See §6 P1. |
| 2 | Picker `app/hooks.py:337` | One cached list feeds every page; templates loop it at `direct_sales.html:673,709`, `bookings.html:420`. | Merge services in, each row tagged. |
| 3 | **Client-side validation** `direct_sales.html:1030-1031, 2519` | `knownMaterialNames` / `knownMaterialCodes` are built from `materials` and **reject any row not in the set** ("Select a valid material from list"). Services absent ⇒ typing `RENT-CEMENT` is blocked before submit. | Add services to both sets. **But** the alternate-material check at `:2533` reuses the same set — so add a separate `knownServiceNames` set and explicitly **reject a service as alternate** there. See §6 P9. |
| 4 | FIFO `_expand_chargeable_items_fifo()` `sales_core.py:259` | Allocates `GRNAllocation` / freezes `cost_rate_at_sale`. A service has no lot. | Skip service names — emit with `cost_rate_at_sale = None`, same as the existing booking-line branch at `:263-267`. |
| 5 | `stock_summary` `stock.py:49-51` | Builds its row set from **`Entry` rows** (`prev_map` ∪ `day_map` ∪ Material names), not from `Material`. We keep writing `Entry` (§9), so rent appears as a phantom uncategorised row. | Exclude service names from `stats`, `totals_by_material`, `day_wise`, grand totals. |
| 6 | GRN **server** guard `ops/grn.py:225` | ⚠️ **Silent trap.** `mat = Material.query.filter_by(name=name).first(); if mat: mat.total += qty` — no `else`. A service name arriving via Excel import creates a `GRNItem` **and an `Entry` IN** with no stock: a fake purchase plus a supplier payment, silently. Absence from the picker is not enough. | Explicit reject on `/grn` add and `/edit_grn` (`misc/pending.py:263,489`); import raises a row error. |
| 7 | Dashboard `core.py:33-45` | Aggregates `Entry` by name, and its own comment says names absent from the master are *"still shown explicitly instead of being silently dropped"*. Rent **survives cutover here** unless filtered. | Filter service names. |
| 8 | Dispatch summary `_dispatch_tracking.py:174,221` | Same `Entry`-by-name pattern. | Filter service names from the grouped summary at `:221`. |
| 9 | Profit cost `_profit_profit_reports.py:84-88` | `material_unit_cost_index` is built from `Material.unit_price`; missing services fall into **"COGS unknown"**. | Treat a service line as **cost 0 and known**, so rent margin = rent charged − driver rent (today's variance block). See §15 #2 for the resulting delta. |
| 10 | Rent analytics `_is_rent_material_name()` `sales_core.py:572` | 6 sites decide "is this rent?" via `'rent' in name` (`sales_core.py:572,799`; `_profit_profit_reports.py:427,452,518,531`). One rename silently disables rent analytics. | Switch to `is_service_name()` (§7). Mirror in JS at `direct_sales.html:1500` (used at `:1887`, `:2028`). |

### 5.4 New

- `Service` model (§4).
- `/services` CRUD + sidebar link under **Directory** (`layout.html:580`, beside
  *Material Brands*), gated on `can_manage_materials`. Add / rename /
  activate-deactivate. **No delete while referenced.** No price, stock or GRN
  controls.
- Rename/merge cascade support (§6 P7).

### 5.5 Deliberately untouched — what "bill and ledger stay normal" means

- Booking add/edit/void; `amount = Σ qty × rate`; `pending = amount − discount −
  paid`; `PendingBill`; FIFO settlement of client payments; cancellation plan.
- Sale: Cash / Credit / Open Khata / Booking Delivery / Mixed; the
  `RENT CHARGED TO CLIENT` / `DELIVERY PERSON RENT (ACTUAL)` / `RENT DIFFERENCE`
  box; `rent_item_revenue`, `delivery_rent_cost`, `rent_variance_loss`.
- Delivery: `SaleDeliveryPerson` → `/delivery_rents` → Pay / waive-off →
  `AccountTransaction('Driver Payment')` → driver ledger FIFO.
- Client financial ledger, including "Delivery Rent Variance (Company Loss)".
- `/daily_transactions` and the client **material** ledger keep showing services —
  they are `Entry`/name-driven, so this is free, and it is the audit trail that
  makes booked-vs-delivered rent checkable.

---

## 6. The ten problems, and how each is solved

### P1 — A Service id could silently post as the wrong material

**The bug.** `lookups.py:170` does `db.session.get(Material, mid)` on a value
posted straight from the form (`material_id[]`, `direct_sales.html:666`). Service
ids will be 1–4. Material ids 1–4 in production are **`12MM STEEL`, `20MM STEEL`,
`6MM STEEL`, `CHAUGATH`**. A Service id of `1` in `material_id[]` posts a rent
charge as 12MM STEEL — right bill total, wrong stock movement, wrong COGS,
**no error anywhere**. It would surface months later as an unexplained variance.

**Solution — three layers.**

1. **Separate channels.** `service_id[]` for services; `material_id[]` stays
   Material-only. Resolution order fixed as *service_id → material_id → typed
   text*.
2. **Keep the text cross-check.** `lookups.py:172-174`
   (`_material_text_matches_record`) already rejects a stale hidden id whose
   typed text does not match. Apply the same check to services.
3. **`resolve_transaction_material()` raises** if the resolved kind is `service`,
   so no un-migrated caller can silently accept one.

**Test:** post a Service id in `material_id[]`; assert rejection, not resolution.

### P2 — Rent would survive cutover in stock-presentation views

`stock_summary`, the dashboard and the dispatch summary all aggregate `Entry` by
name. Sites 5, 7, 8 in §5.3. The dashboard is the subtle one — it *deliberately*
shows names missing from the master.

**Solution:** every site calls the single `is_service_name()` helper. One
implementation, three call sites.

### P3 — Filtering a pool query would break the booking pool

§5.2. **Solution:** the classification table is normative; preflight assertion #6
(§12.3) fails the build if the 1,608 rent `Entry` rows stop appearing in
`daily_transactions` or start appearing in `stock_summary`.

### P4 — Code is a second collision surface

`get_material_by_input` matches `Material.name` **OR** `Material.code`
(`lookups.py:125`), then `ilike` (`:128`). A Service code equal to a Material
code resolves to the wrong record.

**Solution:** the guard in §8 covers **name and code**, both directions.

### P5 — The cutover has an unsafe window

Insert-then-delete leaves a moment where the name is in **both** tables (a fatal
raise); delete-then-insert leaves a moment where it is in **neither** (every rent
line fails with "material not selected"). There is no safe interleaving.

**Solution:** one transaction. `auto_migrate.py` runs inside the app factory
after `db.create_all()` and **before serving**, on its own `sqlite3` connection —
the right place. Two operational notes: `DELETE FROM material` trips the
destructive-statement guard, so the file needs `MIGRATIONS_ALLOW_DESTRUCTIVE=1`
set deliberately and then removed; and two processes booting at once can both
attempt it, so run it in a maintenance window or via the release-deploy runner.

### P6 — Returns: the last open business question — **resolved**

**The data answers it.** In production there are **zero** `material_return_item`
rows for any rent material, and exactly **one** rent movement tagged as a return
(`RENT-GENERAL`, `transaction_category='Return'`, qty 1.0). The 29 booked returns
are all physical goods (DG, KOHAT, steel, cement). Rent returns are a path that
is essentially never used.

**The code answers the semantics.** `returns.py:194-198`: a booked return totals
`qty × rent_rate`; a normal return totals `qty × (unit_rate + rent_rate)`. So
returning a booked *service* refunds `qty ×` the typed rent rate — which is
exactly right, because the service **is** the rent. The existing formula already
does the correct thing.

**Solution:** do **not** build new rent-return semantics. Make the path work by
making `Service` duck-type `Material` (§4: `unit_price` column, read-only `total`).
Then:

| Step | Behaviour | Discrepancy |
|---|---|---|
| `resolve_transaction_material(…)` | resolves the Service | needs site #1 only |
| refund `qty × rate` | identical | none |
| `Entry` IN written | identical → booking pool unchanged | none |
| `Material.query.filter_by(name=…)` → `None` | stock no longer moves | **a fix**, not a break |
| returnable-qty maps (`drafts.py:216,263`) | `Entry`-driven and name-keyed — need no change at all | none |

`returns.py:151` reads `mat_obj.unit_price` — this is precisely why §4 keeps that
attribute. Without it the path raises `AttributeError`.

### P7 — Rename/merge would orphan history

`misc/materials.py:25,170` and `masters/rename_material_label.py:35` cascade
renames into `Entry.material`, `BookingItem.material_name`,
`DirectSaleItem.product_name`, `MaterialReturnItem.material_name`,
`GRNItem.mat_name` and others.

**Solution:** add `Service` to those cascade lists and cascade into the same
tables. A missed cascade orphans history silently.

### P8 — Old bills must stay editable

`resolve_transaction_material(allow_inactive_names=…)` exists so historical
inactive material names still resolve on edit paths.

**Solution:** services accept `allow_inactive_names` identically, so a
deactivated service still resolves when an old bill is edited.

### P9 — The picker would reject services client-side

`direct_sales.html:2519-2522` rejects any row whose product is not in
`knownMaterialNames` / `knownMaterialCodes`, which are built from `materials`
alone (`:1030-1031`). Services missing ⇒ "Select a valid material from list."

**Solution:** add services to both sets. **But** `:2533` reuses the *same* set to
validate the alternate material — so naively merging would *allow* a service as
an alternate, contradicting §5.5. Add a separate `knownServiceNames` set and
explicitly reject a service in the alternate check.

### P10 — The historical P&L delta

`RENT STEEL` carries `unit_price = 1.7857` into `material_unit_cost_index`.
Moving it out changes historical P&L by **≈ Rs 40,550** (22,707.89 delivered
units) and ≈ Rs 44,129 (24,711.99 booked units).

**Solution:** carry `unit_price` across into the `Service` row during cutover, so
the value is **preserved in data**; default the cost index to **exclude** services
(cost 0, known). The delta is disclosed (§15 #2) and is one line to reverse.

---

## 7. Transition rule (mandatory — cutover is a later step)

Support ships before the rows move. During that gap `RENT-CEMENT` is still a
Material, so a Service-only check would make the rent box go dark the day
support ships.

```
is_service_name(x)  =  x resolves to an active-or-historic Service
                       OR ( legacy 'rent' in normalised(x)
                            AND x does not resolve to a Material )
```

- Before cutover: the fallback catches it.
- After cutover: the Service resolves it; the fallback is unreachable.
- Never double-counts: the `AND not a Material` clause closes the overlap.

Verified safe: the four rent names are the **only** rows in the 70-material
master whose name or code contains `rent`, so the fallback matches exactly the
intended set and nothing else.

This rule is what lets site #10 switch all six call sites during the support
pass rather than waiting for cutover.

---

## 8. Identity-collision guard (owner decision: **block**)

Three keys can collide across the two masters:

| Key | Surface | Consequence if unguarded |
|---|---|---|
| **id** | unqualified `db.session.get(Material, mid)` `lookups.py:170` | A Service id resolves to whatever Material shares that integer — silent wrong-material posting |
| **name** | `get_material_by_input` matches `Material.name`, then `ilike` `lookups.py:125,128` | Two masters claim one name; `Entry` rows from both merge into one bucket |
| **code** | the same function also matches `Material.code` `:125,128` | A Service code equal to a Material code resolves to the wrong record |

Enforce in application code (SQLite cannot express a cross-table constraint):

- **Never share the id channel** — separate `service_id[]` (P1).
- On **create** and **rename** of either master: reject if the **name or code**
  (normalised per `_material_norm_key`) exists in the other.
- `resolve_transaction_item()` **raises** on collision, including when typed text
  matches both, rather than guessing.
- Applies to rename/merge machinery (P7).
- Preflight assertions (§12.3) enforce it continuously.

---

## 9. HARD CONSTRAINT — keep writing the `Entry` row

Tempting, and fatal. The service `Entry` OUT row is the **booking pool ledger**,
not just a stock movement. Removing it silently breaks:

| Dependent behaviour | Where |
|---|---|
| booked-rent availability (`booked − delivered + returned`) | `_direct_sales_add_direct_sale.py:205-224` |
| the same pool on the edit-sale path | `_direct_sales_edit_direct_sale.py:204` |
| booking cancellation refund `remaining × rate` | `booking_cancel_plan.py` |
| booked/chargeable auto-split | `_direct_sales_add_direct_sale.py:257-330` |
| booked-material return eligibility | `drafts.py:263+`, `returns.py:176-186` |
| dispatch quantity guard | `_dispatch_add_record.py:60-88` |
| bill/PDF line ↔ movement matching | `_bills_download_invoice.py:145-167` |

**Therefore:** keep writing `Entry`; only the *stock effect* disappears — and it
disappears for free (§5.1). This is why §5.2 exists: the rows stay, so the
stock-presentation views must actively ignore them.

---

## 10. Returns — resolved, no new semantics

Covered in full at **§6 P6**. Summary: the data shows rent returns are
effectively unused (0 `material_return_item` rows; 1 rent return movement ever);
the existing formula already produces the right refund for a service; and
duck-typing `Service` makes the path work with a single change (site #1). No new
rent-return behaviour is built.

---

## 11. Phases

| Phase | Content | Acceptance criteria |
|---|---|---|
| **S0 — master** | `Service` model (§4, incl. duck-typing); `/services` CRUD; sidebar link; collision guard (§8); rename cascade (P7) | Boots clean on empty and populated DBs; a name or code colliding with a Material is refused; **no behaviour change anywhere** |
| **S1 — resolution** | `resolve_transaction_item()` + wrapper (P1); `is_service_name()` with the §7 fallback; `allow_inactive_names` for services (P8) | A service line posts to a booking and a direct sale; goods lines unchanged; a Service id in `material_id[]` is rejected |
| **S2 — picker** | Merge into `hooks.py:337`; tag rows; client-side sets incl. the separate alternate rejection (P9) | Services pickable in Booking and Direct Sale; never selectable as alternate; form submits |
| **S3 — guards** | FIFO skip; `stock_summary`, dashboard, dispatch filters; GRN add/edit reject + import row error | No `GRNAllocation`, `cost_rate_at_sale`, `GRNItem` or `Entry` IN for a service; no service row in any stock view; a rent GRN import fails loudly |
| **S4 — analytics** | `is_service_name()` replaces `_is_rent_material_name()` at 6 Python sites + the JS mirror; service cost known-0 | Rent box **byte-identical** on a fixture set; "COGS unknown" no longer contains rent |
| **S5 — cutover (ops)** | Snapshot. **One transaction**: insert the 4 rows into `service` (preserving `code`, `unit`, `unit_price`) and delete them from `material`. Rebuild. Verify. | §12 dump shows no delta outside §15; no service row in stock views; historical bills/PDFs/ledgers unchanged; `b1` preflight clean |

> **The rows must be deleted from `material`, not deactivated.**
> `_rebuild_material_totals()` iterates `Material.query.all()` with **no**
> `is_active` filter, so a deactivated row would still be assigned a total.

---

## 12. Proving "no discrepancy"

### 12.1 The differential golden test

Unit tests on hand-built fixtures cannot prove nothing changed across 39,269
rows. Only a before/after dump on the real snapshot can:

```
before.amsdb  ──► dump_invariants.py ──► before.json
        │  apply S0-S4, then S5
        ▼
after.amsdb   ──► dump_invariants.py ──► after.json  ──► diff  must be EMPTY
                                                          except §15
```

Run on a **copy of `LEGACY DATA/AMS_SMOOTH_IMPORT.amsdb`** — 1,607 rent sale
lines and 301 rent booking lines is the only input that exercises every path.

### 12.2 Invariants to freeze

| Domain | Frozen values |
|---|---|
| Sales | per sale: `amount`, `paid_amount`, `discount`, `rent_item_revenue`, `delivery_rent_cost`, `rent_variance_loss` |
| Sale lines | per `DirectSaleItem`: `product_name`, `qty`, `price_at_time`; no row added or removed |
| Bookings | per `Booking`: `amount`, `paid_amount`, `discount`; per `BookingItem`: `material_name`, `qty`, `price_at_time` |
| Money | every `PendingBill`, `Payment`, `AccountTransaction` and resulting balance |
| Drivers | every `SaleDeliveryPerson` / `DeliveryRent` row and total |
| Movements | `Entry` row **count** and `Σ qty` per `(type, material)` — unchanged, because we keep `Entry` (§9) |
| Stock | `Material.total` for all **66 non-rent** materials — byte-identical |
| Purchasing | every `GRNItem` and `GRNAllocation` |
| Clients | derived client balances / ledger totals |

### 12.3 Preflight assertions (before and after every phase)

1. No name exists in both `material` and `service`.
2. Every distinct name in `Entry`, `BookingItem`, `DirectSaleItem`,
   `MaterialReturnItem` resolves to **exactly one** master.
3. No `Service` name or code equals any `Material` name or code.
4. For all 70 existing material names:
   `is_service_name(x) == _is_rent_material_name(x)` — proves S0–S4 are a
   genuine no-op before cutover.
5. Rent rows in `stock_summary`, dashboard, dispatch summary: **0**.
6. Rent rows in `daily_transactions` and the client material ledger: **1,608** —
   the guard against the §5.2 classification error.
7. No `Entry` row for a service has `type='IN'` from a GRN.

### 12.4 Reversible sequence

1. Snapshot. **2.** `before.json`. **3.** Ship S0–S4; re-dump — must be
   **identical** (assertion #4 proves why). **4.** Re-snapshot. **5.** S5, one
   transaction. **6.** `after.json`; diff; only §15 may appear. **7.** Rollback =
   re-insert the four `material` rows, delete the four `service` rows, one
   transaction.

Steps 3 and 6 are the two gates. Nothing passes either with an unexplained delta.

---

## 13. Test plan

`tests/test_service_charge_items.py`:

1. A rent-only sale saves with `allow_global_negative_stock = False` and no
   `allow_negative_stock` checkbox → *the core promise*.
2. Booking with a service line: balance consumed by a `Booking Delivery` sale at
   dispatch exactly as today; cancel plan still refunds `remaining × rate`.
3. `stock_summary`, dashboard and dispatch summary contain **no** service row,
   though 1,608 `Entry` rows reference the names.
4. No `Material.total` is written for a service name after sale, edit, void,
   un-void, return, GRN attempt or full rebuild.
5. `/grn` add, `/edit_grn` and the Excel import **reject** a service line and
   create neither a `GRNItem` nor an `Entry` IN.
6. No `GRNAllocation`, `GRNItem.is_locked` or `cost_rate_at_sale` for a service.
7. `rent_item_revenue / delivery_rent_cost / rent_variance_loss` byte-identical
   to today on a fixture set.
8. **A Service id posted in `material_id[]` is rejected, never resolved.**
9. Collision guard: name **and** code refused in both directions; `resolve`
   raises on collision rather than guessing.
10. Transition rule: with the 4 rows still in `material` the rent box is correct
    (fallback); after the move it is still correct (Service path).
11. Returns: a booked service return refunds `qty × rent_rate`; a normal one
    `qty × (unit_rate + rent_rate)`; `Entry` IN written; no stock movement;
    `mat_obj.unit_price` read does not raise.
12. Rename/merge cascade renames a service across every table (P7).
13. Deactivated service still resolves when editing an old bill (P8).
14. Picker: a service is selectable as an item and rejected as an alternate (P9).
15. All §12.3 assertions hold.
16. §12.1 differential dump empty outside §15.
17. Driver payment flow untouched (existing tests must still pass).

---

## 14. Rollback

- **S0–S4 are inert on any DB with no services** — nothing to roll back.
- **S5** is reversible provided the cutover migration records the original row
  values and a `.amsdb` snapshot is taken first (standing convention per
  `README.md` and `GRN_SUPPLIER_SHIFT_PLAN.md`). No transaction is ever edited,
  so stock totals recompute correctly on restore.
- Any migration stays additive (`CREATE TABLE IF NOT EXISTS`, `INSERT OR IGNORE`);
  `MIGRATIONS_ALLOW_DESTRUCTIVE=0` is the default.

---

## 15. Accepted discrepancies — disclose, do not absorb

| # | Change | Magnitude |
|---|---|---|
| 1 | Rent no longer appears in stock views, **including historical dates**. An old printed stock report will not match a new one. | −32,076 · −84,090.6 · −22,707.89 · −3,438 |
| 2 | `RENT STEEL`'s `unit_price = 1.7857` leaves the cost index, so **historical P&L for RENT STEEL changes**. It is a bogus cost being removed, but reported numbers move. Value preserved in the Service row; one line to reverse. | ≈ **Rs 40,550** delivered / ≈ Rs 44,129 booked |
| 3 | Rent leaves the "COGS unknown" bucket, changing that bucket's composition even where totals do not. | directional only |

**Not accepted, and the reason §12 exists:** any change to a bill total, client
balance, payment, driver total, `Entry` row, or any non-rent `Material.total`.

---

## 16. Out of scope (logged, not fixed here)

- `LABOUR` (−505), `CHAUGATH-LABOUR` (−215), `MISC` (−63), `BACK FILLING` (−1)
  remain negative-stock pseudo-materials.
- **1,807 sales paid a driver with no client rent charge** and **484 charged rent
  with no driver cost** — needs a rate card / per-trip view
  (`rent_solution.md` §12.3-4).
- **Rs 1,447,815 of driver rent against 0 `DeliveryPersonPayment` rows.** All
  driver money leaves the shop outside AMS, so cash and daily reconciliation are
  structurally overstated. The in-app path exists — process change, zero code.
- **`rent_item_revenue` double-counts booked rent** (`rent_solution.md` §11.2).
  S4 must not worsen it; test #7 pins current behaviour.
- **Edit-sale has no stock validation at all**
  (`_direct_sales_edit_direct_sale.py` lacks the check at
  `_direct_sales_add_direct_sale.py:376-383`) — `rent_solution.md` §11.1.
- Three `MaterialCategory` rows are all named `General` (ids 2, 10, 11) —
  cosmetic duplicate-master hygiene.

---

### Sources read

`models/{catalog,sales,stock,delivery,rentals,parties,core}.py` ·
`app/services/{lookups,sales_core,void_rebuild,schema,drafts,booking_cancel_plan,
auto_migrate,api}.py` ·
`app/blueprints/{core.py,ops/grn.py,ops/_dispatch_add_record.py,
ops/_dispatch_tracking.py,sales/_direct_sales_add_direct_sale.py,
sales/_direct_sales_edit_direct_sale.py,sales/returns.py,
sales/_bills_download_invoice.py,misc/pending.py,misc/materials.py,
masters/materials.py,masters/rename_material_label.py,ledgers/other.py,
ledgers.py,reports/_profit_profit_reports.py}` ·
`blueprints/inventory/stock.py` · `app/hooks.py` ·
`templates/{direct_sales,bookings,grn_wizard,material_returns,layout}.html` ·
`app/migrations/README.md` · `rent_solution.md` · `GRN_SUPPLIER_SHIFT_PLAN.md` ·
read-only SQL over `LEGACY DATA/AMS_SMOOTH_IMPORT.amsdb` (2026-10-20).
