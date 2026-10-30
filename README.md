# AMS — Ahmed Management System

Flask + SQLAlchemy (SQLite) ERP for a cement / building-materials business:
direct sales and bookings, stock and GRN, dispatch, delivery persons and
rentals, clients / suppliers, chart of accounts, cash flow and reconciliation,
financial ledgers, reports and PDF exports, Excel import / export, plus admin,
permissions and data-maintenance tooling.

`wsgi.py` is the production entry point; `main.py` is the local development
server. A signed PythonAnywhere deployment webhook is available through
`wsgi.py` (see "Deployment" below).

> **About the old audit files.** This repo used to ship a pile of session
> reports (`AUDIT_REPORT.md`, `QA_FULL_AUDIT.md`, orphan plans, schema-failure
> notes, continuation summaries). Those described **August 2026** — empty DB,
> hardcoded webhook token, CSRF only on accounts, duplicate auto bill numbers,
> Open-Khata receivables that could not be settled, wipe FK crashes. **The
> current app is well ahead of those write-ups.** The findings were fixed in
> code and locked with `tests/test_predator_regressions.py`. The historical
> markdown was deleted so nobody treats a stale P0 list as the live state.

---

## Stack

| Area | Technology |
|---|---|
| Web | Flask 3, Flask-Login, Flask-SQLAlchemy |
| ORM / DB | SQLAlchemy 2, SQLite (`instance/ahmed_cement_v44_fresh.db`, schema **v4.4**) |
| Data | pandas, numpy (&lt;2), openpyxl |
| PDF | WeasyPrint (primary) with ReportLab fallback (`app/services/pdf_fallback.py`) |
| Front end | Jinja2, Bootstrap 5.3, Bootstrap Icons, Flatpickr (vendored in `static/vendor`) |
| Tests | pytest (`pytest.ini`) |

Schema source of truth is the **ORM** (`db.create_all()` + column/index helpers
in `app/services/schema.py`). There is no `v44/SCHEMA_v4_4.sql` file; boot logs
a warning and continues. Versioned SQL in `app/migrations/` (`NNNN_*.sql`) runs
automatically on every start via `app/services/auto_migrate.py`.

---

## Layout

```text
main.py, wsgi.py        Dev server / production WSGI entry point
app/                    Factory, services, HTTP blueprints, SQL migrations
  app/services/         Accounting, billing, cash flow, ledgers, imports, PDFs
  app/blueprints/       ledgers, masters, misc, ops, reports, sales, system
  app/migrations/       Auto-applied NNNN_*.sql files (see that folder's README)
blueprints/             Auto-registered packages (accounts, inventory, import_export…)
models/                 SQLAlchemy models by domain
templates/              Jinja (layout.html + per-module pages)
static/                 JS/CSS and vendored libraries
utils/                  Module loader and shared helpers
tools/                  Maintenance, audit, migration and health scripts
tests/                  pytest (backend + frontend)
full_db_sync/           SQLite .amsdb snapshot export/import (stdlib only)
dummy_data/             Sample workbook generator / verifier
instance/               Runtime: SQLite DB, secret_key, logs, backups (git-ignored)
```

`utils/module_loader.py` discovers blueprints at startup.

---

