"""Document → journal lines.  The posting matrix of the master plan (§23).

One builder per document type.  A builder never writes; it returns a list of
:class:`~app.services.ledger.posting.EntrySpec` objects.  ``sync.py`` posts
those and reverses anything the document no longer justifies, which is what
makes edits and voids safe.

Conventions used throughout:

* money is integer paisa (``pricing.to_cents``);
* a trade discount is contra-revenue: revenue is credited **gross** and
  ``4090 Trade discounts allowed`` is debited, so net revenue is right and the
  discount is visible on its own line;
* receivables/payables are the *net* amount the counterparty owes;
* anything whose true classification is unknown goes to a suspense account and
  is listed by the books-agree page — never dropped from profit.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from sqlalchemy import func

from models import (
    db,
    Account,
    AccountTransaction,
    Booking,
    CashFlowEntry,
    Client,
    DeliveryPerson,
    DeliveryPersonPayment,
    DeliveryRent,
    DirectSale,
    GRNAllocation,
    GRN,
    GRNItem,
    MaterialReturn,
    Payment,
    SaleDeliveryPerson,
    Supplier,
    SupplierPayment,
    WaiveOff,
    DirectSaleItem,
    MaterialReturnItem,
)
from app.services import pricing
from app.services.ledger import accounts as coa
from app.services.ledger import periods as period_svc
from app.services.ledger.accounts import GL
from app.services.ledger.posting import EntrySpec, Line

log = logging.getLogger("app.ledger.rules")

__all__ = [
    "DOCUMENT_BUILDERS",
    "build_direct_sale",
    "build_payment",
    "build_waive_off",
    "build_grn",
    "build_supplier_payment",
    "build_material_return",
    "build_driver_payment",
    "build_delivery_rent",
    "build_cash_flow_entry",
    "build_account_transaction",
    "build_booking",
    "build_client_opening",
    "build_supplier_opening",
    "build_account_opening",
    "reset_lookup_cache",
]

_cache: dict[tuple, object] = {}


def reset_lookup_cache() -> None:
    _cache.clear()


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _cents(value) -> int:
    return pricing.to_cents(value or 0)


def _amount_minor(obj, minor_attr: str, value_attr: str) -> int:
    """Authoritative integer amount: the ``*_minor`` mirror when present."""
    minor = getattr(obj, minor_attr, None)
    if minor is not None:
        try:
            return int(minor)
        except (TypeError, ValueError):
            pass
    return _cents(getattr(obj, value_attr, 0))


def _day(*values) -> date:
    for value in values:
        if value:
            return period_svc.normalize_date(value)
    return date.today()


def _service_name(name) -> bool:
    from app.services.lookups import is_service_name

    try:
        return bool(is_service_name(name))
    except Exception:  # pragma: no cover - defensive
        return False


def _cash_code(account_id) -> str:
    """The GL cash account that mirrors one cash/bank/wallet master row.

    Every master row has its own linked GL account (``1010.00001`` …), created
    on demand, so a bank payment can never be reported as cash on hand.  The
    tier account (1010/1020/1030) is the fallback when a document does not say
    which account it used.
    """
    if not account_id:
        return GL.CASH
    key = ("cash", int(account_id))
    if key in _cache:
        return _cache[key]
    try:
        code = coa.cash_gl_for_account(int(account_id)).code
    except Exception:  # pragma: no cover - chart unavailable
        code = GL.CASH
    _cache[key] = code
    return code


def _client_id(sale) -> int | None:
    if getattr(sale, "client_id", None):
        return int(sale.client_id)
    name = (getattr(sale, "client_name", None) or "").strip()
    if not name:
        return None
    key = ("client", name.lower())
    if key in _cache:
        return _cache[key]
    row = Client.query.filter(func.lower(Client.name) == name.lower()).first()
    _cache[key] = row.id if row else None
    return _cache[key]


def _supplier_id(grn) -> int | None:
    if getattr(grn, "supplier_id", None):
        return int(grn.supplier_id)
    name = (getattr(grn, "supplier", None) or "").strip()
    if not name:
        return None
    key = ("supplier", name.lower())
    if key in _cache:
        return _cache[key]
    row = Supplier.query.filter(func.lower(Supplier.name) == name.lower()).first()
    _cache[key] = row.id if row else None
    return _cache[key]


def _driver_id_by_name(name) -> int | None:
    name = (name or "").strip()
    if not name:
        return None
    key = ("driver", name.lower())
    if key in _cache:
        return _cache[key]
    row = DeliveryPerson.query.filter(func.lower(DeliveryPerson.name) == name.lower()).first()
    _cache[key] = row.id if row else None
    return _cache[key]


class _Balances:
    """Signed (debit-positive) accumulator that refuses to emit an open entry."""

    def __init__(self) -> None:
        self._rows: dict[tuple, int] = {}

    def add(self, code, amount, *, client_id=None, supplier_id=None,
            delivery_person_id=None, material=None, service=None, memo=None):
        amount = int(amount or 0)
        if not amount:
            return self
        key = (code, client_id, supplier_id, delivery_person_id, material, service, memo)
        self._rows[key] = self._rows.get(key, 0) + amount
        return self

    @property
    def total(self) -> int:
        return sum(self._rows.values())

    def lines(self) -> list[Line]:
        lines = []
        for key, amount in self._rows.items():
            if amount == 0:
                continue
            code, client_id, supplier_id, driver_id, material, service, memo = key
            lines.append(Line(
                code=code,
                debit=amount if amount > 0 else 0,
                credit=-amount if amount < 0 else 0,
                client_id=client_id,
                supplier_id=supplier_id,
                delivery_person_id=driver_id,
                material_name=material,
                service_name=service,
                memo=memo,
            ))
        return lines


def _spec(event: str, day, description: str, balances: _Balances, *, reference=None,
          adjustment: bool = False) -> EntrySpec | None:
    lines = balances.lines()
    if not lines:
        return None
    return EntrySpec(
        event=event,
        entry_date=_day(day),
        description=description,
        reference=reference,
        lines=lines,
        adjustment=adjustment,
    )


def _specs(*items) -> list[EntrySpec]:
    return [item for item in items if item is not None]


def _bill_ref(obj, fallback_id) -> str:
    return (
        getattr(obj, "auto_bill_no", None)
        or getattr(obj, "manual_bill_no", None)
        or f"#{fallback_id}"
    )


# ---------------------------------------------------------------------------
# GRN — goods in, payable up
# ---------------------------------------------------------------------------
def build_grn(grn: GRN) -> list[EntrySpec]:
    if getattr(grn, "is_void", False):
        return []
    items = [item for item in grn.items if not getattr(item, "is_void", False)]
    goods = _cents(sum(float(item.qty or 0) * float(item.price_at_time or 0) for item in items))
    landed = _cents(
        float(grn.loading_cost or 0)
        + float(grn.freight_cost or 0)
        + float(grn.other_expense or 0)
        + float(grn.adjustment_amount or 0)
    )
    tax = _cents(grn.tax_amount or 0)
    discount = _cents(grn.discount or 0)
    total = goods + landed + tax - discount
    if total == 0 and goods == 0:
        return []

    balances = _Balances()
    supplier_id = _supplier_id(grn)
    balances.add(GL.INVENTORY, goods + landed, supplier_id=supplier_id, memo="goods received")
    balances.add(GL.INPUT_TAX, tax, supplier_id=supplier_id, memo="input tax")
    balances.add(GL.INVENTORY, -discount, supplier_id=supplier_id, memo="supplier discount")
    balances.add(GL.PAYABLE, -total, supplier_id=supplier_id, memo="supplier invoice")
    return _specs(_spec(
        "grn",
        grn.date_posted,
        f"GRN {_bill_ref(grn, grn.id)} received from {grn.supplier or 'supplier'}",
        balances,
        reference=_bill_ref(grn, grn.id),
    ))


# ---------------------------------------------------------------------------
# Supplier payment — payable down, cash out
# ---------------------------------------------------------------------------
def build_supplier_payment(payment: SupplierPayment) -> list[EntrySpec]:
    if getattr(payment, "is_void", False):
        return []
    amount = _amount_minor(payment, "amount_minor", "amount")
    if amount <= 0:
        return []
    supplier_id = payment.supplier_id
    kind = (payment.payment_type or "Payment").strip().lower()
    cash = _cash_code(payment.payment_account_id)
    balances = _Balances()
    if kind == "refund":
        balances.add(cash, amount, supplier_id=supplier_id, memo="supplier refund")
        balances.add(GL.PAYABLE, -amount, supplier_id=supplier_id, memo="supplier refund")
        event = "supplier_refund"
    else:
        balances.add(GL.PAYABLE, amount, supplier_id=supplier_id, memo="supplier payment")
        balances.add(cash, -amount, supplier_id=supplier_id, memo="cash paid")
        event = "supplier_payment"
    return _specs(_spec(
        event,
        payment.date_posted,
        f"Supplier {kind} {amount / 100:,.2f}",
        balances,
        reference=(payment.note or "")[:80] or None,
    ))


# ---------------------------------------------------------------------------
# Direct sale — revenue, discount, receivable, cash, COGS
# ---------------------------------------------------------------------------
def _sale_items(sale) -> list[DirectSaleItem]:
    # Query explicitly: a relationship loaded before the lines were written in
    # the same transaction can be stale, and posting must see the real lines.
    return (
        DirectSaleItem.query.filter_by(sale_id=sale.id)
        .order_by(DirectSaleItem.id.asc())
        .all()
    )


def build_direct_sale(sale: DirectSale) -> list[EntrySpec]:
    if getattr(sale, "is_void", False):
        return []
    items = _sale_items(sale)
    gross_goods = 0
    gross_services = 0
    for item in items:
        gross = _cents(float(item.qty or 0) * float(item.price_at_time or 0))
        if _service_name(item.product_name):
            gross_services += gross
        else:
            gross_goods += gross
    gross = gross_goods + gross_services

    discount = _cents(sale.discount or 0)
    line_discounts = _cents(sum(float(item.discount or 0) for item in items))
    # Per-line discounts are the authoritative split once Phase A has run; the
    # bill-level amount is only the fallback for rows entered before it.
    if line_discounts and not discount:
        discount = line_discounts
    net = gross - discount
    paid = _cents(sale.paid_amount or 0)
    client_id = _client_id(sale)
    cash = _cash_code(sale.payment_account_id)

    specs: list[EntrySpec] = []
    balances = _Balances()
    if gross:
        balances.add(GL.SALES_GOODS, -gross_goods, client_id=client_id, memo="goods sold")
        balances.add(GL.SALES_SERVICES, -gross_services, client_id=client_id, memo="services sold")
    balances.add(GL.TRADE_DISCOUNTS, discount, client_id=client_id, memo="trade discount")
    if net:
        balances.add(GL.RECEIVABLE, net, client_id=client_id, memo="balance due")
    specs.append(_spec(
        "sale",
        sale.date_posted,
        f"Sale {_bill_ref(sale, sale.id)} to {sale.client_name or 'customer'}",
        balances,
        reference=_bill_ref(sale, sale.id),
    ))

    # Cash received with the sale is its own event: the customer owes the net
    # amount, and any money handed over at the counter pays it down.
    if paid:
        receipt = _Balances()
        receipt.add(cash, paid, client_id=client_id, memo="received with sale")
        applied = min(paid, max(net, 0))
        if applied:
            receipt.add(GL.RECEIVABLE, -applied, client_id=client_id, memo="receivable settled")
        if paid > applied:
            receipt.add(GL.CUSTOMER_ADVANCES, -(paid - applied), client_id=client_id,
                        memo="customer advance")
        specs.append(_spec(
            "sale_receipt",
            sale.date_posted,
            f"Cash received on {_bill_ref(sale, sale.id)}",
            receipt,
            reference=_bill_ref(sale, sale.id),
        ))

    # Cost of goods sold — frozen FIFO cost, not a re-count at report time.
    cost = _cost_of_sale(sale, items)
    if cost:
        cogs = _Balances()
        cogs.add(GL.COGS_GOODS, cost, material=None, memo="cost of goods sold")
        cogs.add(GL.INVENTORY, -cost, memo="stock issued")
        specs.append(_spec(
            "sale_cogs",
            sale.date_posted,
            f"Cost of sale {_bill_ref(sale, sale.id)}",
            cogs,
            reference=_bill_ref(sale, sale.id),
        ))

    # Driver rent: charged to the delivery cost, owed to the driver.
    allocations = (
        SaleDeliveryPerson.query.filter_by(sale_id=sale.id, is_void=False)
        .order_by(SaleDeliveryPerson.id.asc())
        .all()
    )
    driver_balances = _Balances()
    for allocation in allocations:
        rent = _cents(allocation.rent_amount or 0)
        if not rent:
            continue
        driver_balances.add(GL.COGS_SERVICES, rent,
                            delivery_person_id=allocation.delivery_person_id,
                            service="Delivery rent", memo="delivery rent")
        driver_balances.add(GL.DRIVER_PAYABLE, -rent,
                            delivery_person_id=allocation.delivery_person_id,
                            memo="due to driver")
    if driver_balances.total == 0 and not allocations:
        # Legacy sales carry their rent on the sale itself.
        legacy = _cents(sale.delivery_rent_cost or 0)
        driver_id = _driver_id_by_name(sale.driver_name)
        if legacy:
            driver_balances.add(GL.COGS_SERVICES, legacy, delivery_person_id=driver_id,
                                service="Delivery rent", memo="delivery rent")
            driver_balances.add(GL.DRIVER_PAYABLE, -legacy, delivery_person_id=driver_id,
                                memo="due to driver")
    specs.append(_spec(
        "delivery_rent",
        sale.date_posted,
        f"Delivery rent on {_bill_ref(sale, sale.id)}",
        driver_balances,
        reference=_bill_ref(sale, sale.id),
    ))

    # Service rent revenue recognised on the sale itself (a rental line that is
    # not a delivered material) is already in gross_services above; the cost of
    # running it belongs to the driver and is posted as delivery rent.
    return _specs(*specs)


def _cost_of_sale(sale, items) -> int:
    allocations = (
        db.session.query(
            func.coalesce(func.sum(GRNAllocation.qty * GRNAllocation.cost_rate), 0)
        )
        .filter(
            GRNAllocation.sale_id == sale.id,
            GRNAllocation.is_void.is_(False),
        )
        .scalar()
        or 0
    )
    cost = _cents(allocations)
    if cost:
        return cost
    return _cents(sum(
        float(item.qty or 0) * float(item.cost_rate_at_sale or 0)
        for item in items
        if not _service_name(item.product_name)
    ))


# ---------------------------------------------------------------------------
# Booking — order taken, advance received
# ---------------------------------------------------------------------------
def build_booking(booking: Booking) -> list[EntrySpec]:
    if getattr(booking, "is_void", False):
        return []
    items = list(booking.items or [])
    gross = _cents(sum(float(item.qty or 0) * float(item.price_at_time or 0) for item in items))
    if not gross:
        gross = _cents(booking.amount or 0)
    discount = _cents(booking.discount or 0)
    net = gross - discount
    if net == 0 and not discount:
        return []
    client_id = None
    name = (booking.client_name or "").strip()
    if name:
        row = Client.query.filter(func.lower(Client.name) == name.lower()).first()
        client_id = row.id if row else None

    goods = 0
    services = 0
    for item in items:
        amount = _cents(float(item.qty or 0) * float(item.price_at_time or 0))
        if _service_name(item.material_name):
            services += amount
        else:
            goods += amount
    if not goods and not services:
        goods = gross

    balances = _Balances()
    balances.add(GL.SALES_GOODS, -goods, client_id=client_id, memo="booked goods")
    balances.add(GL.SALES_SERVICES, -services, client_id=client_id, memo="booked services")
    balances.add(GL.TRADE_DISCOUNTS, discount, client_id=client_id, memo="booking discount")
    balances.add(GL.RECEIVABLE, net, client_id=client_id, memo="booking due")
    specs = [_spec(
        "booking",
        booking.date_posted,
        f"Booking {_bill_ref(booking, booking.id)} for {booking.client_name or 'customer'}",
        balances,
        reference=_bill_ref(booking, booking.id),
    )]

    paid = _cents(booking.paid_amount or 0)
    if paid:
        advance = _Balances()
        advance.add(_cash_code(booking.receive_in_account_id), paid,
                    client_id=client_id, memo="advance received")
        advance.add(GL.RECEIVABLE, -paid, client_id=client_id, memo="advance applied")
        specs.append(_spec(
            "booking_advance",
            booking.date_posted,
            f"Advance on booking {_bill_ref(booking, booking.id)}",
            advance,
            reference=_bill_ref(booking, booking.id),
        ))
    return _specs(*specs)


# ---------------------------------------------------------------------------
# Client receipt / refund / waive-off
# ---------------------------------------------------------------------------
def build_payment(payment: Payment) -> list[EntrySpec]:
    if getattr(payment, "is_void", False):
        return []
    amount = _amount_minor(payment, "amount_minor", "amount")
    discount = _amount_minor(payment, "discount_minor", "discount")
    if amount <= 0 and discount <= 0:
        return []
    client_id = payment.client_id
    if client_id is None and (payment.client_name or "").strip():
        row = Client.query.filter(
            func.lower(Client.name) == (payment.client_name or "").strip().lower()
        ).first()
        client_id = row.id if row else None
    cash = _cash_code(payment.payment_account_id)
    kind = (payment.payment_type or "Receipt").strip().lower()
    specs: list[EntrySpec] = []

    if kind in ("receipt", "payment", "advance"):
        balances = _Balances()
        balances.add(cash, amount, client_id=client_id, memo="received from client")
        balances.add(GL.RECEIVABLE, -(amount + discount), client_id=client_id,
                     memo="receivable settled")
        balances.add(GL.WAIVE_OFF, discount, client_id=client_id, memo="waive-off")
        specs.append(_spec(
            "receipt",
            payment.date_posted,
            f"Receipt from {payment.client_name or 'client'}",
            balances,
            reference=(payment.manual_bill_no or payment.auto_bill_no or "")[:80] or None,
        ))
    elif kind == "refund":
        balances = _Balances()
        balances.add(GL.RECEIVABLE, amount, client_id=client_id, memo="refund to client")
        balances.add(cash, -amount, client_id=client_id, memo="cash refunded")
        specs.append(_spec(
            "refund",
            payment.date_posted,
            f"Refund to {payment.client_name or 'client'}",
            balances,
        ))
    # 'Material Return' and 'Waive-Off' payments are represented by their own
    # documents (MaterialReturn / WaiveOff) so a receipt is not counted twice.
    return _specs(*specs)


def build_waive_off(waive: WaiveOff) -> list[EntrySpec]:
    if getattr(waive, "is_void", False):
        return []
    amount = _amount_minor(waive, "amount_minor", "amount")
    if amount <= 0:
        return []
    client_id = getattr(waive, "client_id", None)
    if client_id is None and (getattr(waive, "client_name", "") or "").strip():
        row = Client.query.filter(
            func.lower(Client.name) == (waive.client_name or "").strip().lower()
        ).first()
        client_id = row.id if row else None
    balances = _Balances()
    balances.add(GL.WAIVE_OFF, amount, client_id=client_id, memo="amount waived")
    balances.add(GL.RECEIVABLE, -amount, client_id=client_id, memo="receivable written off")
    return _specs(_spec(
        "waive_off",
        waive.date_posted,
        f"Waive-off {amount / 100:,.2f} for {getattr(waive, 'client_name', '') or 'client'}",
        balances,
        reference=(getattr(waive, "note", "") or "")[:80] or None,
    ))


# ---------------------------------------------------------------------------
# Material return — revenue down, receivable down
# ---------------------------------------------------------------------------
def build_material_return(ret: MaterialReturn) -> list[EntrySpec]:
    if getattr(ret, "is_void", False):
        return []
    amount = _cents(ret.amount or 0)
    if not amount:
        items = MaterialReturnItem.query.filter_by(material_return_id=ret.id).all()
        amount = _cents(sum(
            float(item.qty or 0) * float(item.unit_rate or item.price_at_time or 0)
            for item in items
        ))
    if not amount:
        return []
    client_id = None
    name = (ret.client_name or "").strip()
    if name:
        row = Client.query.filter(func.lower(Client.name) == name.lower()).first()
        client_id = row.id if row else None
    balances = _Balances()
    balances.add(GL.SALES_GOODS, amount, client_id=client_id, memo="goods returned")
    balances.add(GL.RECEIVABLE, -amount, client_id=client_id, memo="credit note")
    return _specs(_spec(
        "return",
        ret.date_posted,
        f"Return {_bill_ref(ret, ret.id)} from {ret.client_name or 'client'}",
        balances,
        reference=_bill_ref(ret, ret.id),
    ))


# ---------------------------------------------------------------------------
# Driver settlement and standalone delivery rent
# ---------------------------------------------------------------------------
def build_driver_payment(payment: DeliveryPersonPayment) -> list[EntrySpec]:
    paid = _amount_minor(payment, "amount_paid_minor", "amount_paid")
    waived = _amount_minor(payment, "waive_off_minor", "waive_off_amount")
    if paid <= 0 and waived <= 0:
        return []
    driver_id = payment.delivery_person_id
    if paid <= 0:
        paid = 0
    if waived <= 0:
        waived = 0
    cash = _cash_code(payment.payment_account_id)
    balances = _Balances()
    balances.add(GL.DRIVER_PAYABLE, paid + waived, delivery_person_id=driver_id,
                 memo="driver settled")
    if paid:
        balances.add(cash, -paid, delivery_person_id=driver_id, memo="cash paid to driver")
    if waived:
        balances.add(GL.OTHER_INCOME, -waived, delivery_person_id=driver_id,
                     memo="driver waive-off")
    return _specs(_spec(
        "driver_payment",
        payment.date_posted,
        f"Driver settlement {paid / 100:,.2f}"
        + (f" (+{waived / 100:,.2f} waived)" if waived else ""),
        balances,
        reference=(payment.reference or "")[:80] or None,
    ))


def build_delivery_rent(rent: DeliveryRent) -> list[EntrySpec]:
    if getattr(rent, "is_void", False):
        return []
    # A sale with per-driver allocations already carries its rent on the sale;
    # the standalone row exists for sales without allocations.  Posting both
    # would double-count the cost.
    if rent.sale_id:
        allocated = (
            SaleDeliveryPerson.query.filter_by(sale_id=rent.sale_id, is_void=False)
            .count()
        )
        if allocated:
            return []
    amount = _cents(rent.amount or 0)
    if amount <= 0:
        return []
    driver_id = _driver_id_by_name(rent.delivery_person_name)
    balances = _Balances()
    balances.add(GL.COGS_SERVICES, amount, delivery_person_id=driver_id,
                 service="Delivery rent", memo="delivery rent")
    balances.add(GL.DRIVER_PAYABLE, -amount, delivery_person_id=driver_id,
                 memo="due to driver")
    return _specs(_spec(
        "delivery_rent",
        rent.date_posted,
        f"Delivery rent to {rent.delivery_person_name or 'driver'}",
        balances,
        reference=(rent.bill_no or "")[:80] or None,
    ))


# ---------------------------------------------------------------------------
# Cash: manual cash-flow rows and account transfers
# ---------------------------------------------------------------------------
def build_cash_flow_entry(entry: CashFlowEntry) -> list[EntrySpec]:
    if getattr(entry, "is_void", False):
        return []
    amount = _amount_minor(entry, "amount_minor", "amount")
    if amount <= 0:
        return []
    direction = (entry.direction or "").strip().lower()
    source = _cash_code(entry.account_id)
    balances = _Balances()
    if direction == "transfer":
        destination = _cash_code(entry.destination_account_id)
        if destination == source:
            return []
        balances.add(destination, amount, memo="cash transferred in")
        balances.add(source, -amount, memo="cash transferred out")
        event = "cash_transfer"
    elif direction == "in":
        balances.add(source, amount, memo=entry.description or "cash received")
        balances.add(GL.OTHER_INCOME, -amount, memo=entry.category_id and "category" or "cash received")
        event = "cash_in"
    else:
        balances.add(GL.UNCLASSIFIED_EXPENSE, amount, memo=entry.description or "cash spent")
        balances.add(source, -amount, memo="cash paid")
        event = "cash_out"
    return _specs(_spec(
        event,
        entry.date_posted,
        entry.description or entry.note or f"Cash {direction} {amount / 100:,.2f}",
        balances,
        reference=(entry.reference or "")[:80] or None,
    ))


def build_account_transaction(tx: AccountTransaction) -> list[EntrySpec]:
    if getattr(tx, "is_void", False):
        return []
    if tx.source_type:
        # A document (Payment, DeliveryPersonPayment, CashFlowEntry, …) owns
        # this movement and posts it from its own builder.
        return []
    amount = _amount_minor(tx, "amount_minor", "amount")
    if amount <= 0:
        return []
    balances = _Balances()
    if tx.to_account_id and tx.from_account_id:
        balances.add(_cash_code(tx.to_account_id), amount, memo="transfer in")
        balances.add(_cash_code(tx.from_account_id), -amount, memo="transfer out")
        event = "cash_transfer"
    elif tx.to_account_id:
        balances.add(_cash_code(tx.to_account_id), amount, memo="cash received")
        balances.add(GL.OTHER_INCOME, -amount, memo="cash received")
        event = "cash_in"
    elif tx.from_account_id:
        balances.add(GL.UNCLASSIFIED_EXPENSE, amount, memo="cash paid")
        balances.add(_cash_code(tx.from_account_id), -amount, memo="cash paid")
        event = "cash_out"
    else:
        return []
    return _specs(_spec(
        event,
        tx.date_posted,
        tx.description or tx.note or f"Account movement {amount / 100:,.2f}",
        balances,
        reference=(tx.transaction_type or "")[:80] or None,
    ))


# ---------------------------------------------------------------------------
# Opening balances — once, dated, on the balance sheet
# ---------------------------------------------------------------------------
def build_client_opening(client: Client) -> list[EntrySpec]:
    opening = _cents(client.opening_balance or 0)
    if not opening or (client.transferred_to_id and not client.is_active):
        return []
    balances = _Balances()
    balances.add(GL.RECEIVABLE, opening, client_id=client.id, memo="opening balance")
    balances.add(GL.OPENING_EQUITY, -opening, client_id=client.id, memo="opening balance")
    return _specs(_spec(
        "opening",
        getattr(client, "opening_balance_date", None) or getattr(client, "created_at", None),
        f"Opening balance — {client.name}",
        balances,
    ))


def build_supplier_opening(supplier: Supplier) -> list[EntrySpec]:
    opening = _cents(supplier.opening_balance or 0)
    if not opening or (supplier.transferred_to_id and not supplier.is_active):
        return []
    balances = _Balances()
    balances.add(GL.PAYABLE, -opening, supplier_id=supplier.id, memo="opening balance")
    balances.add(GL.OPENING_EQUITY, opening, supplier_id=supplier.id, memo="opening balance")
    return _specs(_spec(
        "opening",
        getattr(supplier, "opening_balance_date", None) or getattr(supplier, "created_at", None),
        f"Opening balance — {supplier.name}",
        balances,
    ))


def build_account_opening(account: Account) -> list[EntrySpec]:
    opening = _amount_minor(account, "opening_balance_minor", "opening_balance")
    if not opening:
        opening = _cents(account.opening_balance or 0)
    if not opening:
        return []
    balances = _Balances()
    balances.add(_cash_code(account.id), opening, memo="opening balance")
    balances.add(GL.OPENING_EQUITY, -opening, memo="opening balance")
    return _specs(_spec(
        "opening",
        getattr(account, "opening_balance_date", None) or getattr(account, "created_at", None),
        f"Opening balance — {account.name}",
        balances,
    ))


DOCUMENT_BUILDERS = {
    "GRN": build_grn,
    "SupplierPayment": build_supplier_payment,
    "DirectSale": build_direct_sale,
    "Booking": build_booking,
    "Payment": build_payment,
    "WaiveOff": build_waive_off,
    "MaterialReturn": build_material_return,
    "DeliveryPersonPayment": build_driver_payment,
    "DeliveryRent": build_delivery_rent,
    "CashFlowEntry": build_cash_flow_entry,
    "AccountTransaction": build_account_transaction,
    "Client": build_client_opening,
    "Supplier": build_supplier_opening,
    "Account": build_account_opening,
}
