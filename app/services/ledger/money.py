"""The single money type used by the book.

The application already stores money as whole paisa (``utils/money.to_minor``)
and does its document-level arithmetic in ``app/services/pricing.py``.  This
module is the accounting-facing front door to those helpers: a small immutable
``Money`` value object plus the largest-remainder ``allocate`` used whenever one
amount has to be split into parts that must still add back to the whole.

Nothing here touches the database or Flask — it is unit-testable on its own.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from app.services import pricing

__all__ = [
    "Money",
    "ZERO",
    "to_minor",
    "from_minor",
    "as_float",
    "allocate",
    "sum_minor",
    "format_minor",
]

ZERO = 0


class Money:
    """An amount in whole paisa (integers), never a float."""

    __slots__ = ("minor",)

    def __init__(self, minor=0):
        self.minor = int(minor or 0)

    # -- construction ------------------------------------------------------
    @classmethod
    def parse(cls, value) -> "Money":
        """Build from paisa (``int``/``Money``) or from a typed rupee amount.

        Strings and ``Decimal``/``float`` values are rupee amounts, exactly as
        they are typed into a form ("1,200.00"); a bare ``int`` is already whole
        paisa, because that is what the database and the journal store.
        """
        if isinstance(value, Money):
            return cls(value.minor)
        if isinstance(value, bool):
            return cls(int(value))
        if isinstance(value, int):
            return cls(value)
        text = str(value).strip().replace(",", "").replace("Rs.", "").replace("Rs", "")
        text = text.replace("PKR", "").strip() or "0"
        return cls(pricing.to_cents(text))

    @classmethod
    def from_minor(cls, minor) -> "Money":
        return cls(minor)

    # -- arithmetic --------------------------------------------------------
    def add(self, other) -> "Money":
        return Money(self.minor + Money.parse(other).minor)

    def sub(self, other) -> "Money":
        return Money(self.minor - Money.parse(other).minor)

    def neg(self) -> "Money":
        return Money(-self.minor)

    def mul_qty(self, qty) -> "Money":
        """``rate x qty`` with a single half-up rounding to paisa."""
        return Money(pricing.line_gross_cents(qty, self.decimal()))

    def pct(self, rate) -> "Money":
        return Money(pricing.to_cents(self.decimal() * pricing._dec(rate) / Decimal(100)))

    # -- presentation ------------------------------------------------------
    def decimal(self) -> Decimal:
        return pricing.from_cents(self.minor)

    def as_float(self) -> float:
        return pricing.cents_to_float(self.minor)

    def format(self, symbol: str = "") -> str:
        text = f"{format_minor(self.minor)}"
        return f"{symbol}{text}" if symbol else text

    # -- dunder ------------------------------------------------------------
    def __eq__(self, other):
        try:
            return self.minor == Money.parse(other).minor
        except Exception:
            return NotImplemented

    def __hash__(self):
        return hash(self.minor)

    def __int__(self):
        return self.minor

    def __bool__(self):
        return self.minor != 0

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"Money({self.minor})"


def to_minor(value) -> int:
    """Rupee value (any type the app accepts) → integer paisa."""
    return pricing.to_cents(value)


def from_minor(minor) -> Decimal:
    return pricing.from_cents(minor)


def as_float(minor) -> float:
    return pricing.cents_to_float(minor)


def allocate(total_minor: int, weights) -> list[int]:
    """Split *total_minor* across *weights* in whole paisa (largest remainder)."""
    return pricing.allocate_cents(int(total_minor or 0), weights)


def sum_minor(values) -> int:
    return int(sum(int(v or 0) for v in values))


def format_minor(minor, *, decimals: int = 2) -> str:
    """Format paisa for a template/PDF without ever printing a fraction."""
    from decimal import Decimal

    value = (Decimal(int(minor or 0)) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if decimals == 0:
        return f"{value:,.0f}"
    return f"{value:,.{decimals}f}"