## Getting started

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python main.py            # 0.0.0.0:5000
```

WeasyPrint needs `pango`, `cairo` and `gdk-pixbuf`. When they are missing
(common on shared hosts) PDF downloads fall back to ReportLab.

Default first-run admin (empty DB): `Admin` / `Admin@fbm12345`. Change it.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `5000` | Dev server port |
| `AMS_DEBUG` | unset | `1` enables the Werkzeug debugger in `main.py` |
| `APP_DB_PATH` | `instance/ahmed_cement_v44_fresh.db` | SQLite path |
| `AMS_SCHEMA_VERSION` | `v44` | Runtime schema (`v44` only) |
| `SECRET_KEY` | generated into `instance/secret_key` | Session signing |
| `AMS_HTTPS` | unset | `1` for secure / `SameSite=None` cookies |
| `MAX_UPLOAD_MB` | `256` | Upload size limit |
| `SQLITE_JOURNAL_MODE` | auto | `DELETE` is forced on PythonAnywhere (no POSIX shm for WAL) |
| `ALLOW_EMPTY_DB` | `1` | Missing DB is a valid first run |
| `MIGRATIONS_ALLOW_DESTRUCTIVE` | `0` | Permit `DROP`/`DELETE` in SQL migration files |

---

## Tests

```bash
pytest
```

Regression coverage for the old PRED defects (concurrent bill numbers,
future-dated payments, Open-Khata visibility/settlement, CSRF on all mutating
routes, sale idempotency, reconciled-period guard, wipe FK order, SQL-leak
flashes, `check_bill` auto bills, stock/bill/receivable blind spots) lives in
`tests/test_predator_regressions.py`. Schema/import, full-DB snapshots,
auto-migrations, wipe, ledgers/PDF and login have their own modules.

The end-to-end FIFO/GRN/P&L/API/live-refresh regression is in
`tests/test_fifo_grn_profit_realtime.py`. The Accounts workspace money entry
paths are covered by `tests/test_accounts_new_transaction.py` (Receive / Pay /
Transfer), `tests/test_accounts_receive_from_clients.py` (client and party
receives, payable lookup, discounts and waive-offs), and
`tests/test_accounts_pay_live_balances.py` (GRNs, supplier payments and live
balance refresh). The latter also tests the rendered form in a browser DOM
when Node and jsdom are installed.

`tests/test_accounts_cash_flow_trace.py` posts a set of distinct paisa amounts
through the real forms, then traces sources through account balances, ledgers,
Cash Flow, CSV/PDF, transfers and reversals. It also checks historical opening
and count carry-forwards and runs the independent raw-SQL account verifier.
Run it alone with `pytest -q tests/test_accounts_cash_flow_trace.py`; each test
uses its own disposable SQLite database. It does not inspect or repair a live
or historical business database.

Cash Flow shows account-linked movements once, with their source/reference and
paisa precision. A historical source without a confirmed company cash/bank posting is
listed separately as **unassigned**: it is not added to account totals, since
it may duplicate another document or represent a non-cash event. A source or
account-posting mismatch is flagged for review; the report never repairs data.

`tests/frontend/` holds offline DOM tests for the browser-side scripts. They are
not collected by pytest; run them with node after installing jsdom:

```bash
npm i jsdom
node tests/frontend/test_grn_consumption_dom.js   # GRN consumption indicator
node tests/frontend/test_pan_view_dom.js          # table pan / fit behaviour
node tests/frontend/test_ledger_live_dom.js       # live revision watcher
node tests/frontend/test_accounts_transaction_dom.js # Receive / Pay / Transfer, client + party pickers
```

Point `GRN_PAGE_HTML` at a saved `/grn` page to run the consumption test against
the real rendered screen instead of its built-in fixture.

---

## What is already solid (vs the old audits)

Do **not** re-open these as if they were still broken:

| Area | Current behaviour |
|---|---|
| Empty production DB | First-run bootstrap is supported; full data moves via SQLite snapshots |
| Hardcoded webhook token / wrong GitHub repo in `main.py` | Removed. The replacement uses a server-only secret and a signed WSGI webhook; `main.py` stays a local server |
| CSRF | Session CSRF on **every** mutating endpoint (`app/hooks.py`); no webhook skip remains |
| Duplicate auto bill numbers | `get_next_bill_no` takes the SQLite write lock first |
| Future-dated money | Payments and reconciliation dates in the future are rejected |
| Open Khata | `ensure_open_khata_client()` seeds a real master; receivables show and settle |
| Domain wipe FK order | Sale lines unlink `grn_item_id` before GRN lots are deleted |
| GRN date/time override | Adding **and** editing a GRN accept a hand-typed date and time (24-hour `14:45`, 12-hour `2:45 PM`, optional seconds). The stock-ledger rows follow the GRN stamp, and unreadable input is refused with a message instead of silently using the server clock |
| Sale forms | Add/edit fields match the backend; missing-field scare was not confirmed |
| Import FK / blank bill uniqueness | Parent-first insert; unique auto-bill indexes ignore blanks |
| Data transport | Full moves use `.amsdb` SQLite snapshots (`full_db_sync/`), not Excel |
| Schema going forward | Numbered SQL migrations apply on every boot |

Sale, booking, payment, GRN, stock, ledger and wipe paths are exercised by
pytest. The predator truth engine (`tools/predator_truth_engine.py`) can still
be run against a copy of a live DB for an independent raw-SQL check.

---

## Accounts → New Transaction (Receive, Pay, Transfer)

`/accounts/transactions/new` is the fourth Accounts workspace tab and the front
door for money movement. All three modes validate in
`app/services/account_transactions.py` and then hand the movement to the same
balance applier, journal autoposting, period guard, idempotency index and
accounting audit as every other entry point — never to a second ledger.

**Receive → who the money came from**

| Source | What it does |
|---|---|
| **Client** | Searchable picker over the active client master (name, code, phone). Picking a client fetches its live payable from `/accounts/transactions/client-dues` and shows it under the field. The receipt settles through `save_client_payment`, so FIFO bill allocation, waive-off loss rows and the client ledger stay authoritative. A **general discount** is supported: `amount 0` plus a discount posts a Waive-Off, needs no destination account and moves no cash. |
| **Party** | Loan givers, lenders, partners and other outside parties, stored in `CashFlowParty`. Picking a saved party reuses it and its type; typing a new name registers it so the list grows. A `loan` party is described as "Loan received from …" on the ledger row. |
| **Other** | Free-text counterparty recorded on the transaction only. This is the original behaviour and is still what a post with no `receive_source` means. |

A discount only makes sense against a receivable, so it is refused for party,
free-text, Pay and Transfer submissions instead of being dropped silently.

The picker rows come from the page payload (`window.AW_DATA.clients` /
`.parties`) and are capped at 50 visible matches; the payable is fetched for the
one picked client, so opening the tab never projects every client ledger.

---

## The book: journal, periods, and one set of numbers

The application keeps a real double-entry book next to the documents. It is
additive — no document table changed — and it is the reason the dashboard, the
P&L and the ledger pages can no longer disagree.

**The layers (one direction only):**

```
documents  →  journal (the book)  →  read models  →  pages / exports / API
```

1. **Documents own the event.** Saving, editing or voiding a GRN, sale, booking,
   payment, return, driver settlement, cash-flow row or opening balance makes
   that document post its own journal entry inside the same transaction
   (`app/services/ledger/autopost.py` → `sync.py` → `posting_rules.py`).
2. **The book owns the numbers.** Only `app/services/ledger/query.py` aggregates
   journal lines. Nothing else sums money tables for a report.
3. **Read models own the page shapes.** `app/services/read_models/` (dashboard,
   client_account, supplier_account, stock_view, pnl_view, cash_view,
   payables_view, driver_view, books_agree) returns plain dicts in whole paisa,
   cached in `app/services/cache.py` and keyed by `ledger_version`. A posting
   bumps the version, so the next page load rebuilds instead of showing stale
   numbers — no restart, no manual refresh button.

**Rules the book enforces**

| Rule | Where |
|---|---|
| One balanced entry per document event; retries do not duplicate | `journal_entry.idempotency_key` (unique), `posting.post_spec` |
| Editing a document = reversal + re-post, both visible | `posting.reverse_entry`, `sync.sync_document` |
| A voided/deleted document reverses every entry it posted | `sync.release_document` |
| `journal_line` can never be updated or deleted | SQLite triggers in `app/migrations/0003_create_ledger_tables.sql` |
| A locked month refuses postings; corrections land in the open month | `ledger/periods.py::assert_can_post`, `posting.reverse_entry` |
| Discounts are contra-revenue (`4090`), never divided into lines at render time | `posting_rules.build_direct_sale` |
| Anything with no known classification goes to suspense (`1400`), never dropped | `posting_rules`, `books_agree` |

**Where to look** — `/ledger` is the module: Overview, Chart of accounts,
Journal (with entry drill-down), Trial balance, Profit & loss, Balance sheet,
Cash, Receivables, Payables, Periods (checklist → soft close → lock → reopen),
Coverage and **Books agree**. The books-agree gate compares every control
account with the list that supports it (receivables with clients, payables with
suppliers, inventory with GRN lots minus FIFO consumption, cash with the cash
masters, driver payable with allocations) and names each difference.

**Periods.** `accounting_period` is one row per calendar month with three
states: `open` (post freely), `soft_closed` (post, but the entry is flagged as
an adjustment) and `locked` (nothing posts; a reversal of a locked entry is
dated into the current open month). Closing runs a checklist, records the
month's result and rolls it into retained earnings. `payments_crud
._assert_period_open` delegates to the same rule, so a locked month also stops
payments and reconciliations.

**Boot behaviour.** On start the app seeds the chart of accounts, maps every
cash/bank/wallet master to its own GL account, and — when the journal is empty —
replays the existing history once (`ledger/replay.py`). Later boots only post
documents that have no live entry. Set `AMS_LEDGER_AUTOPOST=0` or
`AMS_LEDGER_SYNC_ON_BOOT=0` to disable this for a diagnostic run.

**Tests.** `tests/test_journal_posting.py` (posting matrix per event),
`tests/test_period_close.py` (the state machine and the lock), 
`tests/test_ledger_pages.py` (pages render the book, API equals the pages) and
`tests/test_read_model_consistency.py` (dashboard = P&L = subledgers = API, and
the cache rebuilds after a posting).

---

## Supplier ledger shift (moving a whole ledger)

When a supplier's ledger was entered under the wrong name (a typo, a duplicate
supplier, or a new "clean" supplier being started), the **Shift Supplier
Ledger** flow moves *everything* to the receiving supplier in one transaction:

* every GRN (matched by `supplier_id` **or** by the legacy stored name, exactly
  like the ledger projection does),
* every supplier payment — active, voided, standalone and GRN auto-payments,
* the supplier's opening balance (added onto the target, zeroed on the source),
* legacy `Entry` stock rows still carrying the old supplier name,
* accounts linked to the supplier (`Account.linked_supplier_id` /
  `linked_party_name`, optionally renamed),
* the cash/bank transactions of the moved payments (descriptions re-pointed) and
  manual cash-flow rows + their `CashFlowParty`.

Inventory quantities, material totals, GRN lots, FIFO allocations
(`GRNAllocation`, `DirectSaleItem.grn_item_id`) and customer sales are never
touched. The emptied supplier is suspended and marked `transferred_to_id`, so it
can be hard-deleted afterwards with no residue; **Reclaim** reverses the shift
using the ids recorded in `accounting_audit_log`.

Where to find it:

| Entry point | Route |
|---|---|
| Suppliers page → delete modal → *Option 1c* | `GET /supplier_ledger_shift/<id>` |
| Supplier ledger header → *Shift entire ledger* / *Reclaim shifted ledger* | `GET /supplier_ledger_shift/<id>`, `POST /reclaim_supplier_ledger/<id>` |
| Supplier ledger row → shift icon (single GRN or single payment) | `POST /shift_grn_supplier/<id>`, `POST /shift_supplier_payment/<id>` |
| JSON preview used by the page | `GET /api/supplier_ledger_shift_preview/<id>` |

Service: `app/services/supplier_ledger_shift.py`
(`shift_supplier_ledger`, `preview_supplier_ledger_shift`,
`shift_supplier_payment_to_supplier`, `reclaim_supplier_ledger`).
Tests: `tests/test_supplier_ledger_shift.py`. All routes require
`can_manage_suppliers`. A GRN auto-payment cannot be moved on its own — it is
owned by its GRN, so shift the GRN (its payment follows) or use the full shift.

---

## Data: load, snapshot, migrate

**Day-to-day full copy** (recommended): Import / Export Center →
**Full Database Snapshot (.db)**. Export produces `AMS_FULL_<ts>.amsdb`.
Import backs up the live file, wipes rows (schema stays), copies source rows
with original IDs, then runs `integrity_check` + `foreign_key_check` +
row-count parity.

CLI (same engine, no Flask required):

```bash
python -m full_db_sync export --db instance/ahmed_cement_v44_fresh.db
python -m full_db_sync verify --db AMS_FULL_xxx.amsdb
python -m full_db_sync import --source AMS_FULL_xxx.amsdb \
    --db instance/ahmed_cement_v44_fresh.db --confirm
