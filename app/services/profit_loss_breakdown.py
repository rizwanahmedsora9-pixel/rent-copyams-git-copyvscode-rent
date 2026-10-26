"""Plain-language Profit & Loss breakdown for the existing P&L Control Center.

This module does **no accounting of its own**.  It reads the authoritative rows
produced by :func:`app.services.profit_loss.build_profit_loss_report` and
groups them into the view model the enhanced report page shows:

    Delivered goods sales  -  FIFO cost of goods sold   =  Gross profit
    Earned service revenue -  direct service costs      =  Service margin
    Gross profit + service margin + other income - operating expenses
        - other losses + other gains                    =  Net profit / loss

Recognized revenue and cost are only ever read from rows that the P&L includes
in net (``include_in_net``), so the report and the dashboard can never double
count a sale, a payment, a return or a purchase.

Also attached here are the two settlement axes the report filters on:

* ``payment_status`` — paid / partly paid / unpaid, taken from the client
  ledger's per-document obligation (``_make_client_obligations``), which is the
  same FIFO bill allocation the client ledger and payables pages use.
* ``booking_status`` — open / partially delivered / completed / cancelled,
  derived from the supplied-versus-booked quantity the projection already
  tracks.

Neither axis changes revenue: credit is a receivable, an advance is not income.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time

from app.services.financial_ledgers import (
    _client_snapshot,
    _make_client_obligations,
    _same_name_id_groups,
)
from app.services.grn_consumption import build_grn_consumption
from app.services.time_money import pk_today

EPS = 1e-6

PAYMENT_PAID = "paid"
PAYMENT_PARTIAL = "partially_paid"
PAYMENT_UNPAID = "unpaid"
PAYMENT_NA = "not_applicable"

BOOKING_OPEN = "open"
BOOKING_PARTIAL = "partially_delivered"
BOOKING_COMPLETED = "completed"
BOOKING_CANCELLED = "cancelled"

GROUP_GOODS = "goods"
GROUP_SERVICES = "services"
GROUP_RETURNS = "returns"
GROUP_EXPENSES = "expenses"
GROUP_OTHER = "other"

PAYMENT_STATUS_CHOICES = (
    ("", "All payment statuses"),
    (PAYMENT_PAID, "Paid"),
    (PAYMENT_PARTIAL, "Partially paid"),
    (PAYMENT_UNPAID, "Unpaid"),
    ("credit", "Credit outstanding (any unpaid balance)"),
)

BOOKING_STATUS_CHOICES = (
    ("", "All booking statuses"),
    (BOOKING_OPEN, "Open (nothing delivered)"),
    (BOOKING_PARTIAL, "Partially delivered"),
    (BOOKING_COMPLETED, "Completed"),
    (BOOKING_CANCELLED, "Cancelled / void"),
)

TRANSACTION_GROUP_CHOICES = (
    ("", "All transaction types"),
    (GROUP_GOODS, "Goods sales (delivered)"),
    (GROUP_SERVICES, "Services"),
    (GROUP_RETURNS, "Returns"),
    (GROUP_EXPENSES, "Operating expenses"),
    (GROUP_OTHER, "Other income / other"),
)

GROUP_LABELS = {
    GROUP_GOODS: "Goods sales",
    GROUP_SERVICES: "Services",
    GROUP_RETURNS: "Returns",
    GROUP_EXPENSES: "Operating expenses",
    GROUP_OTHER: "Other income / other",
}

PAYMENT_LABELS = {
    PAYMENT_PAID: "Paid",
    PAYMENT_PARTIAL: "Partially paid",
    PAYMENT_UNPAID: "Unpaid",
    PAYMENT_NA: "Not a customer bill",
}

BOOKING_LABELS = {
    BOOKING_OPEN: "Open",
    BOOKING_PARTIAL: "Partially delivered",
    BOOKING_COMPLETED: "Completed",
    BOOKING_CANCELLED: "Cancelled",
}


def _f(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _norm(value):
    return " ".join(str(value or "").strip().casefold().split())


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def row_group(row):
    """Map a P&L row to the report's transaction-type filter group."""
    entry_type = str(row.get("entry_type") or "")
    if entry_type in {"material_sale", "booking_supply"}:
        return GROUP_GOODS
    if entry_type in {"delivery_service", "delivery_cost"}:
        return GROUP_SERVICES
    if entry_type == "return":
        return GROUP_RETURNS
    if entry_type == "expense":
        return GROUP_EXPENSES
    return GROUP_OTHER


