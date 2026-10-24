# AMS — Full Application Audit, Data Cleanup & One-Cycle Smoke Test

**Date:** 2026-10-24
**Branch:** `arena/2a5c9d26-rent-copyams-git-copyvscode-re`
**Databases:** the destructive run was rehearsed on a throw-away file
(`instance/audit_full_cycle.db`), then executed for real on the application database
**`instance/ahmed_cement_v44_fresh.db`** after taking a backup
(`instance/ahmed_cement_v44_fresh.PRE_AUDIT_BACKUP.db`). The app now holds exactly the
one-cycle dataset below, so every figure can be opened in the UI.

---

## Verdict

| Question | Answer |
|---|---|
| Does the full test suite pass? | **Yes — 330 passed, 6 skipped, 11 subtests passed, 0 failed** (15 min 57 s) |
| Does every screen render without a 500 / BuildError? | **Yes — 49 page requests, all HTTP 200** |
| Does one "wipe everything" actually clean every business table? | **Yes — 21 dirty tables → 0 rows**, identity/settings/audit survive, restart is clean |
| Does one of each master + one GRN + one cash booking + one booking-with-due land correctly? | **Yes — 76/76 checks passed, exit code 0** |
| Do supplier ledger, GRN, transactions, Profit & Loss and audit trail show the right numbers? | **Yes — every figure reconciles to the rupee** |
| Are there remaining data-integrity problems? | **None — consistency report 9/9 OK, truth engine 6/6 sections OK, preflight 0 blockers / 0 watch** |

Reproduce everything with one command:

```bash
python tools/audit_full_cycle.py          # add --keep to inspect the DB, --skip-wipe for the cycle only
python tools/audit_full_cycle.py --db instance/ahmed_cement_v44_fresh.db --keep   # apply to the app DB
```

The app currently runs on the audited database on `http://localhost:5000`
(`Admin` / `Admin@fbm12345`).

---

## 1. Full application audit

### 1.1 Test suite

```
330 passed, 6 skipped, 2 warnings, 11 subtests passed in 957.72s (0:15:57)
EXIT=0
```

* The 6 skips are all in `tests/test_full_db_sidecar.py`, which is skipped when the
  external data-lab template (`../data lab for migration old to new/NewData/…`) is absent.
  They are pure-stdlib sidecar-gate tests and do not touch the app.
* The 2 warnings are the pre-existing benign `SAWarning` on `grn_item` bulk deletes inside
  `app/services/void_rebuild.py` (0 rows matched by a defensive delete).

### 1.2 Page sweep

49 GET requests across every main module — dashboard, clients, suppliers, materials,
services, delivery persons, bookings, payments, direct sales, material returns, pending
bills, GRN, dispatching, tracking, ledgers (client / decision / supplier / material),
financial details, cash flow (+ differences), Profit & Loss, unpaid / mixed / daily
transactions, delivery rents, notifications, settings, activity log, inventory, all
`/accounts/*` screens, void audit, system report and the KPI endpoints.

**Result: 0 non-200 responses, 0 tracebacks, 0 BuildErrors.**

### 1.3 Permission audit (`python tools/audit_permissions.py`)

```
UNIQUE VIEWS: 309   MAPPED: 240   INLINE: 36   ROOT-ONLY: 17   ROLE-GATED: 3   PUBLIC-OK: 3
STALE MAP KEYS: 0
REMAINING GAPS: 10
```

The 10 residual entries are benign and were verified individually:

* 7 are **read-only lookup APIs** behind `@login_required`
  (`/api/clients/search`, `/api/suppliers/search`, `/api/last_sold_price`,
  `/api/check_bill/<no>`, `/api/client_next_code`, `/api/client_booking_status/<code>`,
  `/api/ui/theme`) — they expose no mutation and no cross-client financial totals.
* 3 are **disabled legacy stubs** that only flash a message and redirect
  (`/delete_all_data`, `/generate_dummy_data`) or serve a login-only upload file.

### 1.4 Data-integrity tools on the audit database

| Tool | Result |
|---|---|
| `tools/consistency_report.py` | 9 / 9 checks OK |
| `tools/predator_truth_engine.py` (independent raw SQL, does not import the app) | 6 / 6 sections OK — 0 FK orphans, 0 duplicate bills, 0 diverged accounts/materials |
| `tools/health/preflight_check.py` | 0 blockers, 0 watch items |

---

## 2. The one-time data cleanup

The wipe was executed through the real UI endpoint (not by deleting rows in scripts):
Settings → *Check all* → `confirm_text=DELETE ALL DATA` + `hard_delete_override=1`
→ `POST /delete_selected_data` (34-wipe-target set).

Before the wipe a junk dataset was seeded into **21 business tables** (masters, GRN,
booking, payment, supplier payment, direct sale, material return, cash flow, account
transactions, account categories, delivery-person payment, pending bills…).