```

Details: `full_db_sync/README.md`.

**Dummy dataset:** `dummy_data/README.md` (full XLSX for the legacy full-raw
importer). Prefer `.amsdb` for real full-data moves.

**Old-format `ahmed_cement.db`:** the snapshot importer **refuses** it (missing
v4.4 columns) before touching the target. Convert with the external migrate
tool, then import the resulting v4.4 file. The XLSX pipeline under
`tools/migrate/` is **superseded** (kept for old `ALLEXPORT` workbooks). An
“opening balances only” migration was proposed and **never built** — live
path is full-history SQLite.

**Adding schema later:** drop `app/migrations/0002_short_name.sql` (never edit
an already-applied file). See `app/migrations/README.md`.

---

## Deployment

The replacement is `deploy_hook.py`, mounted by `wsgi.py` at `/deploy`.
It uses only Python's standard library on Linux/PythonAnywhere. A signed GitHub
push to the configured branch runs `git pull --ff-only origin <branch>` and
touches the PythonAnywhere WSGI file to request a reload. No API token,
GitHub Actions workflow, scheduler, or Flask CSRF exemption is required.

### Install and configure with `setup_Deploy.py` (recommended)

`setup_Deploy.py` is a single-file, standard-library installer for this deployment.
It can bootstrap a fresh account, repair a broken one, and verify a working one. It
never imports the Flask application, never starts a second server, and never touches
the database files.

Obtain it in one of two ways:

* download the raw file from the repository (this works before a merge too, but the
  branch must contain the file):

  ```bash
  # use main once this change is merged; until then use the branch that contains it
  curl -fsSL -o ~/setup_Deploy.py \
    https://raw.githubusercontent.com/rizwanahmedsora9-pixel/AMS/main/setup_Deploy.py
  ```

* or copy it from a checkout you already have (`cp /path/to/AMS/setup_Deploy.py ~/`).

Run it in a **PythonAnywhere Bash console** on the production account:

```bash
cd /home/rehmanahmed        # the production checkout; there is no AMS subfolder
python3 setup_Deploy.py     # guided: prints a plan and asks before material changes
```

It defaults to `--target /home/rehmanahmed`, `--repo
https://github.com/rizwanahmedsora9-pixel/AMS.git`, `--branch main` and
`--domain rehmanahmed.pythonanywhere.com`; `--target/--repo/--branch/--domain`
override those for a different account or a staging copy.

