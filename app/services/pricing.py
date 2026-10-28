"""Document pricing — the one place discount arithmetic is allowed to happen.

Why this module exists
----------------------
A bill carries **one** discount for the whole document, but the profit report has
to show revenue **per line**.  Until now that split was recomputed in the report
with a plain division::

    discount_share = gross / bill_gross * discount

A division like that produces repeating decimals (2,000 over a 41,000 bill gives
1,756.0975609756…), and because nothing rounded the result those fractions ended
up in revenue, gross profit, margin and the Excel export.  Rate x Qty stopped
equalling revenue on screen, and a returned rent line even produced a phantom
loss because the return reversed the *gross* rate while the sale had been
recognised *net*.

The rules implemented here
--------------------------
1. **Money is whole paisa.**  Every amount is converted to integer minor units
   (``Decimal`` scaled by 100, rounded half-up) before it is split.
2. **Largest remainder.**  Splitting an amount into parts uses the standard
   largest-remainder method, so the parts always add back to the whole exactly.
   No share is ever left at a repeating decimal.
3. **Stored, not derived.**  A line's discount is written to the document line
   when the document is posted (``DirectSaleItem.discount``,
   ``BookingItem.discount``).  Reports read the stored value; they never divide.
4. **Services opt-in.**  An automatic split (one bill-level amount with no line
   selection) applies to goods lines only.  A service such as rent is sold at a
   fixed rate, so it keeps its full value unless the user explicitly selects it.

The module is deliberately free of Flask, SQLAlchemy and model imports so it can
be unit-tested on its own and used from services, blueprints and reports alike.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

__all__ = [
    "CENT",
    "to_cents",
    "from_cents",
    "cents_to_float",
    "round_money",
    "money_float",
    "allocate_cents",
    "allocate_amount",
    "line_gross_cents",
    "allocate_document_discount",
    "discount_from_named_amounts",
    "split_named_amount",
    "net_cents",
    "resolve_line_discounts",
]

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def _dec(value) -> Decimal:
    """Best-effort conversion to ``Decimal`` (never raises)."""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value if value is not None else 0).strip() or "0")
    except (InvalidOperation, ValueError, TypeError, AttributeError):
        return Decimal("0")


def round_money(value) -> Decimal:
    """Round a money value to two decimals, half-up, dropping negative zero."""
    result = _dec(value).quantize(CENT, rounding=ROUND_HALF_UP)
    return Decimal("0.00") if result == 0 else result


def money_float(value) -> float:
    """Money as the float the legacy columns and templates expect (2 dp)."""
    return float(round_money(value))


def to_cents(value) -> int:
    """Money value -> integer paisa, rounded half-up."""
    return int(round_money(value) * 100)


def from_cents(cents) -> Decimal:
    """Integer paisa -> two-decimal money value."""
    try:
        return (Decimal(int(cents or 0)) / 100).quantize(CENT)
    except (TypeError, ValueError, InvalidOperation):
        return ZERO


def cents_to_float(cents) -> float:
    return float(from_cents(cents))


def line_gross_cents(qty, rate) -> int:
    """``qty x rate`` rounded once to whole paisa.

    Quantity may be fractional (bags, tons); money never is.  This is the single
    place where the multiplication is rounded, so a stored line always shows
    ``Rate x Qty = Gross`` exactly.
    """
    return to_cents(_dec(qty) * _dec(rate))


def net_cents(qty, rate, discount_cents: int = 0) -> int:
    """Line gross minus its own discount, in paisa, never below zero."""
    return max(0, line_gross_cents(qty, rate) - int(discount_cents or 0))


# --------------------------------------------------------------------------- #
# Largest-remainder allocation
# --------------------------------------------------------------------------- #
def allocate_cents(total_cents: int, weights) -> list[int]:
    """Split ``total_cents`` across ``weights`` in whole paisa.

    The returned integers sum to ``total_cents`` exactly.  Weights may be any
    non-negative numbers (line gross values, quantities, arbitrary ratios); they
    do not have to be integers or to add up to anything in particular.

    * Negative or unparseable weights count as zero.
    * When every weight is zero the amount is spread as evenly as possible, so
      a zero-priced set of lines still gets a complete, exact split.
    * The remainder (the paisa that do not divide evenly) goes to the largest
      fractional parts first — the standard, defensible rule.
    """
    total = int(total_cents or 0)
    count = len(weights)
    if count == 0:
        return []

    clean = []
    for weight in weights:
        value = _dec(weight)
        clean.append(value if value > 0 else Decimal("0"))

    weight_sum = sum(clean)
    if weight_sum <= 0:
        # No usable weights: spread evenly, remainder to the earliest lines.
        base, extra = divmod(abs(total), count)
        shares = [base] * count
        for index in range(extra):
            shares[index] += 1
        if total < 0:
            shares = [-share for share in shares]
        return shares

    sign = -1 if total < 0 else 1
    absolute = abs(total)

    exact = [Decimal(absolute) * weight / weight_sum for weight in clean]
    floors = [int(value) for value in exact]          # truncation toward zero
    assigned = sum(floors)
    remainder = absolute - assigned

    order = sorted(
        range(count),
        key=lambda index: (exact[index] - floors[index], clean[index]),
        reverse=True,
    )
    for index in order[:max(0, remainder)]:
        floors[index] += 1

    return [sign * share for share in floors]


def allocate_amount(total, weights) -> list[Decimal]:
    """``allocate_cents`` for money values; returns two-decimal ``Decimal`` list."""
    return [from_cents(share) for share in allocate_cents(to_cents(total), weights)]


# --------------------------------------------------------------------------- #
# Document-level helpers
# --------------------------------------------------------------------------- #
def _line_value_cents(line) -> int:
    """A line's own value in paisa: explicit ``gross`` or ``qty x rate``."""
    if not isinstance(line, dict):
        return 0
    if line.get("gross") is not None:
        return max(0, to_cents(line.get("gross")))
    return max(0, line_gross_cents(line.get("qty", 0), line.get("rate", 0)))