| Check | Evidence |
|---|---|
| Junk seeded across modules | 21 non-empty business tables |
| Wipe accepted | `POST /delete_selected_data` → HTTP 302 |
| Business data after wipe | **0 rows in every business table** (only the bootstrap OPEN-KHATA client and "General" material category remain, by design) |
| Identity / settings / audit survive | users = 1, settings = 0, audit log = 4 (incl. the wipe record) |
| Wipe is auditable | 1 `wipe` row in `audit_log` |
| Bill counter | reset to its documented baseline |
| App restarts on the wiped DB | clean bootstrap, no error |
| Restart does not resurrect data | still 0 business rows |
| Login after wipe / after restart | works |
| Empty-state screens | 12 screens render (dashboard, clients, suppliers, materials, services, GRN, bookings, payments, accounts, cash flow, P&L, void audit) |

---

## 3. The one-cycle smoke dataset

Every record was created through the application's own routes (form POSTs), exactly as a
user would, and then read back from the database **and** from the rendered pages.

| # | Record | Values | Created via |
|---|---|---|---|
| 1 | Supplier | **Ravi Cement Supplier** | `POST /add_supplier` → flash "Supplier Added" |
| 2 | Client | **Bilal Construction** (`AUD-CLI-100`) | `POST /add_client` → flash "Client Registered" |
| 3 | Material | **OPC Cement** (stock unit: bags) | `POST /add_material` → flash "Brand Added" |
| 4 | Service | **Rent** | `POST /services/add` → flash "Service added" |
| 5 | Cash account | **Main Cash**, opening balance 200,000 | `POST /accounts/accounts/add` |
| 6 | GRN (purchase) | 100 bags × 1,200 = **120,000**, 50,000 paid in cash | `POST /grn` (`action=add`) → flash "GRN added successfully" |
| 7 | Cash booking (material + rent service) | 20 bags × 1,400 = 28,000 **plus 1 Rent × 2,000** = **30,000**, **10,000 paid now**, **20,000 due** | `POST /add_booking` → flash "Booking added successfully" |
| 8 | Booking due | **20,000** auto-registered as a pending bill | automatic on booking |
| 9 | Dispatch (supply) | 10 bags fulfilled against the booking | `POST /add_record` → flash "Record Saved" |

Derived state, all verified:

| Figure | Expected | Actual |
|---|---|---|
| Material stock after GRN | 100 bags | **100** |
| Material stock after dispatch | 90 bags | **90** |
| Supplier payable | 70,000 | **70,000** |
| Client outstanding | 20,000 | **20,000** |
| Cash account balance | 200,000 − 50,000 + 10,000 = 160,000 | **160,000.00** |

---

## 4. Evidence — what each screen shows

### 4.1 Supplier ledger (`/supplier_ledger/<id>`)

| Line | Amount |
|---|---|
| GRN `AUD-GRN-100` — purchase (debit) | **120,000.00** |
| Supplier payment — cash (credit) | **50,000.00** |
| **Closing balance (payable)** | **70,000.00** |

Also verified: the GRN bill number appears on the ledger, and the **PDF downloads
correctly — HTTP 200, 4,373 bytes**. The same payment appears on `/payments?party=all` and
on `/accounts/payments/suppliers` (auto-created from the GRN paid amount).

### 4.2 GRN

`/grn` lists the purchase with its bill number, one line item (100 bags @ 1,200) and total
**120,000.00**; the paid amount **50,000.00** is stored on the GRN and mirrored into a
supplier payment row.

`/accounts/receipts` now shows the **full GRN value 120,000.00** (previously only the
landed-cost columns were summed, so an ordinary purchase rendered as Rs. 0.00).

### 4.3 All transactions

| Screen | Evidence |
|---|---|
| Client ledger | booking + **20,000.00** due visible |
| Bookings | the booking is listed with amount 30,000 / paid 10,000 |
| Pending bills | the **20,000.00** booking due |
| Cash flow | the 50,000 cash-out and the 10,000 cash-in are both visible |
| Payments / accounts → supplier payments | the 50,000 supplier payment |
| Accounts → receipts | GRN total 120,000.00 |
| Dispatching / tracking | dispatch record with the 10 bags |
| Dashboard KPIs | Daily Cash Received **Rs. 10,000**, Daily Due **Rs. 20,000**, Total Outstanding **Rs. 20,000** |

### 4.4 Profit & Loss (`/profit_reports`, custom period = today)

| Line | Amount |
|---|---|
| Recognised revenue (10 bags × 1,400) | **14,000.00** |
| COGS (10 bags × 1,200 GRN cost) | **12,000.00** |
| **Gross profit** | **2,000.00** |

Exports: **PDF → HTTP 200** (valid `%PDF`), **Excel → HTTP 200** (valid `PK`/xlsx).
The drill-down `/profit_reports/transaction/Booking/<id>` renders cleanly (HTTP 200).

Revenue recognition is supply-based: the booking itself is an order/advance; only the
10 dispatched bags count as revenue, and their cost is taken from the GRN — which is why
the booking's 30,000 does not inflate the P&L.

### 4.5 Audit trail

| Evidence | Value |
|---|---|
| `audit_log` rows for the whole cycle | **18** |
| Rows tied to the booking cycle | **2** |
| `/settings/activity` | HTTP 200 |
| `/void_audit` | HTTP 200 |
| `/system_report` | HTTP 200 |

---

