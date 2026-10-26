"""Authoritative, read-only Profit & Loss projection.

The operational tables remain the source of truth.  This module turns them into
one dated set of report rows; the dashboard, transaction explorer, detail page
and exports all consume those same rows.  Nothing in this module writes to the
database.

Recognition rules
-----------------
* Cash and credit sales are earned on the sale date, not when settled.
* A booking is an order/advance, not revenue.  Revenue and cost are recognized
  only for quantities actually supplied against a BookingAllocation (or a
  conservatively matched legacy dispatch Entry).
* Material cost is FIFO: every dispatched quantity is valued from the oldest
  open GRN lot at that delivery date (lot date, then GRN id, then lot id), and
  the same lot queue is consumed once across sales, booking supplies and
  legacy dispatches.  A lot is available from the calendar day its GRN is
  posted — the same rule posting-time FIFO uses.  Recorded GRNAllocations stay
  authoritative for their own rows; every unrecorded unit is costed from the
  exact lot the replay consumes, at that lot's own rate.  A frozen line rate is
  used only for quantity no real lot can cover, and is labelled an estimate
  when it was only a posting-time placeholder.
* GRN/frozen costs are recorded costs.  Historical/master fallbacks are clearly
  labelled estimates.  When a delivery predates every purchase, the estimate
  comes from the NEAREST later lot (the head of the FIFO queue) — never from
  the newest purchase in the dataset.  A row with no defensible cost is
  visible, but excluded from net profit instead of silently treating its cost
  as zero.
* Receipts, supplier payments and driver payments are settlements.  Purchases
  are inventory.  Neither is posted again as P&L activity.
* Returns are dated reversals linked to the original supplied quantity where
  possible.  Discounts, waives, expenses and reconciliation differences are
  separate events and are never mirrored into a second P&L row.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from functools import lru_cache
import calendar
import json
import re

from sqlalchemy import or_
from sqlalchemy.orm import selectinload

from models import (
    AccountTransaction,
    AccountingAuditLog,
    AuditLog,
    Booking,
    BookingAllocation,
    BookingAllocationRepairArchive,
    BookingItem,
    CashFlowEntry,
    Client,
    DeliveryPersonPayment,
    DeliveryRent,
    DirectSale,
    DirectSaleItem,
    Entry,
    FbmCashDrawerEntry,
    GRN,
    GRNAllocation,
    GRNItem,
    Material,
    MaterialReturn,
    Payment,
    PendingBill,
    SaleDeliveryPerson,
    Service,
    Supplier,
    WaiveOff,
    db,
)
from app.services.financial_ledgers import (
    build_client_financial_ledger,
    build_current_payables,
    build_supplier_payable_overview,
)
from app.services.profit_loss_breakdown import (
    BOOKING_STATUS_CHOICES,
    PAYMENT_STATUS_CHOICES,
    TRANSACTION_GROUP_CHOICES,
    attach_document_status,
    build_breakdown,
)
from app.services.grn_consumption import build_grn_consumption
from app.services.sales_core import _direct_sale_default_bill_ref, normalize_sale_category
from app.services.time_money import pk_now, pk_today

EPS = 0.000001
COST_ACTUAL = "actual"
COST_ESTIMATED = "estimated"
COST_MISSING = "missing"
COST_NA = "not_applicable"


def _f(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _norm(value):
    return " ".join(str(value or "").strip().casefold().split())


@lru_cache(maxsize=16384)
def _mat_key(value):
    """Normalised material identity.

    Pure function of a string, and by far the hottest helper in the projection
    (it is called for every lot/candidate comparison).  Memoising it is a pure
    performance change: same input, same output, no state.
    """
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().casefold())


def _active(obj):
    return not bool(getattr(obj, "is_void", False))


def _as_dt(value, time_value=None):
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    raw = str(value or "").strip()
    if not raw:
        return None
    if time_value:
        raw = f"{raw} {str(time_value).strip()}"
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
        "%Y-%m-%d", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y",
        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y",
    ):
        try:
            return datetime.strptime(raw, fmt)
        except (TypeError, ValueError):
            continue
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def _day(value):
    dt = _as_dt(value)
    return dt.date() if dt else None


def _lot_available(lot_date, tx_dt):
    """A lot exists for a delivery once its GRN is posted that day.

    Posting-time FIFO compares calendar dates (``GRN.date_posted.date()``
    ``> sale_date``), so a GRN posted at 18:00 is consumed by a sale issued at
    10:00 on the same day.  Comparing full datetimes here would silently skip
    that lot, hand the issue to an older lot or a "later GRN" estimate, and
    disagree with the recorded allocation.
    """
    if not lot_date or not tx_dt:
        return True
    return lot_date.date() <= tx_dt.date()


def _ref(obj, prefix):
    if obj is None:
        return ""
    return str(
        getattr(obj, "manual_bill_no", None)
        or getattr(obj, "bill_no", None)
        or getattr(obj, "auto_bill_no", None)
        or f"{prefix}-{obj.id}"
    )


def _booking_ref(booking):
    return _ref(booking, "BK")


def _sale_ref(sale):
    try:
        return _direct_sale_default_bill_ref(sale)
    except Exception:
        return _ref(sale, "DS")


def _return_ref(ret):
    return _ref(ret, "RTN")


def _grn_ref(grn):
    return _ref(grn, "GRN")


def _date_in(value, start_date=None, end_date=None):
    dt = _as_dt(value)
    if dt is None:
        return start_date is None and end_date is None
    day = dt.date()
    return (start_date is None or day >= start_date) and (end_date is None or day <= end_date)


def _add_months(value, months):
    month_index = value.year * 12 + value.month - 1 + months
    year, month0 = divmod(month_index, 12)
    month = month0 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(value.day, last_day))


def resolve_report_period(period="this_month", start_date=None, end_date=None, today=None):
    """Resolve presets to inclusive dates; custom dates remain inclusive."""
    today = today or pk_today()
    period = (period or "custom").strip().lower()
    if period in {"today", "day"}:
        return today, today, "Today"
    if period == "yesterday":
        value = today - timedelta(days=1)
        return value, value, "Yesterday"
    if period in {"this_week", "week"}:
        return today - timedelta(days=today.weekday()), today, "This week to date"
    if period in {"this_month", "month"}:
        return today.replace(day=1), today, "This month to date"
    if period == "last_month":
        start = _add_months(today.replace(day=1), -1)
        return start, date(start.year, start.month, calendar.monthrange(start.year, start.month)[1]), "Last full month"
    if period in {"this_year", "year"}:
        return date(today.year, 1, 1), today, "This year to date"
    if period == "last_year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), "Last full year"
    if period in {"last_7_days", "7d"}:
        return today - timedelta(days=6), today, "Last 7 days"
    if period in {"last_30_days", "30d"}:
        return today - timedelta(days=29), today, "Last 30 days"
    if period in {"all", "all_time"}:
        return None, None, "All dates"
    if isinstance(start_date, str):
        start_date = _day(start_date)
    if isinstance(end_date, str):
        end_date = _day(end_date)
    if start_date and end_date and start_date > end_date:
        start_date, end_date = end_date, start_date
    return start_date, end_date, "Custom period"


def previous_equivalent_period(start_date, end_date):
    """Return a prior period with the same calendar/day-span semantics.

    Month-to-date compares the same day span in the preceding month.  A full
    calendar month compares the full preceding month.  Other bounded ranges
    compare an equally long immediately preceding range.
    """
    if not start_date or not end_date:
        return None, None, "No prior comparison for an open-ended period"
    if start_date.day == 1:
        prev_start = _add_months(start_date, -1)
        current_month_end = date(start_date.year, start_date.month, calendar.monthrange(start_date.year, start_date.month)[1])
        if end_date == current_month_end:
            prev_end = date(prev_start.year, prev_start.month, calendar.monthrange(prev_start.year, prev_start.month)[1])
            label = "Previous full month"
        else:
            prev_end = min(
                _add_months(end_date, -1),
                date(prev_start.year, prev_start.month, calendar.monthrange(prev_start.year, prev_start.month)[1]),
            )
            label = "Same day span in previous month"
        return prev_start, prev_end, label
    duration = (end_date - start_date).days
    prev_end = start_date - timedelta(days=1)
    prev_start = prev_end - timedelta(days=duration)
    return prev_start, prev_end, "Immediately preceding equivalent period"


def _base_row(**kwargs):
    row = {
        "date": None,
        "source": "",
        "source_label": "",
        "transaction_type": "",
        "entry_type": "other",
        "category": "Other",
        "subcategory": "",
        "reference": "",
        "document_type": None,
        "document_id": None,
        "source_type": None,
        "source_id": None,
        "client": "",
        "client_code": "",
        "account": "",
        "supplier": "",
        "material": "-",
        "booked_material": "",
        "qty": 0.0,
        "sale_rate": 0.0,
        "cost_rate": 0.0,
        "gross_revenue": 0.0,
        "discount": 0.0,
        "material_revenue": 0.0,
        "delivery_revenue": 0.0,
        "other_income": 0.0,
        "material_cost": 0.0,
        "delivery_cost": 0.0,
        "expense": 0.0,
        "other_gain": 0.0,
        "other_loss": 0.0,
        "cost_status": COST_NA,
        "cost_basis": "No product cost applicable",
        "cost_sources": [],
        "cost_amount_recorded": 0.0,
        "grn_ids": [],
        "include_in_net": True,
        "is_void": False,
        "note": "",
        "payment_status": "Not a settlement row",
        "booking_id": None,
        "booking_item_id": None,
        "sale_id": None,
        "entry_id": None,
        "return_id": None,
        "original_sources": [],
        "warnings": [],
    }
    row.update(kwargs)
    signed_revenue = row["material_revenue"] + row["delivery_revenue"] + row["other_income"]
    signed_cost = row["material_cost"] + row["delivery_cost"]
    calculated_profit = signed_revenue - signed_cost - row["expense"] - row["other_loss"] + row["other_gain"]
    row["revenue"] = signed_revenue
    row["cogs"] = signed_cost if row["include_in_net"] else 0.0
    row["profit"] = calculated_profit if row["include_in_net"] else 0.0
    row["uncounted_profit_before_cost"] = calculated_profit if not row["include_in_net"] else 0.0
    row["discount_loss"] = row["discount"] + row["other_loss"]
    row["cogs_known"] = row["cost_status"] != COST_MISSING
    row["is_loss"] = bool(row["include_in_net"] and calculated_profit < -EPS)
    return row


def _service_classifier():
    names = set()
    for service in Service.query.all():
        names.add(_mat_key(service.name))
        names.add(_mat_key(service.code))
    names.discard("")

    def is_service(name):
        key = _mat_key(name)
        return bool(key and (key in names or "rent" in str(name or "").casefold()))

    return is_service


class _Projection:
    def __init__(self, *, exclude_sale_id=None, through_date=None):
        """Build the FIFO/cost projection.

        ``exclude_sale_id`` and ``through_date`` are used only by sale posting
        when it asks for the lots available on a delivery date.  Normal reports
        replay the complete active ledger.  Sale posting must use this same
        replay rather than a private ``GRNAllocation``-only query, otherwise
        booking/legacy dispatches can leave a lot looking open after the P&L
        has already consumed it.
        """
        self.exclude_sale_id = int(exclude_sale_id) if exclude_sale_id else None
        if isinstance(through_date, datetime):
            through_date = through_date.date()
        self.through_date = through_date
        self.rows = []
        self.warnings = []
        self.candidates = []
        self.delivery_components = defaultdict(list)
        self.booking_remaining = {}
        self.booking_original_qty = {}
        self.booking_recognized_revenue = defaultdict(float)
        self.booking_unfulfilled_value = defaultdict(float)
        self.booking_advance_estimate = defaultdict(float)
        self.booking_outstanding = []
        self.voided_source_count = 0
        self.clients = Client.query.order_by(Client.name.asc()).all()
        self.client_by_name = {_norm(c.name): c for c in self.clients}
        self.client_by_code = {_norm(c.code): c for c in self.clients}
        self.materials = Material.query.order_by(Material.name.asc()).all()
        self.is_service = _service_classifier()
        sales_query = DirectSale.query.options(
            selectinload(DirectSale.items), selectinload(DirectSale.invoice)
        )
        if self.exclude_sale_id:
            sales_query = sales_query.filter(DirectSale.id != self.exclude_sale_id)
        self.sales = sales_query.order_by(
            DirectSale.date_posted.asc(), DirectSale.id.asc()
        ).all()
        self.sale_by_id = {s.id: s for s in self.sales}
        self.sale_item_by_id = {item.id: item for sale in self.sales for item in sale.items}
        self.bookings = Booking.query.options(selectinload(Booking.items)).order_by(
            Booking.date_posted.asc(), Booking.id.asc()
        ).all()
        self.booking_by_id = {b.id: b for b in self.bookings}
        self.booking_item_by_id = {item.id: item for booking in self.bookings for item in booking.items}
        self.grns = GRN.query.options(selectinload(GRN.items)).order_by(
            GRN.date_posted.asc(), GRN.id.asc()
        ).all()
        self.grn_item_by_id = {item.id: item for grn in self.grns for item in grn.items}
        self.grn_by_id = {grn.id: grn for grn in self.grns}
        self.entries = Entry.query.order_by(Entry.created_at.asc(), Entry.id.asc()).all()
        self.returns = MaterialReturn.query.options(selectinload(MaterialReturn.items)).order_by(
            MaterialReturn.date_posted.asc(), MaterialReturn.id.asc()
        ).all()
        self.payments = Payment.query.order_by(Payment.date_posted.asc(), Payment.id.asc()).all()
        self.pending_bills = PendingBill.query.all()
        self.delivery_rents = DeliveryRent.query.order_by(DeliveryRent.id.asc()).all()
        self.delivery_allocations = SaleDeliveryPerson.query.order_by(SaleDeliveryPerson.id.asc()).all()
        self.material_by_key = {_mat_key(m.name): m for m in self.materials}
        self.purchase_rates = defaultdict(list)
        for grn in self.grns:
            if not _active(grn):
                continue
            for item in grn.items:
                if not _active(item) or _f(item.price_at_time) <= 0:
                    continue
                self.purchase_rates[_mat_key(item.mat_name)].append((_as_dt(grn.date_posted), _f(item.price_at_time), item, grn))
        for values in self.purchase_rates.values():
            values.sort(key=lambda value: (value[0] or datetime.min, value[2].id), reverse=True)
        self.grn_allocations_by_item = defaultdict(list)
        grn_alloc_query = GRNAllocation.query
        booking_alloc_query = BookingAllocation.query
        if self.exclude_sale_id:
            grn_alloc_query = grn_alloc_query.filter(GRNAllocation.sale_id != self.exclude_sale_id)
            booking_alloc_query = booking_alloc_query.filter(BookingAllocation.sale_id != self.exclude_sale_id)
        for alloc in grn_alloc_query.order_by(GRNAllocation.id.asc()).all():
            self.grn_allocations_by_item[alloc.sale_item_id].append(alloc)
        self.booking_allocations = booking_alloc_query.order_by(BookingAllocation.id.asc()).all()
        self.booking_allocations_by_sale_item = defaultdict(list)
        self.booking_sale_ids = set()
        for alloc in self.booking_allocations:
            self.booking_allocations_by_sale_item[alloc.sale_item_id].append(alloc)
            self.booking_sale_ids.add(alloc.sale_id)
        self.booking_gross = {}
        for booking in self.bookings:
            self.booking_gross[booking.id] = sum(
                _f(item.qty) * _f(item.price_at_time)
                for item in booking.items
            )
        self.sale_gross = {}
        for sale in self.sales:
            self.sale_gross[sale.id] = sum(
                self.chargeable_qty(sale, item) * _f(item.price_at_time)
                for item in sale.items
                if _f(item.price_at_time) > 0
            )
        self.pending_by_sale = defaultdict(list)
        for pending in self.pending_bills:
            if _active(pending) and pending.source_table == "direct_sale" and pending.source_id:
                self.pending_by_sale[pending.source_id].append(pending)
        self.rent_by_sale = defaultdict(list)
        for rent in self.delivery_rents:
            if _active(rent) and rent.sale_id:
                self.rent_by_sale[rent.sale_id].append(rent)
        self.driver_alloc_by_sale = defaultdict(list)
        for alloc in self.delivery_allocations:
            if _active(alloc):
                self.driver_alloc_by_sale[alloc.sale_id].append(alloc)
        self.voided_source_count = sum(1 for sale in self.sales if not _active(sale))
        self.voided_source_count += sum(1 for booking in self.bookings if not _active(booking))
        self.voided_source_count += sum(1 for ret in self.returns if not _active(ret))
        self.fifo_plan = {}
        # Final balance of every GRN lot after the replay.  Exposed read-only so
        # the GRN consumption indicator (app.services.grn_consumption) can be
        # proven to agree with the FIFO cost this report recognizes.
        self.grn_lot_balances = {}
        self._build_fifo_cost_plan()

    def available_fifo_lots(self, material_name, as_of_dt=None):
        """Open GRN lots on a supply date, from this projection's FIFO replay.

        Each returned dict carries the exact lot id and its own purchase rate;
        sale posting uses them in FIFO order and never blends two lot rates.
        """
        key = _mat_key(material_name)
        if not key:
            return []
        if isinstance(as_of_dt, datetime):
            as_of_dt = as_of_dt.date()
        rows = []
        for lot in self.grn_lot_balances.values():
            if _mat_key(lot.get("material")) != key:
                continue
            lot_date = _day(lot.get("date"))
            if as_of_dt and lot_date and lot_date > as_of_dt:
                continue
            remaining = max(0.0, _f(lot.get("remaining")))
            if remaining <= EPS:
                continue
            rows.append({
                "grn_item_id": lot["grn_item_id"],
                "grn_id": lot.get("grn_id"),
                "reference": lot.get("reference", ""),
                "supplier": lot.get("supplier", ""),
                "date": lot.get("date"),
                "available": remaining,
                "rate": max(0.0, _f(lot.get("rate"))),
            })
        rows.sort(key=lambda row: (
            row["date"] or datetime.min,
            row["grn_id"] or 0,
            row["grn_item_id"] or 0,
        ))
        return rows

    def client_info(self, name, code=None):
        client = self.client_by_code.get(_norm(code)) if code else None
        client = client or self.client_by_name.get(_norm(name))
        return (client.code if client else (code or "")), (client.name if client else (name or ""))

    def warn(self, code, message, *, document_type=None, document_id=None, reference="", amount=0.0, material=""):
        warning_date = None
        warning_client = ""
        warning_material = ""
        if document_type == "Entry":
            source = next((entry for entry in self.entries if entry.id == document_id), None)
            if source:
                warning_date = _as_dt(source.date, source.time)
                warning_client = source.client or ""
                warning_material = source.material or ""
        elif document_type == "DirectSale":
            source = self.sale_by_id.get(document_id)
            if source:
                warning_date = source.date_posted
                warning_client = source.client_name or ""
        elif document_type == "Booking":
            source = self.booking_by_id.get(document_id)
            if source:
                warning_date = source.date_posted
                warning_client = source.client_name or ""
        elif document_type == "MaterialReturn":
            source = next((ret for ret in self.returns if ret.id == document_id), None)
            if source:
                warning_date = source.date_posted
                warning_client = source.client_name or ""
        warning_material = material or warning_material
        self.warnings.append({
            "code": code,
            "message": message,
            "document_type": document_type,
            "document_id": document_id,
            "reference": reference,
            "amount": _f(amount),
            "date": warning_date,
            "client": warning_client,
            "material": warning_material,
        })

    def fallback_cost(self, material_name, tx_date):
        """Return (rate, basis, sources); every result here is an estimate."""
        key = _mat_key(material_name)
        values = self.purchase_rates.get(key, [])
        if not values:
            # Legacy names sometimes differ only by descriptive text.  Exact
            # normalized matching always wins; substring matching is estimate-only.
            values = [value for other_key, rows in self.purchase_rates.items() if key and (key in other_key or other_key in key) for value in rows]
            values.sort(key=lambda value: (value[0] or datetime.min, value[2].id), reverse=True)
        if values:
            chosen = None
            for value in values:
                if value[0] and tx_date and value[0].date() <= tx_date:
                    chosen = value
                    break
            if chosen is None:
                # Nothing was bought on/before the supply date.  FIFO values
                # the issue from the next lot in the queue, so the nearest
                # LATER GRN is the defensible estimate.  Taking values[0] here
                # used the newest purchase in the whole dataset (often months
                # after the delivery), which rewrote historical profit with a
                # future price and is not FIFO at all.
                if tx_date:
                    forward = [value for value in values if value[0] and value[0].date() > tx_date]
                else:
                    forward = []
                if forward:
                    chosen = min(forward, key=lambda value: (value[0], value[2].id))
                    basis = "Estimated from the nearest later GRN; no purchase existed on/before supply"
                else:
                    # No dates at all on either side: the oldest recorded
                    # purchase is still closer to FIFO than the newest one.
                    chosen = min(values, key=lambda value: (value[0] or datetime.min, value[2].id))
                    basis = "Estimated from the earliest GRN; date of supply is unknown"
            else:
                basis = "Estimated from latest GRN on/before supply; no frozen allocation"
            dt, rate, item, grn = chosen
            return rate, basis, [{
                "type": "GRNItem", "id": item.id, "grn_item_id": item.id,
                "lot_rate": rate, "grn_id": grn.id, "reference": _grn_ref(grn),
                "supplier": grn.supplier or "", "date": _as_dt(grn.date_posted), "rate": rate,
            }]
        material = self.material_by_key.get(key)
        if material is None:
            material = next((m for other, m in self.material_by_key.items() if key and (key in other or other in key)), None)
        if material is not None and _f(material.unit_price) > 0:
            return _f(material.unit_price), "Estimated from current Material master price; not a frozen historical cost", []
        return 0.0, "No frozen, allocated, historical GRN or Material master cost is available", []

    def _build_fifo_cost_plan(self):
        """Reconstruct missing historical costs from one global FIFO sequence.

        Recorded allocations/frozen rates remain authoritative for their own
        rows.  For unallocated quantities, all active GRN lots and all dated
        physical issues are walked once in chronological order.  The result is
        deliberately labelled an estimate: it is much stronger evidence than a
        latest/master-price shortcut, but it is not rewritten into a fake GRN
        allocation on the source row.
        """
        lots_by_material = defaultdict(list)
        lot_by_item = {}
        for grn in self.grns:
            if not _active(grn):
                continue
            for item in grn.items:
                if not _active(item):
                    continue
                lot = {
                    "grn_item_id": item.id, "grn_id": grn.id,
                    "date": _as_dt(grn.date_posted), "material": item.mat_name or "",
                    "remaining": max(0.0, _f(item.qty)),
                    "original_qty": max(0.0, _f(item.qty)),
                    "rate": max(0.0, _f(item.price_at_time)),
                    "reference": _grn_ref(grn), "supplier": grn.supplier or "",
                }
                lots_by_material[_mat_key(item.mat_name)].append(lot)
                lot_by_item[item.id] = lot
        for lots in lots_by_material.values():
            lots.sort(key=lambda lot: (lot["date"] or datetime.min, lot["grn_id"], lot["grn_item_id"]))

        def lot_available(lot_date, tx_dt):
            return _lot_available(lot_date, tx_dt)

        def consume_fifo(material_name, qty, tx_dt):
            remaining = max(0.0, qty)
            layers = []
            for lot in lots_by_material.get(_mat_key(material_name), []):
                if remaining <= EPS:
                    break
                if not lot_available(lot["date"], tx_dt):
                    continue
                take = min(remaining, lot["remaining"])
                if take <= EPS:
                    continue
                lot["remaining"] -= take
                remaining -= take
                layers.append({**{k: v for k, v in lot.items() if k != "remaining"}, "qty": take})
            return layers, remaining

        def consume_forward(material_name, qty):
            """Draw a shortage from the *next* real lots in FIFO order.

            Used when the dated queue cannot cover the dispatch: nothing was
            purchased on/before the supply date, or every earlier lot was
            already consumed.  The report has the real lot queue, so it costs
            each slice from the lot that covered it (10 x 1,000 + 30 x 1,200),
            never the whole quantity at one material-wide rate.  Consuming the
            lots here also stops a later delivery from using the same units
            twice.
            """
            remaining = max(0.0, qty)
            layers = []
            for lot in lots_by_material.get(_mat_key(material_name), []):
                if remaining <= EPS:
                    break
                take = min(remaining, lot["remaining"])
                if take <= EPS:
                    continue
                lot["remaining"] -= take
                remaining -= take
                layers.append({**{k: v for k, v in lot.items() if k != "remaining"}, "qty": take})
            return layers, remaining

        def lot_history_exists(material_name, tx_dt):
            """True when this material had a posted lot on or before the issue.

            A frozen sale-line rate is only a trustworthy posting record when
            there was purchase history to anchor it.  Before the first purchase
            it is a placeholder (``_cost_rate_for_material``'s forward guess),
            so the report replaces it with the lots that actually covered the
            units instead of re-applying that one rate to every unit.
            """
            return any(
                lot_available(lot["date"], tx_dt)
                for lot in lots_by_material.get(_mat_key(material_name), [])
            )

        def master_price(material_name):
            material = self.material_by_key.get(_mat_key(material_name))
            return _f(material.unit_price) if material is not None else 0.0

        def consume_specific(grn_item_id, qty, tx_dt):
            lot = lot_by_item.get(grn_item_id)
            if not lot or qty <= 0:
                return
            if not lot_available(lot["date"], tx_dt):
                return
            lot["remaining"] = max(0.0, lot["remaining"] - qty)

        def plan_event(kind, object_id, item, qty, tx_dt, allow_frozen=True):
            key = (kind, object_id)
            qty = max(0.0, _f(qty))
            if qty <= EPS or self.is_service(item.product_name):
                return
            recorded_allocations = []
            if kind == "direct":
                recorded_allocations = [a for a in self.grn_allocations_by_item.get(item.id, []) if _active(a)]
            frozen = _f(getattr(item, "cost_rate_at_sale", None)) if allow_frozen else 0.0
            cost = 0.0
            estimated = False
            missing = False
            bases = []
            sources = []
            grn_ids = []
            layers = []
            covered = 0.0
            for alloc in recorded_allocations:
                take = min(_f(alloc.qty), max(0.0, qty - covered))
                if take <= EPS:
                    continue
                covered += take
                consume_specific(alloc.grn_item_id, take, tx_dt)
                grn_item = self.grn_item_by_id.get(alloc.grn_item_id)
                grn = self.grn_by_id.get(grn_item.grn_id) if grn_item else None
                rate = _f(alloc.cost_rate)
                basis = "Recorded FIFO GRN allocation cost"
                actual = rate > 0
                if rate <= 0 and grn_item is not None and grn is not None and _active(grn_item) and _active(grn) and _f(grn_item.price_at_time) > 0:
                    rate = _f(grn_item.price_at_time)
                    basis = "Recorded GRN item cost linked to allocation"
                    actual = True
                if rate <= 0 and frozen > 0:
                    rate = frozen
                    basis = "Frozen sale-line cost"
                    actual = True
                if rate <= 0:
                    rate, basis, fallback_sources = self.fallback_cost(item.product_name, tx_dt.date() if tx_dt else None)
                    actual = False
                    sources.extend(fallback_sources)
                if rate > 0:
                    cost += take * rate
                    bases.append(basis)
                    estimated = estimated or not actual
                    if grn_item is not None and grn is not None:
                        sources.append({
                            "type": "GRNAllocation", "id": alloc.id,
                            "grn_item_id": alloc.grn_item_id, "lot_rate": _f(grn_item.price_at_time),
                            "grn_id": grn.id, "reference": _grn_ref(grn),
                            "supplier": grn.supplier or "", "date": _as_dt(grn.date_posted),
                            "qty": take, "rate": rate, "amount": take * rate,
                        })
                        grn_ids.append(grn.id)
                    layers.append({"grn_item_id": alloc.grn_item_id, "qty": take, "rate": rate})
                else:
                    missing = True
                    bases.append("No cost available for recorded allocation quantity")

            def add_layer_sources(layer_rows):
                """Record one evidence line per consumed lot (qty x rate)."""
                nonlocal missing
                for layer in layer_rows:
                    if _f(layer.get("rate")) <= 0:
                        missing = True
                        bases.append("Dated GRN lot without a purchase rate")
                        continue
                    sources.append({
                        "type": ("GRNItem" if layer.get("grn_item_id") else "ReturnLot"),
                        "id": layer.get("grn_item_id"), "grn_item_id": layer.get("grn_item_id"),
                        "lot_rate": _f(layer["rate"]), "grn_id": layer.get("grn_id"),
                        "reference": layer.get("reference", ""), "supplier": layer.get("supplier", ""),
                        "date": layer.get("date"), "qty": _f(layer["qty"]), "rate": _f(layer["rate"]),
                        "amount": _f(layer["qty"]) * _f(layer["rate"]),
                    })
                    if layer.get("grn_id"):
                        grn_ids.append(layer["grn_id"])

            def line_reference():
                sale = self.sale_by_id.get(getattr(item, "sale_id", None) or 0)
                if sale is not None:
                    return _sale_ref(sale)
                ret = getattr(item, "return_obj", None)
                if ret is not None:
                    return _return_ref(ret)
                entry = getattr(item, "entry_obj", None)
                if entry is not None:
                    return _ref(entry, "OUT")
                return f"Line #{item.id}"

            def add_frozen_source(quantity, basis_text, actual=True):
                """A frozen scalar still has to reconcile to the row cost.

                ``actual`` is False when nothing had been purchased yet: the
                posting path could only freeze a placeholder (the material
                master price or a forward guess), so the row stays an estimate.
                """
                nonlocal cost, estimated
                if frozen <= 0 or quantity <= EPS:
                    return False
                amount = quantity * frozen
                cost += amount
                if actual:
                    bases.append(basis_text)
                else:
                    estimated = True
                    bases.append(basis_text + " (posting estimate; no purchase history)")
                sources.append({
                    "type": "FrozenRate", "id": item.id, "grn_id": None,
                    "grn_item_id": None, "reference": line_reference(),
                    "supplier": "", "date": tx_dt, "qty": quantity,
                    "rate": frozen, "amount": amount,
                })
                return True

            def single_rate_sources(quantity):
                """Last resort: one labelled estimate rate for the remainder."""
                nonlocal cost, estimated, missing
                rate, basis, fallback_sources = self.fallback_cost(item.product_name, tx_dt.date() if tx_dt else None)
                if rate <= 0:
                    missing = True
                    bases.append(basis)
                    return
                cost += quantity * rate
                estimated = True
                bases.append(basis)
                if fallback_sources:
                    for source_row in fallback_sources:
                        sources.append({
                            **source_row, "qty": quantity,
                            "amount": quantity * _f(source_row.get("rate")),
                        })
                    grn_ids.extend(s["grn_id"] for s in fallback_sources if s.get("grn_id"))
                else:
                    sources.append({
                        "type": "EstimatedRate", "id": None, "grn_id": None,
                        "grn_item_id": None, "reference": line_reference(),
                        "supplier": "", "date": tx_dt, "qty": quantity,
                        "rate": rate, "amount": quantity * rate,
                    })

            def estimate_from_next_lots(quantity):
                """Split a shortage over the next real lots, each at its own rate.

                Used only when nothing was purchased on/before the supply, i.e.
                the delivery was posted against negative stock and posting could
                only freeze one material-wide placeholder rate.  The report has
                the real queue, so 10 bags from a 1,000 lot and 30 from a 1,200
                lot are costed as 10,000 + 36,000, not 40 x one rate.  Legacy
                names that differ only by descriptive text are matched exactly
                like ``fallback_cost`` does, so a renamed material still uses
                its real lots instead of a single blended rate.
                """
                nonlocal cost, estimated
                key = _mat_key(item.product_name)
                # Exact material identity only.  A merely similar legacy name
                # keeps the labelled single-rate estimate; taking another
                # material's lots would silently move stock between items.
                candidate_lots = list(lots_by_material.get(key, []))
                candidate_lots.sort(key=lambda lot: (lot["date"] or datetime.min, lot["grn_id"] or 0, lot["grn_item_id"] or 0))
                remaining = quantity
                taken = []
                for lot in candidate_lots:
                    if remaining <= EPS:
                        break
                    take = min(remaining, lot["remaining"])
                    if take <= EPS:
                        continue
                    lot["remaining"] -= take
                    remaining -= take
                    taken.append({**{k: v for k, v in lot.items() if k != "remaining"}, "qty": take})
                if taken:
                    cost += sum(_f(layer["qty"]) * _f(layer["rate"]) for layer in taken)
                    estimated = True
                    if has_history:
                        bases.append("Estimated from the next GRN lots in FIFO order; earlier lots were fully consumed")
                    else:
                        bases.append("Estimated from the nearest later GRN lots in FIFO order; no purchase existed on/before supply")
                    add_layer_sources(taken)
                    layers.extend(taken)
                return remaining

            residual = max(0.0, qty - covered)
            fifo_layers, uncovered = consume_fifo(item.product_name, residual, tx_dt)
            layers.extend(fifo_layers)
            has_history = lot_history_exists(item.product_name, tx_dt)
            if residual > EPS:
                # A frozen scalar is NEVER applied over real FIFO lots.  The GRN
                # page consumes these exact lots, so the P&L must cost the very
                # same quantities at the very same per-lot rates.  This also
                # makes a later/addendum GRN update both views in real time.
                if fifo_layers:
                    cost += sum(_f(layer["qty"]) * _f(layer["rate"]) for layer in fifo_layers)
                    estimated = True
                    bases.append("Reconstructed global FIFO estimate from dated GRN lots and all material issues; each lot keeps its own rate")
                    add_layer_sources(fifo_layers)
                still_uncovered = uncovered
                if still_uncovered > EPS:
                    # Dated lots are exhausted (or never existed): continue
                    # down the queue so the shortage is still split over the
                    # real purchases that covered it, each at its own rate.
                    still_uncovered = estimate_from_next_lots(still_uncovered)
                if still_uncovered > EPS:
                    # Only quantity that no real lot can cover falls back to a
                    # frozen or labelled estimate rate.  A frozen rate that
                    # merely repeats today's material master price and has no
                    # purchase history behind it remains an estimate.
                    frozen_is_placeholder = (
                        frozen > 0
                        and not has_history
                        and not lots_by_material.get(_mat_key(item.product_name))
                        and abs(frozen - master_price(item.product_name)) <= max(0.01, master_price(item.product_name) * 1e-9)
                    )
                    if not add_frozen_source(
                        still_uncovered,
                        "Frozen sale-line cost for quantity beyond every GRN lot",
                        actual=has_history or not frozen_is_placeholder,
                    ):
                        single_rate_sources(still_uncovered)
            status = COST_MISSING if missing else (COST_ESTIMATED if estimated else COST_ACTUAL)
            sources = _merge_sources(sources)
            supplier = next((s.get("supplier", "") for s in sources if s.get("supplier")), "")
            self.fifo_plan[key] = {
                "cost": cost, "status": status, "basis": "; ".join(dict.fromkeys(bases)),
                "sources": sources, "grn_ids": list(dict.fromkeys(grn_ids)), "supplier": supplier,
                "layers": layers,
            }

        events = []
        linked_entry_ids = set()
        for entry in self.entries:
            if entry.source_module == "sales" and entry.source_table == "direct_sale" and entry.source_id:
                linked_entry_ids.add(entry.id)
            if entry.nimbus_no == "Direct Sale":
                linked_entry_ids.add(entry.id)
        for sale in self.sales:
            if not _active(sale):
                continue
            category = normalize_sale_category(sale.category)
            for item in sale.items:
                if self.is_service(item.product_name):
                    continue
                allocations = [a for a in self.booking_allocations_by_sale_item.get(item.id, []) if _active(a)]
                free_direct = _f(item.price_at_time) <= 0 and not allocations and category not in {"Mixed Transaction", "Booking Delivery"}
                chargeable = self.chargeable_qty(sale, item) if (_f(item.price_at_time) > 0 or free_direct) else 0.0
                if chargeable > EPS:
                    events.append((_as_dt(sale.date_posted), sale.id, 0, item.id, "direct", item.id, item, chargeable, True))
                if allocations:
                    for alloc in allocations:
                        if _f(alloc.qty) > EPS and alloc.booking_item_id in self.booking_item_by_id:
                            # If the same legacy line also has direct FIFO
                            # allocations, those belong to its excess portion.
                            allow_frozen = not any(_active(g) for g in self.grn_allocations_by_item.get(item.id, []))
                            events.append((_as_dt(sale.date_posted), sale.id, 1, alloc.id, "booking", alloc.id, item, _f(alloc.qty), allow_frozen))
                elif chargeable <= EPS and _f(item.qty) > EPS and _f(item.price_at_time) <= 0:
                    # Unlinked zero-priced physical rows cannot earn revenue,
                    # but their quantity still affects the reconstruction.
                    events.append((_as_dt(sale.date_posted), sale.id, 2, item.id, "unrecognized", item.id, item, _f(item.qty), False))
        for entry in self.entries:
            if entry.id in linked_entry_ids or not _active(entry) or entry.type != "OUT" or _f(entry.qty) <= 0 or self.is_service(entry.material):
                continue
            item = type("EntryCostLine", (), {"id": -int(entry.id), "product_name": entry.material, "qty": _f(entry.qty), "cost_rate_at_sale": None, "grn_item_id": None, "entry_obj": entry})()
            events.append((_as_dt(entry.date, entry.time), entry.id or 0, 3, entry.id or 0, "entry", entry.id, item, _f(entry.qty), False))
        booking_refs = set()
        for booking in self.bookings:
            if _active(booking):
                booking_refs.update({_norm(_booking_ref(booking)), _norm(booking.manual_bill_no), _norm(booking.auto_bill_no)})
        booking_refs.discard("")

        def entry_looks_booked(entry):
            text = " ".join(str(getattr(entry, field, "") or "") for field in ("transaction_category", "client_category", "nimbus_no", "transaction_type"))
            refs = {_norm(entry.bill_no), _norm(entry.auto_bill_no), _norm(entry.source_bill_no)} - {""}
            return "book" in text.casefold() or bool((entry.booked_material or "").strip()) or bool(refs & booking_refs)

        def entry_has_booking(entry):
            if not entry_looks_booked(entry):
                return False
            _code, canonical = self.client_info(entry.client, entry.client_code)
            booked_name = entry.booked_material or entry.material
            return any(
                _active(booking) and _norm(booking.client_name) == _norm(canonical)
                and any(_mat_key(booked_item.material_name) == _mat_key(booked_name) for booked_item in booking.items)
                for booking in self.bookings
            )

        fifo_supply_candidates = []

        def remember_candidate(kind, object_id, item, qty, tx_dt):
            if kind not in {"direct", "booking", "entry"}:
                return
            if kind == "entry":
                entry = getattr(item, "entry_obj", None)
                if entry is None or not entry_has_booking(entry):
                    return
                client_name = self.client_info(entry.client, entry.client_code)[1]
            else:
                sale = self.sale_by_id.get(item.sale_id)
                if sale is None:
                    return
                client_name = self.client_info(sale.client_name, sale.client_code)[1]
            plan = self.fifo_plan.get((kind, object_id))
            if not plan:
                return
            fifo_supply_candidates.append({
                "origin": "direct" if kind == "direct" else "booked",
                "material": item.product_name, "client": client_name, "date": tx_dt,
                "qty": qty, "remaining": qty, "layers": plan.get("layers", []),
                "unit_cost": (plan["cost"] / qty if qty > EPS else 0.0),
                "object_id": object_id,
            })

        def restore_return(ret, return_item, tx_dt):
            qty_remaining = _f(return_item.qty)
            origin = "booked" if (ret.return_type or "normal").lower() == "booked" else "direct"
            client_name = self.client_info(ret.client_name)[1]
            candidates = [
                candidate for candidate in fifo_supply_candidates
                if candidate["origin"] == origin
                and _mat_key(candidate["material"]) == _mat_key(return_item.material_name)
                and _norm(candidate["client"]) == _norm(client_name)
                and candidate["remaining"] > EPS
                and (candidate["date"] or datetime.max) <= (tx_dt or datetime.min)
            ]
            candidates.sort(key=lambda candidate: (candidate["date"] or datetime.min, candidate["object_id"] or 0))
            for candidate in candidates:
                if qty_remaining <= EPS:
                    break
                take = min(qty_remaining, candidate["remaining"])
                if take <= EPS:
                    continue
                candidate["remaining"] -= take
                qty_remaining -= take
                restored = 0.0
                for layer in candidate["layers"]:
                    share = take * _f(layer.get("qty")) / candidate["qty"] if candidate["qty"] > EPS else 0.0
                    lot = lot_by_item.get(layer.get("grn_item_id"))
                    if not lot or share <= EPS:
                        continue
                    # Restore only the consumed quantity still evidenced by
                    # this layer; never inflate a lot beyond its original GRN.
                    before = lot["remaining"]
                    lot["remaining"] = min(lot.get("original_qty", lot["remaining"] + share), lot["remaining"] + share)
                    restored += lot["remaining"] - before
                synthetic_qty = max(0.0, take - restored)
                if synthetic_qty > EPS:
                    # A master/frozen cost with no surviving lot link becomes
                    # a dated return layer.  It remains an estimate for later
                    # unallocated issues, not a fabricated GRN allocation.
                    lots_by_material[_mat_key(return_item.material_name)].append({
                        "grn_item_id": None, "grn_id": None, "date": tx_dt,
                        "remaining": synthetic_qty, "original_qty": synthetic_qty,
                        "rate": max(0.0, _f(candidate["unit_cost"])),
                        "reference": _return_ref(ret), "supplier": "",
                    })
                    lots_by_material[_mat_key(return_item.material_name)].sort(
                        key=lambda lot: (lot["date"] or datetime.min, lot["grn_id"] or 0, lot["grn_item_id"] or 0)
                    )

        # Returns restore evidenced cost layers before any later issue is
        # costed.  Matching is intentionally the same conservative FIFO pool
        # used by the P&L return rows below.
        for ret in self.returns:
            if not _active(ret):
                continue
            for return_item in ret.items:
                if _f(return_item.qty) <= EPS or self.is_service(return_item.material_name):
                    continue
                line = type("ReturnCostLine", (), {
                    "id": -int(return_item.id), "product_name": return_item.material_name,
                    "qty": _f(return_item.qty), "cost_rate_at_sale": None, "grn_item_id": None,
                    "return_obj": ret, "return_item": return_item,
                })()
                events.append((_as_dt(ret.date_posted), ret.id, 4, return_item.id, "return", return_item.id, line, _f(return_item.qty), False))

        events.sort(key=lambda event: (event[0] or datetime.max, event[1] or 0, event[2], event[3] or 0))
        for tx_dt, _owner_id, _priority, _sequence, kind, object_id, item, qty, allow_frozen in events:
            if (
                self.through_date
                and tx_dt is not None
                and _day(tx_dt) is not None
                and _day(tx_dt) > self.through_date
            ):
                # Posting-time snapshot: later dispatches and returns cannot
                # consume or restore stock that was available on this date.
                continue
            if kind == "return":
                restore_return(item.return_obj, item.return_item, tx_dt)
                continue
            plan_event(kind, object_id, item, qty, tx_dt, allow_frozen=allow_frozen)
            remember_candidate(kind, object_id, item, qty, tx_dt)

        self.grn_lot_balances = {
            lot["grn_item_id"]: {
                "grn_item_id": lot["grn_item_id"],
                "grn_id": lot["grn_id"],
                "material": lot.get("material", ""),
                "date": lot.get("date"),
                "received": lot.get("original_qty", lot["remaining"]),
                "remaining": lot["remaining"],
                "rate": lot["rate"],
                "reference": lot["reference"],
                "supplier": lot.get("supplier", ""),
            }
            for lots in lots_by_material.values()
            for lot in lots
            if lot.get("grn_item_id")
        }

    def material_cost(self, item, tx_date, plan_key=None):
        """Resolve a sale/supply line's cost and its evidence."""
        if plan_key and plan_key in self.fifo_plan:
            plan = self.fifo_plan[plan_key]
            return plan["cost"], plan["status"], plan["basis"], plan["sources"], plan["grn_ids"], plan["supplier"]
        if self.is_service(item.product_name):
            return 0.0, COST_NA, "Service charge; no stock COGS", [], [], ""
        qty = _f(item.qty)
        if qty <= 0:
            return 0.0, COST_ACTUAL, "Zero quantity", [], [], ""
        frozen = _f(getattr(item, "cost_rate_at_sale", None))
        allocations = self.grn_allocations_by_item.get(item.id, [])
        known_cost = 0.0
        covered_qty = 0.0
        estimated = False
        missing = False
        bases = []
        sources = []
        grn_ids = []

        def add_estimate(quantity):
            nonlocal known_cost, estimated, missing
            rate, basis, fallback_sources = self.fallback_cost(item.product_name, tx_date)
            if rate > 0:
                known_cost += quantity * rate
                estimated = True
                bases.append(basis)
                for source_row in fallback_sources:
                    sources.append({
                        **source_row, "qty": quantity,
                        "amount": quantity * _f(source_row.get("rate")),
                    })
                grn_ids.extend(s["grn_id"] for s in fallback_sources if s.get("grn_id"))
            else:
                missing = True
                bases.append(basis)

        for alloc in allocations:
            if not _active(alloc):
                continue
            quantity = _f(alloc.qty)
            if quantity <= 0:
                continue
            covered_qty += quantity
            grn_item = self.grn_item_by_id.get(alloc.grn_item_id)
            grn = self.grn_item_by_id.get(alloc.grn_item_id)
            grn_obj = self.grn_by_id.get(grn_item.grn_id) if grn_item else None
            rate = _f(alloc.cost_rate)
            basis = "Recorded FIFO GRN allocation cost"
            if rate <= 0 and grn_item is not None and _active(grn_item) and grn_obj is not None and _active(grn_obj):
                rate = _f(grn_item.price_at_time)
                basis = "Recorded GRN item cost linked to allocation"
            if rate <= 0 and frozen > 0:
                rate = frozen
                basis = "Frozen sale-line cost"
            if rate > 0:
                known_cost += quantity * rate
                bases.append(basis)
                if grn_item is not None and grn_obj is not None:
                    sources.append({
                        "type": "GRNAllocation", "id": alloc.id,
                        "grn_item_id": grn_item.id, "grn_id": grn_obj.id,
                        "reference": _grn_ref(grn_obj), "supplier": grn_obj.supplier or "",
                        "date": _as_dt(grn_obj.date_posted), "qty": quantity,
                        "rate": rate, "amount": quantity * rate,
                    })
                    grn_ids.append(grn_obj.id)
            else:
                add_estimate(quantity)

        residual = max(0.0, qty - covered_qty)
        if residual > EPS:
            if frozen > 0:
                known_cost += residual * frozen
                bases.append("Frozen sale-line cost")
                sources.append({
                    "type": "FrozenRate", "id": item.id, "grn_id": None,
                    "grn_item_id": None, "reference": f"Line #{item.id}",
                    "supplier": "", "date": _as_dt(tx_date) if tx_date else None,
                    "qty": residual, "rate": frozen, "amount": residual * frozen,
                })
            elif getattr(item, "grn_item_id", None):
                grn_item = self.grn_item_by_id.get(item.grn_item_id)
                grn_obj = self.grn_by_id.get(grn_item.grn_id) if grn_item else None
                if grn_item is not None and grn_obj is not None and _active(grn_item) and _active(grn_obj) and _f(grn_item.price_at_time) > 0:
                    rate = _f(grn_item.price_at_time)
                    known_cost += residual * rate
                    bases.append("Recorded GRN cost linked to sale line")
                    sources.append({
                        "type": "GRNItem", "id": grn_item.id,
                        "grn_item_id": grn_item.id, "grn_id": grn_obj.id,
                        "reference": _grn_ref(grn_obj), "supplier": grn_obj.supplier or "",
                        "date": _as_dt(grn_obj.date_posted), "qty": residual,
                        "rate": rate, "amount": residual * rate,
                    })
                    grn_ids.append(grn_obj.id)
                else:
                    add_estimate(residual)
            else:
                add_estimate(residual)
        if missing:
            status = COST_MISSING
        elif estimated:
            status = COST_ESTIMATED
        else:
            status = COST_ACTUAL
        unique_bases = list(dict.fromkeys(bases))
        sources = _merge_sources(sources)
        supplier = next((s.get("supplier", "") for s in sources if s.get("supplier")), "")
        return known_cost, status, "; ".join(unique_bases), sources, list(dict.fromkeys(grn_ids)), supplier

    def payment_status(self, sale):
        due = max(0.0, _f(sale.amount) - _f(sale.discount) - _f(sale.paid_amount))
        open_pending = sum(_f(p.amount) for p in self.pending_by_sale.get(sale.id, []) if not p.is_paid)
        if open_pending > EPS:
            return f"Outstanding bill: Rs. {open_pending:,.2f}"
        if due <= EPS:
            return "Paid at posting / no opening credit"
        if _f(sale.paid_amount) > 0:
            return "Part-paid at posting; later settlement is separate"
        return "Credit at posting; later settlement is separate"

    def chargeable_qty(self, sale, item):
        """Physical quantity not already represented by booking allocations.

        Current posting code splits booked and excess rows.  Older combined
        rows are handled defensively so the same physical units cannot earn
        revenue twice.
        """
        qty = _f(item.qty)
        allocated = sum(
            _f(alloc.qty) for alloc in self.booking_allocations_by_sale_item.get(item.id, [])
            if _active(alloc)
        )
        if allocated > EPS and normalize_sale_category(sale.category) in {"Mixed Transaction", "Booking Delivery"}:
            return max(0.0, qty - allocated)
        return qty

    def add_direct_chargeable(self):
        for sale in self.sales:
            if not _active(sale):
                continue
            sale_category = normalize_sale_category(sale.category)
            sale_gross = self.sale_gross.get(sale.id, 0.0)
            discount = max(0.0, _f(sale.discount))
            code, client_name = self.client_info(sale.client_name, sale.client_code)
            for item in sale.items:
                rate = _f(item.price_at_time)
                qty = self.chargeable_qty(sale, item)
                has_booking_allocation = any(_active(a) for a in self.booking_allocations_by_sale_item.get(item.id, []))
                if qty <= 0:
                    continue
                if rate <= 0 and has_booking_allocation:
                    continue
                if rate <= 0 and sale_category in {"Mixed Transaction", "Booking Delivery"}:
                    self.warn("unlinked_zero_supply", f"{_sale_ref(sale)} / {item.product_name}: zero-priced line has no active BookingAllocation; revenue and cost are not invented", document_type="DirectSale", document_id=sale.id, reference=_sale_ref(sale), material=item.product_name)
                    continue
                # A zero-rate line outside booking workflows is a genuine free
                # direct-sale line: recognize no revenue, but retain its cost.
                gross = qty * rate
                discount_share = min(gross, gross / sale_gross * discount) if sale_gross > 0 else 0.0
                net = max(0.0, gross - discount_share)
                if self.is_service(item.product_name):
                    self.delivery_components[sale.id].append({
                        "material": item.product_name,
                        "qty": qty,
                        "gross": gross,
                        "revenue": net,
                        "discount": discount_share,
                        "origin": "Direct sale service",
                        "booking_id": None,
                        "booking_item_id": None,
                        "source_item_id": item.id,
                    })
                    self.candidates.append({
                        "origin": "direct", "document_type": "DirectSale", "document_id": sale.id,
                        "sale_id": sale.id, "booking_item_id": None, "material": item.product_name,
                        "client": client_name, "client_code": code, "date": sale.date_posted,
                        "qty": qty, "remaining_return_qty": qty, "unit_cost": 0.0,
                        "cost_status": COST_NA, "unit_revenue": net / qty,
                        "is_service": True, "reference": _sale_ref(sale),
                    })
                    continue
                cost_line = item
                if abs(qty - _f(item.qty)) > EPS:
                    cost_line = type("ChargeableCostLine", (), {
                        "id": item.id, "product_name": item.product_name, "qty": qty,
                        "cost_rate_at_sale": getattr(item, "cost_rate_at_sale", None),
                        "grn_item_id": getattr(item, "grn_item_id", None),
                    })()
                cost, cost_status, basis, sources, grn_ids, supplier = self.material_cost(cost_line, _day(sale.date_posted), plan_key=("direct", item.id))
                include = cost_status != COST_MISSING
                if not include:
                    cost = 0.0
                source_type = "cash_sale" if sale_category == "Cash" else ("credit_sale" if sale_category == "Credit Customer" else "other_sale")
                label = "Cash Sale" if sale_category == "Cash" else ("Credit Sale" if sale_category == "Credit Customer" else "Other Material Sale")
                if sale.id in self.booking_sale_ids:
                    if sale_category == "Mixed Transaction":
                        label = "Chargeable Excess / Direct Sale"
                        source_type = "other_sale"
                row = _base_row(
                    date=sale.date_posted, source="DirectSale", source_label=label,
                    transaction_type=label, entry_type="material_sale", category=source_type,
                    reference=_sale_ref(sale), document_type="DirectSale", document_id=sale.id,
                    source_type="DirectSaleItem", source_id=item.id, client=client_name, client_code=code,
                    supplier=supplier, material=item.product_name, qty=qty, sale_rate=rate,
                    cost_rate=(cost / qty if include else 0.0), gross_revenue=gross, discount=discount_share,
                    material_revenue=net, material_cost=cost, cost_status=cost_status, cost_basis=basis,
                    cost_sources=sources, cost_amount_recorded=cost, grn_ids=grn_ids,
                    include_in_net=include, note=sale.note or "", payment_status=self.payment_status(sale), sale_id=sale.id,
                )
                if not include:
                    row["warnings"].append("Cost unresolved: this row's revenue is reported, but its result is excluded from net profit.")
                    self.warn("missing_cost", f"{row['reference']} / {item.product_name}: material cost unresolved", document_type="DirectSale", document_id=sale.id, reference=row["reference"], amount=net)
                self.rows.append(row)
                self.candidates.append({
                    "origin": "direct", "document_type": "DirectSale", "document_id": sale.id,
                    "sale_id": sale.id, "booking_item_id": None, "material": item.product_name,
                    "client": client_name, "client_code": code, "date": sale.date_posted,
                    "qty": qty, "remaining_return_qty": qty,
                    "unit_cost": (cost / qty if include else 0.0), "cost_status": cost_status,
                    "unit_revenue": net / qty, "is_service": False, "reference": row["reference"],
                    "cost_sources": sources,
                })
            # Older records can persist rent_item_revenue without a positive
            # service line. Preserve that evidenced revenue instead of silently
            # dropping it; never add it where allocation-backed services exist.
            has_booking_lines = any(
                any(_active(a) for a in self.booking_allocations_by_sale_item.get(item.id, []))
                for item in sale.items
            )
            existing_components = self.delivery_components.get(sale.id, [])
            legacy_rent = max(0.0, _f(getattr(sale, "rent_item_revenue", None)))
            if legacy_rent > EPS and not has_booking_lines and sum(_f(c.get("gross")) for c in existing_components) <= EPS:
                legacy_discount = min(legacy_rent, discount) if sale_gross <= EPS else 0.0
                self.delivery_components[sale.id].append({
                    "material": "Rent / legacy sale revenue", "qty": 0.0,
                    "gross": legacy_rent, "revenue": max(0.0, legacy_rent - legacy_discount),
                    "discount": legacy_discount, "origin": "Legacy stored sale rent revenue",
                    "booking_id": None, "booking_item_id": None, "source_item_id": None,
                })

    def add_booking_supply(self):
        # Exact allocation-backed fulfillment, including alternate material.
        for alloc in self.booking_allocations:
            sale = self.sale_by_id.get(alloc.sale_id)
            sale_item = self.sale_item_by_id.get(alloc.sale_item_id)
            booking_item = self.booking_item_by_id.get(alloc.booking_item_id)
            booking = self.booking_by_id.get(booking_item.booking_id) if booking_item else None
            if not sale or not sale_item or not booking_item or not booking:
                if _active(alloc):
                    self.warn("orphan_allocation", f"BookingAllocation {alloc.id} has no complete active source chain", reference=f"BA-{alloc.id}")
                continue
            if not _active(alloc) or not _active(sale) or not _active(booking):
                continue
            qty = _f(alloc.qty)
            if qty <= 0:
                continue
            code, client_name = self.client_info(sale.client_name or booking.client_name, sale.client_code)
            rate = _f(booking_item.price_at_time)
            gross = qty * rate
            booking_gross = self.booking_gross.get(booking.id, 0.0)
            discount_share = min(gross, gross / booking_gross * max(0.0, _f(booking.discount))) if booking_gross > 0 else 0.0
            net = max(0.0, gross - discount_share)
            is_service = self.is_service(sale_item.product_name)
            self.booking_remaining[booking_item.id] = self.booking_remaining.get(booking_item.id, _f(booking_item.qty)) - qty
            self.booking_original_qty[booking_item.id] = _f(booking_item.qty)
            self.booking_recognized_revenue[booking.id] += net
            booked_name = booking_item.material_name or ""
            if booked_name and _mat_key(booked_name) != _mat_key(sale_item.product_name):
                note = f"Alternate supplied for booked {booked_name}"
            else:
                note = "Booking rate is the currently stored BookingItem rate; earlier edited rates are not versioned."
            if is_service:
                self.delivery_components[sale.id].append({
                    "material": sale_item.product_name, "qty": qty, "gross": gross,
                    "revenue": net, "discount": discount_share, "origin": "Booking supply service",
                    "booking_id": booking.id, "booking_item_id": booking_item.id, "source_item_id": sale_item.id,
                })
                self.candidates.append({
                    "origin": "booked", "document_type": "DirectSale", "document_id": sale.id,
                    "sale_id": sale.id, "booking_item_id": booking_item.id, "material": sale_item.product_name,
                    "client": client_name, "client_code": code, "date": sale.date_posted,
                    "qty": qty, "remaining_return_qty": qty, "unit_cost": 0.0,
                    "cost_status": COST_NA, "unit_revenue": net / qty, "is_service": True,
                    "reference": _sale_ref(sale), "booking_id": booking.id,
                })
                continue
            cost, cost_status, basis, sources, grn_ids, supplier = self.material_cost(
                sale_item, _day(sale.date_posted), plan_key=("booking", alloc.id)
            )
            include = cost_status != COST_MISSING
            if not include:
                cost = 0.0
            row = _base_row(
                date=sale.date_posted, source="DirectSale", source_label="Booking Material Supplied",
                transaction_type="Booking Material Supplied", entry_type="booking_supply", category="booking_supply",
                reference=_sale_ref(sale), document_type="DirectSale", document_id=sale.id,
                source_type="BookingAllocation", source_id=alloc.id, client=client_name, client_code=code,
                supplier=supplier, material=sale_item.product_name, booked_material=booked_name,
                qty=qty, sale_rate=rate, cost_rate=(cost / qty if include else 0.0),
                gross_revenue=gross, discount=discount_share, material_revenue=net, material_cost=cost,
                cost_status=cost_status, cost_basis=basis, cost_sources=sources,
                cost_amount_recorded=cost, grn_ids=grn_ids, include_in_net=include,
                note=note, payment_status="Booking order/advance ledger; settlement is not revenue",
                booking_id=booking.id, booking_item_id=booking_item.id, sale_id=sale.id,
                original_sources=[{"type": "Booking", "id": booking.id, "reference": _booking_ref(booking)}],
            )
            if not include:
                row["warnings"].append("Booked supply has no resolved actual/estimated cost; excluded from net profit.")
                self.warn("missing_cost", f"{row['reference']} / booked {booked_name}: supplied material cost unresolved", document_type="DirectSale", document_id=sale.id, reference=row["reference"], amount=net)
            if _mat_key(booked_name) != _mat_key(sale_item.product_name):
                row["warnings"].append("Historical booked rates are not versioned; current BookingItem rate used.")
            self.rows.append(row)
            self.candidates.append({
                "origin": "booked", "document_type": "DirectSale", "document_id": sale.id,
                "sale_id": sale.id, "booking_item_id": booking_item.id, "material": sale_item.product_name,
                "client": client_name, "client_code": code, "date": sale.date_posted,
                "qty": qty, "remaining_return_qty": qty, "unit_cost": (cost / qty if include else 0.0),
                "cost_status": cost_status, "unit_revenue": net / qty, "is_service": False,
                "reference": row["reference"], "booking_id": booking.id,
                "cost_sources": sources,
            })

        self.add_standalone_dispatch()

    def _standalone_match(self, entry):
        booked_name = entry.booked_material or entry.material
        key = _mat_key(booked_name)
        code, canonical_name = self.client_info(entry.client, entry.client_code)
        candidates = []
        for booking in self.bookings:
            if not _active(booking):
                continue
            if _norm(booking.client_name) != _norm(canonical_name):
                continue
            for item in booking.items:
                if _mat_key(item.material_name) != key:
                    continue
                remaining = self.booking_remaining.get(item.id, _f(item.qty))
                candidates.append((booking, item, remaining))
        # An explicit bill reference narrows the match but never creates an
        # allocation when no open booking item exists.
        entry_refs = {_norm(entry.bill_no), _norm(entry.auto_bill_no), _norm(entry.source_bill_no)} - {""}
        if entry_refs:
            narrowed = [value for value in candidates if {_norm(_booking_ref(value[0])), _norm(value[0].manual_bill_no), _norm(value[0].auto_bill_no)} & entry_refs]
            if narrowed:
                candidates = narrowed
        candidates.sort(key=lambda value: (_as_dt(value[0].date_posted) or datetime.min, value[0].id, value[1].id))
        return candidates

    def add_standalone_dispatch(self):
        linked_entry_ids = set()
        for entry in self.entries:
            if entry.source_module == "sales" and entry.source_table == "direct_sale" and entry.source_id:
                linked_entry_ids.add(entry.id)
            if entry.nimbus_no == "Direct Sale":
                linked_entry_ids.add(entry.id)
        booking_refs = set()
        for booking in self.bookings:
            if _active(booking):
                booking_refs.update({_norm(_booking_ref(booking)), _norm(booking.manual_bill_no), _norm(booking.auto_bill_no)})
        booking_refs.discard("")
        for entry in self.entries:
            if entry.id in linked_entry_ids:
                if _active(entry) and entry.source_table == "direct_sale" and entry.source_id and entry.source_id not in self.sale_by_id:
                    self.warn("orphan_dispatch", f"Dispatch {entry.id} references missing DirectSale {entry.source_id}; no revenue invented", document_type="Entry", document_id=entry.id, reference=_ref(entry, "OUT"))
                continue
            if not _active(entry) or entry.type != "OUT" or _f(entry.qty) <= 0:
                continue
            category_text = " ".join(str(getattr(entry, field, "") or "") for field in ("transaction_category", "client_category", "nimbus_no", "transaction_type"))
            entry_refs = {_norm(entry.bill_no), _norm(entry.auto_bill_no), _norm(entry.source_bill_no)} - {""}
            if "book" not in category_text.casefold() and not (entry.booked_material or "").strip() and not (entry_refs & booking_refs):
                continue
            matches = self._standalone_match(entry)
            open_matches = [value for value in matches if value[2] > EPS]
            if not open_matches:
                self.warn("unmatched_dispatch", f"Dispatch {entry.id} / {entry.material} could not be matched conservatively to an open booking item; no revenue invented", document_type="Entry", document_id=entry.id, reference=_ref(entry, "OUT"))
                continue
            remaining_qty = _f(entry.qty)
            entry_date = _as_dt(entry.date, entry.time)
            for booking, item, remaining in open_matches:
                if remaining_qty <= EPS:
                    break
                qty = min(remaining_qty, remaining)
                if qty <= EPS:
                    continue
                rate = _f(item.price_at_time)
                gross = qty * rate
                booking_gross = self.booking_gross.get(booking.id, 0.0)
                discount_share = min(gross, gross / booking_gross * max(0.0, _f(booking.discount))) if booking_gross > 0 else 0.0
                net = max(0.0, gross - discount_share)
                code, client_name = self.client_info(entry.client or booking.client_name, entry.client_code)
                self.booking_remaining[item.id] = self.booking_remaining.get(item.id, _f(item.qty)) - qty
                self.booking_original_qty[item.id] = _f(item.qty)
                self.booking_recognized_revenue[booking.id] += net
                if self.is_service(entry.material):
                    row = _base_row(
                        date=entry_date, source="Entry", source_label="Legacy Booking Delivery Supplied",
                        transaction_type="Booking Delivery Supplied", entry_type="delivery_service", category="booking_delivery",
                        reference=_ref(entry, "OUT"), document_type="Entry", document_id=entry.id,
                        source_type="Entry", source_id=entry.id, client=client_name, client_code=code,
                        material=entry.material, booked_material=item.material_name, qty=qty,
                        sale_rate=rate, gross_revenue=gross, discount=discount_share,
                        delivery_revenue=net, delivery_cost=0.0, cost_status=COST_MISSING,
                        cost_basis="Standalone operational dispatch has no linked delivery-cost record",
                        include_in_net=False, note="Revenue shown; result excluded until direct delivery cost is resolved.",
                        payment_status="Legacy booking dispatch", booking_id=booking.id, booking_item_id=item.id,
                        entry_id=entry.id, original_sources=[{"type": "Booking", "id": booking.id, "reference": _booking_ref(booking)}],
                    )
                    self.rows.append(row)
                    self.candidates.append({
                        "origin": "booked", "document_type": "Entry", "document_id": entry.id,
                        "sale_id": None, "booking_item_id": item.id, "material": entry.material,
                        "client": client_name, "client_code": code, "date": entry_date,
                        "qty": qty, "remaining_return_qty": qty, "unit_cost": 0.0,
                        "cost_status": COST_MISSING, "unit_revenue": net / qty, "is_service": True,
                        "reference": row["reference"], "booking_id": booking.id,
                        "delivery_include_in_net": False,
                    })
                    self.warn("missing_delivery_cost", f"Dispatch {entry.id}: standalone booking delivery has no linked delivery-cost record", document_type="Entry", document_id=entry.id, reference=row["reference"], amount=net)
                else:
                    # Entry has no frozen/GRN allocation.  Reuse the same cost
                    # resolver contract through a lightweight line object.
                    line = type("DispatchCostLine", (), {
                        "id": -int(entry.id), "product_name": entry.material,
                        "qty": qty, "cost_rate_at_sale": None, "grn_item_id": None,
                    })()
                    full_cost, cost_status, basis, sources, grn_ids, supplier = self.material_cost(
                        line, entry_date.date() if entry_date else None, plan_key=("entry", entry.id)
                    )
                    entry_qty = _f(entry.qty)
                    cost = full_cost * qty / entry_qty if entry_qty > EPS else 0.0
                    include = cost_status != COST_MISSING
                    if not include:
                        cost = 0.0
                    row = _base_row(
                        date=entry_date, source="Entry", source_label="Legacy Booking Material Supplied",
                        transaction_type="Booking Material Supplied", entry_type="booking_supply", category="booking_supply",
                        reference=_ref(entry, "OUT"), document_type="Entry", document_id=entry.id,
                        source_type="Entry", source_id=entry.id, client=client_name, client_code=code,
                        supplier=supplier, material=entry.material, booked_material=item.material_name,
                        qty=qty, sale_rate=rate, cost_rate=(cost / qty if include else 0.0),
                        gross_revenue=gross, discount=discount_share, material_revenue=net,
                        material_cost=cost, cost_status=cost_status, cost_basis=basis,
                        cost_sources=sources, cost_amount_recorded=cost, grn_ids=grn_ids,
                        include_in_net=include, note="Conservative legacy dispatch match; no DirectSale or BookingAllocation exists.",
                        payment_status="Legacy booking dispatch", booking_id=booking.id,
                        booking_item_id=item.id, entry_id=entry.id,
                        original_sources=[{"type": "Booking", "id": booking.id, "reference": _booking_ref(booking)}],
                    )
                    self.rows.append(row)
                    self.candidates.append({
                        "origin": "booked", "document_type": "Entry", "document_id": entry.id,
                        "sale_id": None, "booking_item_id": item.id, "material": entry.material,
                        "client": client_name, "client_code": code, "date": entry_date,
                        "qty": qty, "remaining_return_qty": qty, "unit_cost": (cost / qty if include else 0.0),
                        "cost_status": cost_status, "unit_revenue": net / qty, "is_service": False,
                        "reference": row["reference"], "booking_id": booking.id,
                        "cost_sources": sources,
                    })
                    if not include:
                        self.warn("missing_cost", f"Dispatch {entry.id}: supplied material cost unresolved", document_type="Entry", document_id=entry.id, reference=row["reference"], amount=net)
                remaining_qty -= qty
            if remaining_qty > EPS:
                self.warn("unmatched_dispatch", f"Dispatch {entry.id}: {remaining_qty:g} units exceed conservatively matched booked balance; excess revenue/cost not invented", document_type="Entry", document_id=entry.id, reference=_ref(entry, "OUT"))

    def add_delivery_rows(self):
        # One index instead of scanning every candidate for every sale (the
        # old shape was O(sales x candidates) and dominated the report).
        service_candidates_by_sale = defaultdict(list)
        for candidate in self.candidates:
            if candidate.get("is_service"):
                service_candidates_by_sale[candidate.get("sale_id")].append(candidate)
        for sale in self.sales:
            if not _active(sale):
                continue
            components = self.delivery_components.get(sale.id, [])
            rents = self.rent_by_sale.get(sale.id, [])
            allocations = self.driver_alloc_by_sale.get(sale.id, [])
            recorded_cost = _f(getattr(sale, "delivery_rent_cost", None))
            # A positive stored/allocation value is evidence of the direct
            # delivery cost.  A bare zero with no rent/allocation evidence is
            # not silently accepted as a proven zero-cost delivery.
            cost_recorded = recorded_cost > 0
            basis = "Recorded DirectSale delivery rent cost"
            if recorded_cost <= 0 and rents:
                latest = rents[-1]
                if _f(latest.amount) > 0:
                    recorded_cost = _f(latest.amount)
                    basis = "Recorded legacy DeliveryRent cost"
                    cost_recorded = True
            if recorded_cost <= 0 and allocations:
                alloc_cost = sum(_f(a.rent_amount) for a in allocations)
                if alloc_cost > 0:
                    recorded_cost = alloc_cost
                    basis = "Recorded driver allocation rent"
                    cost_recorded = True
            if not components:
                if recorded_cost > 0:
                    code, client_name = self.client_info(sale.client_name, sale.client_code)
                    self.rows.append(_base_row(
                        date=sale.date_posted, source="DirectSale", source_label="Direct Delivery Cost",
                        transaction_type="Delivery Cost", entry_type="delivery_cost", category="delivery_cost",
                        reference=_sale_ref(sale), document_type="DirectSale", document_id=sale.id,
                        source_type="DirectSale", source_id=sale.id, client=client_name, client_code=code,
                        material="Delivery", delivery_cost=recorded_cost, cost_status=COST_ACTUAL,
                        cost_basis=basis, cost_amount_recorded=recorded_cost,
                        note="Delivery cost with no recorded customer delivery-charge line.",
                        payment_status=self.payment_status(sale), sale_id=sale.id,
                    ))
                continue
            code, client_name = self.client_info(sale.client_name, sale.client_code)
            by_material = defaultdict(lambda: {"qty": 0.0, "gross": 0.0, "revenue": 0.0, "discount": 0.0, "booking_ids": set()})
            for component in components:
                target = by_material[component["material"]]
                for key in ("qty", "gross", "revenue", "discount"):
                    target[key] += _f(component[key])
                if component.get("booking_id"):
                    target["booking_ids"].add(component["booking_id"])
            gross_total = sum(value["gross"] for value in by_material.values())
            quantity_total = sum(max(0.0, value["qty"]) for value in by_material.values())
            allocation_total = gross_total if gross_total > EPS else quantity_total
            for material, value in by_material.items():
                allocation_value = value["gross"] if gross_total > EPS else max(0.0, value["qty"])
                share = recorded_cost * allocation_value / allocation_total if allocation_total > EPS else 0.0
                status = COST_ACTUAL if cost_recorded else COST_MISSING
                include = cost_recorded
                row = _base_row(
                    date=sale.date_posted, source="DirectSale", source_label="Delivery Service / Rent",
                    transaction_type="Delivery Service / Rent", entry_type="delivery_service", category="delivery",
                    reference=_sale_ref(sale), document_type="DirectSale", document_id=sale.id,
                    source_type="DirectSale", source_id=sale.id, client=client_name, client_code=code,
                    material=material or "Delivery", qty=value["qty"],
                    sale_rate=(value["gross"] / value["qty"] if value["qty"] else 0.0),
                    cost_rate=(share / value["qty"] if value["qty"] else 0.0),
                    gross_revenue=value["gross"], discount=value["discount"],
                    delivery_revenue=value["revenue"], delivery_cost=(share if include else 0.0),
                    cost_status=status, cost_basis=(basis if cost_recorded else "No delivery cost record; result excluded rather than assumed zero"),
                    cost_amount_recorded=share, include_in_net=include, note=sale.note or "",
                    payment_status=self.payment_status(sale), sale_id=sale.id,
                    booking_id=(next(iter(value["booking_ids"])) if len(value["booking_ids"]) == 1 else None),
                    original_sources=[{"type": "Booking", "id": bid, "reference": _booking_ref(self.booking_by_id[bid])} for bid in value["booking_ids"] if bid in self.booking_by_id],
                )
                if not include:
                    row["warnings"].append("Delivery revenue is shown, but its result is excluded until delivery cost is recorded.")
                    self.warn("missing_delivery_cost", f"{row['reference']} / {material}: delivery cost unresolved", document_type="DirectSale", document_id=sale.id, reference=row["reference"], amount=value["revenue"])
                self.rows.append(row)
                for candidate in service_candidates_by_sale.get(sale.id, []):
                    if _mat_key(candidate.get("material")) == _mat_key(material):
                        candidate["delivery_include_in_net"] = include
                        candidate["delivery_cost_status"] = status

    def _return_candidate_index(self):
        """Candidates grouped by (origin, is_service, material, client).

        The return matcher used to scan every candidate for every returned line
        (O(returns x candidates), seconds on a production database).  The
        grouping key is exactly the equality part of the old filter, so the
        pre-sorted candidate list -- and therefore every match -- is unchanged.
        """
        index = defaultdict(list)
        for candidate in self.candidates:
            index[(
                candidate["origin"],
                candidate["is_service"],
                _mat_key(candidate["material"]),
                _norm(candidate["client"]),
            )].append(candidate)
        return index

    def add_returns(self):
        candidate_index = self._return_candidate_index()
        for ret in self.returns:
            if not _active(ret):
                continue
            code, client_name = self.client_info(ret.client_name)
            for item in ret.items:
                qty_remaining = _f(item.qty)
                if qty_remaining <= 0:
                    continue
                is_service = self.is_service(item.material_name)
                origin = "booked" if (ret.return_type or "normal").lower() == "booked" else "direct"
                candidates = [
                    candidate
                    for candidate in candidate_index.get(
                        (origin, is_service, _mat_key(item.material_name), _norm(client_name)), []
                    )
                    if _f(candidate["remaining_return_qty"]) > EPS
                    and (_as_dt(candidate["date"]) or datetime.max) <= (_as_dt(ret.date_posted) or datetime.min)
                ]
                candidates.sort(key=lambda c: (_as_dt(c["date"]) or datetime.min, c["document_id"] or 0))
                matched_slices = []
                for candidate in candidates:
                    if qty_remaining <= EPS:
                        break
                    take = min(qty_remaining, _f(candidate["remaining_return_qty"]))
                    if take <= EPS:
                        continue
                    candidate["remaining_return_qty"] -= take
                    qty_remaining -= take
                    matched_slices.append((candidate, take))
                if not matched_slices or qty_remaining > EPS:
                    matched_slices.append((None, qty_remaining))
                for candidate, qty in matched_slices:
                    if qty <= EPS:
                        continue
                    if origin == "booked":
                        rate = _f(item.rent_rate) or _f(item.price_at_time)
                        if rate <= 0 and candidate is not None:
                            rate = _f(candidate["unit_revenue"])
                    elif is_service:
                        rate = _f(item.unit_rate) + _f(item.rent_rate)
                    else:
                        rate = _f(item.unit_rate) or _f(item.price_at_time)
                    reversal_revenue = -qty * rate
                    if candidate is None:
                        cost_status = COST_MISSING
                        cost = 0.0
                        unit_cost = 0.0
                        basis = "Original supplied line could not be matched; cost and result unresolved"
                        original_sources = []
                        booking_id = None
                        sale_id = None
                        document_type = None
                        document_id = None
                    else:
                        cost_status = candidate["cost_status"]
                        unit_cost = _f(candidate["unit_cost"])
                        cost = -qty * unit_cost if cost_status != COST_MISSING else 0.0
                        basis = f"Reversal of {candidate['reference']} using original {cost_status} unit cost"
                        original_sources = [{"type": candidate["document_type"], "id": candidate["document_id"], "reference": candidate["reference"]}]
                        booking_id = candidate.get("booking_id")
                        sale_id = candidate.get("sale_id")
                        document_type = candidate["document_type"]
                        document_id = candidate["document_id"]
                    include = candidate is not None and cost_status != COST_MISSING
                    if is_service and candidate is not None:
                        # If the original delivery result was excluded for a
                        # missing driver cost, exclude its later charge reversal
                        # too; otherwise a return could create a spurious loss.
                        include = bool(candidate.get("delivery_include_in_net", cost_status != COST_MISSING))
                        if not include:
                            cost_status = COST_MISSING
                    # Services normally have no product cost, but their delivery
                    # cost was recognized on the original delivery row.  A return
                    # reverses the charge only; original delivery cost remains.
                    material_cost = 0.0 if is_service else cost
                    delivery_revenue = reversal_revenue if is_service else 0.0
                    material_revenue = 0.0 if is_service else reversal_revenue
                    # Show which original GRN lots the returned quantity is
                    # taken out of; the reversal stays at the original unit cost.
                    reversed_cost_sources = (
                        [] if is_service or candidate is None or cost_status == COST_MISSING
                        else _reversed_sources(candidate, qty)
                    )
                    row = _base_row(
                        date=ret.date_posted, source="MaterialReturn",
                        source_label=("Booked Return" if origin == "booked" else "Material Return"),
                        transaction_type=("Booked Return" if origin == "booked" else "Material Return"),
                        entry_type="return", category=("booked_return" if origin == "booked" else "sale_return"),
                        reference=_return_ref(ret), document_type="MaterialReturn", document_id=ret.id,
                        source_type="MaterialReturnItem", source_id=item.id, client=client_name, client_code=code,
                        material=item.material_name, qty=-qty, sale_rate=rate, cost_rate=unit_cost,
                        gross_revenue=reversal_revenue, material_revenue=material_revenue,
                        delivery_revenue=delivery_revenue, material_cost=material_cost,
                        cost_status=cost_status, cost_basis=basis, cost_amount_recorded=abs(cost),
                        cost_sources=reversed_cost_sources,
                        grn_ids=list(dict.fromkeys(s.get("grn_id") for s in reversed_cost_sources if s.get("grn_id"))),
                        include_in_net=include, note=ret.note or "",
                        payment_status="Return credit/refund is settlement, not new revenue",
                        booking_id=booking_id, sale_id=sale_id, return_id=ret.id,
                        original_sources=original_sources,
                    )
                    if not include:
                        row["warnings"].append("Return is visible but excluded from net because its original supply/rate-cost chain is unresolved.")
                        self.warn("unmatched_return", f"{row['reference']} / {item.material_name}: original supplied line not fully matched", document_type="MaterialReturn", document_id=ret.id, reference=row["reference"], amount=abs(reversal_revenue))
                    self.rows.append(row)
                    if candidate is not None and origin == "booked" and candidate.get("booking_item_id"):
                        booking_item_id = candidate["booking_item_id"]
                        self.booking_remaining[booking_item_id] = self.booking_remaining.get(booking_item_id, 0.0) + qty
                        if booking_id:
                            self.booking_recognized_revenue[booking_id] += reversal_revenue

    def add_other_events(self):
        cash_flow_by_tx = {}
        cashflows = CashFlowEntry.query.options(
            selectinload(CashFlowEntry.category), selectinload(CashFlowEntry.subcategory),
            selectinload(CashFlowEntry.party),
        ).all()
        for flow in cashflows:
            if flow.account_tx_id and _active(flow):
                cash_flow_by_tx[flow.account_tx_id] = flow
        transactions = AccountTransaction.query.options(
            selectinload(AccountTransaction.from_account), selectinload(AccountTransaction.to_account)
        ).order_by(AccountTransaction.date_posted.asc(), AccountTransaction.id.asc()).all()
        for tx in transactions:
            if not _active(tx) or _f(tx.amount) <= 0:
                continue
            tx_type = (tx.transaction_type or "").strip()
            amount = _f(tx.amount)
            flow = cash_flow_by_tx.get(tx.id)
            description = tx.description or tx_type
            account = tx.from_account if tx_type in {"Expense", "Payment", "Loss", "Reconciliation Loss"} else tx.to_account
            common = dict(
                date=tx.date_posted, reference=f"ATX-{tx.id}", document_type="AccountTransaction",
                document_id=tx.id, source_type="AccountTransaction", source_id=tx.id,
                account=(account.name if account else ""), note=tx.note or "",
                payment_status="Account movement; source classification determines P&L treatment",
            )
            if tx_type in {"Reconciliation Loss", "Reconciliation Excess"}:
                is_loss = tx_type == "Reconciliation Loss"
                self.rows.append(_base_row(
                    **common, source="AccountTransaction",
                    source_label=tx_type, transaction_type=tx_type, entry_type="other",
                    category="reconciliation", material="Account Reconciliation",
                    other_loss=(amount if is_loss else 0.0), other_gain=(0.0 if is_loss else amount),
                    cost_basis="Explicit finalized account reconciliation difference",
                ))
            elif tx_type == "Expense":
                self.rows.append(_base_row(
                    **common, source="AccountTransaction", source_label="Operating Expense",
                    transaction_type="Operating Expense", entry_type="expense",
                    category=(flow.category.name if flow and flow.category else (description or "General Expense")),
                    subcategory=(flow.subcategory.name if flow and flow.subcategory else ""),
                    client=(flow.party.name if flow and flow.party else ""),
                    material="Expense", expense=amount,
                    cost_basis="Direct dated operating expense; no source settlement mirror counted",
                ))
            elif tx_type == "Payment" and not tx.source_type:
                # Other/manual payments are explicit expenses in the existing
                # Accounts workflow.  Loan repayments and transfers are not.
                combined = _norm(f"{description} {tx.note or ''}")
                if any(term in combined for term in ("loan repayment", "account transfer", "supplier payment", "driver payment", "client refund")):
                    continue
                self.rows.append(_base_row(
                    **common, source="AccountTransaction", source_label="Other Direct Payment / Expense",
                    transaction_type="Other Expense", entry_type="expense", category=(description or "Other Payment"),
                    material="Expense", expense=amount,
                    cost_basis="Unlinked manual other payment; settlement-linked payment sources excluded",
                ))
            elif tx_type == "Loss" and not tx.source_type:
                self.rows.append(_base_row(
                    **common, source="AccountTransaction", source_label="Other Recorded Loss",
                    transaction_type="Other Loss", entry_type="other", category="other_loss",
                    material="Other Loss", other_loss=amount, cost_basis="Explicit unlinked account loss",
                ))
            elif tx_type == "Receipt" and not tx.source_type and _norm(description).startswith("money received from"):
                source_name = re.sub(r"^money received from\s*", "", description, flags=re.I)
                self.rows.append(_base_row(
                    **common, source="AccountTransaction", source_label="Other Earned Income",
                    transaction_type="Other Income", entry_type="other_income", category="other_income",
                    client=source_name, material="Other Income", other_income=amount,
                    cost_basis="Explicit other-source receipt in Accounts (client settlements use a different source path)",
                ))

        for drawer in FbmCashDrawerEntry.query.order_by(FbmCashDrawerEntry.date_posted.asc(), FbmCashDrawerEntry.id.asc()).all():
            if not _active(drawer) or drawer.entry_type != "out" or _f(drawer.amount) <= 0:
                continue
            # Drawer rows are standalone; account/cash-flow imports carrying a
            # source marker are not posted a second time.
            if "[src:" in _norm(drawer.note) or "[account_tx:" in _norm(drawer.note):
                continue
            self.rows.append(_base_row(
                date=drawer.date_posted, source="FbmCashDrawerEntry", source_label="Cash Drawer Expense",
                transaction_type="Operating Expense", entry_type="expense",
                category=(drawer.category or "Cash Drawer Expense"), reference=f"DRAWER-{drawer.id}",
                document_type="FbmCashDrawerEntry", document_id=drawer.id,
                source_type="FbmCashDrawerEntry", source_id=drawer.id, material="Expense",
                expense=_f(drawer.amount), note=drawer.note or "",
                cost_basis="Standalone manual cash-drawer expense",
            ))

        represented_payment_ids = set()
        payment_by_id = {payment.id: payment for payment in self.payments}
        for waive in WaiveOff.query.order_by(WaiveOff.date_posted.asc(), WaiveOff.id.asc()).all():
            if not _active(waive) or _f(waive.amount) <= 0:
                continue
            if _norm(waive.note).startswith("[direct_sale_discount:"):
                continue
            if waive.payment_id:
                represented_payment_ids.add(waive.payment_id)
            if waive.payment_id and (
                waive.payment_id not in payment_by_id or not _active(payment_by_id[waive.payment_id])
            ):
                continue
            code, client_name = self.client_info(waive.client_name, waive.client_code)
            self.rows.append(_base_row(
                date=waive.date_posted, source="WaiveOff", source_label="Customer Discount / Waive-Off Loss",
                transaction_type="Discount / Waive-Off", entry_type="other", category="discount_waive",
                reference=(waive.bill_no or f"WAIVE-{waive.id}"), document_type="WaiveOff", document_id=waive.id,
                source_type="WaiveOff", source_id=waive.id, client=client_name, client_code=code,
                material="Discount / Waive-Off", other_loss=_f(waive.amount),
                note=(waive.reason or waive.note or ""), cost_basis="Customer concession recorded separately from cash settlement",
            ))
        for payment in self.payments:
            if not _active(payment) or _f(payment.discount) <= 0 or payment.id in represented_payment_ids:
                continue
            code, client_name = self.client_info(payment.client_name, payment.client.code if payment.client else None)
            self.rows.append(_base_row(
                date=payment.date_posted, source="Payment", source_label="Legacy Payment Discount / Waive-Off",
                transaction_type="Discount / Waive-Off", entry_type="other", category="discount_waive",
                reference=_ref(payment, "PAY"), document_type="Payment", document_id=payment.id,
                source_type="Payment", source_id=payment.id, client=client_name, client_code=code,
                material="Discount / Waive-Off", other_loss=_f(payment.discount),
                note=payment.discount_reason or payment.note or "",
                cost_basis="Legacy Payment.discount with no WaiveOff representation; cash amount excluded",
            ))
        allocation_by_id = {alloc.id: alloc for alloc in self.delivery_allocations}
        for payment in DeliveryPersonPayment.query.order_by(DeliveryPersonPayment.date_posted.asc(), DeliveryPersonPayment.id.asc()).all():
            if not _active(payment) or _f(payment.waive_off_amount) <= 0:
                continue
            sale = self.sale_by_id.get(payment.sale_id)
            if sale is None and payment.allocation_id:
                allocation = allocation_by_id.get(payment.allocation_id)
                sale = self.sale_by_id.get(allocation.sale_id) if allocation else None
            if sale is not None and not _active(sale):
                continue
            code, client_name = self.client_info(sale.client_name if sale else "", sale.client_code if sale else None)
            self.rows.append(_base_row(
                date=payment.date_posted, source="DeliveryPersonPayment", source_label="Driver Waive-Off Gain",
                transaction_type="Delivery Cost Concession", entry_type="other", category="delivery_concession",
                reference=(payment.reference or (f"DPP-{payment.id}")), document_type="DeliveryPersonPayment", document_id=payment.id,
                source_type="DeliveryPersonPayment", source_id=payment.id, client=client_name, client_code=code,
                material="Delivery Cost Concession", other_gain=_f(payment.waive_off_amount),
                sale_id=(sale.id if sale else None), note=payment.note or "",
                cost_basis="Explicit driver concession; amount_paid remains a settlement and is excluded",
            ))

    def finish_bookings(self):
        for booking in self.bookings:
            if not _active(booking):
                continue
            code, client_name = self.client_info(booking.client_name)
            unfulfilled = 0.0
            for item in booking.items:
                original = _f(item.qty)
                remaining = self.booking_remaining.get(item.id, original)
                if remaining < -EPS:
                    self.warn("over_supply", f"{_booking_ref(booking)} / {item.material_name}: supplied quantity exceeds current booked quantity by {abs(remaining):g}", document_type="Booking", document_id=booking.id, reference=_booking_ref(booking))
                if remaining > original + EPS:
                    self.warn("return_over_supply", f"{_booking_ref(booking)} / {item.material_name}: returned quantity implies more open balance than booked", document_type="Booking", document_id=booking.id, reference=_booking_ref(booking))
                open_qty = max(0.0, remaining)
                if open_qty <= EPS:
                    continue
                gross_open = open_qty * _f(item.price_at_time)
                booking_gross = self.booking_gross.get(booking.id, 0.0)
                discount_open = gross_open / booking_gross * max(0.0, _f(booking.discount)) if booking_gross > 0 else 0.0
                net_open = max(0.0, gross_open - discount_open)
                unfulfilled += net_open
                self.booking_outstanding.append({
                    "booking_id": booking.id, "booking_item_id": item.id,
                    "reference": _booking_ref(booking), "client": client_name, "client_code": code,
                    "material": item.material_name, "booked_qty": original,
                    "supplied_qty": max(0.0, original - remaining), "remaining_qty": open_qty,
                    "rate": _f(item.price_at_time), "outstanding_value": net_open,
                    "date": booking.date_posted, "paid_amount": _f(booking.paid_amount),
                })
            self.booking_unfulfilled_value[booking.id] = unfulfilled
            # This is deliberately an estimate: later unallocated receipts do
            # not identify which booking order they settle.
            self.booking_advance_estimate[booking.id] = max(0.0, min(
                unfulfilled, _f(booking.paid_amount) - self.booking_recognized_revenue.get(booking.id, 0.0)
            ))

    def build(self):
        self.add_direct_chargeable()
        self.add_booking_supply()
        self.add_delivery_rows()
        self.add_returns()
        self.add_other_events()
        self.finish_bookings()
        self.rows.sort(key=lambda row: (_as_dt(row["date"]) or datetime.min, row["document_id"] or 0, row["source_id"] or 0))
        return self


def _report_match(row, filters):
    client = _norm(filters.get("client"))
    if client:
        if filters.get("_client_fuzzy"):
            if client not in _norm(row.get("client")) and client not in _norm(row.get("client_code")):
                return False
        else:
            client_codes = filters.get("_client_codes", set())
            if _norm(row.get("client")) != client and _norm(row.get("client_code")) not in client_codes:
                return False
    material = _norm(filters.get("material"))
    if material and material not in _norm(row.get("material")) and material not in _norm(row.get("booked_material")):
        return False
    supplier = _norm(filters.get("supplier"))
    if supplier and supplier not in _norm(row.get("supplier")):
        return False
    category = _norm(filters.get("category"))
    if category and category not in {_norm(row.get("category")), _norm(row.get("entry_type")), _norm(row.get("transaction_type"))}:
        return False
    grn_id = filters.get("grn_id")
    if grn_id not in (None, ""):
        try:
            if int(grn_id) not in (row.get("grn_ids") or []):
                return False
        except (TypeError, ValueError):
            return False
    entity_type = _norm(filters.get("entity_type"))
    if entity_type == "category":
        category_value = _norm(filters.get("client_category") or filters.get("client"))
        if category_value and _norm(row.get("client_category")) != category_value:
            return False
    payment_status = _norm(filters.get("payment_status"))
    if payment_status:
        if payment_status == "credit":
            # Any bill with money still owed: unpaid or partly paid.
            if _f(row.get("document_outstanding")) <= EPS:
                return False
        elif _norm(row.get("payment_status_key")) != payment_status:
            return False
    booking_status = _norm(filters.get("booking_status"))
    if booking_status and _norm(row.get("booking_status_key")) != booking_status:
        return False
    transaction_group = _norm(filters.get("transaction_group"))
    if transaction_group and _norm(row.get("transaction_group")) != transaction_group:
        return False
    return True


def _select_rows(rows, start_date, end_date, filters=None):
    filters = filters or {}
    return [row for row in rows if _date_in(row.get("date"), start_date, end_date) and _report_match(row, filters)]


def _sum(rows, key):
    return sum(_f(row.get(key)) for row in rows)


def _merge_sources(sources):
    """Collapse repeated evidence lines for the same lot/rate.

    One quantity can meet the same lot twice (FIFO consumption plus the
    estimate for a shortage), and a sale with two lines of one material can
    produce several entries for one GRN.  Merging keeps the printed layers
    equal to the costed quantity instead of showing the same lot twice.
    """
    merged = {}
    order = []
    for source in sources or []:
        key = (
            source.get("type"), source.get("grn_id"), source.get("grn_item_id"),
            source.get("reference"), round(_f(source.get("rate")), 6),
        )
        entry = merged.get(key)
        if entry is None:
            entry = {}
            merged[key] = entry
            order.append(key)
        entry.update({k: v for k, v in source.items() if v is not None and k not in {"qty", "amount"}})
        entry["qty"] = _f(entry.get("qty")) + _f(source.get("qty"))
        entry["amount"] = _f(entry.get("amount")) + _f(source.get("amount"))
    return [merged[key] for key in order]


def _reversed_sources(candidate, qty):
    """Scale a supply line's evidence to the returned quantity (negative).

    A return reverses the same lots at the same unit costs, so the detail view
    shows which GRNs the returned bags came from rather than one blended rate.
    """
    sources = candidate.get("cost_sources") or []
    candidate_qty = _f(candidate.get("qty"))
    if not sources or candidate_qty <= EPS or qty <= EPS:
        return []
    share = min(1.0, qty / candidate_qty)
    reversed_rows = []
    for source in sources:
        reversed_rows.append({
            **source,
            "qty": -_f(source.get("qty")) * share,
            "amount": -_f(source.get("amount")) * share,
            "reversal_of": candidate.get("reference"),
        })
    return _merge_sources(reversed_rows)


def summarize_rows(rows):
    """Summarize the authoritative rows without recalculating any source event."""
    included = [row for row in rows if row.get("include_in_net")]
    unresolved = [row for row in rows if not row.get("include_in_net")]
    total_revenue = _sum(rows, "revenue")
    total_cogs = _sum(included, "cogs")
    total_expenses = _sum(rows, "expense")
    other_losses = _sum(rows, "other_loss")
    other_gains = _sum(rows, "other_gain")
    unresolved_revenue = _sum(unresolved, "revenue")
    total_profit = _sum(rows, "profit")
    recognized_revenue = _sum(included, "revenue")
    material_rows = [row for row in rows if _f(row.get("material_revenue"))]
    delivery_rows = [row for row in rows if _f(row.get("delivery_revenue")) or _f(row.get("delivery_cost"))]
    actual_material_cost = sum(_f(row.get("material_cost")) for row in included if row.get("cost_status") == COST_ACTUAL)
    estimated_material_cost = sum(_f(row.get("material_cost")) for row in included if row.get("cost_status") == COST_ESTIMATED)
    summary = {
        "gross_material_revenue": sum(
            max(0.0, _f(row.get("gross_revenue")))
            for row in rows
            if row.get("entry_type") != "return" and abs(_f(row.get("material_revenue"))) > EPS
        ),
        "total_material_revenue": _sum(rows, "material_revenue"),
        "total_delivery_revenue": _sum(rows, "delivery_revenue"),
        "total_other_income": _sum(rows, "other_income"),
        "total_revenue": total_revenue,
        "total_material_cost": _sum(included, "material_cost"),
        "actual_material_cost": actual_material_cost,
        "estimated_material_cost": estimated_material_cost,
        "total_delivery_cost": _sum(included, "delivery_cost"),
        "total_cogs": total_cogs,
        "total_expenses": total_expenses,
        "total_other_losses": other_losses,
        "total_other_gains": other_gains,
        "total_discounts": _sum(rows, "discount"),
        "total_discount_loss": _sum(rows, "discount_loss"),
        "material_returns_revenue": -sum(min(0.0, _f(row.get("material_revenue"))) for row in rows),
        "delivery_returns_revenue": -sum(min(0.0, _f(row.get("delivery_revenue"))) for row in rows),
        "returned_material_cost": -sum(min(0.0, _f(row.get("material_cost"))) for row in included),
        "total_profit": total_profit,
        "recorded_cost_profit": sum(_f(row.get("profit")) for row in included if row.get("cost_status") != COST_ESTIMATED),
        "total_profit_sum": sum(_f(row.get("profit")) for row in included if _f(row.get("profit")) > 0),
        "total_loss_sum": -sum(_f(row.get("profit")) for row in included if _f(row.get("profit")) < 0),
        "profit_entry_count": sum(1 for row in included if _f(row.get("profit")) > EPS),
        "loss_entry_count": sum(1 for row in included if _f(row.get("profit")) < -EPS),
        "profit_rows": sum(1 for row in included if _f(row.get("profit")) >= 0),
        "loss_rows": sum(1 for row in included if _f(row.get("profit")) < 0),
        "unknown_cost_rows": len(unresolved),
        "unknown_cost_revenue": sum(max(0.0, _f(row.get("revenue"))) for row in unresolved),
        "unresolved_revenue": unresolved_revenue,
        "estimated_cost_rows": sum(1 for row in included if row.get("cost_status") == COST_ESTIMATED),
        "known_cost_revenue": recognized_revenue,
        "recognized_revenue": recognized_revenue,
        "margin_pct": (total_profit / recognized_revenue * 100.0 if abs(recognized_revenue) > EPS else 0.0),
        "markup_pct": (total_profit / total_cogs * 100.0 if abs(total_cogs) > EPS else 0.0),
        "material_row_count": len(material_rows),
        "delivery_row_count": len(delivery_rows),
        "row_count": len(rows),
    }
    summary["formula_check"] = (
        total_revenue + other_gains - total_cogs - total_expenses - other_losses - unresolved_revenue
    )
    summary["formula_difference"] = summary["formula_check"] - total_profit
    return summary


def _comparison(current, previous, previous_start, previous_end, label):
    metrics = {}
    for key in ("total_revenue", "total_cogs", "total_expenses", "total_profit", "margin_pct"):
        old = _f(previous.get(key))
        new = _f(current.get(key))
        change = new - old
        metrics[key] = {
            "current": new, "previous": old, "change": change,
            "change_pct": (change / abs(old) * 100.0 if abs(old) > EPS else None),
        }
    return {
        "available": bool(previous_start and previous_end),
        "start_date": previous_start, "end_date": previous_end, "label": label,
        "summary": previous, "metrics": metrics,
    }


def _explorer_rows(rows, args):
    selected = list(rows)
    metric = _norm(args.get("metric"))
    metric_map = {
        "revenue": lambda r: _f(r.get("revenue")) > 0,
        "discount_loss": lambda r: _f(r.get("discount_loss")) > 0,
        "cogs": lambda r: r.get("include_in_net") and abs(_f(r.get("cogs"))) > EPS,
        "net_profit": lambda r: bool(r.get("include_in_net")),
        "unknown_cost": lambda r: not r.get("include_in_net"),
        "profit": lambda r: r.get("include_in_net") and _f(r.get("profit")) > EPS,
        "loss": lambda r: r.get("include_in_net") and _f(r.get("profit")) < -EPS,
        "material_revenue": lambda r: abs(_f(r.get("material_revenue"))) > EPS,
        "delivery_revenue": lambda r: abs(_f(r.get("delivery_revenue"))) > EPS,
        "expenses": lambda r: _f(r.get("expense")) > 0,
        "returns": lambda r: r.get("entry_type") == "return",
        "other": lambda r: abs(_f(r.get("other_gain"))) > EPS or abs(_f(r.get("other_loss"))) > EPS or abs(_f(r.get("other_income"))) > EPS,
    }
    if metric in metric_map:
        selected = [row for row in selected if metric_map[metric](row)]
    query = _norm(args.get("q"))
    if query:
        selected = [row for row in selected if query in _norm(" ".join(str(row.get(key) or "") for key in ("reference", "client", "material", "booked_material", "transaction_type", "category", "note")))]
    entry_type = _norm(args.get("entry_type"))
    if entry_type:
        selected = [row for row in selected if entry_type in {_norm(row.get("entry_type")), _norm(row.get("category")), _norm(row.get("source"))}]
    cost_status = _norm(args.get("cost_status"))
    if cost_status:
        selected = [row for row in selected if _norm(row.get("cost_status")) == cost_status]
    def _tiebreak(row):
        return (
            _as_dt(row.get("date")) or datetime.min,
            row.get("document_id") or 0,
            row.get("source_id") or 0,
        )

    sort_keys = {
        "date": lambda row: (_as_dt(row.get("date")) or datetime.min,),
        "reference": lambda row: (_norm(row.get("reference")),),
        "revenue": lambda row: (_f(row.get("revenue")),),
        "cost": lambda row: (_f(row.get("cogs")),),
        "profit": lambda row: (_f(row.get("profit")),),
        "customer": lambda row: (_norm(row.get("client")),),
        "qty": lambda row: (_f(row.get("qty")),),
        "material": lambda row: (_norm(row.get("material")),),
    }
    sort_key = _norm(args.get("sort"))
    order = _norm(args.get("order"))
    if sort_key in sort_keys:
        key_fn = sort_keys[sort_key]
        # The clicked column is the primary key; the date/document tuple only
        # breaks ties, so equal values keep a stable, reproducible order.
        selected.sort(key=lambda row: (key_fn(row), _tiebreak(row)), reverse=(order != "asc"))
    else:
        selected.sort(key=_tiebreak, reverse=True)
    return selected


def _insights(rows):
    by_client = {}
    by_material = {}
    by_day = {}
    by_type = {}
    for row in rows:
        if row.get("entry_type") in {"material_sale", "booking_supply", "delivery_service", "return"}:
            client = row.get("client") or "Unspecified / Account"
            target = by_client.setdefault(client, {"client": client, "sold_revenue": 0.0, "profit_known": 0.0, "rows": 0})
            target["sold_revenue"] += _f(row.get("revenue"))
            target["profit_known"] += _f(row.get("profit"))
            target["rows"] += 1
        material = row.get("material") or "-"
        if material != "-" and row.get("entry_type") not in {"expense", "other", "other_income"}:
            target = by_material.setdefault(material, {"material": material, "sold_qty": 0.0, "sold_revenue": 0.0, "profit_known": 0.0, "unknown_rows": 0})
            target["sold_qty"] += _f(row.get("qty"))
            target["sold_revenue"] += _f(row.get("revenue"))
            target["profit_known"] += _f(row.get("profit"))
            target["unknown_rows"] += 0 if row.get("include_in_net") else 1
        day = _day(row.get("date"))
        day_key = day.isoformat() if day else "Undated"
        target = by_day.setdefault(day_key, {"day": day_key, "date": day, "revenue": 0.0, "cogs": 0.0, "expenses": 0.0, "profit": 0.0, "rows": 0})
        target["revenue"] += _f(row.get("revenue"))
        target["cogs"] += _f(row.get("cogs"))
        target["expenses"] += _f(row.get("expense")) + _f(row.get("other_loss"))
        target["profit"] += _f(row.get("profit"))
        target["rows"] += 1
        type_key = row.get("transaction_type") or "Other"
        target = by_type.setdefault(type_key, {"type": type_key, "revenue": 0.0, "cogs": 0.0, "profit": 0.0, "rows": 0})
        target["revenue"] += _f(row.get("revenue"))
        target["cogs"] += _f(row.get("cogs"))
        target["profit"] += _f(row.get("profit"))
        target["rows"] += 1
    top_clients = sorted(by_client.values(), key=lambda r: r["sold_revenue"], reverse=True)
    top_items = sorted(by_material.values(), key=lambda r: r["sold_revenue"], reverse=True)
    daily = sorted(by_day.values(), key=lambda r: r["day"], reverse=True)
    types = sorted(by_type.values(), key=lambda r: r["revenue"], reverse=True)
    return top_clients, top_items, daily, types


def _payments_received(context, start_date, end_date, filters):
    client_filter = _norm(filters.get("client"))
    total = 0.0
    count = 0
    for payment in context.payments:
        if not _active(payment) or payment.payment_type != "Receipt" or _f(payment.amount) <= 0:
            continue
        if not _date_in(payment.date_posted, start_date, end_date):
            continue
        code, name = context.client_info(payment.client_name, payment.client.code if payment.client else None)
        if client_filter and _norm(name) != client_filter:
            continue
        total += _f(payment.amount)
        count += 1
    # Embedded posting-time receipts are included only when no source-linked
    # Payment row already represents the same receipt.
    linked_receipts = {(p.source_type, p.source_id) for p in context.payments if _active(p) and p.payment_type == "Receipt" and p.source_id}
    for sale in context.sales:
        if ("DirectSale", sale.id) in linked_receipts:
            continue
        if not _active(sale) or _f(sale.paid_amount) <= 0 or not _date_in(sale.date_posted, start_date, end_date):
            continue
        code, name = context.client_info(sale.client_name, sale.client_code)
        if client_filter and _norm(name) != client_filter:
            continue
        total += _f(sale.paid_amount)
        count += 1
    for booking in context.bookings:
        if ("Booking", booking.id) in linked_receipts:
            continue
        if not _active(booking) or _f(booking.paid_amount) <= 0 or not _date_in(booking.date_posted, start_date, end_date):
            continue
        code, name = context.client_info(booking.client_name)
        if client_filter and _norm(name) != client_filter:
            continue
        total += _f(booking.paid_amount)
        count += 1
    return {"amount": total, "count": count}


def _financial_position(context, start_date, end_date, filters):
    client_filter = _norm(filters.get("client"))
    payables = build_current_payables(client_filter=(filters.get("client") or ""), status="all", per_page=200)
    all_receivable_rows = payables.get("all_rows", [])
    customer_receivables = sum(max(0.0, _f(row.get("balance"))) for row in all_receivable_rows)
    customer_credits = sum(max(0.0, -_f(row.get("balance"))) for row in all_receivable_rows)
    supplier_filter = _norm(filters.get("supplier"))
    suppliers = [s for s in Supplier.query.filter_by(is_active=True).order_by(Supplier.name.asc()).all() if not supplier_filter or supplier_filter in _norm(s.name)]
    supplier_overview = build_supplier_payable_overview(suppliers)
    supplier_payables = sum(_f(info.get("outstanding")) for info in supplier_overview.values())
    supplier_advances = sum(_f(info.get("advance")) for info in supplier_overview.values())
    purchases = []
    for grn in context.grns:
        if not _active(grn) or not _date_in(grn.date_posted, start_date, end_date):
            continue
        if supplier_filter and supplier_filter not in _norm(grn.supplier):
            continue
        for item in grn.items:
            if not _active(item):
                continue
            material_filter = _norm(filters.get("material"))
            if material_filter and material_filter not in _norm(item.mat_name):
                continue
            purchases.append({
                "date": grn.date_posted, "reference": _grn_ref(grn), "grn_id": grn.id,
                "supplier": grn.supplier or "", "material": item.mat_name,
                "qty": _f(item.qty), "rate": _f(item.price_at_time),
                "amount": _f(item.qty) * _f(item.price_at_time),
            })
    outstanding = [row for row in context.booking_outstanding if (not client_filter or _norm(row["client"]) == client_filter) and (not _norm(filters.get("material")) or _norm(filters.get("material")) in _norm(row["material"]))]
    advances = sum(context.booking_advance_estimate.get(row["booking_id"], 0.0) for row in {r["booking_id"]: r for r in outstanding}.values())
    return {
        "customer_receivables": customer_receivables,
        "customer_credits": customer_credits,
        "supplier_payables": supplier_payables,
        "supplier_advances": supplier_advances,
        "receivable_rows": sorted(all_receivable_rows, key=lambda r: abs(_f(r.get("balance"))), reverse=True)[:20],
        "supplier_rows": sorted(supplier_overview.values(), key=lambda r: abs(_f(r.get("balance"))), reverse=True)[:20],
        "payments_received": _payments_received(context, start_date, end_date, filters),
        "purchases": purchases,
        "purchase_value": sum(row["amount"] for row in purchases),
        "purchase_qty": sum(row["qty"] for row in purchases),
        "purchase_material_breakdown": list(_insights([
            _base_row(date=row["date"], material=row["material"], qty=row["qty"], material_revenue=row["amount"], entry_type="purchase")
            for row in purchases
        ])[1]),
        "outstanding_bookings": sorted(outstanding, key=lambda r: r["outstanding_value"], reverse=True),
        "outstanding_booking_value": sum(_f(row["outstanding_value"]) for row in outstanding),
        "customer_advances_estimate": advances,
        "current_stock": [{
            "material": material.name, "code": material.code, "qty": _f(material.total),
            "unit": material.unit or "", "category": material.category.name if material.category else "Uncategorized",
        } for material in context.materials],
        "note": "Balances and stock are current positions, not as-of-date profit. Customer advances use only identifiable Booking.paid_amount; later unallocated receipts are not assigned to a booking.",
    }


def _filter_options(context):
    # Newest 300 GRN lots are offered as a stock-batch filter; older lots stay
    # reachable through the item/supplier filters and the GRN screen itself.
    grn_choices = []
    for grn in sorted(
        context.grns, key=lambda row: (_as_dt(row.date_posted) or datetime.min, row.id), reverse=True
    )[:300]:
        if not _active(grn):
            continue
        grn_choices.append({
            "id": grn.id,
            "label": " · ".join(
                part for part in (
                    _grn_ref(grn),
                    (grn.supplier or "").strip(),
                    (_day(grn.date_posted).isoformat() if _day(grn.date_posted) else ""),
                ) if part
            ),
        })
    client_categories = sorted({
        (getattr(client, "category", "") or "").strip()
        for client in context.clients
        if (getattr(client, "category", "") or "").strip()
    })
    return {
        "clients": context.clients,
        "materials": context.materials,
        "services": Service.query.order_by(Service.name.asc()).all(),
        "suppliers": Supplier.query.filter_by(is_active=True).order_by(Supplier.name.asc()).all(),
        "client_categories": client_categories,
        "grn_choices": grn_choices,
        "categories": [
            ("cash_sale", "Cash Sales"), ("credit_sale", "Credit Sales"),
            ("booking_supply", "Booking Supplies"), ("other_sale", "Other / Excess Sales"),
            ("delivery", "Delivery Revenue"), ("sale_return", "Sale Returns"),
            ("booked_return", "Booked Returns"), ("expense", "Expenses"),
            ("other_income", "Other Income"), ("reconciliation", "Reconciliation"),
            ("discount_waive", "Discounts / Waives"),
        ],
    }


def build_profit_loss_report(
    *, start_date=None, end_date=None, client="", material="", supplier="",
    category="", grn_id=None, metric="", q="", entry_type="", cost_status="",
    page=1, per_page=25, include_position=False, previous=True, context=None,
    payment_status="", booking_status="", transaction_group="", entity_type="",
    client_category="", sort="", order="", include_breakdown=True,
):
    """Return the complete report view model from one authoritative projection.

    ``payment_status``, ``booking_status``, ``transaction_group``, ``entity_type``
    and ``client_category`` are the enhanced report filters.  They only select
    rows: no filter changes a revenue, cost or profit calculation.
    ``sort``/``order`` order the paginated transaction table without touching
    the totals, which always cover every matching row.
    """
    if isinstance(start_date, str):
        start_date = _day(start_date)
    if isinstance(end_date, str):
        end_date = _day(end_date)
    if start_date and end_date and start_date > end_date:
        start_date, end_date = end_date, start_date
    context = context or _Projection().build()
    # Attach the settlement / booking status used by the new filters and by the
    # breakdown tables.  Idempotent: a caller may pass a context that already
    # carries them.
    if not getattr(context, "settlement", None):
        attach_document_status(context)
    filters = {
        "client": (client or "").strip(), "material": (material or "").strip(),
        "supplier": (supplier or "").strip(), "category": (category or "").strip(),
        "grn_id": grn_id,
        "payment_status": (payment_status or "").strip(),
        "booking_status": (booking_status or "").strip(),
        "transaction_group": (transaction_group or "").strip(),
        "entity_type": (entity_type or "").strip(),
        "client_category": (client_category or "").strip(),
    }
    # Resolve a typed customer code to its canonical name once.
    if filters["client"] and _norm(filters["entity_type"]) != "category":
        client_obj = context.client_by_code.get(_norm(filters["client"])) or context.client_by_name.get(_norm(filters["client"]))
        if client_obj:
            filters["client"] = client_obj.name
        filters["_client_fuzzy"] = client_obj is None
        filters["_client_codes"] = {_norm(filters["client"]), _norm(client_obj.code) if client_obj else ""}
    current_rows = _select_rows(context.rows, start_date, end_date, filters)
    summary = summarize_rows(current_rows)
    prev_start, prev_end, prev_label = previous_equivalent_period(start_date, end_date) if previous else (None, None, "Comparison disabled")
    previous_rows = _select_rows(context.rows, prev_start, prev_end, filters) if prev_start and prev_end else []
    previous_summary = summarize_rows(previous_rows)
    comparison = _comparison(summary, previous_summary, prev_start, prev_end, prev_label)
    explorer_args = {
        "metric": metric, "q": q, "entry_type": entry_type, "cost_status": cost_status,
        "sort": (sort or "").strip(), "order": (order or "").strip(),
    }
    entries = _explorer_rows(current_rows, explorer_args)
    try:
        per_page = int(per_page)
    except (TypeError, ValueError):
        per_page = 25
    per_page = min(100, max(10, per_page))
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    total_entries = len(entries)
    pages = max(1, (total_entries + per_page - 1) // per_page)
    page = min(max(1, page), pages)
    page_rows = entries[(page - 1) * per_page: page * per_page]
    top_clients, top_items, daily, types = _insights(current_rows)
    export_summary = summarize_rows(entries)
    export_previous_rows = _explorer_rows(previous_rows, explorer_args)
    export_previous_summary = summarize_rows(export_previous_rows)
    export_comparison = _comparison(export_summary, export_previous_summary, prev_start, prev_end, prev_label)
    _export_clients, export_items, export_daily, _export_types = _insights(entries)
    # Reuse the projection's FIFO replay for the GRN/stock view.  Building an
    # independent replay here was the source of GRN-vs-P&L lot differences.
    inventory_view = (
        build_grn_consumption(fifo_context=context) if include_breakdown else None
    )
    breakdown = (
        build_breakdown(
            context, current_rows, start_date=start_date, end_date=end_date,
            filters=filters, inventory=inventory_view,
        )
        if include_breakdown else None
    )
    current_document_keys = {(row.get("document_type"), row.get("document_id")) for row in current_rows}
    report = {
        "context": context,
        "rows": current_rows,
        "all_rows": context.rows,
        "recent_transactions": list(reversed(current_rows))[:10],
        "transactions": page_rows,
        "export_transactions": entries,
        "entries_count": total_entries,
        "pagination": {"page": page, "pages": pages, "per_page": per_page, "total": total_entries, "start": ((page - 1) * per_page + 1 if total_entries else 0), "end": min(page * per_page, total_entries)},
        "summary": summary,
        "previous_summary": previous_summary,
        "comparison": comparison,
        "export_summary": export_summary,
        "export_previous_summary": export_previous_summary,
        "export_comparison": export_comparison,
        "export_daily_insights": export_daily,
        "export_top_items": export_items,
        "previous_rows": previous_rows,
        "start_date": start_date, "end_date": end_date,
        "start_date_str": start_date.isoformat() if start_date else "",
        "end_date_str": end_date.isoformat() if end_date else "",
        "filters": filters,
        "explorer": explorer_args,
        "top_clients": top_clients[:20], "top_items": top_items[:20],
        "daily_insights": daily, "type_insights": types,
        "warnings": [
            warning for warning in context.warnings
            if (warning.get("document_type"), warning.get("document_id")) in current_document_keys
            or (
                warning.get("code") in {"unmatched_dispatch", "orphan_dispatch", "unlinked_zero_supply"}
                and _date_in(warning.get("date"), start_date, end_date)
                and (not filters.get("client") or _norm(filters["client"]) in _norm(warning.get("client")))
                and (not filters.get("material") or _norm(filters["material"]) in _norm(warning.get("material")))
            )
        ],
        "unsupported": [
            "BookingItem and editable source lines have no general rate-history snapshot: earlier activity uses current stored rates where no immutable snapshot exists; available audit detail is shown instead of inventing history.",
            "Hard-deleted source documents cannot be reconstructed from ordinary report rows; only explicit allocation repair archives and audit records are used as evidence.",
            "Standalone imported dispatches without a reliable client/material/open-booking match are warnings only; revenue and cost are never invented.",
            "Unlinked legacy AccountTransaction Payment rows (except identifiable loans/transfers/settlements) are treated as explicit other expenses; review their stored descriptions.",
            "GRN loading, freight, tax and other landed-cost fields are not allocated into the existing FIFO unit-cost records, so they are not silently expensed or added to COGS here.",
            "Customer receipts, supplier payments, driver payments and purchases are position/settlement information, not new P&L events.",
            "Current stock quantities have no authoritative moving-average valuation; stock value is therefore not presented as a profit figure.",
        ],
        "generated_at": pk_now(),
        "voided_source_count": context.voided_source_count,
        "filter_options": _filter_options(context),
        "financial_position": _financial_position(context, start_date, end_date, filters) if include_position else None,
        "breakdown": breakdown,
        "inventory_view": inventory_view,
        "sort": explorer_args["sort"],
        "order": explorer_args["order"],
        "payment_status_choices": PAYMENT_STATUS_CHOICES,
        "booking_status_choices": BOOKING_STATUS_CHOICES,
        "transaction_group_choices": TRANSACTION_GROUP_CHOICES,
        "selected": {
            "payment_status": filters["payment_status"],
            "booking_status": filters["booking_status"],
            "transaction_group": filters["transaction_group"],
            "entity_type": filters["entity_type"],
            "client_category": filters["client_category"],
        },
    }
    # Legacy aliases are intentionally references to the same computed values;
    # the existing plain-language cards remain compatible.
    report.update(summary)
    report.update({
        "client": filters["client"], "material": filters["material"], "supplier": filters["supplier"],
        "category": filters["category"], "grn_id": grn_id, "entry_metric": metric,
        "entry_metric_label": {"profit": "Profit Entries", "loss": "Loss Entries"}.get(metric, ""),
        "payment_received_total": report["financial_position"]["payments_received"]["amount"] if include_position else _payments_received(context, start_date, end_date, filters)["amount"],
        "clients": context.clients,
    })
    return report


def _audit_for(document_type, document_id):
    structured = AccountingAuditLog.query.filter_by(entity_type=document_type, entity_id=document_id).order_by(AccountingAuditLog.created_at.asc()).all()
    generic = AuditLog.query.filter(
        or_(AuditLog.action.ilike(f"%{document_type}%"), AuditLog.details.ilike(f"%{document_type}%")),
        or_(AuditLog.details.ilike(f"%id={document_id}%"), AuditLog.details.ilike(f"%{document_type} {document_id}%"), AuditLog.details.ilike(f"%{document_type}:{document_id}%")),
    ).order_by(AuditLog.timestamp.asc()).all()
    rows = []
    for audit in structured:
        rows.append({
            "date": audit.created_at, "user": audit.username or "System", "action": audit.action,
            "before": audit.before_json, "after": audit.after_json, "reason": audit.reason or "",
        })
    for audit in generic:
        rows.append({"date": audit.timestamp, "user": audit.username or "System", "action": audit.action, "before": "", "after": audit.details or "", "reason": ""})
    rows.sort(key=lambda row: row["date"] or datetime.min)
    return rows


def build_transaction_detail(document_type, document_id, *, start_date=None, end_date=None, context=None):
    """Trace one complete source document through lifetime and selected-period rows."""
    if isinstance(start_date, str):
        start_date = _day(start_date)
    if isinstance(end_date, str):
        end_date = _day(end_date)
    context = context or _Projection().build()
    model_map = {
        "DirectSale": DirectSale, "Booking": Booking, "MaterialReturn": MaterialReturn,
        "Entry": Entry, "AccountTransaction": AccountTransaction, "WaiveOff": WaiveOff,
        "Payment": Payment, "DeliveryPersonPayment": DeliveryPersonPayment,
        "FbmCashDrawerEntry": FbmCashDrawerEntry,
    }
    model = model_map.get(document_type)
    if model is None:
        return None
    source = db.session.get(model, int(document_id))
    if source is None:
        return None

    def belongs(row):
        if row.get("document_type") == document_type and row.get("document_id") == int(document_id):
            return True
        if document_type == "Booking" and row.get("booking_id") == int(document_id):
            return True
        if document_type == "DirectSale" and row.get("sale_id") == int(document_id):
            return True
        if document_type == "DirectSale" and any(src.get("type") == "DirectSale" and src.get("id") == int(document_id) for src in row.get("original_sources", [])):
            return True
        if document_type == "Booking" and any(src.get("type") == "Booking" and src.get("id") == int(document_id) for src in row.get("original_sources", [])):
            return True
        return False

    lifetime_rows = [row for row in context.rows if belongs(row)]
    period_rows = [row for row in lifetime_rows if _date_in(row.get("date"), start_date, end_date)]
    reference = ""
    original_bill_type = None
    client_name = ""
    client_code = ""
    if document_type == "DirectSale":
        reference = _sale_ref(source)
        original_bill_type = "DirectSale"
        client_code, client_name = context.client_info(source.client_name, source.client_code)
    elif document_type == "Booking":
        reference = _booking_ref(source)
        original_bill_type = "Booking"
        client_code, client_name = context.client_info(source.client_name)
    elif document_type == "MaterialReturn":
        reference = _return_ref(source)
        original_bill_type = "MaterialReturn"
        client_code, client_name = context.client_info(source.client_name)
    elif document_type == "Entry":
        reference = _ref(source, "OUT")
        client_code, client_name = context.client_info(source.client, source.client_code)
    elif document_type == "Payment":
        reference = _ref(source, "PAY")
        original_bill_type = "Payment"
        client_code, client_name = context.client_info(source.client_name, source.client.code if source.client else None)
    else:
        reference = getattr(source, "reference", None) or getattr(source, "bill_no", None) or f"{document_type}-{document_id}"
    if not client_name and lifetime_rows:
        client_name = lifetime_rows[0].get("client") or ""
        client_code = lifetime_rows[0].get("client_code") or ""
    payment_history = []
    outstanding = None
    if client_name:
        client_obj = context.client_by_name.get(_norm(client_name))
        if client_obj:
            ledger = build_client_financial_ledger(client_obj)
            for row in ledger.get("rows", []):
                if row.get("type") in {"Payment", "Receipt", "Refund", "Material Return", "Waive-Off"}:
                    payment_history.append(row)
            if document_type in {"DirectSale", "Booking"}:
                obligation = next((o for o in ledger.get("obligations", []) if o.get("source_type") == document_type and o.get("source_id") == int(document_id)), None)
                if obligation:
                    outstanding = {
                        "gross": _f(obligation.get("gross")), "remaining": _f(obligation.get("remaining")),
                        "reference": obligation.get("reference"),
                    }
    archives = []
    if document_type == "DirectSale":
        archives = BookingAllocationRepairArchive.query.filter_by(sale_id=int(document_id)).order_by(BookingAllocationRepairArchive.archived_at.asc()).all()
    elif document_type == "Booking":
        item_ids = [item.id for item in source.items]
        if item_ids:
            archives = BookingAllocationRepairArchive.query.filter(BookingAllocationRepairArchive.booking_item_id.in_(item_ids)).order_by(BookingAllocationRepairArchive.archived_at.asc()).all()
    archive_rows = [{
        "date": archive.archived_at, "reference": f"ARCH-{archive.id}",
        "qty": _f(archive.qty), "reason": archive.repair_reason,
        "violations": archive.violations, "source_json": archive.source_row_json,
        "booking_snapshot": archive.booking_item_snapshot_json,
    } for archive in archives]
    related_booking = None
    if document_type == "DirectSale":
        related_booking = next((row.get("booking_id") for row in lifetime_rows if row.get("booking_id")), None)
    return {
        "document_type": document_type, "document_id": int(document_id), "source": source,
        "reference": reference, "client": client_name, "client_code": client_code,
        "original_bill_type": original_bill_type, "related_booking_id": related_booking,
        "rows": lifetime_rows, "period_rows": period_rows,
        "summary": summarize_rows(lifetime_rows), "period_summary": summarize_rows(period_rows),
        "start_date": start_date, "end_date": end_date,
        "start_date_str": start_date.isoformat() if start_date else "",
        "end_date_str": end_date.isoformat() if end_date else "",
        "payment_history": payment_history, "outstanding": outstanding,
        "audit_rows": _audit_for(document_type, int(document_id)), "archive_rows": archive_rows,
        "history_note": "Current source values and available structured/legacy audit records are shown. DirectSale/Booking line-rate edits are not generally versioned; no earlier rate is invented. Allocation repair archives are shown where available.",
        "generated_at": pk_now(),
    }