| Command | What it does |
| --- | --- |
| `python3 setup_Deploy.py --check-only` | inspects and validates only, writes nothing (safe on a live site) |
| `python3 setup_Deploy.py --dry-run` | prints the plan and every command it would run, changes nothing |
| `python3 setup_Deploy.py --yes` | unattended run: confirms the material changes it would normally ask about |
| `python3 setup_Deploy.py --show-secret` | prints the webhook secret once, privately, for the GitHub box |
| `python3 setup_Deploy.py --restore-installer-file` | archives a locally edited `setup_Deploy.py` and restores the tracked copy |
| `python3 setup_Deploy.py --help` | all options (python, venv, wsgi file, retries, timeouts, skip-…) |

Exit codes: `0` everything required is done, `2` done except the listed manual steps,
`1` blocked/failed — so a script can tell "needs a human" from "broken".

#### What the installer does automatically

* Inspects first and prints what it found: checkout, origin, branch, local edits,
  hook, WSGI file, secret, virtualenv, requirements, SQLite files, existing state.
  It never assumes the home directory is empty.
* Bootstraps the home-directory checkout (`git init`, `remote add`, `fetch`,
  `checkout -b <branch> --track origin/<branch>`) and adds the `*` rule to
  `.git/info/exclude` so unrelated account files stay invisible to Git.