## 5. Defects found by this audit, and fixed

| # | Defect | Impact before the fix | Fix |
|---|---|---|---|
| 1 | `templates/accounts/receipts.html` summed only GRN landed-cost columns | a normal purchase showed **Total Rs. 0.00** | page now uses `calculate_grn_total()` (the same authoritative total as the GRN page, ledger and printed bill) |
| 2 | Same page labelled cash **outflow** as "Grand Total Receipts" | misleading KPI | relabelled "Total Cash Movement"; numbers now use thousands separators |
| 3 | **Services & Charges** routes (`/services`, add / edit / toggle / delete / merge / migrate-rent) had **no permission mapping** | a standard user who could not manage materials could still rename (cascading into history), merge and delete services | all 7 endpoints mapped to `can_manage_materials` |
| 4 | `api_client_financial_summary` unprotected | client dues/payment history readable by any signed-in user | mapped to `can_view_client_ledger` |
| 5 | `daily_reconciliation` and `financial_tracker` missing from the map | cash-day board could write counted balances / lock days without `can_view_cash_flow` | both mapped |
| 6 | `full_db_import_report/<name>` had no admin guard (its siblings did) | any signed-in LAN user could download the import report by guessing a filename | `_require_admin_root()` guard added |
| 7 | preflight `rows()` swallowed **every** SQLite error | a broken watch query reported "no drift" — a safety tool failing silently | only "no such table/column" is tolerated; everything else raises. This immediately exposed a real syntax error in `w7` (missing space before `FROM`) that had been silently disabling the account-drift watch |
| 8 | preflight `w7` compared stored balance to the **raw transaction net** | every account opened with an opening balance was reported as drifting (a healthy 200,000-opening cash account flagged on every run) | compares against `opening_balance + net`, exactly like `payments_crud.ledger_balance` — same correction applied to `tools/consistency_report.py` |
| 9 | preflight `w2` counted the pending bill that **mirrors** a booking's own bill number as a duplicate | every normal booking-with-a-due polluted the watch list, training operators to ignore it | a pending bill is only counted when no active booking / direct sale owns that bill number — real duplicates are still flagged (`/tmp/dup.db` → w2 + w6 fire) |
| 10 | `templates/pending_bills.html` printed a bare float | amounts showed as `20000.0` | `Rs. 20,000.00` |

Regression proof for #7–#9: `tests/test_preflight_watch_fixes.py` (3 tests) plus manual
runs against two deliberately damaged databases:

* `/tmp/corrupt.db` (balance 159,500 vs opening 200,000 and net −40,000) → **w7 fires** with
  `{'stored': 159500.0, 'ledger': -40000.0, 'opening': 200000.0}`.
* `/tmp/dup.db` (two bookings sharing `MB NO.DUP-1`) → **w2 + w6 fire**.

---

## 6. New tooling and tests

| File | Purpose |
|---|---|
| `tools/audit_full_cycle.py` (**new**) | 3-phase acceptance run: destructive cleanup → one complete business cycle → integrity tools. 76 checks, exit code 0/1. `--keep`, `--skip-wipe`, `--db`. |
| `tests/test_full_cycle_smoke.py` (**new**) | 4 pytest regressions: wipe → one-of-each cycle → ledger / P&L / audit figures |
| `tests/test_preflight_watch_fixes.py` (**new**) | 3 regressions: opening-balance drift detection, derived dues are not false duplicates, real duplicates still fire |
| `tests/test_audit_permissions_fixes.py` (**new**) | 4 regressions for the permission-map gaps above |
| `tests/test_audit_reporting_fixes.py` (**new**) | 3 regressions for the receipts / pending-bills reporting fixes |
| `tools/health/preflight_check.py` (**hardened**) | strict error handling, opening-balance-aware `w7`, mirror-aware `w2` |
| `tools/consistency_report.py` (**corrected**) | account balance check now uses the authoritative opening + movement formula |
| `README.md`, `tools/README.md` | document the new audit command and its contract |

All 14 new tests are part of the 330-test green run above.

---

## 7. How to re-run

```bash
# 1. the whole acceptance story on a throw-away DB (wipe + cycle + integrity)
python tools/audit_full_cycle.py

# 2. keep the database to inspect the figures yourself
python tools/audit_full_cycle.py --keep          # → instance/audit_full_cycle.db

# 3. read-only health checks on the live database
python tools/consistency_report.py
python tools/health/preflight_check.py
python tools/predator_truth_engine.py --db instance/ahmed_cement_v44_fresh.db --check

# 4. the full test suite
python -m pytest -q
```

---

## 8. Known, accepted limitations

* The 6 skipped tests need the external data-lab template directory; they are unrelated to
  the application code.
* 7 read-only lookup APIs and 2 disabled legacy stubs remain outside the permission map
  (detailed in §1.3). They cannot mutate data.
* The two `SAWarning`s come from defensive bulk deletes in `void_rebuild.py`
  (0 rows matched); they are warnings, not failures.
* The smoke run intentionally uses **one** row per section. The bulk datasets in
  `dummy_data/` and the `.amsdb` legacy snapshots were **not** imported, per the request.
