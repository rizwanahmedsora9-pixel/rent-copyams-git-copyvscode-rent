# Account setup — current selection inventory for your review

**Your choices so far:** redesign Accounts pages only; review existing choices before deciding changes; start with account setup. Underlying posting services remain out of scope. No items below have been approved, removed or changed.

**Important limitation:** the financial-classification tree, channel/entity rules and statuses below are validated on the server, not just displayed on the page. A pages-only redesign can reorganize or hide existing choices but cannot safely add new valid classification types or change their rules. If you want those values configurable or different, that requires a separate, explicitly approved server/configuration change; a visual-only change must not pretend to support it.

This inventory reflects code in this checkout, not the live database. It does not include your current saved account names, custom groups, clients or suppliers. Existing options are listed to help you decide, not as recommendations.

## 1. Account group / category

This is distinct from the financial classification tree below. The select lists active `AccountCategory` records, and the form has Quick Add Group. The code attempts to ensure these five active groups exist: **Company, Own Funds, Clients, External, Loan**. Other custom groups may be present in your database. Group values can affect existing payment and report behavior, so removing/renaming them is not purely cosmetic. Existing code can recreate a default if it is absent or inactive.

**Your decision:** which of these groups should be shown, renamed or excluded? List any additional groups. Should quick-add remain on this screen? Please do not assume hiding a group changes historical accounts or server behavior.

## 2. Financial classification — current code-defined choices

Format: **subcategory → account type** `(allowed channels; required linked entity)`. “None” means no linked entity is required. A single allowed channel is automatically set; for multiple channels, the first is currently the default. The available subcategories and types depend on the previous selection.

### Assets

- Cash → **Main Cash** (Cash; None); **Petty Cash** (Cash; None); **Cash Drawer** (Cash; None); **Site Cash** (Cash; None); **Temporary Cash** (Cash; None)
- Bank → **Operating Bank** (Bank; None); **Collection Bank** (Bank; None); **Savings Bank** (Bank; None); **Settlement Bank** (Bank; None)
- Digital Wallet → **Mobile Wallet** (Digital Wallet; None); **Payment App** (Digital Wallet; None)
- Client Receivables → **Client Ledger** (Ledger Only; Client); **Client Advance Receivable** (Ledger Only; Client)
- Advances Given → **Advance to Worker** (Ledger Only; Worker); **Advance to Party** (Ledger Only; Other Party); **Staff Advance** (Ledger Only; Worker)
- Other Assets → **General Asset** (Ledger Only, Cash, Bank, Other; None); **Security Deposit Given** (Ledger Only; Other Party)

### Liabilities

- Supplier Payables → **Supplier Ledger** (Ledger Only; Supplier); **Supplier Outstanding** (Ledger Only; Supplier)
- Client Advances → **Client Advance Received** (Ledger Only; Client)
- Loans Payable → **Bank Loan** (Ledger Only; Other Party); **Personal Loan** (Ledger Only; Other Party); **Partner Loan** (Ledger Only; Partner); **External Loan** (Ledger Only; Other Party)
- Partner / External Payables → **Partner Payable** (Ledger Only; Partner); **External Payable** (Ledger Only; Other Party)
- Expenses Payable → **Expense Payable** (Ledger Only; None)
- Other Liabilities → **General Liability** (Ledger Only; None)

### Equity / Own Funds

- Owner Capital → **Owner Capital** (Ledger Only, Cash, Bank; None)
- Partner Capital → **Partner Capital** (Ledger Only; Partner)
- Owner Drawings / Withdrawals → **Owner Drawings** (Ledger Only; None)
- Retained Funds → **Retained Earnings** (Ledger Only; None)
- Other Equity → **General Equity** (Ledger Only; None)

### Income

- Sales Income → **Sales Income** (Ledger Only; None)
- Service Income → **Service Income** (Ledger Only; None)
- Other Income → **Other Income** (Ledger Only; None)

### Expenses

- Operating Expenses → **Operating Expense** (Ledger Only; None)
- Delivery & Transport → **Delivery & Transport** (Ledger Only; None)
- Salaries & Labour → **Salary & Labour** (Ledger Only; None)
- Rent & Utilities → **Rent & Utilities** (Ledger Only; None)
- Administrative Expenses → **Administrative Expense** (Ledger Only; None)
- Other Expenses → **Other Expense** (Ledger Only; None)

### External / Clearing

- Partner Clearing → **Partner Clearing** (Ledger Only; Partner)
- Intercompany / Company Transfer → **Intercompany Transfer** (Ledger Only; None)
- External Settlement → **External Settlement** (Ledger Only; Other Party)
- Suspense / Temporary Clearing → **Suspense / Temporary** (Ledger Only; None)

**Your decision:** for each category/subcategory/type, mark keep, hide, rename for display, or request a new choice. Specify any different channel/entity rules. Hiding or renaming on a page must preserve the underlying values; changing validation, adding types or changing posting meaning needs approval to expand scope.

## 3. Channel and linked entity

Current channels: Cash, Bank, Digital Wallet, Ledger Only, Other. Types above determine which are available. The page hides the channel selector when there is only one. Client/supplier links select an existing active record; partner, worker, vehicle and other party use a name field when required by the classification. Not every entity type is used by a current classification.

- Cash details: physical location, responsible person (both optional in server validation).
- Bank: bank name, account title and account number/IBAN required; branch optional.
- Digital wallet: provider and wallet number required; account holder optional.
- Ledger Only / Other: no channel-specific fields in this form.

**Your decision:** which fields should be visible, and in which order? Which existing server-required fields must remain prominent? Any changes to requiredness or allowed combinations need separate backend approval.

## 4. Opening, status and adjustment

- Opening: amount, Debit or Credit direction, effective date. The form defaults to zero, debit and the presented date for a new account. The server also supplies defaults if fields are omitted. Debit is stored as positive and credit as negative.
- Status: Active, Inactive, Archived; new accounts default to Active. Existing status affects whether an account is selectable in new transactions.
- Edit only: desired balance, calculated direction/difference, adjustment date and a reason. Current reasons are Physical cash verification, Bank reconciliation, Historical correction, Data migration correction, Opening position correction, Accounting correction, Other (with explanation).

**Your decision:** which controls should appear on create vs edit; which default selections are acceptable; how should balance direction be explained; should adjustment be on the edit page or reached through a separate confirmation? Changing the underlying opening/adjustment rules is outside the current pages-only scope.

## 5. Other account setup interactions

- Account name (required) and note (optional).
- Create/edit preview of classification, linked entity, channel details, opening and status; edit adds calculated balance and pending adjustment.
- Manage accounts: channel and group filters, search and account actions, including edit, status change, delete and ledger navigation; available actions depend on permissions and record state.
- Account groups management supports create/edit/toggle/delete, subject to existing permissions and checks.

**Your decision:** preferred page layout, labels, visibility, search/filter controls, which actions need a confirmation, and whether you want a step-by-step form or a single page. UI controls must not imply that protected actions are allowed when the server rejects them.

## 6. Review worksheet

You can reply in this format, in parts:

1. **Groups:** keep …; hide …; rename …; add …; quick-add yes/no.
2. **Classification:** keep …; hide …; display rename old → new …; request additions … . (Use the full category → subcategory → type when names repeat.)
3. **Channel/entity:** show …; hide …; rule changes requested … .
4. **Opening/status/adjustment:** show …; hide …; default selections … .
5. **Layout:** single page / guided steps / your own description.

Unspecified selections remain undecided. We will not remove or rename your historical records or change financial behavior as a result of this review.