* Archives whatever `setup_Deploy.py` already sits in the checkout into
  `<checkout>/instance/setup/backups/<timestamp>/downloaded--setup_Deploy.py`
  before the checkout can replace it, and copies any account file that a tracked
  AMS path would overwrite (with a manifest), asking before continuing.
* Updates an existing checkout with `git fetch` + `git merge --ff-only origin/<branch>`.
  It never runs `git reset --hard`, `git clean`, `git stash`, or a forced checkout;
  modified tracked files stop the update and are reported instead of discarded.
* Creates or reuses `/home/rehmanahmed/.venv` (using the same Python version as the
  Web app) and installs `requirements.txt` with that virtualenv's own Python.
  Transient network failures are retried a bounded number of times; real errors are
  reported with pip's message.
* Creates `instance/deploy_secret.txt` with `secrets.token_hex(32)` and mode `0600`,
  keeps a valid existing secret (only tightening the mode), and rotates only when you
  ask (`--rotate-secret`) or confirm. Git history is checked for the previously
  committed secret and a stale root-level copy is offered for quarantine.
* Writes exactly one managed block into the WSGI file shown in the Web tab:

  ```python
  # >>> AMS deployment setup (managed by setup_Deploy.py) >>>
  ...
  os.environ["AMS_WSGI_FILE"] = "/var/www/rehmanahmed_pythonanywhere_com_wsgi.py"
  os.environ["AMS_DEPLOY_BRANCH"] = "main"
  os.environ.setdefault("SQLITE_JOURNAL_MODE", "DELETE")
  os.environ.setdefault("AMS_HTTPS", "1")
  from wsgi import application
  # <<< AMS deployment setup (managed by setup_Deploy.py) <<<
  ```

  Everything outside the markers is preserved (host setup, a custom `APP_DB_PATH`,
  deliberate overrides). The file is backed up first, validated as Python before
  writing, and the block is replaced rather than duplicated on later runs. A
  hand-written AMS import is adopted only after a confirmation, keeping the old line
  as a comment; another application's WSGI file is refused untouched, and the
  installer never guesses a WSGI file name that does not exist.
* Verifies in stages (files installed → virtualenv/dependencies → secret → WSGI
  configured → site reachable → `/deploy/health` → signature accepted/unsigned
  rejected → webhook → real push→pull), and writes a redacted log and report under
  `<checkout>/instance/setup/`.

#### What still needs you (authentication or manual action)