def _document_key(row):
    document_type = row.get("document_type")
    document_id = row.get("document_id")
    if document_type and document_id:
        return (str(document_type), int(document_id))
    if row.get("booking_id"):
        return ("Booking", int(row["booking_id"]))
    if row.get("sale_id"):
        return ("DirectSale", int(row["sale_id"]))
    return None


def booking_status_map(context):
    """Booking status from booked vs supplied quantity (no extra queries)."""
    statuses = {}
    for booking in getattr(context, "bookings", []):
        if bool(getattr(booking, "is_void", False)):
            statuses[booking.id] = BOOKING_CANCELLED
            continue
        booked = 0.0
        supplied = 0.0
        for item in booking.items:
            original = _f(item.qty)
            remaining = _f(context.booking_remaining.get(item.id, original))
            booked += max(0.0, original)
            supplied += max(0.0, original - max(0.0, remaining))
        if booked <= EPS:
            statuses[booking.id] = BOOKING_OPEN
        elif supplied <= EPS:
            statuses[booking.id] = BOOKING_OPEN
        elif supplied >= booked - EPS:
            statuses[booking.id] = BOOKING_COMPLETED
        else:
            statuses[booking.id] = BOOKING_PARTIAL
    return statuses


def settlement_index(context, *, snapshot=None):
    """Per-document settlement from the client ledger's own bill allocation.

    Returns ``{(source_type, source_id): {...}}`` with the document's gross,
    embedded payments/discounts, remaining balance and derived status.  This is
    the same FIFO allocation the client ledger shows, so the report's
    "outstanding" figure always matches the ledger.
    """
    snapshot = snapshot or _client_snapshot()
    groups = _same_name_id_groups(snapshot)
    index = {}
    seen = set()
    for client in snapshot.get("clients", []):
        name_key = _norm(getattr(client, "name", None))
        if name_key in seen:
            continue
        seen.add(name_key)
        # Clients sharing a name are one financial identity; the first master
        # row carries the ledger.
        detail = _make_client_obligations(client, snapshot=snapshot)
        for obligation in detail["obligations"]:
            key = (obligation["source_type"], int(obligation["source_id"]))
            gross = _f(obligation["gross"])
            paid = _f(obligation.get("embedded_paid", obligation.get("embedded_credit")))
            discount = _f(obligation.get("embedded_discount"))
            remaining = _f(obligation.get("remaining"))
            if remaining <= 0.005:
                status = PAYMENT_PAID
            elif paid > 0.005 or discount > 0.005:
                status = PAYMENT_PARTIAL
            else:
                status = PAYMENT_UNPAID
            index[key] = {
                "gross": gross,
                "collected": paid,
                "discount": discount,
                "remaining": max(0.0, remaining),
                "status": status,
                "status_label": PAYMENT_LABELS[status],
                "date": obligation.get("date"),
                "reference": obligation.get("reference") or "",
                "client": getattr(client, "name", "") or "",
            }
    return index


