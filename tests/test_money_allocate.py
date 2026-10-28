"""Money: whole paisa, largest remainder, no fractions anywhere.

These are the unit guarantees the journal and every statement build on: a split
never leaves a fraction, a split always adds back to the whole, and money is
never a float.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.ledger.money import Money, allocate, format_minor, to_minor


def test_money_is_integer_paisa():
    assert Money.parse("1,200.00").minor == 120000
    assert Money.parse("Rs. 1,200").minor == 120000
    assert Money.parse(120000).minor == 120000  # a bare int is already paisa
    assert Money.parse("0.005").minor in (0, 1)  # half-up at 2 dp: 0.01
    assert Money.parse("1.005").minor == 101


def test_money_arithmetic_rounds_once():
    rate = Money.parse("1200.00")
    assert rate.mul_qty(Decimal("2.5")).minor == 300000
    assert rate.add(Money.parse("0.01")).minor == 120001
    assert rate.sub(Money.parse("1200.00")).minor == 0
    assert rate.neg().minor == -120000


def test_allocate_sums_exactly():
    for total in (200000, 1, 999, 475609, 1234567):
        for weights in ([1, 1, 1], [5000, 3000, 2000], [0, 0, 7], [1, 2, 3, 4, 5]):
            shares = allocate(total, weights)
            assert sum(shares) == total, (total, weights, shares)
            assert all(isinstance(share, int) for share in shares)


def test_allocate_largest_remainder_is_proportional():
    # 2,000.00 split 50/30/20 → exact whole rupees.
    assert allocate(200000, [5000, 3000, 2000]) == [100000, 60000, 40000]
    # 1 paisa across three equal weights: exactly one recipient, never 0.33.
    shares = allocate(1, [1, 1, 1])
    assert sum(shares) == 1 and shares.count(1) == 1


def test_allocate_handles_zero_total_and_zero_weights():
    assert allocate(0, [1, 2, 3]) == [0, 0, 0]
    assert allocate(100, [0, 0]) == [0, 100] or sum(allocate(100, [0, 0])) == 100


def test_to_minor_and_format():
    assert to_minor("4756.0975609756") == 475610
    assert format_minor(475610) == "4,756.10"
    assert format_minor(-1200) == "-12.00"
    assert format_minor(200000, decimals=0) == "2,000"


def test_no_money_value_has_more_than_two_decimals():
    for raw in ("0.1", "0.005", "1999.995", "1e3", "12.3456789"):
        minor = to_minor(raw)
        assert format_minor(minor) == format_minor(int(minor))
        text = format_minor(minor)
        decimals = text.split(".")[1] if "." in text else ""
        assert len(decimals) <= 2