def _eligible_indexes(lines, *, allow_services: bool) -> list[int]:
    if allow_services:
        return list(range(len(lines)))
    goods = [
        index for index, line in enumerate(lines)
        if not (line.get("is_service") if isinstance(line, dict) else False)
    ]
    # A document that only contains services must still be able to carry a
    # discount; falling back keeps the arithmetic total and avoids silently
    # dropping an amount the user typed.
    return goods if goods else list(range(len(lines)))


def allocate_document_discount(
    total_discount,
    lines,
    *,
    allow_services: bool = False,
) -> list[Decimal]:
    """Split one bill-level discount across the document's lines.

    Parameters
    ----------
    total_discount
        The amount typed on the bill.  Returned untouched in total.
    lines
        Sequence of dicts with ``gross`` (qty x rate) and optional
        ``is_service``.  Order is preserved in the result.
    allow_services
        ``False`` (the default) keeps services such as rent at their full
        value: only goods lines receive a share.  A document without goods
        lines falls back to sharing across everything.

    Returns
    -------
    list[Decimal]
        One two-decimal discount per line, summing to ``total_discount``.
    """
    if not lines:
        return []
    shares = [ZERO] * len(lines)
    total_cents = to_cents(total_discount)
    if total_cents <= 0:
        return shares

    eligible = _eligible_indexes(lines, allow_services=allow_services)
    weights = [_line_value_cents(lines[index]) for index in eligible]

    # A discount can never exceed the value it is taken from.
    eligible_gross = sum(weights)
    if eligible_gross <= 0:
        return shares
    capped = min(total_cents, eligible_gross)

    split = allocate_cents(capped, weights)
    for position, index in enumerate(eligible):
        shares[index] = from_cents(split[position])
    return shares


