"""Discount that a material return gives back.

A sale that carried a discount earned *net* revenue.  When the customer returns
part of it, the return must give back the same net amount: the gross value plus
the discount that belonged to the returned quantity.  Reversing only the gross
value leaves the discount behind on the sale and shows a loss that never
happened (a 5,000 rent line discounted to 4,756.10 used to leave -243.90).

The per-line discount is stored on the document line, so the reversal is a
lookup, not a calculation:

* this module walks the client's own sale lines the same way the profit report
  does -- oldest first, skipping quantity already returned -- and reports the
  discount sitting on the quantity being returned;
* the return stores that amount on ``MaterialReturnItem.discount_reversed`` at
  posting;
* the profit report reads the stored figure.  Nothing divides a bill discount.

Both the return screen and the profit report use this ordering, so they can
never disagree about how much a return gives back.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func

from models import Booking, BookingItem, DirectSale, DirectSaleItem, MaterialReturn, MaterialReturnItem, db

from app.services import pricing

__all__ = [
    "unit_discount_for_source",
    "discount_for_return_line",
]

EPS = 0.005


def _as_datetime(value):
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    return None


def _is_service(name):
    try:
        from app.services.lookups import is_service_name
        return bool(is_service_name(name))
    except Exception:
        return False


def _candidates(client_name, material_name, as_of, return_type):
    """Discounted stock or service lines this return can give back, oldest first."""
    material_name = str(material_name or "").strip()
    client_name = str(client_name or "").strip()
    as_of_dt = _as_datetime(as_of)
    rows = []

    if (return_type or "normal").lower() != "booked":
        query = (
            db.session.query(DirectSaleItem, DirectSale)
            .join(DirectSale, DirectSale.id == DirectSaleItem.sale_id)
            .filter(func.lower(func.trim(DirectSaleItem.product_name)) == material_name.lower())
            .filter(func.lower(func.trim(DirectSale.client_name)) == client_name.lower())
            .filter(DirectSaleItem.discount > 0)
        )
        for item, sale in query.all():
            sale_dt = _as_datetime(sale.date_posted)
            if as_of_dt and sale_dt and sale_dt > as_of_dt:
                continue
            rows.append({
                "kind": "sale",
                "date": sale_dt,
                "order": sale.id,
                "item_id": item.id,
                "qty": float(item.qty or 0),
                "discount": float(item.discount or 0),
            })
    else:
        query = (
            db.session.query(BookingItem, Booking)
            .join(Booking, Booking.id == BookingItem.booking_id)
            .filter(func.lower(func.trim(BookingItem.material_name)) == material_name.lower())
            .filter(func.lower(func.trim(Booking.client_name)) == client_name.lower())
            .filter(BookingItem.discount > 0)
        )
        for item, booking in query.all():
            booking_dt = _as_datetime(booking.date_posted)
            if as_of_dt and booking_dt and booking_dt > as_of_dt:
                continue
            rows.append({
                "kind": "booking",
                "date": booking_dt,
                "order": booking.id,
                "item_id": item.id,
                "qty": float(item.qty or 0),
                "discount": float(item.discount or 0),
            })

    rows.sort(key=lambda row: (row["date"] or datetime.min, row["order"] or 0, row["item_id"] or 0))
    return rows


def _returned_before(client_name, material_name, as_of, return_type):
    """Quantity already returned for this client and material before this return."""
    total = 0.0
    as_of_dt = _as_datetime(as_of)
    rets = (
        db.session.query(MaterialReturnItem, MaterialReturn)
        .join(MaterialReturn, MaterialReturn.id == MaterialReturnItem.material_return_id)
        .filter(func.lower(func.trim(MaterialReturnItem.material_name)) == str(material_name or "").strip().lower())
        .filter(func.lower(func.trim(MaterialReturn.client_name)) == str(client_name or "").strip().lower())
        .all()
    )
    for item, ret in rets:
        if getattr(ret, "is_void", False):
            continue
        ret_dt = _as_datetime(ret.date_posted)
        if as_of_dt and ret_dt and ret_dt > as_of_dt:
            continue
        booked = (ret.return_type or "normal").lower() == "booked"
        if booked != ((return_type or "normal").lower() == "booked"):
            continue
        total += float(item.qty or 0)
    return total


def _unit_discount(row):
    if row["qty"] <= EPS or row["discount"] <= 0:
        return 0.0
    return row["discount"] / row["qty"]


def unit_discount_for_source(client_name, material_name, as_of=None, return_type=None):
    """Per-unit discount on the line this return gives back (one unit)."""
    total = discount_for_return_line(client_name, material_name, 1, as_of, return_type)
    return pricing.money_float(total)


def discount_for_return_line(client_name, material_name, qty, as_of=None, return_type=None):
    """Discount this return line withdraws, in money (whole paisa).

    Walks the same queue the profit report uses: oldest discounted line first,
    minus the quantity already returned.  A return that spans two sale lines
    takes the matching discount from each, so the reversal is still exact.
    """
    qty = float(qty or 0)
    if qty <= EPS:
        return 0.0
    rows = _candidates(client_name, material_name, as_of, return_type)
    if not rows:
        return 0.0

    skip = _returned_before(client_name, material_name, as_of, return_type)
    remaining = qty
    total = 0.0
    for row in rows:
        if remaining <= EPS:
            break
        available = max(0.0, row["qty"] - skip)
        skip = max(0.0, skip - row["qty"])
        if available <= EPS:
            continue
        take = min(remaining, available)
        total += take * _unit_discount(row)
        remaining -= take
    return pricing.money_float(total)