* **Web app settings.** With a PythonAnywhere API token (`export API_TOKEN=…`,
  or `--api-token-file`) the installer reads the Web tab configuration and updates
  source directory/working directory, virtualenv, Python version and the `/static/`
  mapping through the documented API, then reloads. Without a token it changes
  nothing and prints the exact Web tab steps: Source code and Working directory
  `/home/rehmanahmed`, virtualenv `/home/rehmanahmed/.venv`, `/static/` →
  `/home/rehmanahmed/static`. Creating a web app through the API depends on the
  account type — if it is refused, the installer says so and prints the manual steps.
* **The GitHub webhook.** With a token (`GITHUB_TOKEN`/`GH_TOKEN`, the `gh` CLI, or
  `--github-token-file`) it creates or verifies the webhook
  (`https://rehmanahmed.pythonanywhere.com/deploy`, content type
  `application/json`, push events only, SSL verification on, secret from the server
  file) and pings it to confirm a `200`/`pong`. Without a token it prints the
  two-field GitHub form. An existing webhook is never duplicated and its secret is
  never changed unless you pass `--update-webhook`.
* **The first real push.** Only a signed push → pull → reload proves the pipeline.
  The installer deliberately does not create a commit to test it; push a commit to
  the deployed branch. *Recent Deliveries* should answer `Deploy queued` (200)
  within a second, and once the background pull finishes GET `/deploy/status`
  reports `Deployed <sha>; WSGI reload requested`.
* **Secrets.** Tokens come from the environment, a `0600` file, or a hidden prompt —
  never from chat, never into Git, never into the log or report (only fingerprints).
  `--show-secret` is the one explicit, private display step for pasting the secret
  into GitHub; clear the screen afterwards.

#### Rerun, recovery and troubleshooting

* Every run is safe to repeat: it re-detects the state, keeps the existing secret and
  virtualenv, and rewrites the WSGI block only when it has drifted. `--check-only`
  revalidates without writing. Writes are atomic, so an interrupted run leaves no
  half-written file — just rerun it.
* If a stage fails, the log/report names the stage, the cause and the next action.
  Common cases:
  - *hook returns 409 / "working tree is not clean"* — tracked files are modified.
    Review with `git -C /home/rehmanahmed diff` and commit or restore deliberately.
    For `setup_Deploy.py` itself use `--restore-installer-file`.
  - *wrong origin or branch* — the installer refuses and changes nothing; rerun with
    `--set-origin` / `--switch-branch` once you have confirmed it is right.
  - *dependencies failed* — the report contains pip's own error; fix it and rerun.
  - *WSGI file missing or different name* — the installer prints the block to paste
    and refuses to guess; pass `--wsgi-file` with the exact path from the Web tab
    (needed for custom domains), or `--create-wsgi-file` for a brand-new app.
  - *something looks wrong after a change* — every replaced file is in
    `<checkout>/instance/setup/backups/<timestamp>/` (with a `manifest.json`).
* Two installers cannot run at once: the second exits with the lock file
  (`<checkout>/instance/setup/setup.lock`) explained. It does not delete the lock.
* It never rolls back or restores the live database, never runs the app against the
  production DB as a "check", and never claims success for an unverified step.

### Manual setup (reference — `setup_Deploy.py` performs these steps)

AMS site: https://rehmanahmed.pythonanywhere.com/

The manual procedure is kept here as documentation of what the installer automates and as a fallback for an account where automation is unavailable.

First merge this change into the branch you deploy (normally `main`), then
install or update **this** repository on PythonAnywhere. The server checkout's
`origin` must be `rizwanahmedsora9-pixel/AMS`, and its current branch must match
`AMS_DEPLOY_BRANCH`. Do not reuse the other application's checkout or secret.
Configure the Web tab virtualenv and install `requirements.txt` into it.

The production checkout lives directly in `/home/rehmanahmed`, with no `AMS`
subfolder. PythonAnywhere home directories already contain account files, so
`git clone ... .` usually cannot work. For this first installation only, run
in a **PythonAnywhere Bash console**, after the changes are merged:

```bash
cd /home/rehmanahmed
# Stop if this home directory already belongs to a Git checkout.
if git rev-parse --git-dir >/dev/null 2>&1; then
    echo "Existing Git checkout: stop and inspect it before continuing."
else
    git init &&
    git remote add origin https://github.com/rizwanahmedsora9-pixel/AMS.git &&
    git fetch origin main &&
    git checkout -b main --track origin/main &&
    printf '\n# Deployment-only home checkout: ignore unrelated untracked account files\n*\n' >> .git/info/exclude
fi
```

Do not force checkout or delete account files if Git reports a collision.
The local exclude rule keeps unrelated home files from blocking deployment;
tracked AMS files are still checked for edits. This is a deployment-only
checkout: new untracked files are ignored, so do development in a separate
checkout. Never run `git clean -fdx` here—it could erase account data.