def attach_document_status(context):
    """Attach filterable status attributes to every projection row (one pass)."""
    payments = settlement_index(context)
    bookings = booking_status_map(context)
    for row in context.rows:
        row["transaction_group"] = row_group(row)
        row["transaction_group_label"] = GROUP_LABELS.get(row["transaction_group"], "Other")
        key = _document_key(row)
        # A booking delivery is billed on the *booking*, not on the zero-priced
        # hand-over document, so settlement (advance applied, balance owed)
        # comes from the booking obligation whenever one exists.  Everything
        # else settles against its own bill, with the booking as a fallback for
        # legacy rows that carry a booking reference but no bill of their own.
        if row.get("booking_id"):
            booking_key = ("Booking", int(row["booking_id"]))
            if row.get("entry_type") == "booking_supply" or (key not in payments):
                if booking_key in payments:
                    key = booking_key
        info = payments.get(key) if key else None
        if info is None and row.get("booking_id"):
            booking_key = ("Booking", int(row["booking_id"]))
            if booking_key in payments:
                key = booking_key
                info = payments[booking_key]
        if info:
            row["payment_status_key"] = info["status"]
            row["payment_status_label"] = info["status_label"]
            row["document_gross"] = info["gross"]
            row["document_collected"] = info["collected"]
            row["document_outstanding"] = info["remaining"]
        else:
            row["payment_status_key"] = PAYMENT_NA
            row["payment_status_label"] = PAYMENT_LABELS[PAYMENT_NA]
            row["document_gross"] = 0.0
            row["document_collected"] = 0.0
            row["document_outstanding"] = 0.0
        client_obj = context.client_by_name.get(_norm(row.get("client")))
        row["client_category"] = (getattr(client_obj, "category", "") or "") if client_obj else ""
        booking_id = row.get("booking_id")
        row["booking_status_key"] = bookings.get(booking_id, "") if booking_id else ""
        row["booking_status_label"] = BOOKING_LABELS.get(row["booking_status_key"], "")
        row["document_key"] = key
    context.settlement = payments
    context.booking_status = bookings
    return payments


def _group_rows(rows, key_fn):
    grouped = defaultdict(list)
    for row in rows:
        key = key_fn(row)
        if key in (None, ""):
            continue
        grouped[key].append(row)
    return grouped


def _bucket():
    return {
        "revenue": 0.0, "material_revenue": 0.0, "service_revenue": 0.0,
        "cogs": 0.0, "material_cost": 0.0, "service_cost": 0.0,
        "expenses": 0.0, "other_income": 0.0, "other_gain": 0.0, "other_loss": 0.0,
        "profit": 0.0, "qty": 0.0, "rows": 0,
    }


def _add(bucket, row):
    bucket["revenue"] += _f(row.get("revenue"))
    bucket["material_revenue"] += _f(row.get("material_revenue"))
    bucket["service_revenue"] += _f(row.get("delivery_revenue"))
    bucket["cogs"] += _f(row.get("cogs"))
    bucket["material_cost"] += _f(row.get("material_cost"))
    bucket["service_cost"] += _f(row.get("delivery_cost"))
    bucket["expenses"] += _f(row.get("expense"))
    bucket["other_income"] += _f(row.get("other_income"))
    bucket["other_gain"] += _f(row.get("other_gain"))
    bucket["other_loss"] += _f(row.get("other_loss"))
    bucket["profit"] += _f(row.get("profit"))
    bucket["qty"] += _f(row.get("qty"))
    bucket["rows"] += 1
    return bucket


def _margin(profit, revenue):
    revenue = _f(revenue)
    return round(profit / revenue * 100.0, 2) if abs(revenue) > EPS else 0.0


def _money_rows(rows):
    return [row for row in rows if row.get("include_in_net")]


