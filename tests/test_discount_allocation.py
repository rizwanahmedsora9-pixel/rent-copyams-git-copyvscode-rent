"""Per-line discount: whole paisa, stored once, never divided again.

These tests lock the fix for the "discount comes back in points" defect:

* a bill-level discount is allocated with largest-remainder rounding, so the
  shares always add back to the discount exactly;
* a service line (rent) keeps its full value, because the automatic split
  applies to goods only;
* the amounts are STORED on the document line, and the profit report reads them
  instead of dividing the bill discount while the page loads;
* returning a discounted line reverses what was recognised, so a sale plus its
  return nets to zero instead of leaving a phantom loss.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from models import (
    Booking,
    BookingItem,
    Client,
    DirectSale,
    DirectSaleItem,
    GRN,
    GRNItem,
    Material,
    MaterialCategory,
    MaterialReturn,
    MaterialReturnItem,
    Service,
    db,
)
from app.services import pricing
from app.services.profit_loss import build_profit_loss_report


# --------------------------------------------------------------------------- #
# Pure arithmetic
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "total,weights",
    [
        (2000, [36000, 5000]),
        (1000, [10000, 3000, 3000]),
        (100, [1, 1, 1]),
        (1, [1, 1, 1]),
        (999, [7, 11, 13]),
        (0, [5, 5]),
        (1234567, [1, 2, 3, 4, 5, 6, 7]),
    ],
)
def test_allocation_is_exact_and_whole_paisa(total, weights):
    shares = pricing.allocate_cents(total, weights)
    assert sum(shares) == total, "the parts must add back to the whole"
    assert all(isinstance(share, int) for share in shares)


def test_allocation_never_produces_repeating_decimals():
    lines = [
        {"name": "CEMENT-OPC", "qty": 30, "rate": 1200},
        {"name": "RENT-MIXER", "qty": 1, "rate": 5000, "is_service": True},
    ]
    shares = pricing.allocate_document_discount(2000, lines)
    assert shares == [Decimal("2000.00"), Decimal("0.00")]
    assert sum(shares) == Decimal("2000.00")


def test_service_can_be_discounted_only_when_asked():
    lines = [
        {"name": "CEMENT-OPC", "qty": 30, "rate": 1200},
        {"name": "RENT-MIXER", "qty": 1, "rate": 5000, "is_service": True},
    ]
    shares = pricing.allocate_document_discount(2000, lines, allow_services=True)
    assert shares == [Decimal("1756.10"), Decimal("243.90")]
    assert sum(shares) == Decimal("2000.00")


def test_discount_can_never_exceed_the_line_value():
    lines = [{"name": "A", "qty": 1, "rate": 100}, {"name": "B", "qty": 1, "rate": 100}]
    shares = pricing.cap_shares([Decimal("500.00"), Decimal("0.00")], lines)
    assert shares == [Decimal("100.00"), Decimal("0.00")]


def test_named_amounts_are_honoured_exactly():
    lines = [
        {"name": "CEMENT-OPC", "qty": 30, "rate": 1200},
        {"name": "STEEL-10MM", "qty": 10, "rate": 300},
        {"name": "RENT-MIXER", "qty": 1, "rate": 5000},
    ]
    shares = pricing.discount_from_named_amounts(
        {"CEMENT-OPC": 1500, "STEEL-10MM": 500}, lines
    )
    assert shares == [Decimal("1500.00"), Decimal("500.00"), Decimal("0.00")]


def test_line_gross_is_rounded_once():
    assert pricing.from_cents(pricing.line_gross_cents(3, 333.335)) == Decimal("1000.01")


# --------------------------------------------------------------------------- #
# Report integration
# --------------------------------------------------------------------------- #
def login(client, username="Admin", password="Admin@fbm12345"):
    resp = client.post("/login", data={"username": username, "password": password})
    assert resp.status_code in (302, 303)


@pytest.fixture()
def books(app_factory, tmp_path):
    app = app_factory(tmp_path / "discount.db")
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        from app.services.schema import _ensure_default_admin
        _ensure_default_admin()
        db.session.commit()

        cement = MaterialCategory(name="CEMENT", is_active=True)
        db.session.add(cement)
        db.session.flush()
        db.session.add(Material(name="CEMENT-OPC", code="C-1", category_id=cement.id,
                                unit="Bag", unit_price=1200))
        db.session.add(Service(code="RENT-MIXER", name="RENT-MIXER", unit="Pcs",
                               unit_price=0, is_active=True))
        db.session.add(Client(name="ACME Builders", code="CL-1"))
        db.session.flush()
        grn = GRN(supplier="SUPPLIER A", manual_bill_no="G-1",
                  date_posted=datetime(2026, 9, 1, 10, 0))
        db.session.add(grn)
        db.session.flush()
        db.session.add(GRNItem(grn_id=grn.id, mat_name="CEMENT-OPC", qty=500,
                               price_at_time=1000))
        db.session.commit()
    return app


def _sale(lines, discount, day, ref):
    """Create a sale and store the per-line split exactly as the route does."""
    amount = sum(qty * rate for _name, qty, rate in lines)
    sale = DirectSale(
        client_name="ACME Builders", client_code="CL-1", amount=amount,
        discount=discount, paid_amount=amount - discount, category="Cash",
        date_posted=datetime(2026, 9, day, 11, 0), manual_bill_no=ref,
    )
    db.session.add(sale)
    db.session.flush()
    rows = [
        {
            "name": name, "qty": qty, "rate": rate,
            "gross": pricing.cents_to_float(pricing.line_gross_cents(qty, rate)),
            "is_service": name.startswith("RENT"),
        }
        for name, qty, rate in lines
    ]
    shares = pricing.cap_shares(
        pricing.allocate_document_discount(discount, rows), rows
    )
    for (name, qty, rate), share in zip(lines, shares):
        db.session.add(DirectSaleItem(
            sale_id=sale.id, product_name=name, qty=qty, price_at_time=rate,
            discount=float(share),
            discount_kind="allocated" if share > 0 else "none",
        ))
    db.session.flush()
    return sale


def test_report_uses_stored_discount_and_shows_whole_rupees(books):
    with books.app_context():
        _sale([("CEMENT-OPC", 30, 1200), ("RENT-MIXER", 1, 5000)], 2000, 10, "S-RENT")
        db.session.commit()

        report = build_profit_loss_report(
            start_date=date(2026, 9, 1), end_date=date(2026, 9, 30)
        )
        rows = {row["material"]: row for row in report["transactions"]}

        assert rows["RENT-MIXER"]["revenue"] == 5000.00
        assert rows["RENT-MIXER"]["discount"] == 0.00
        assert rows["CEMENT-OPC"]["revenue"] == 34000.00
        assert rows["CEMENT-OPC"]["discount"] == 2000.00

        summary = report["summary"]
        assert summary["total_discounts"] == 2000.00
        assert summary["total_gross_revenue"] == 41000.00
        assert summary["total_revenue"] == 39000.00
        # Gross - Discounts = Net, exactly.
        assert summary["total_gross_revenue"] - summary["total_discounts"] == \
            summary["total_revenue"]


def test_every_line_has_gross_minus_discount_equal_to_revenue(books):
    with books.app_context():
        _sale([("CEMENT-OPC", 30, 1200), ("RENT-MIXER", 1, 5000)], 2000, 10, "S-1")
        _sale([("CEMENT-OPC", 7, 1000), ("RENT-MIXER", 1, 333)], 111, 11, "S-2")
        db.session.commit()
        report = build_profit_loss_report(
            start_date=date(2026, 9, 1), end_date=date(2026, 9, 30)
        )
        for row in report["transactions"]:
            assert round(row["gross_revenue"] - row["discount"], 2) == round(row["revenue"], 2), row
            for key in ("gross_revenue", "discount", "revenue", "profit", "sale_rate"):
                assert round(float(row[key]), 2) == float(row[key]), (key, row[key])


def test_returning_a_discounted_line_nets_to_zero(books):
    with books.app_context():
        _sale([("CEMENT-OPC", 30, 1200), ("RENT-MIXER", 1, 5000)], 2000, 10, "S-1")
        ret = MaterialReturn(client_name="ACME Builders", return_type="normal",
                             amount=5000, manual_bill_no="R-1",
                             date_posted=datetime(2026, 9, 20, 10, 0))
        db.session.add(ret)
        db.session.flush()
        db.session.add(MaterialReturnItem(material_return_id=ret.id,
                                          material_name="RENT-MIXER", qty=1,
                                          unit_rate=5000, price_at_time=5000))
        db.session.commit()

        report = build_profit_loss_report(
            start_date=date(2026, 9, 1), end_date=date(2026, 9, 30)
        )
        rent_rows = [row for row in report["transactions"]
                     if row["material"] == "RENT-MIXER"]
        assert len(rent_rows) == 2
        assert sum(row["revenue"] for row in rent_rows) == 0.0


def test_booking_discount_is_stored_per_line_and_prorated(books):
    with books.app_context():
        booking = Booking(client_name="ACME Builders", amount=126000, discount=3333,
                          paid_amount=0, manual_bill_no="B-1",
                          date_posted=datetime(2026, 9, 2, 10, 0))
        db.session.add(booking)
        db.session.flush()
        cement_item = BookingItem(booking_id=booking.id, material_name="CEMENT-OPC",
                                  qty=100, price_at_time=1200, discount=3333)
        rent_item = BookingItem(booking_id=booking.id, material_name="RENT-MIXER",
                                qty=1, price_at_time=6000, discount=0)
        db.session.add_all([cement_item, rent_item])
        db.session.commit()

        from app.services.profit_loss import _Projection
        projection = _Projection().build()
        assert projection.booking_item_discount_total[cement_item.id] == Decimal("3333.00")
        assert projection.booking_item_discount_total[rent_item.id] == Decimal("0.00")


def test_named_selection_keeps_services_at_full_value():
    lines = [
        {"name": "CEMENT-OPC", "qty": 30, "rate": 1200, "gross": 36000},
        {"name": "RENT-MIXER", "qty": 1, "rate": 5000, "gross": 5000, "is_service": True},
    ]
    total, shares, errors = pricing.resolve_line_discounts(
        lines, 2000, {"RENT-MIXER": 2000}
    )
    assert not errors
    assert total == 2000.0
    assert shares == [Decimal("0.00"), Decimal("2000.00")]


def test_unknown_item_is_rejected_not_silently_dropped():
    lines = [{"name": "CEMENT-OPC", "qty": 30, "rate": 1200, "gross": 36000}]
    _total, _shares, errors = pricing.resolve_line_discounts(
        lines, 500, {"NOT ON THIS BILL": 500}
    )
    assert errors and "not on this bill" in errors[0]


def test_extra_bill_amount_spreads_over_unselected_goods_lines():
    lines = [
        {"name": "CEMENT-OPC", "qty": 10, "rate": 1000, "gross": 10000},
        {"name": "STEEL-10MM", "qty": 10, "rate": 1000, "gross": 10000},
        {"name": "RENT-MIXER", "qty": 1, "rate": 5000, "gross": 5000, "is_service": True},
    ]
    total, shares, errors = pricing.resolve_line_discounts(
        lines, 3000, {"CEMENT-OPC": 1000}
    )
    assert not errors
    assert total == 3000.0
    # 1,000 typed on cement, the other 2,000 lands on the only other goods line.
    # The rent line is untouched because services keep their full value.
    assert shares == [Decimal("1000.00"), Decimal("2000.00"), Decimal("0.00")]
    assert sum(shares) == Decimal("3000.00")


def test_legacy_rows_are_backfilled_once(books):
    """Rows posted before per-line storage get their share written at boot."""
    from sqlalchemy import text

    with books.app_context():
        sale = _sale([("CEMENT-OPC", 30, 1200), ("RENT-MIXER", 1, 5000)], 2000, 12, "S-OLD")
        # Mark it as legacy, exactly as an older database would look.
        db.session.execute(text(
            "UPDATE direct_sale_item SET discount = 0, discount_kind = NULL "
            "WHERE sale_id = :sid"
        ), {"sid": sale.id})
        db.session.commit()

        from app.services.schema import _backfill_line_discounts
        _backfill_line_discounts()

        rows = db.session.execute(text(
            "SELECT product_name, discount, discount_kind FROM direct_sale_item "
            "WHERE sale_id = :sid ORDER BY id"
        ), {"sid": sale.id}).fetchall()
        assert [row[2] for row in rows] == ["allocated", "none"]
        assert round(rows[0][1], 2) == 2000.00
        assert round(rows[0][1] + rows[1][1], 2) == 2000.00

        # Running it again changes nothing.
        _backfill_line_discounts()
        again = db.session.execute(text(
            "SELECT product_name, discount FROM direct_sale_item "
            "WHERE sale_id = :sid ORDER BY id"
        ), {"sid": sale.id}).fetchall()
        assert [round(row[1], 2) for row in again] == [round(row[1], 2) for row in rows]


def test_return_withdraws_the_discount_sitting_on_the_goods(books):
    from app.services.return_discounts import discount_for_return_line

    with books.app_context():
        sale = _sale([("CEMENT-OPC", 30, 1200), ("RENT-MIXER", 1, 5000)], 2000, 10, "S-1")
        # The rent line was not discounted, so returning it withdraws nothing.
        assert discount_for_return_line("ACME Builders", "RENT-MIXER", 1,
                                       as_of=datetime(2026, 9, 20)) == 0.0
        # Cement carries the whole 2,000 over 30 bags: 66.67 a bag.
        once = discount_for_return_line("ACME Builders", "CEMENT-OPC", 10,
                                        as_of=datetime(2026, 9, 20))
        assert once == 666.67
        # Returning everything gives the discount back in full, not once per call.
        from models import MaterialReturn, MaterialReturnItem
        ret = MaterialReturn(client_name="ACME Builders", return_type="normal",
                             amount=12000, manual_bill_no="R-1",
                             date_posted=datetime(2026, 9, 20, 10, 0))
        db.session.add(ret)
        db.session.flush()
        db.session.add(MaterialReturnItem(material_return_id=ret.id,
                                          material_name="CEMENT-OPC", qty=10,
                                          unit_rate=1200, price_at_time=1200,
                                          discount_reversed=once))
        db.session.commit()
        rest = discount_for_return_line("ACME Builders", "CEMENT-OPC", 20,
                                        as_of=datetime(2026, 9, 22))
        assert round(once + rest, 2) == 2000.00
        assert sale.id


def test_return_of_an_undiscounted_sale_withdraws_nothing(books):
    from app.services.return_discounts import discount_for_return_line

    with books.app_context():
        _sale([("CEMENT-OPC", 5, 1200)], 0, 15, "S-PLAIN")
        db.session.commit()
        assert discount_for_return_line("ACME Builders", "CEMENT-OPC", 5,
                                        as_of=datetime(2026, 9, 20)) == 0.0
