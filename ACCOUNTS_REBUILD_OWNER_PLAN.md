# Accounts rebuild — owner-led plan

**Status:** discovery and proposal only; no rebuild or data migration performed.  
**Owner:** you decide the choices, labels, rules, and priorities. Examples below are questions or existing behavior, not decisions made for you.

**Decisions received:** scope is **Accounts pages only**, keeping existing posting services; review the existing choices before changing them; start with **account setup**. The detailed code-based inventory is in `ACCOUNT_SETUP_SELECTION_INVENTORY.md`. Individual selections and page design remain for your approval. Changing server-enforced classification options or rules would require separately approved scope beyond a pages-only rebuild.

## 1. Goal and boundaries

Rebuild the Accounts experience from a clean design, so that a user can see where money came from, where it went, why it moved, and what changed in the relevant account and party ledger. Keep the operational screens, payment services, account balances, audit history, period controls and general ledger consistent.

“From scratch” should describe the design and implementation approach, **not deletion of the existing financial data**. A parallel, staged replacement is proposed because other modules already post into Accounts. Nothing is cut over until you approve the behavior and the comparison checks pass. Whether this covers only the Accounts interface or also its accounting services and related reports is **your decision D01**.

## 2. What exists now — reviewed in this checkout

* `/accounts/` (`blueprints/accounts/dashboard.py`, `templates/accounts/dashboard.html`) combines daily KPIs, account balances, receive/pay/transfer forms and a transaction tree. The dashboard uses a ledger read model for its daily cash figures but also computes some account totals separately.
* `blueprints/accounts/transactions.py` handles receive from clients, other sources and account groups; payments to suppliers and drivers, client refunds, expenses, loan repayments and transfers. A separate transfer form also exists. Client/supplier/driver branches call their respective services; manual movements use the shared account-effect service.
* `models/cash.py` holds `Account`, `AccountTransaction`, openings, transaction categories, reconciliation and day-close records. `models/ledger.py` holds separate general-ledger accounts, journals and periods. An Accounts account and a GL account are not automatically the same kind of object.
* Account create/edit uses the code-defined classification tree in `blueprints/accounts/classification.py` (category → subcategory → type → permitted channel and entity), alongside separately managed `AccountCategory` groups. The form maps that classification back to legacy account fields. Account statuses and channel vocabulary are also code-defined.
* Transaction labels are separate `CashFlowCategory` / `CashFlowSubcategory` records, managed at `/accounts/transaction-categories`; `build_unified_category_catalog()` supplies only active user-managed transaction labels. The code also contains hard-coded transaction nodes and older default-tree definitions. Labels, account groups, account classifications and actual posting behavior are therefore **different things**, even if their names look similar.
* Other surfaces include account list/ledger, category management, client and supplier payments, receipts, expenditures, KPI drill-downs, transfers, audit and reconciliation. Sales, bookings, purchases/GRNs, returns, delivery settlements and cash/day-close flows can also affect the books. Their links must be inventoried before replacing shared services.
* Existing tests include `test_account_create_edit.py`, `test_account_categories.py`, `test_transaction_category_configuration.py`, `test_accounts_money_controls.py`, `test_transaction_tree_and_visual_flow.py`, `test_journal_posting.py`, `test_ledger_pages.py` and `test_day_close_and_permissions.py`. These are coverage starting points, not proof that every real-world path is correct.

### Specific issues to validate during discovery

1. The main dashboard template is large and combines many flows, making selection dependencies and changes harder to follow. Receive/pay/transfer behavior is also spread across separate routes and services.
2. Account classifications are hard-coded while transaction labels are managed by users. Configuring one does not necessarily configure the other, or change GL posting.
3. `account_form.validate_account_form` can create a submitted account group and commit during validation. Investigate atomicity and whether this bypasses the intended group-management workflow.
4. The new-transaction route substitutes the current time when a submitted date is invalid, rather than reporting the invalid value. Agree on date behavior before changing it.
5. Several loan checks compare classification names against `loans`, while the classification tree includes `Loans Payable`; legacy group/type fields also participate. Test real existing loan accounts across all paths before drawing conclusions.
6. In `add_transaction`, the fallback pay branch handles targets not explicitly matched earlier; verify unexpected target values cannot be treated as an ordinary payment. There are multiple transfer paths, so behavior and controls need cross-checking.
7. Category catalog queries currently catch broad exceptions and return partial/empty results. Investigate how configuration failures are surfaced so they are not mistaken for an intentionally empty list.

These are code-reading findings and investigation targets, not claims that all live data or every posting path has been audited. The older `ACCOUNTING_MASTER_PLAN.md` contains historical design assumptions and should not be treated as the current implementation; `ACCOUNTS_DEEP_AUDIT.md` describes a prior audit and fixes. Reverify against code and a safe data copy.

