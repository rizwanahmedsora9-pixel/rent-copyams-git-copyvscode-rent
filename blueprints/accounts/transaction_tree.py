"""Authoritative data-driven Transaction Tree & Category/Subcategory registry.

Just as ``blueprints/accounts/classification.py`` governs how an Account is
classified when created or edited, this module governs how every financial
transaction in the Accounts section is:

1. **Entered** — a single data-driven hierarchy of Directions -> Transaction
   Nodes -> Categories -> Subcategories -> Counterparty / Account / GL rules.
2. **Categorized** — unifies all master Categories & Subcategories from
   ``CLASSIFICATION`` (`Assets`, `Liabilities`, `Equity`, `Income`,
   `Expenses`, `External`), all dynamic ``CashFlowCategory`` /
   ``CashFlowSubcategory`` rows, and all ``AccountCategory`` groups so every
   category and subcategory appears in transactions.
3. **Displayed** — parses raw internal markers (``[SRC:Payment:12]``,
   ``[AUTO_GRN_PAY:19]``, etc.) into concrete visual ``[From] ──► [To]``
   endpoints, badges, GL debit/credit previews, and a collapsible
   ``Direction -> Category -> Subcategory -> Transactions`` tree.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func

from . import classification as cls


# ---------------------------------------------------------------------------
# 1. Default Transaction Categories & Subcategories (merged with CLASSIFICATION
#    and dynamic CashFlowCategory / CashFlowSubcategory rows)
# ---------------------------------------------------------------------------

DEFAULT_EXPENSE_CATEGORY_TREE: "OrderedDict[str, Dict[str, Any]]" = OrderedDict([
    ("Utilities", {
        "master_category": "Expenses",
        "master_subcategory": "Operating",
        "direction": "out",
        "icon": "bi-lightning-charge",
        "subcategories": [
            "Electricity Bill",
            "Gas Bill",
            "Water & Sewerage",
            "Internet & Telephone",
            "Office Utilities",
        ],
    }),
    ("Fuel & Transport", {
        "master_category": "Expenses",
        "master_subcategory": "Operating",
        "direction": "out",
        "icon": "bi-fuel-pump",
        "subcategories": [
            "Diesel & Petrol",
            "Generator Fuel",
            "Vehicle Rent / Freight",
            "Delivery & Carriage",
            "Loader / Crane Fuel",
        ],
    }),
    ("Rent", {
        "master_category": "Expenses",
        "master_subcategory": "Operating",
        "direction": "out",
        "icon": "bi-building",
        "subcategories": [
            "Yard / Godown Rent",
            "Office / Shop Rent",
            "Machinery / Equipment Rent",
        ],
    }),
    ("Salaries & Wages", {
        "master_category": "Expenses",
        "master_subcategory": "Operating",
        "direction": "out",
        "icon": "bi-people",
        "subcategories": [
            "Monthly Staff Salary",
            "Daily Wage / Mazdoori",
            "Loading / Unloading Labour",
            "Overtime & Bonus",
            "Staff Food & Tea",
        ],
    }),
    ("Maintenance & Repairs", {
        "master_category": "Expenses",
        "master_subcategory": "Operating",
        "direction": "out",
        "icon": "bi-tools",
        "subcategories": [
            "Vehicle Repair & Tyres",
            "Machinery & Shutter Repair",
            "Yard & Building Maintenance",
            "Electrical & Plumbing Repair",
        ],
    }),
    ("Office & Admin", {
        "master_category": "Expenses",
        "master_subcategory": "Operating",
        "direction": "out",
        "icon": "bi-briefcase",
        "subcategories": [
            "Office Supplies & Stationery",
            "Tea, Guest & Refreshments",
            "Printing & Registers",
            "Cleaning & Janitorial",
            "Security & Watchman",
        ],
    }),
    ("Cost of Goods", {
        "master_category": "Expenses",
        "master_subcategory": "Cost of Goods",
        "direction": "out",
        "icon": "bi-box-seam",
        "subcategories": [
            "Purchases",
            "Freight / Unloading",
            "Direct Yard Labour",
            "Packing & Material Handling",
        ],
    }),
    ("Financial", {
        "master_category": "Expenses",
        "master_subcategory": "Financial",
        "direction": "out",
        "icon": "bi-bank",
        "subcategories": [
            "Bank Charges",
            "Interest Expense",
            "Waive-Off / Bad Debt",
            "Tax & Govt Fee",
        ],
    }),
    ("Personal", {
        "master_category": "Equity",
        "master_subcategory": "Drawings",
        "direction": "out",
        "icon": "bi-person-badge",
        "subcategories": [
            "Owner Drawings",
            "Partner Drawings",
            "Personal / Household Expense",
            "Medical & Family",
        ],
    }),
    ("Miscellaneous", {
        "master_category": "Expenses",
        "master_subcategory": "Operating",
        "direction": "out",
        "icon": "bi-grid",
        "subcategories": [
            "Miscellaneous Expense",
            "Charity / Sadaqah",
            "Tips / Bakshish",
            "General Yard Expense",
        ],
    }),
])

DEFAULT_INCOME_CATEGORY_TREE: "OrderedDict[str, Dict[str, Any]]" = OrderedDict([
    ("Sales", {
        "master_category": "Income",
        "master_subcategory": "Sales",
        "direction": "in",
        "icon": "bi-cart-check",
        "subcategories": [
            "Product Sales",
            "Direct Sales",
            "Cash Counter Sale",
        ],
    }),
    ("Service Income", {
        "master_category": "Income",
        "master_subcategory": "Service Income",
        "direction": "in",
        "icon": "bi-truck",
        "subcategories": [
            "Delivery Charges",
            "Rental Income",
            "Loading / Service Fee",
        ],
    }),
    ("Other Income", {
        "master_category": "Income",
        "master_subcategory": "Other Income",
        "direction": "in",
        "icon": "bi-cash-coin",
        "subcategories": [
            "Scrap / Misc Income",
            "Discount Received",
            "Commission / Rebate",
            "Supplier Refund / Recovery",
        ],
    }),
    ("Capital & Own Funds", {
        "master_category": "Equity",
        "master_subcategory": "Capital",
        "direction": "in",
        "icon": "bi-Briefcase-fill",
        "subcategories": [
            "Owner Capital",
            "Partner Capital",
            "Own Funds Deposit",
            "Retained Earnings",
        ],
    }),
    ("Loans & Borrowings", {
        "master_category": "Liabilities",
        "master_subcategory": "Loans",
        "direction": "in",
        "icon": "bi-journal-arrow-down",
        "subcategories": [
            "Bank Loan",
            "Partner Loan",
            "External Loan",
            "Temporary Borrowing",
        ],
    }),
    ("Receivables & Advances", {
        "master_category": "Assets",
        "master_subcategory": "Receivables",
        "direction": "in",
        "icon": "bi-person-check",
        "subcategories": [
            "Client Receivable",
            "Partner Receivable",
            "Staff Advance Recovery",
        ],
    }),
    ("External Party", {
        "master_category": "External",
        "master_subcategory": "External Party",
        "direction": "both",
        "icon": "bi-globe2",
        "subcategories": [
            "External Holding",
            "Escrow / Clearing",
            "Third-Party Deposit",
        ],
    }),
])


# ---------------------------------------------------------------------------
# 2. Authoritative Data-Driven Transaction Tree Registry
# ---------------------------------------------------------------------------

TRANSACTION_TREE: "OrderedDict[str, Dict[str, Any]]" = OrderedDict([
    ("receive", {
        "mode": "receive",
        "direction": "in",
        "label": "Money In (Receive)",
        "short_label": "Receive Money",
        "subtitle": "Record cash or bank inflows from clients, capital, loans, or income categories.",
        "icon": "bi-arrow-down-left-circle-fill",
        "color": "emerald",
        "nodes": OrderedDict([
            ("client_ledger", {
                "key": "client_ledger",
                "label": "Client Receivable Payment",
                "category": "Receivables",
                "subcategory": "Client Receivable",
                "badge": "Client Ledger",
                "icon": "bi-person-lines-fill",
                "description": "Receive payment or settle dues for a client (with optional Waive-Off / Discount).",
                "from_label": "Client (Receivable Ledger)",
                "to_label": "Company Cash / Bank Account",
                "gl_debit": "1010 / 1020 Cash or Bank (+ 8010 Waive-Off if discount)",
                "gl_credit": "1100 Accounts Receivable (Client)",
                "requires_client": True,
                "supports_discount": True,
                "supports_category": True,
            }),
            ("account_category", {
                "key": "account_category",
                "label": "Receive from Account Group / Loan / Capital",
                "category": "Capital & Loans",
                "subcategory": "Account Group Inflow",
                "badge": "Account Group",
                "icon": "bi-diagram-3-fill",
                "description": "Draw funds from a Loan account, Own Funds / Capital account, or another Account Category.",
                "from_label": "Source Account (Selected Category)",
                "to_label": "Destination Company Account",
                "gl_debit": "1010 / 1020 Destination Account",
                "gl_credit": "2300 / 3000 / 1010 Source Account",
                "requires_source_account": True,
                "supports_category": True,
            }),
            ("other_source", {
                "key": "other_source",
                "label": "Income / Categorized Receipt",
                "category": "Other Income",
                "subcategory": "Scrap / Misc Income",
                "badge": "Categorized Inflow",
                "icon": "bi-cash-stack",
                "description": "Receive money from any Income / Asset / Equity / External category & subcategory.",
                "from_label": "Income Category / Payer",
                "to_label": "Destination Company Account",
                "gl_debit": "1010 / 1020 Destination Account",
                "gl_credit": "4000 / 4100 Income or Capital Head",
                "requires_source_label": True,
                "supports_category": True,
                "supports_reference": True,
            }),
        ]),
    }),
    ("pay", {
        "mode": "pay",
        "direction": "out",
        "label": "Money Out (Pay)",
        "short_label": "Pay Money",
        "subtitle": "Pay suppliers, settle drivers, refund clients, repay loans, or record categorized expenses.",
        "icon": "bi-arrow-up-right-circle-fill",
        "color": "rose",
        "nodes": OrderedDict([
            ("supplier", {
                "key": "supplier",
                "label": "Supplier Payment (Payables)",
                "category": "Payables",
                "subcategory": "Supplier Payable",
                "badge": "Supplier Ledger",
                "icon": "bi-truck-flatbed",
                "description": "Pay an active supplier and automatically reduce Supplier Payable balance.",
                "from_label": "Source Cash / Bank Account",
                "to_label": "Supplier (Payable Ledger)",
                "gl_debit": "2000 Accounts Payable (Supplier)",
                "gl_credit": "1010 / 1020 Source Cash or Bank",
                "requires_supplier": True,
                "supports_category": True,
            }),
            ("driver", {
                "key": "driver",
                "label": "Driver / Delivery Service Payment",
                "category": "Payables",
                "subcategory": "Driver / Rent Payable",
                "badge": "Driver Ledger (FIFO)",
                "icon": "bi-truck",
                "description": "Settle delivery rent dues FIFO in the Driver Ledger and deduct from the source account.",
                "from_label": "Source Cash / Bank Account",
                "to_label": "Delivery Person / Driver Ledger",
                "gl_debit": "2100 Driver / Delivery Payable",
                "gl_credit": "1010 / 1020 Source Cash or Bank",
                "requires_driver": True,
                "supports_reference": True,
                "supports_category": True,
            }),
            ("other_expense", {
                "key": "other_expense",
                "label": "Operating / Business Expense",
                "category": "Utilities",
                "subcategory": "Office Utilities",
                "badge": "Business Expense",
                "icon": "bi-receipt-cutoff",
                "description": "Record an operating, yard, fuel, rent, salary, maintenance, or financial expense with category & subcategory.",
                "from_label": "Source Cash / Bank Account",
                "to_label": "Expense Category & Payee",
                "gl_debit": "6000+ Operating / Cost Expense Head",
                "gl_credit": "1010 / 1020 Source Cash or Bank",
                "supports_category": True,
                "supports_payee": True,
                "supports_reference": True,
                "supports_proof": True,
            }),
            ("personal_expense", {
                "key": "personal_expense",
                "label": "Personal / Owner Drawing Expense",
                "category": "Personal",
                "subcategory": "Owner Drawings",
                "badge": "Owner / Personal",
                "icon": "bi-person-workspace",
                "description": "Record a personal expense or owner drawing with full category, subcategory, payee, and voucher details.",
                "from_label": "Source Cash / Bank Account",
                "to_label": "Personal / Drawings Category",
                "gl_debit": "3100 / 6900 Drawings or Expense Head",
                "gl_credit": "1010 / 1020 Source Cash or Bank",
                "supports_category": True,
                "supports_payee": True,
                "supports_reference": True,
                "supports_proof": True,
            }),
            ("loan", {
                "key": "loan",
                "label": "Loan / Liability Repayment",
                "category": "Loans",
                "subcategory": "Bank Loan",
                "badge": "Loan Account",
                "icon": "bi-journal-check",
                "description": "Repay a Loan account (moves negative loan balance toward zero).",
                "from_label": "Source Cash / Bank Account",
                "to_label": "Destination Loan Account",
                "gl_debit": "2300 Loan Payable Account",
                "gl_credit": "1010 / 1020 Source Cash or Bank",
                "requires_loan_account": True,
                "supports_reference": True,
                "supports_category": True,
            }),
            ("client_refund", {
                "key": "client_refund",
                "label": "Client Cash Refund",
                "category": "Receivables",
                "subcategory": "Client Receivable",
                "badge": "Client Refund",
                "icon": "bi-arrow-return-left",
                "description": "Issue a cash/bank refund to a client and update both Client Ledger and Account balance.",
                "from_label": "Source Cash / Bank Account",
                "to_label": "Client (Refund on Ledger)",
                "gl_debit": "1100 Accounts Receivable (Client)",
                "gl_credit": "1010 / 1020 Source Cash or Bank",
                "requires_client_refund": True,
                "supports_category": True,
            }),
            ("company_transfer", {
                "key": "company_transfer",
                "label": "Intra-Company Account Transfer",
                "category": "Internal Transfer",
                "subcategory": "Cash ⇄ Bank / Wallet",
                "badge": "Internal Transfer",
                "icon": "bi-arrow-left-right",
                "description": "Transfer funds between two company accounts (Cash, Bank, or Digital Wallet).",
                "from_label": "Source Company Account",
                "to_label": "Destination Company Account",
                "gl_debit": "1010 / 1020 / 1030 Destination Account",
                "gl_credit": "1010 / 1020 / 1030 Source Account",
                "requires_to_account": True,
                "supports_category": True,
            }),
        ]),
    }),
    ("transfer", {
        "mode": "transfer",
        "direction": "transfer",
        "label": "Internal Transfer (Account ⇄ Account)",
        "short_label": "Transfer Funds",
        "subtitle": "Move money between Cash, Bank, Digital Wallet, or Category accounts with zero P&L impact.",
        "icon": "bi-arrow-left-right",
        "color": "cyan",
        "nodes": OrderedDict([
            ("company_transfer", {
                "key": "company_transfer",
                "label": "Account-to-Account Transfer",
                "category": "Internal Transfer",
                "subcategory": "Inter-Account Transfer",
                "badge": "Contra Transfer",
                "icon": "bi-arrow-left-right",
                "description": "Move money directly from one active account to another.",
                "from_label": "Source Account",
                "to_label": "Destination Account",
                "gl_debit": "1010 / 1020 / 1030 Destination Account",
                "gl_credit": "1010 / 1020 / 1030 Source Account",
                "requires_to_account": True,
                "supports_category": True,
            }),
        ]),
    }),
])


# ---------------------------------------------------------------------------
# 3. Unified Category & Subcategory Catalog Builder
# ---------------------------------------------------------------------------

def build_unified_category_catalog() -> Dict[str, Any]:
    """Return only user-managed, active transaction categories and children."""
    from models import AccountCategory, CashFlowCategory

    # Transaction labels are user configuration, never accounting-rule defaults.
    classification_groups = []
    categories_map = OrderedDict()

    def _ensure_cat(name, direction="both", master_category="", master_subcategory="", icon="bi-tag"):
        entry = dict(name=name, direction=direction, master_category=master_category,
                     master_subcategory=master_subcategory, icon=icon, subcategories=[])
        categories_map[name] = entry
        return entry

    def _add_sub(entry, name):
        if name not in entry["subcategories"]:
            entry["subcategories"].append(name)

    # Merge dynamic DB CashFlowCategory & CashFlowSubcategory rows
    try:
        cf_cats = (
            CashFlowCategory.query.filter(CashFlowCategory.is_active == True)
            .order_by(CashFlowCategory.sort_order.asc(), CashFlowCategory.name.asc())
            .all()
        )
        for cf_cat in cf_cats:
            c_dir = (cf_cat.direction or "both").lower()
            entry = _ensure_cat(
                cf_cat.name,
                direction=c_dir if c_dir in ("in", "out", "both") else "both",
                master_category="Expenses" if c_dir == "out" else ("Income" if c_dir == "in" else "Custom"),
                master_subcategory=cf_cat.name,
                icon="bi-bookmark-star",
            )
            for sub in getattr(cf_cat, "subcategories", []) or []:
                if getattr(sub, "is_active", True):
                    _add_sub(entry, sub.name)
    except Exception:
        pass

    # Merge dynamic DB AccountCategory rows
    account_groups: List[str] = []
    try:
        for ac in (
            AccountCategory.query.filter(AccountCategory.is_active == True)
            .order_by(AccountCategory.name.asc())
            .all()
        ):
            if ac.name and ac.name not in account_groups:
                account_groups.append(ac.name)
    except Exception:
        pass

    all_categories = list(categories_map.values())
    out_categories = [c for c in all_categories if c["direction"] in ("out", "both")]
    in_categories = [c for c in all_categories if c["direction"] in ("in", "both")]

    return {
        "classification_groups": classification_groups,
        "all_categories": all_categories,
        "out_categories": out_categories,
        "in_categories": in_categories,
        "account_groups": account_groups,
        "by_name": {c["name"]: c for c in all_categories},
    }


def is_valid_transaction_category(category_name: Optional[str], direction: Optional[str] = None) -> bool:
    """Check an active user-managed category and its allowed direction."""
    name = (category_name or "").strip().casefold()
    if not name:
        return True
    return any(c["name"].casefold() == name and
               (direction not in ("in", "out") or c["direction"] in (direction, "both"))
               for c in build_unified_category_catalog()["all_categories"])


def validate_transaction_labels(category, subcategory, direction=None):
    """Reject disabled labels, wrong directions and mismatched children."""
    category, subcategory = (category or '').strip(), (subcategory or '').strip()
    if not category:
        if subcategory:
            raise ValueError('Select a parent category before a subcategory.')
        return
    cat = build_unified_category_catalog()['by_name'].get(category)
    if not cat or (direction in ('in', 'out') and cat['direction'] not in (direction, 'both')):
        raise ValueError('Select an active category for this transaction direction.')
    if subcategory and subcategory not in cat['subcategories']:
        raise ValueError('Select an active subcategory belonging to the selected category.')



def transaction_tree_registry_json() -> Dict[str, Any]:
    """Export the full data-driven Transaction Tree + Category Catalog for JS."""
    catalog = build_unified_category_catalog()
    modes_out: List[Dict[str, Any]] = []
    for mode_key, mode_meta in TRANSACTION_TREE.items():
        nodes_out = []
        for node_key, node_meta in mode_meta["nodes"].items():
            nodes_out.append({
                "key": node_key,
                "label": node_meta["label"],
                "category": node_meta["category"],
                "subcategory": node_meta["subcategory"],
                "badge": node_meta["badge"],
                "icon": node_meta["icon"],
                "description": node_meta["description"],
                "from_label": node_meta["from_label"],
                "to_label": node_meta["to_label"],
                "gl_debit": node_meta["gl_debit"],
                "gl_credit": node_meta["gl_credit"],
                "requires_client": bool(node_meta.get("requires_client")),
                "supports_discount": bool(node_meta.get("supports_discount")),
                "requires_source_account": bool(node_meta.get("requires_source_account")),
                "requires_source_label": bool(node_meta.get("requires_source_label")),
                "requires_supplier": bool(node_meta.get("requires_supplier")),
                "requires_driver": bool(node_meta.get("requires_driver")),
                "requires_loan_account": bool(node_meta.get("requires_loan_account")),
                "requires_client_refund": bool(node_meta.get("requires_client_refund")),
                "requires_to_account": bool(node_meta.get("requires_to_account")),
                "supports_category": bool(node_meta.get("supports_category")),
                "supports_payee": bool(node_meta.get("supports_payee")),
                "supports_reference": bool(node_meta.get("supports_reference")),
                "supports_proof": bool(node_meta.get("supports_proof")),
            })
        modes_out.append({
            "mode": mode_key,
            "direction": mode_meta["direction"],
            "label": mode_meta["label"],
            "short_label": mode_meta["short_label"],
            "subtitle": mode_meta["subtitle"],
            "icon": mode_meta["icon"],
            "color": mode_meta["color"],
            "nodes": nodes_out,
        })
    return {
        "modes": modes_out,
        "catalog": catalog,
    }


# ---------------------------------------------------------------------------
# 4. Concrete Transaction Context & Source Marker Parser
# ---------------------------------------------------------------------------

_SRC_TAG_RE = re.compile(r"\[SRC:([A-Za-z_]+)(?::(\d+))?\]", re.IGNORECASE)
_AUTO_GRN_RE = re.compile(r"\[AUTO_GRN_PAY:(\d+)\]", re.IGNORECASE)
_CLIENT_TAG_RE = re.compile(r"\[CLIENT:(\d+)\]", re.IGNORECASE)
_BOOKING_VOID_TAG_RE = re.compile(r"\[VOID_BY_BOOKING:\d+\]", re.IGNORECASE)


def strip_internal_markers(note: Optional[str]) -> str:
    """Remove internal sync markers like ``[SRC:Payment:12]`` from user-facing notes."""
    if not note:
        return ""
    cleaned = _SRC_TAG_RE.sub("", note)
    cleaned = _AUTO_GRN_RE.sub("", cleaned)
    cleaned = _CLIENT_TAG_RE.sub("", cleaned)
    cleaned = _BOOKING_VOID_TAG_RE.sub("", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def describe_account_transaction(tx, account_map: Optional[Dict[int, Any]] = None) -> Dict[str, Any]:
    """Build a concrete, visual description of an ``AccountTransaction`` row.

    Resolves:
      - ``direction``: ``'in'`` | ``'out'`` | ``'transfer'``
      - ``tree_branch``: High-level branch in the Transaction Tree
      - ``category`` & ``subcategory``: Specific category and subcategory
      - ``from_label`` & ``to_label``: Concrete endpoints for ``[From] ──► [To]``
      - ``counterparty_label`` & ``counterparty_badge``: Never leaves Counterparty blank
      - ``source_badge`` & ``source_kind`` & ``source_id``: Clean badge for `[SRC:...]`
      - ``clean_note``: Note text without raw internal bracket tags
    """
    from models import Account

    def _get_acc(acc_id: Optional[int]):
        if not acc_id:
            return None
        if account_map and acc_id in account_map:
            return account_map[acc_id]
        try:
            return getattr(tx, "from_account", None) if acc_id == tx.from_account_id else getattr(tx, "to_account", None) or Account.query.get(acc_id)
        except Exception:
            return None

    from_acc = _get_acc(getattr(tx, "from_account_id", None))
    to_acc = _get_acc(getattr(tx, "to_account_id", None))

    tx_type = (getattr(tx, "transaction_type", None) or "Transaction").strip()
    raw_note = getattr(tx, "note", None) or ""
    clean_note = strip_internal_markers(raw_note)
    desc = (getattr(tx, "description", None) or "").strip()

    # Detect source document from explicit columns or note tags
    source_type = (getattr(tx, "source_type", None) or "").strip()
    source_id = getattr(tx, "source_id", None)

    if not source_type:
        m_src = _SRC_TAG_RE.search(raw_note)
        if m_src:
            source_type = m_src.group(1)
            if m_src.group(2):
                source_id = int(m_src.group(2))
        else:
            m_grn = _AUTO_GRN_RE.search(raw_note)
            if m_grn:
                source_type = "GRN"
                source_id = int(m_grn.group(1))

    source_badge = ""
    src_low = source_type.lower()
    if src_low == "payment":
        source_badge = f"Client Payment #{source_id}" if source_id else "Client Payment"
    elif src_low == "clientrefund":
        source_badge = f"Client Refund #{source_id}" if source_id else "Client Refund"
    elif src_low == "supplierpayment":
        source_badge = f"Supplier Payment #{source_id}" if source_id else "Supplier Payment"
    elif src_low == "directsale":
        source_badge = f"Direct Sale #{source_id}" if source_id else "Direct Sale"
    elif src_low == "booking":
        source_badge = f"Booking #{source_id}" if source_id else "Booking"
    elif src_low == "grn":
        source_badge = f"GRN Bill #{source_id}" if source_id else "GRN Bill"
    elif src_low in ("cashflowentry", "cashflow"):
        source_badge = f"Cash Flow #{source_id}" if source_id else "Cash Flow"
    elif src_low in ("accountreconciliation", "reconciliation"):
        source_badge = f"Reconciliation #{source_id}" if source_id else "Reconciliation"
    elif getattr(tx, "reconciliation_id", None):
        source_badge = f"Reconciliation #{tx.reconciliation_id}"

    # Determine direction & tree branch & category/subcategory
    cat = (getattr(tx, "category", None) or "").strip()
    subcat = (getattr(tx, "subcategory", None) or "").strip()
    payee = (getattr(tx, "payee", None) or "").strip()

    if from_acc and to_acc:
        # Both accounts are present: either Internal Transfer or Loan Repayment / Drawdown
        to_is_loan = ((getattr(to_acc, "source_category", None) or "").lower() == "loan"
                      or (getattr(to_acc, "class_subcategory", None) or "").lower() == "loans")
        from_is_loan = ((getattr(from_acc, "source_category", None) or "").lower() == "loan"
                        or (getattr(from_acc, "class_subcategory", None) or "").lower() == "loans")
        if to_is_loan or tx_type == "Payment":
            direction = "out"
            tree_branch = "Loan & Liability Settlements"
            cat = cat or "Loans"
            subcat = subcat or (getattr(to_acc, "class_account_type", None) or "Loan Repayment")
        elif from_is_loan:
            direction = "in"
            tree_branch = "Capital & Loan Inflows"
            cat = cat or "Loans"
            subcat = subcat or (getattr(from_acc, "class_account_type", None) or "Loan Drawdown")
        else:
            direction = "transfer"
            tree_branch = "Internal Account Transfers"
            cat = cat or "Internal Transfer"
            from_sub = getattr(from_acc, "class_subcategory", None) or (from_acc.category or "Cash").title()
            to_sub = getattr(to_acc, "class_subcategory", None) or (to_acc.category or "Bank").title()
            subcat = subcat or f"{from_sub} → {to_sub}"

        from_label = from_acc.name
        to_label = to_acc.name
        counterparty_label = f"{from_acc.name} ⇄ {to_acc.name}"
        counterparty_kind = "Account"

    elif to_acc and not from_acc:
        # Money IN to `to_acc`
        direction = "in"
        to_label = to_acc.name
        if src_low == "payment" or "client payment" in desc.lower():
            tree_branch = "Client Receivable Receipts"
            cat = cat or "Receivables"
            subcat = subcat or "Client Receivable"
            party = payee or desc.replace("Client payment from", "").split("(")[0].strip() or "Client"
            from_label = f"Client: {party}"
            counterparty_label = party
            counterparty_kind = "Client"
        elif src_low == "directsale" or "direct sale" in desc.lower():
            tree_branch = "Direct Sales & Bookings"
            cat = cat or "Sales"
            subcat = subcat or "Direct Sales"
            party = payee or desc
            from_label = party
            counterparty_label = party
            counterparty_kind = "Direct Sale"
        elif src_low == "booking" or "booking" in desc.lower():
            tree_branch = "Direct Sales & Bookings"
            cat = cat or "Sales"
            subcat = subcat or "Product Sales (Booking)"
            party = payee or desc
            from_label = party
            counterparty_label = party
            counterparty_kind = "Booking"
        elif tx_type in ("Reconciliation Excess", "Adjustment"):
            tree_branch = "Reconciliation & Adjustments"
            cat = cat or "Adjustments"
            subcat = subcat or tx_type
            from_label = tx_type
            counterparty_label = tx_type
            counterparty_kind = "System"
        else:
            tree_branch = "Other Receipts & Income"
            cat = cat or "Other Income"
            subcat = subcat or "Categorized Receipt"
            party = payee or desc.replace("Money received from", "").strip() or cat
            from_label = party
            counterparty_label = party
            counterparty_kind = "Income / Payer"

    else:
        # Money OUT from `from_acc`
        direction = "out"
        from_label = from_acc.name if from_acc else "Account"
        if tx_type == "Supplier Payment" or src_low in ("supplierpayment", "grn"):
            tree_branch = "Supplier & GRN Payables"
            cat = cat or "Payables"
            subcat = subcat or ("GRN Instant Payment" if src_low == "grn" else "Supplier Payable")
            party = payee or desc.replace("Supplier payment to", "").split("(")[0].strip() or "Supplier"
            to_label = f"Supplier: {party}"
            counterparty_label = party
            counterparty_kind = "Supplier"
        elif tx_type == "Driver Payment":
            tree_branch = "Driver / Delivery Settlements"
            cat = cat or "Payables"
            subcat = subcat or "Driver / Rent Payable"
            party = payee or desc.replace("Driver service payment to", "").split("(")[0].strip() or "Driver"
            to_label = f"Driver: {party}"
            counterparty_label = party
            counterparty_kind = "Driver"
        elif tx_type == "Refund" or src_low == "clientrefund":
            tree_branch = "Client Refunds"
            cat = cat or "Receivables"
            subcat = subcat or "Client Refund"
            party = payee or desc.replace("Client refund to", "").split("(")[0].strip() or "Client"
            to_label = f"Client Refund: {party}"
            counterparty_label = party
            counterparty_kind = "Client"
        elif tx_type in ("Reconciliation Loss", "Loss", "Adjustment"):
            tree_branch = "Reconciliation & Adjustments"
            cat = cat or "Adjustments"
            subcat = subcat or tx_type
            to_label = tx_type
            counterparty_label = tx_type
            counterparty_kind = "System"
        else:
            tree_branch = "Operating & Personal Expenses"
            cat = cat or (desc if desc in DEFAULT_EXPENSE_CATEGORY_TREE else "Operating")
            # Determine subcategory from tx.subcategory or matching default
            if not subcat:
                if desc and desc.lower() != cat.lower():
                    subcat = desc
                else:
                    subcat = "General Expense"
            party = payee or (f"{cat} ({subcat})" if subcat else cat)
            to_label = f"{cat}: {payee}" if payee else f"{cat} — {subcat}"
            counterparty_label = payee or subcat or cat
            counterparty_kind = "Expense / Payee"

    return {
        "tx_id": getattr(tx, "id", None),
        "direction": direction,
        "direction_label": {
            "in": "Money In",
            "out": "Money Out",
            "transfer": "Transfer",
        }.get(direction, "Movement"),
        "tree_branch": tree_branch,
        "category": cat,
        "subcategory": subcat,
        "from_label": from_label,
        "to_label": to_label,
        "counterparty_label": counterparty_label,
        "counterparty_kind": counterparty_kind,
        "source_type": source_type,
        "source_id": source_id,
        "source_badge": source_badge,
        "clean_note": clean_note,
        "reference_no": (getattr(tx, "reference_no", None) or "").strip(),
        "payment_method": (getattr(tx, "payment_method", None) or "").strip(),
        "amount": float(getattr(tx, "amount", 0) or 0),
        "date_posted": getattr(tx, "date_posted", None),
        "transaction_type": tx_type,
        "is_void": bool(getattr(tx, "is_void", False)),
    }


# ---------------------------------------------------------------------------
# 5. Live Hierarchical Transaction Tree & Account Daily Equation Builder
# ---------------------------------------------------------------------------

def build_live_transaction_tree(
    date_from: Optional[datetime] = None,
    date_to_excl: Optional[datetime] = None,
    account_id: Optional[int] = None,
    limit_rows: int = 300,
) -> Dict[str, Any]:
    """Aggregate ``AccountTransaction`` rows into a 3-level data-driven tree:
    ``Direction (In / Out / Transfer) -> Category -> Subcategory -> Transactions``.
    """
    from models import Account, AccountTransaction
    from sqlalchemy import or_

    accounts = Account.query.all()
    account_map = {a.id: a for a in accounts}

    q = AccountTransaction.query.filter(AccountTransaction.is_void == False)
    if date_from is not None:
        q = q.filter(AccountTransaction.date_posted >= date_from)
    if date_to_excl is not None:
        q = q.filter(AccountTransaction.date_posted < date_to_excl)
    if account_id is not None:
        q = q.filter(
            or_(
                AccountTransaction.from_account_id == account_id,
                AccountTransaction.to_account_id == account_id,
            )
        )

    rows = (
        q.order_by(AccountTransaction.date_posted.desc(), AccountTransaction.id.desc())
        .limit(limit_rows)
        .all()
    )

    directions: "OrderedDict[str, Dict[str, Any]]" = OrderedDict([
        ("in", {
            "key": "in",
            "label": "Money In (Receipts & Inflows)",
            "icon": "bi-arrow-down-left-circle-fill",
            "color": "emerald",
            "sign": "+",
            "total": 0.0,
            "count": 0,
            "categories": OrderedDict(),
        }),
        ("out", {
            "key": "out",
            "label": "Money Out (Payments & Expenses)",
            "icon": "bi-arrow-up-right-circle-fill",
            "color": "rose",
            "sign": "-",
            "total": 0.0,
            "count": 0,
            "categories": OrderedDict(),
        }),
        ("transfer", {
            "key": "transfer",
            "label": "Internal Transfers (Account ⇄ Account)",
            "icon": "bi-arrow-left-right",
            "color": "cyan",
            "sign": "⇄",
            "total": 0.0,
            "count": 0,
            "categories": OrderedDict(),
        }),
    ])

    for tx in rows:
        info = describe_account_transaction(tx, account_map=account_map)
        d_key = info["direction"]
        if d_key not in directions:
            d_key = "out"
        d_bucket = directions[d_key]
        amt = float(info["amount"] or 0.0)
        d_bucket["total"] += amt
        d_bucket["count"] += 1

        cat_key = info["category"] or info["tree_branch"] or "General"
        if cat_key not in d_bucket["categories"]:
            d_bucket["categories"][cat_key] = {
                "name": cat_key,
                "branch": info["tree_branch"],
                "total": 0.0,
                "count": 0,
                "pct": 0.0,
                "subcategories": OrderedDict(),
            }
        cat_bucket = d_bucket["categories"][cat_key]
        cat_bucket["total"] += amt
        cat_bucket["count"] += 1

        sub_key = info["subcategory"] or "General"
        if sub_key not in cat_bucket["subcategories"]:
            cat_bucket["subcategories"][sub_key] = {
                "name": sub_key,
                "total": 0.0,
                "count": 0,
                "transactions": [],
            }
        sub_bucket = cat_bucket["subcategories"][sub_key]
        sub_bucket["total"] += amt
        sub_bucket["count"] += 1
        sub_bucket["transactions"].append(info)

    # Convert OrderedDicts to sorted lists and calculate percentages
    directions_list = []
    for d_key, d_bucket in directions.items():
        d_total = d_bucket["total"] or 0.0
        cats_list = []
        for cat_bucket in sorted(
            d_bucket["categories"].values(), key=lambda c: c["total"], reverse=True
        ):
            cat_bucket["pct"] = round((cat_bucket["total"] / d_total) * 100.0, 1) if d_total > 0 else 0.0
            cat_bucket["subcategories"] = sorted(
                cat_bucket["subcategories"].values(),
                key=lambda s: s["total"],
                reverse=True,
            )
            cats_list.append(cat_bucket)
        d_bucket["categories"] = cats_list
        directions_list.append(d_bucket)

    total_in = directions["in"]["total"]
    total_out = directions["out"]["total"]
    total_transfer = directions["transfer"]["total"]
    total_count = sum(d["count"] for d in directions_list)

    return {
        "directions": directions_list,
        "total_in": total_in,
        "total_out": total_out,
        "total_transfer": total_transfer,
        "net_movement": total_in - total_out,
        "total_count": total_count,
    }


def enrich_accounts_with_period_flows(
    accounts: List[Any],
    date_from: datetime,
    date_to_excl: datetime,
) -> None:
    """Attach ``period_in``, ``period_out``, ``period_transfer_in``,
    ``period_transfer_out``, and ``period_tx_count`` to each ``Account`` object
    so the Dashboard table shows the complete visual equation.
    """
    from models import AccountTransaction

    by_id = {a.id: a for a in accounts}
    for a in accounts:
        a.period_in = 0.0
        a.period_out = 0.0
        a.period_transfer_in = 0.0
        a.period_transfer_out = 0.0
        a.period_tx_count = 0

    if not by_id:
        return

    txs = AccountTransaction.query.filter(
        AccountTransaction.is_void == False,
        AccountTransaction.date_posted >= date_from,
        AccountTransaction.date_posted < date_to_excl,
    ).all()

    for tx in txs:
        amt = float(tx.amount or 0.0)
        is_transfer = (tx.from_account_id is not None and tx.to_account_id is not None)
        if tx.to_account_id in by_id:
            acc = by_id[tx.to_account_id]
            acc.period_tx_count += 1
            if is_transfer:
                acc.period_transfer_in += amt
            else:
                acc.period_in += amt
        if tx.from_account_id in by_id:
            acc = by_id[tx.from_account_id]
            acc.period_tx_count += 1
            if is_transfer:
                acc.period_transfer_out += amt
            else:
                acc.period_out += amt