def build_breakdown(context, rows, *, start_date=None, end_date=None, filters=None,
                    inventory=None, include_inventory=True):
    """Build the enhanced breakdown view model from the authoritative rows."""
    filters = filters or {}
    included = _money_rows(rows)
    excluded = [row for row in rows if not row.get("include_in_net")]

    goods_sales = 0.0
    goods_cost = 0.0
    services_revenue = 0.0
    services_cost = 0.0
    other_income = 0.0
    other_gains = 0.0
    other_losses = 0.0
    expenses = 0.0
    for row in included:
        group = row.get("transaction_group") or row_group(row)
        if group in {GROUP_GOODS, GROUP_RETURNS}:
            goods_sales += _f(row.get("material_revenue"))
            goods_cost += _f(row.get("material_cost"))
        if group in {GROUP_SERVICES, GROUP_RETURNS}:
            services_revenue += _f(row.get("delivery_revenue"))
            services_cost += _f(row.get("delivery_cost"))
        other_income += _f(row.get("other_income"))
        other_gains += _f(row.get("other_gain"))
        other_losses += _f(row.get("other_loss"))
        expenses += _f(row.get("expense"))

    gross_profit = goods_sales - goods_cost
    service_margin = services_revenue - services_cost
    net_profit = gross_profit + service_margin + other_income + other_gains - expenses - other_losses

    # ---- settlement (credit) at document level -------------------------
    documents = {}
    for row in included:
        key = row.get("document_key")
        if not key:
            continue
        bucket = documents.setdefault(key, {
            "document_type": key[0], "document_id": key[1],
            "reference": row.get("reference") or "",
            "date": row.get("date"), "client": row.get("client") or "",
            "client_code": row.get("client_code") or "",
            "booking_id": row.get("booking_id"),
            "revenue": 0.0, "cost": 0.0, "profit": 0.0,
            "payment_status_key": row.get("payment_status_key") or PAYMENT_NA,
            "payment_status_label": row.get("payment_status_label") or "",
            "booking_status_key": row.get("booking_status_key") or "",
            "booking_status_label": row.get("booking_status_label") or "",
            "document_gross": _f(row.get("document_gross")),
            "document_collected": _f(row.get("document_collected")),
            "document_outstanding": _f(row.get("document_outstanding")),
            "entry_types": set(),
        })
        bucket["revenue"] += _f(row.get("revenue"))
        bucket["cost"] += _f(row.get("cogs"))
        bucket["profit"] += _f(row.get("profit"))
        bucket["entry_types"].add(row.get("entry_type"))
    credit_outstanding = 0.0
    credit_collected = 0.0
    credit_documents = 0
    for bucket in documents.values():
        if bucket["payment_status_key"] == PAYMENT_NA:
            continue
        bucket["entry_types"] = sorted(bucket["entry_types"])
        # Only the unpaid part of revenue actually recognized here is a
        # receivable for this period; the rest of a booking balance relates to
        # goods that have not been delivered (an order balance, not a sale).
        recognized = max(0.0, _f(bucket["revenue"]))
        bucket["period_outstanding"] = min(max(0.0, bucket["document_outstanding"]), recognized) if recognized else 0.0
        if bucket["document_outstanding"] > 0.005:
            credit_outstanding += bucket["period_outstanding"]
            credit_documents += 1
        credit_collected += 0.0
    document_rows = sorted(
        documents.values(),
        key=lambda row: (_day(row.get("date")) or date.min, row["document_id"]),
        reverse=True,
    )

    # Collected against documents whose revenue is recognized in the period.
    collected_on_recognized = sum(
        _f(row.get("document_collected")) for row in documents.values()
        if row["payment_status_key"] != PAYMENT_NA
    )
    # Document-level collection dedupes rows that repeat the same bill.
    credit_collected = collected_on_recognized

    advances = 0.0
    advance_rows = []
    seen_bookings = set()
    for row in getattr(context, "booking_outstanding", []):
        if row["booking_id"] in seen_bookings:
            # One booking can have several open lines; its advance is a
            # booking-level balance and must be counted once.
            continue
        seen_bookings.add(row["booking_id"])
        estimate = _f(context.booking_advance_estimate.get(row["booking_id"], 0.0))
        if estimate <= EPS:
            continue
        advances += estimate
        advance_rows.append({
            "reference": row["reference"], "client": row["client"],
            "material": row["material"], "remaining_qty": _f(row["remaining_qty"]),
            "outstanding_value": _f(row["outstanding_value"]),
            "advance_value": estimate, "date": row["date"],
            "booking_id": row["booking_id"],
        })
    advance_rows.sort(key=lambda row: row["advance_value"], reverse=True)

    # Estimated profit on undelivered bookings (deliberately separate).
    estimated_rows = []
    estimated_total = 0.0
    today = pk_today()
    for row in getattr(context, "booking_outstanding", []):
        qty = _f(row["remaining_qty"])
        if qty <= EPS:
            continue
        rate = _f(row["rate"])
        cost_rate, basis, _sources = context.fallback_cost(row["material"], today)
        # No cost evidence means no estimate: assuming zero cost would invent a
        # 100% margin on an undelivered booking.
        known_cost = cost_rate > EPS
        est_profit = qty * (rate - cost_rate) if known_cost else 0.0
        if known_cost:
            estimated_total += est_profit
        estimated_rows.append({
            "reference": row["reference"], "client": row["client"], "material": row["material"],
            "remaining_qty": qty, "rate": rate, "estimated_cost_rate": cost_rate,
            "estimated_profit": est_profit, "cost_known": known_cost,
            "cost_basis": basis, "outstanding_value": _f(row["outstanding_value"]),
        })
    estimated_rows.sort(
        key=lambda row: (row["cost_known"], row["estimated_profit"]), reverse=True
    )

    # ---- breakdown tables ----------------------------------------------
    by_item = defaultdict(lambda: {**_bucket(), "material": "", "grn_ids": set()})
    by_service = defaultdict(lambda: {**_bucket(), "service": ""})
    by_expense = defaultdict(lambda: {**_bucket(), "category": ""})
    by_customer = defaultdict(lambda: {**_bucket(), "client": "", "client_code": "", "client_category": ""})
    by_day = defaultdict(lambda: {**_bucket(), "day": ""})
    for row in included:
        group = row.get("transaction_group") or row_group(row)
        if group in {GROUP_GOODS, GROUP_RETURNS} and (
            abs(_f(row.get("material_revenue"))) > EPS or abs(_f(row.get("material_cost"))) > EPS
        ):
            key = row.get("material") or "-"
            bucket = by_item[key]
            bucket["material"] = key
            _add(bucket, row)
            bucket["grn_ids"].update(row.get("grn_ids") or [])
        if group in {GROUP_SERVICES} and (
            abs(_f(row.get("delivery_revenue"))) > EPS or abs(_f(row.get("delivery_cost"))) > EPS
        ):
            key = row.get("material") or "Delivery"
            bucket = by_service[key]
            bucket["service"] = key
            _add(bucket, row)
        if abs(_f(row.get("expense"))) > EPS:
            key = row.get("subcategory") or row.get("category") or "Other expense"
            bucket = by_expense[key]
            bucket["category"] = key
            _add(bucket, row)
        if group in {GROUP_GOODS, GROUP_SERVICES, GROUP_RETURNS}:
            key = (row.get("client") or "Unspecified", row.get("client_code") or "")
            bucket = by_customer[key]
            bucket["client"], bucket["client_code"] = key
            bucket["client_category"] = bucket["client_category"] or row.get("client_category", "")
            _add(bucket, row)
        day = _day(row.get("date"))
        day_key = day.isoformat() if day else "Undated"
        bucket = by_day[day_key]
        bucket["day"] = day_key
        _add(bucket, row)

    def _finalise(bucket, *, profit_key="profit"):
        bucket["margin_pct"] = _margin(bucket[profit_key], bucket["revenue"])
        bucket["cost"] = bucket["cogs"]
        return bucket

    item_rows = sorted(
        (_finalise(bucket) for bucket in by_item.values()),
        key=lambda row: row["profit"], reverse=True,
    )
    for row in item_rows:
        row["grn_ids"] = sorted(row["grn_ids"])
        row["gross_profit"] = row["material_revenue"] - row["material_cost"]
        row["margin_pct"] = _margin(row["gross_profit"], row["material_revenue"])
        row["remaining_inventory_qty"] = 0.0
        row["remaining_inventory_value"] = 0.0

    if inventory:
        for row in item_rows:
            material_row = next(
                (m for m in inventory.get("by_material", [])
                 if _norm(m["material"]) == _norm(row["material"])),
                None,
            )
            if material_row:
                row["remaining_inventory_qty"] = material_row["remaining_qty"]
                row["remaining_inventory_value"] = material_row["remaining_value"]

    service_rows = sorted(
        (_finalise(bucket) for bucket in by_service.values()),
        key=lambda row: row["service_cost"], reverse=True,
    )
    for row in service_rows:
        row["margin"] = row["service_revenue"] - row["service_cost"]
        row["margin_pct"] = _margin(row["margin"], row["service_revenue"])

    expense_rows = sorted(
        (_finalise(bucket) for bucket in by_expense.values()),
        key=lambda row: row["expenses"], reverse=True,
    )
    customer_rows = sorted(
        (_finalise(bucket) for bucket in by_customer.values()),
        key=lambda row: row["profit"], reverse=True,
    )
    day_rows = sorted((_finalise(bucket) for bucket in by_day.values()), key=lambda row: row["day"])

    direct_service_cost_total = services_cost
    delivery_cost_rows = [
        {
            "reference": row.get("reference"), "date": row.get("date"),
            "client": row.get("client"), "material": row.get("material"),
            "qty": _f(row.get("qty")), "cost": _f(row.get("delivery_cost")),
            "revenue": _f(row.get("delivery_revenue")), "driver": row.get("account") or "",
            "cost_basis": row.get("cost_basis") or "", "document_type": row.get("document_type"),
            "document_id": row.get("document_id"),
        }
        for row in included if _f(row.get("delivery_cost")) or row.get("entry_type") == "delivery_cost"
    ]
    delivery_cost_rows.sort(key=lambda row: _day(row.get("date")) or date.min, reverse=True)

    other_direct_cost_total = sum(
        _f(row.get("other_loss")) for row in included if row.get("entry_type") != "expense"
    )

    chain_total = (
        gross_profit + service_margin + other_income + other_gains - expenses - other_losses
    )
    report_profit = sum(_f(row.get("profit")) for row in included)
    reconcile = {
        "goods_sales": goods_sales,
        "goods_cost": goods_cost,
        "gross_profit": gross_profit,
        "service_revenue": services_revenue,
        "service_cost": services_cost,
        "service_margin": service_margin,
        "other_income": other_income,
        "other_gains": other_gains,
        "other_losses": other_losses,
        "operating_expenses": expenses,
        "net_profit": net_profit,
        "report_profit": report_profit,
        "difference": net_profit - report_profit,
        "matches": abs(net_profit - report_profit) < 0.01,
    }

    cards = [
        {
            "key": "delivered_goods_sales", "label": "Delivered Goods Sales",
            "value": goods_sales, "icon": "bi-bag-check", "colour": "sky",
            "sub": "Revenue recognized on goods actually delivered (net of returns)",
            "realized": True,
        },
        {
            "key": "earned_service_revenue", "label": "Earned Service Revenue",
            "value": services_revenue, "icon": "bi-truck", "colour": "teal",
            "sub": "Delivery / service charges earned on the same deliveries",
            "realized": True,
        },
        {
            "key": "cogs_fifo", "label": "Cost of Goods Sold (FIFO)",
            "value": goods_cost, "icon": "bi-box-seam", "colour": "amber",
            "sub": "Actual FIFO cost of the lots dispatched (oldest GRN lot first)",
            "realized": True,
        },
        {
            "key": "direct_service_costs", "label": "Direct Service Costs",
            "value": services_cost, "icon": "bi-person-badge", "colour": "rose",
            "sub": "Delivery-person / rent cost recorded against those services",
            "realized": True,
        },
        {
            "key": "gross_profit", "label": "Gross Profit",
            "value": gross_profit, "icon": "bi-graph-up-arrow", "colour": "green",
            "sub": "Delivered goods sales − FIFO cost of goods sold",
            "realized": True, "primary": True,
        },
        {
            "key": "service_margin", "label": "Service Margin",
            "value": service_margin, "icon": "bi-signpost-split", "colour": "violet",
            "sub": "Earned service revenue − direct service costs",
            "realized": True,
        },
        {
            "key": "operating_expenses", "label": "Operating Expenses",
            "value": expenses, "icon": "bi-receipt", "colour": "slate",
            "sub": "Salaries, rent, utilities and other recorded expense categories",
            "realized": True,
        },
        {
            "key": "net_profit", "label": "Net Profit / Loss",
            "value": net_profit, "icon": "bi-cash-coin",
            "colour": "green" if net_profit >= 0 else "red",
            "sub": "Gross profit + service margin + other income − expenses − other losses",
            "realized": True, "hero": True,
        },
        {
            "key": "credit_outstanding", "label": "Credit / Outstanding Receivables",
            "value": credit_outstanding, "icon": "bi-hourglass-split", "colour": "amber",
            "sub": f"Unpaid balance on this period's delivered revenue ({credit_documents} bills)",
            "realized": False, "position": True,
        },
        {
            "key": "customer_advances", "label": "Customer Advances",
            "value": advances, "icon": "bi-piggy-bank", "colour": "indigo",
            "sub": "Money received for goods not yet delivered — not profit",
            "realized": False, "position": True,
        },
        {
            "key": "estimated_undelivered_profit", "label": "Estimated Profit on Undelivered Bookings",
            "value": estimated_total, "icon": "bi-calculator", "colour": "slate",
            "sub": "Estimate only — remaining booking qty × (booked price − latest GRN cost)",
            "realized": False, "estimate": True,
        },
    ]

    grn_section = None
    if include_inventory:
        grn_section = _grn_section(context, inventory, start_date=start_date, end_date=end_date, filters=filters)

    payment_summary = {
        "collected_on_recognized_documents": credit_collected,
        "outstanding_on_recognized_documents": credit_outstanding,
        "document_balance_total": sum(
            max(0.0, _f(row.get("document_outstanding"))) for row in documents.values()
            if row["payment_status_key"] != PAYMENT_NA
        ),
        "documents_with_balance": credit_documents,
        "document_rows": [
            row for row in document_rows if row["payment_status_key"] != PAYMENT_NA
        ][:200],
        # The largest balances first: capping a date-ordered list would hide the
        # biggest receivable behind newer small ones.
        "top_credit_rows": sorted(
            (
                row for row in document_rows
                if row["payment_status_key"] != PAYMENT_NA and row["document_outstanding"] > 0.005
            ),
            key=lambda row: row["document_outstanding"],
            reverse=True,
        )[:40],
        "advance_rows": advance_rows[:100],
        "advance_total": advances,
        "note": (
            "Collection and outstanding figures come from the client ledger's own bill allocation. "
            "Receiving cash does not create revenue, and an unpaid bill does not reduce profit."
        ),
    }

    notes = [
        "Revenue and cost are recognized when goods are delivered and services earned, on the delivery date — "
        "never on the booking date, the GRN date or the payment date.",
        "Credit sales count in full; the unpaid part is shown as a receivable and never subtracted from profit.",
        "Customer advances and unfulfilled bookings are excluded from net profit; their estimated margin is shown "
        "separately and is not added to any profit figure.",
        "Purchases and supplier payments are inventory/settlement movements: their cost enters profit only through "
        "the FIFO cost of goods actually delivered.",
        "Returns reverse the original revenue and the original FIFO cost on the return date.",
    ]
    if not context.client_by_code:
        notes.append("No company-site entity exists in this application; the customer selector covers clients.")

    return {
        "cards": cards,
        "card_index": {card["key"]: card for card in cards},
        "by_item": item_rows,
        "by_service": service_rows,
        "by_expense_category": expense_rows,
        "by_customer": customer_rows,
        "by_day": day_rows,
        "documents": document_rows,
        "delivery_cost_rows": delivery_cost_rows,
        "direct_service_cost_total": direct_service_cost_total,
        "other_direct_cost_total": other_direct_cost_total,
        "payment_summary": payment_summary,
        "advances": {row["booking_id"]: row["advance_value"] for row in advance_rows},
        "estimated_undelivered": {
            "total": estimated_total,
            "unknown_cost_rows": sum(1 for row in estimated_rows if not row["cost_known"]),
            "rows": estimated_rows[:100],
            "note": (
                "Estimated future margin on undelivered booking quantity, priced at the currently stored booking "
                "rate against the latest recorded GRN cost. It is excluded from realized gross profit and net profit."
            ),
        },
        "grn_section": grn_section,
        "reconcile": reconcile,
        "notes": notes,
        "unresolved_rows": len(excluded),
        "unresolved_revenue": sum(max(0.0, _f(row.get("revenue"))) for row in excluded),
        "chain_total": chain_total,
    }