## 3. Proposed end-to-end experience — subject to your choices

1. **Overview:** owner-selected balances, money in/out, pending dues, exceptions and date/account filters. Every total has an agreed definition and a drill-down to source rows.
2. **Record a movement:** choose an enabled action, select the relevant party and/or source and destination, enter the details you require, and see an understandable preview before saving. The preview must come from the same validated posting rules as the save, not from label text alone.
3. **Accounts and setup:** manage the lists and relationships you authorize, including account groups, classifications, channels, categories, subcategories and required fields. Where choices are empty, guide the authorized user to setup instead of silently injecting business categories.
4. **History:** filter/search a unified register and open each row to see its source, account effect, party effect, journal reference, status and audit. Preserve legacy rows even where some details are missing.
5. **Controls:** reconcile, review exceptions, void/correct, and close/reopen periods according to your approved policy and permissions.

The experience above is a proposal, not a selected menu or accounting policy.

## 4. Selection-point register — all owner decisions pending

You can define individual values yourself, delegate specific values, or leave a point unchanged. No examples, current defaults, or proposed behavior below count as your approval. “Owner-defined” does not mean users can bypass server validation, choose invalid source records, or alter historical postings without the approved controls.

| ID | Where / selection point | What you need to define |
|---|---|---|
| D01 | Scope | UI only, Accounts module and its services, or Accounts plus related modules/reports; which workflows first. |
| D02 | Navigation | Screen names/order, overview cards, default landing page, desktop/mobile priorities, and whether entry is a wizard, single form, or another layout. |
| D03 | Account hierarchy | Names and allowed hierarchy of groups, accounting classifications, subcategories and types; which are business-configurable vs fixed technical accounting concepts. Decide whether legacy groups remain distinct. |
| D04 | Account channels | Available channels and labels; which types can use each channel; channel-specific fields and requiredness. Clarify cash, bank and wallet treatment. |
| D05 | Linked entities | Which account types require or allow a client, supplier, driver, worker, partner, vehicle or other party; how records are selected and whether free text is permitted. |
| D06 | Account lifecycle | Status names, which statuses may receive new transactions, what to do with used accounts, delete/archive behavior, and who may create, edit or deactivate. |
| D07 | Openings and adjustments | How debit/credit or signed openings are entered, effective date, who can adjust, evidence/reason, approval and impact on previously closed periods. |
| D08 | Transaction actions | Exact receive, pay, transfer, refund, loan, expense and non-cash/waive-off actions to show; their names, order, availability and whether actions can be enabled/disabled. |
| D09 | Categories and subcategories | Exact values, direction, parent-child rules, requiredness, display order, who maintains them, active/inactive behavior and whether multiple categories per movement are needed. Existing stored labels must be preserved. |
| D10 | Transaction inputs | For **each action**: party type, source/destination account, amount, date/time, method, category, subcategory, reference, note, proof, discount/waive-off and any additional fields; required/optional/hidden and field dependencies. |
| D11 | Payment methods | Values and labels, allowed account channels, required payment details, and whether defaults are permitted or an explicit selection is required. |
| D12 | Valid combinations and balances | Which accounts may be sources/destinations for each action; same-account rules; negative balance/overdraft policy, loan behavior and handling of inactive parties/accounts. |
| D13 | Accounting meaning | For each action/category: effect on account, party ledger and GL; chart mapping, capital vs income, personal drawings vs business expense, loan principal vs interest, refunds, advances and waive-offs. Approve with your accountant where appropriate. Names alone must not determine posting. |
| D14 | Allocation | Client/supplier/driver bill allocation, advances, part-payments, overpayments, discount and FIFO vs manual selection, including backdated activity. |
| D15 | Date and period controls | Timezone, date defaults, back/future dates, closed-period restrictions, day-close vs monthly close and authorized reopening/correction. |
| D16 | Corrections | Edit, void, restore, reversal and any deletion rules for manual and source-linked records; approval, reason and proof requirements; who may perform each. |
| D17 | Reconciliation | Accounts in scope, statement/count inputs, discrepancy treatment, adjustment authority, frequency, lock/unlock and evidence. |
| D18 | Reports and filters | Exact definitions for each KPI, inclusion of internal transfers and non-cash items, date basis, source/status/account/party filters, sorting, exports and drill-downs. |
| D19 | Access and approvals | Roles for view/setup/post/edit/void/reconcile/close/export, maker-checker or limits, and whether proof/audit information has restricted access. |
| D20 | Existing configuration and history | Keep, map, retire or rename each existing group/classification/category/method; treatment of unknown/duplicate/inactive values, historical labels and outstanding balances. No automatic destructive mapping. |
| D21 | Cutover and support | Staging data, comparison period, sign-off people, cutover time, rollback window, backups, training and acceptance criteria. |