Use `/home/rehmanahmed` for both Source code and Working directory in the
Web tab, `/home/rehmanahmed/.venv` for the virtualenv, and map `/static/` to
`/home/rehmanahmed/static`. Create the virtualenv with a Python version matching
the Web app, for example:

```bash
cd /home/rehmanahmed
python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

1. In a PythonAnywhere Bash console, generate a **new**, server-only secret:

   ```bash
   cd /home/rehmanahmed
   mkdir -p instance
   (umask 077; python3 -c 'import secrets; print(secrets.token_hex(32))' > instance/deploy_secret.txt)
   ```

   The uploaded root-level `deploy_secret.txt` was tracked in Git. Removing it
   does not erase history: consider it exposed and replace it in any other
   webhook that shared it. Never commit the new value or paste it into chat.

2. In **Web → WSGI configuration file**, preserve any required host setup,
   then load AMS like this (assuming the checkout is `/home/rehmanahmed`;
   adjust the repository path if you cloned it elsewhere):

   ```python
   import os
   import sys

   sys.path.insert(0, "/home/rehmanahmed")
   os.environ["AMS_WSGI_FILE"] = "/var/www/rehmanahmed_pythonanywhere_com_wsgi.py"
   os.environ["AMS_DEPLOY_BRANCH"] = "main"
   os.environ.setdefault("SQLITE_JOURNAL_MODE", "DELETE")
   os.environ.setdefault("AMS_HTTPS", "1")
   from wsgi import application
   ```

   Use the exact WSGI filename shown in the Web tab (custom domains can differ).
   Do not point `AMS_WSGI_FILE` at the repository's `wsgi.py`. If your existing
   setup uses a custom `APP_DB_PATH`, preserve it and back up that database.
   The hook does not change, delete, or copy database files.

3. Click the Web tab's **Reload** button once. Open `/deploy/health`; it should
   say the hook is reachable. This is only a reachability check, not proof that
   Git, secrets, or reload permissions are configured correctly.

4. In **rizwanahmedsora9-pixel/AMS → Settings → Webhooks → Add webhook**:
   - Payload URL: `https://rehmanahmed.pythonanywhere.com/deploy`
   - Content type: `application/json`
   - Secret: copy the new server file's contents privately into GitHub.
   - Events: **Just the push event**; keep SSL verification enabled.

5. GitHub's signed ping should return `pong`. After a push to the deployed
   branch, **Recent Deliveries** should answer `Deploy queued` (200) within a
   second; once the background pull finishes, GET `/deploy/status` reports
   `Deployed <commit>; WSGI reload requested`. Then verify the app and
   PythonAnywhere logs. These changes are
   currently on the Arena working branch; pushes here do not deploy `main`
   until merged. No live deployment has been performed by adding this code.

### Behaviour and troubleshooting

- Optional `AMS_DEPLOY_SECRET_FILE` overrides `instance/deploy_secret.txt`.
  Set environment variables before importing `wsgi` and reload after changes.
- Wrong branch, deleted branch, and non-push events are ignored (and answer
  200 so Recent Deliveries stays clean). Missing secrets return 503; bad
  signatures return 401; malformed JSON returns 400. Bodies above 32 MB
  (`AMS_DEPLOY_MAX_BODY`) are refused with 413 before any parsing; anything
  GitHub actually sends — including multi-megabyte push payloads from bulk
  imports or patch stacks — is accepted and verified normally.
- Local changes, a wrong checkout branch, or a diverged pull fail safely and
  are reported at GET `/deploy/status` and in the server log. The hook never
  stashes, resets, or discards server edits.
- Signed pushes are acknowledged immediately — GitHub marks a delivery failed
  when the response takes more than ~10 s — and the pull + reload runs on a
  background worker. A burst of pushes coalesces: the worker re-pulls until it
  lands the newest commit, and a lock held by another deploy is retried
  automatically. Git commands have timeouts and cannot ask for interactive
  credentials. For private repos, configure read access on the server
  separately.
- Deployment errors go to PythonAnywhere's error log. GET `/deploy`,
  `/deploy/health`, and `/deploy/status` reveal no paths, secrets, or Git logs
  (only the last result code and message). Reload failure returns an error
  even if code pulled.
- Dependencies are **not** installed by the hook. Install changed requirements
  manually and reload. AMS applies its existing migrations on startup; take
  a database snapshot before releases with schema changes. There is no
  automatic rollback or database backup in this hook.
- The webhook is production WSGI-only; `python main.py` does not mount it.