def _grn_section(context, inventory, *, start_date=None, end_date=None, filters=None):
    """Purchases in the period and remaining inventory valued at GRN FIFO cost."""
    filters = filters or {}
    material_filter = _norm(filters.get("material"))
    supplier_filter = _norm(filters.get("supplier"))
    grn_filter = filters.get("grn_id")
    try:
        grn_filter = int(grn_filter) if grn_filter not in (None, "") else None
    except (TypeError, ValueError):
        grn_filter = None
    purchase_rows = []
    for grn in getattr(context, "grns", []):
        if bool(getattr(grn, "is_void", False)):
            continue
        if grn_filter and grn.id != grn_filter:
            continue
        if not _in_period(grn.date_posted, start_date, end_date):
            continue
        if supplier_filter and supplier_filter not in _norm(grn.supplier):
            continue
        for item in grn.items:
            if bool(getattr(item, "is_void", False)):
                continue
            if material_filter and material_filter not in _norm(item.mat_name):
                continue
            purchase_rows.append({
                "date": grn.date_posted,
                "reference": (grn.manual_bill_no or grn.auto_bill_no or f"GRN-{grn.id}"),
                "grn_id": grn.id,
                "supplier": grn.supplier or "",
                "material": item.mat_name,
                "qty": _f(item.qty),
                "rate": _f(item.price_at_time),
                "amount": _f(item.qty) * _f(item.price_at_time),
                "consumption": (inventory or {}).get("items", {}).get(item.id),
            })
    purchase_rows.sort(key=lambda row: row["date"] or datetime.min, reverse=True)
    inventory = inventory or {}
    return {
        "purchase_rows": purchase_rows,
        "purchase_value": sum(row["amount"] for row in purchase_rows),
        "purchase_qty": sum(row["qty"] for row in purchase_rows),
        "purchase_count": len(purchase_rows),
        "inventory": inventory.get("inventory", {}),
        "inventory_by_material": inventory.get("by_material", []),
        "lot_count": inventory.get("inventory", {}).get("lot_count", 0),
        "reconstructed_lot_count": inventory.get("reconstructed_lot_count", 0),
        "recorded_lot_count": inventory.get("recorded_lot_count", 0),
        "warnings": inventory.get("warnings", [])[:20],
        "note": (
            "Purchases received in the period are inventory, not an expense. Remaining inventory is valued at each "
            "GRN lot's own recorded cost (never an average), and is shown outside the profit figures."
        ),
    }


def _in_period(value, start_date=None, end_date=None):
    day = _day(value)
    if day is None:
        return False
    if start_date and day < start_date:
        return False
    if end_date and day > end_date:
        return False
    return True


def build_inventory_view(context=None):
    """Inventory valuation shared by the report page and the GRN page."""
    return build_grn_consumption(fifo_context=context)