### Per-action specification to complete with you

For each action selected in D08, record: **name → available to which roles → source → destination → party → allowed account types/channels → allowed methods → category/subcategory choices → required/optional fields → amount/discount/date rules → account effect → party effect → GL effect → preview → edit/void/restore rules → report placement**. Also specify whether a user must actively select a value or an approved default may be used. Unanswered cells block that action's production cutover.

### Configuration safeguards to agree

A proposed implementation would use stable internal identifiers rather than names for configurable options, maintain historical snapshots/versions for posted transactions, and validate configuration both in the browser and on the server. Renaming or disabling an option must not rewrite past transactions; disabled options should not be selectable for new entries unless you explicitly approve an exception. A user-defined label must map to an explicitly approved supported accounting action and GL rule; arbitrary labels alone must not generate invented postings. Permissions and valid accounting invariants remain enforced regardless of configuration. All of this requires your approval before implementation.

## 5. Implementation and verification plan

**Phase 0 — decisions and baseline.** Walk through D01–D21 with you, collect your actual lists and examples, capture existing workflow screenshots and inventories on a copy, run the relevant existing test suite, and identify each posting entry point and current data shape. Record unresolved decisions explicitly.

**Phase 1 — specification and prototype.** Produce the action-by-action matrix and clickable/wireframe flows for your approval, including empty states, invalid combinations, previews, permissions and report definitions. No live postings.

**Phase 2 — foundations.** Implement approved configuration and stable IDs, server-side validation, permission checks and auditable changes. Keep existing records and APIs compatible; plan how old labels are resolved. Avoid changing GL policy merely because a display label changes.

**Phase 3 — rebuild flows.** Build separately testable account setup, receive/pay/transfer and history components; connect to established posting services or replace services only where explicitly in scope. One authoritative command per event should atomically write source, account effect, journal and audit as applicable. Use integer minor units for amounts, idempotency on retries, concurrency protection, and approved period checks. Verify rather than assume each existing path already has these protections.

**Phase 4 — reports and controls.** Build approved overview, drill-downs, reconciliation, corrections and period controls using shared definitions. Each number should reconcile to its underlying rows and, where applicable, the book and party subledgers; distinguish cash movement, internal transfer, non-cash and income/expense.

**Phase 5 — migration and shadow comparison.** Take and test a restorable backup. Inventory configuration and historical records; propose mappings for owner approval, quarantine unresolved items rather than guessing. Run old and new calculations on copies or in read-only shadow mode, compare openings, account balances, individual source links, journal debits/credits, party balances, and report totals. Investigate differences before any cutover. Do not dual-post money in shadow mode.

**Phase 6 — approval and cutover.** Demonstrate every selected action and report to you; require explicit sign-off on choices, migration, access and comparison results. Roll out behind a controlled switch. Define a rollback procedure that accounts for transactions created after cutover; restoring a pre-cutover backup alone would discard those transactions. Reconcile any post-cutover activity before rollback. Keep old data and audit trails intact.

## 6. Acceptance checks

* Every visible business selection and its values/rules is traceable to an owner decision; no unapproved category, method or action is silently supplied. Empty configuration is handled explicitly.
* For each action, valid selections post correctly and invalid, inactive, missing, tampered or unauthorized values are rejected server-side. Changing a parent clears incompatible children, and the form preserves user input on validation errors.
* A transaction updates its source document, account balance, party balance, journal and audit together where applicable, or none on failure. Transfers preserve combined cash/bank balance and are not treated as income or expense; non-cash settlements do not create cash movement.
* Duplicate submissions, concurrent updates, period locks, amount rounding, backdating, permissions, edit/void/restore and linked-source corrections have regression tests for each supported path.
* Existing records and source links remain readable; renames/deactivations do not rewrite historical meaning. Legacy data with uncertain mappings is flagged for review.
* Every dashboard total and drill-down reconciles under identical filters; demonstrate account opening + movements = closing and balanced journal entries, and compare control accounts to subledgers as applicable.
* Test normal, empty and large datasets, keyboard/mobile usability, and backup/restore and rollback on a copy. Test results and unresolved issues are reported honestly before sign-off.

## 7. Starting decisions needed from you

1. **Scope (D01):** Should the rebuild cover the Accounts pages only, the Accounts pages plus their underlying posting services, or also connected sales/purchase/ledger reports?
2. **Owner-defined choices (D03–D11):** Do you want to define the exact lists now, or should we first show an inventory of all existing choices for you to keep/change/remove? Clarify whether every dropdown must be editable in the app or you will approve fixed system choices too.
3. **First workflow (D08/D10):** Which should be designed first: account setup, receive money, pay money, or transfer?

Once those are answered, the remaining decision register can be completed one workflow at a time. No implementation decision about your business rules is assumed from this plan.