def discount_from_named_amounts(
    named_amounts,
    lines,
    *,
    name_key: str = "name",
) -> list[Decimal]:
    """Honour a per-line discount dict the user selected explicitly.

    ``named_amounts`` maps a line name to the amount typed against it.  When a
    name appears on more than one line the amount is split across those lines by
    value (largest remainder), so selecting "CEMENT-OPC" once still produces an
    exact, stored amount on each matching line.

    Names that are not on the document are ignored; the caller is expected to
    validate them, this function only performs exact arithmetic.
    """
    shares = [ZERO] * len(lines)
    if not named_amounts or not lines:
        return shares

    buckets: dict[str, list[int]] = {}
    for index, line in enumerate(lines):
        key = str(line.get(name_key) or "").strip()
        if key:
            buckets.setdefault(key, []).append(index)

    for raw_name, raw_amount in named_amounts.items():
        key = str(raw_name or "").strip()
        indexes = buckets.get(key)
        if not indexes:
            continue
        amount = round_money(raw_amount)
        if amount <= 0:
            continue
        if len(indexes) == 1:
            shares[indexes[0]] = amount
            continue
        weights = [_line_value_cents(lines[i]) for i in indexes]
        for position, index in enumerate(indexes):
            shares[index] = from_cents(allocate_cents(to_cents(amount), weights)[position])
    return shares


def split_named_amount(amount, indexes, lines) -> list[Decimal]:
    """Split one amount across a subset of line indexes, by line value."""
    if not indexes:
        return []
    weights = [_line_value_cents(lines[i]) for i in indexes]
    return [from_cents(share) for share in allocate_cents(to_cents(amount), weights)]


def cap_shares(shares, lines) -> list[Decimal]:
    """Never let a line's discount exceed that line's own value."""
    out = []
    for share, line in zip(shares, lines):
        limit = _line_value_cents(line)
        value = to_cents(share)
        out.append(from_cents(min(max(0, value), max(0, limit))))
    return out


def sum_money(values) -> Decimal:
    return round_money(sum((round_money(value) for value in values), Decimal("0.00")))


def resolve_line_discounts(lines, bill_discount, named=None):
    """Turn a bill-level discount plus the user's item selection into shares.

    ``lines`` are the document's own lines: ``{"name", "qty", "rate", "gross",
    "is_service"}``.  ``named`` is ``{item name: amount}`` from the discount
    panel.  Returns ``(bill_discount, shares, errors)`` where ``shares`` is
    aligned with ``lines`` and adds back to ``bill_discount`` exactly.

    This is the only place a bill discount becomes per-line amounts.  Sales,
    bookings and returns all call it, so no two pages can disagree.
    """
    errors = []
    bill = round_money(bill_discount)
    named = {str(key).strip(): round_money(value)
             for key, value in (named or {}).items()
             if str(key).strip() and round_money(value) > 0}

    if named:
        known = {(line.get("name") or "").strip() for line in lines}
        unknown = sorted(name for name in named if name not in known)
        if unknown:
            errors.append(
                "Discount was entered for an item that is not on this bill: "
                + ", ".join(unknown)
                + ". Remove it or add the item."
            )
            return float(bill), [ZERO for _ in lines], errors

        shares = discount_from_named_amounts(named, lines)
        selected_total = sum_money(shares)
        if selected_total <= 0:
            return 0.0, [ZERO for _ in lines], errors

        # Anything typed in the bill-level box beyond the selection is spread
        # over the unselected goods lines, so the bill total is still the
        # discount the customer was actually given.
        extra = round_money(bill - selected_total)
        if extra > 0:
            selected_indexes = {index for index, share in enumerate(shares) if share > 0}
            spare = [
                index for index, line in enumerate(lines)
                if index not in selected_indexes
                and not line.get("is_service")
                and to_cents(line.get("gross")) > 0
            ]
            if not spare:
                spare = sorted(selected_indexes)
            if spare:
                add = split_named_amount(extra, spare, lines)
                for index, share in zip(spare, add):
                    shares[index] += share

        shares = cap_shares(shares, lines)
        total = sum_money(shares)
        return float(total), shares, errors

    if bill <= 0 or not lines:
        return float(bill), [ZERO for _ in lines], errors

    shares = cap_shares(allocate_document_discount(bill, lines), lines)
    return float(sum_money(shares)), shares, errors