### What is covered by tests, and what still needs a real server

Automated tests (`tests/test_setup_deploy.py`, run with the rest of the suite) cover,
in temporary directories with local Git remotes and scripted pip/HTTP — no GitHub or
PythonAnywhere call is ever made and `/var/www` is never touched:

* fresh install, home-directory bootstrap with account files preserved and the `*`
  exclude rule in place, idempotent rerun;
* installer-file collision archived before checkout, colliding home file backed up,
  non-home checkouts excluding only the installer file;
* dirty tracked files, wrong origin, wrong branch: refused and left untouched, with
  `--set-origin` / `--switch-branch` as the explicit remedies;
* missing interpreter, incompatible virtualenv version, pip failure (transient
  retried a bounded number of times, permanent not), missing imports;
* existing secret preserved and permissions tightened, explicit rotation, exposed
  root-level secret quarantined, and no secret or token ever reaching the log,
  report, state file or console;
* custom WSGI content and a custom `APP_DB_PATH` preserved, hand-written AMS import
  adopted as a comment, another application's WSGI file refused, generated block is
  valid Python with exactly the required variables;
* API and webhook stages falling back to printed manual steps without a token, an
  API refusal changing nothing, bounded HTTP retries, the concurrent-run lock, and
  `--check-only`/`--dry-run` writing nothing at all;
* the pre-existing repository suite (including `tests/test_deploy_hook.py`) still
  passing.

Still to be verified on the real account, because no automated test can prove it:

* whether the **free** PythonAnywhere plan allows the documented webapp / static
  files / reload API calls (the manual Web tab steps are the fallback), and whether
  the account may write `/var/www/<domain>_wsgi.py`;
* a real signed GitHub push → `git pull --ff-only` → WSGI reload, confirmed by a
  `200` in *Recent Deliveries*;
* the exact Python version available in the Web tab versus the console used to
  create the virtualenv (the installer warns when they differ).

---

## Tools

Nothing under `tools/` is imported at runtime. Start with:

```bash
python tools/audit_full_cycle.py            # full wipe + one complete business cycle
python tools/consistency_report.py          # read-only integrity
python tools/health/preflight_check.py      # blockers that stop new sales
python tools/predator_truth_engine.py --db instance/ahmed_cement_v44_fresh.db --check
```

`tools/audit_full_cycle.py` is the acceptance walk-through. On a throw-away
SQLite file it seeds junk into every module, runs Settings → **Check all** with
the hard-delete confirmation, proves every business table is empty, that
identity/settings/audit survive, and that the app still boots and logs in.
It then creates one supplier, one client, one material and one service
("Rent"), buys stock on a GRN (partly paid), writes one cash booking that bills
both the material and the Rent service with cash paid now + a due, supplies the
booked material, and verifies the figures in the supplier ledger
(and its PDF), the GRN list, client ledger, pending bills, cash flow, Accounts
receipts, the Profit & Loss report (and its PDF/Excel exports) and the
activity / void-audit trail. It finishes by running the consistency report and
the preflight watch on the same database. Add `--keep` to inspect the file and
`--skip-wipe` to run only the business cycle. Exit code 0 = every check passed.
**Never point `--db` at an existing or live database:** the audit deletes that
file and its SQLite sidecars before it starts, even with `--skip-wipe`.

Repair scripts in `tools/repair_controlled/` require `--confirm` and take a
backup first. See `tools/README.md`.

The last full audit (test suite, wipe, one complete business cycle and the
integrity tools) is written up in [`AUDIT_REPORT.md`](AUDIT_REPORT.md).

---

## Remaining work (real, not the old P0 list)

These are the gaps that still match the **current** tree:

1. **Deployment activation** requires PythonAnywhere and GitHub setup and a
   real push/reload check (see "Deployment" above).
2. **`User.password_plain`** still exists for legacy rows. Successful login
   upgrades to `password_hash` and clears plaintext. Rotate any account that
   has never logged in since the hash-only era.
3. **`_WIPE_BACKUP_ENABLED` is `False`.** Granular wipe in Settings does not
   auto-file a DB copy (the deployment hook also makes no backups). Take a
   snapshot before a wipe.
4. **Sale POST idempotency** is tested, but unkeyed double-submit uniqueness
   is still a product choice (payload hash + key). Do not rely on the browser
   alone.
5. **Scale:** list pages that embed every client in the combobox grow with
   client count; `/api/clients/search` exists for a lazy picker if payloads
   get large.

Do not restore the deleted audit markdown. If a new defect is found, file it
against **this** code (repro + pytest), not against August 2026 notes.

---

## License

No license has been added to this repository yet.
