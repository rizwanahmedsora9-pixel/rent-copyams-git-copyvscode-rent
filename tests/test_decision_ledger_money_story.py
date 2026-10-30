"""Decision Ledger money-story regression tests.

The Decision Ledger is the owner's "should I deliver / should I trust this
client" page.  These tests pin the numbers it must show:

* **Money given** per client across the three real-money channels
  (Payment receipts net of refunds, Booking "Paid Now" advances,
  Direct-sale paid-now) — with non-cash Material Return rows excluded,
  because goods returned against dues are not money.
* **Booking-rate equivalents**: booked / dispatched / remaining value per
  material (qty x latest booking rate) and collectively.
* **Smart reading lines**: the money-vs-deliveries gap and the expected
  pending once remaining goods are delivered.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

ADMIN = {"username": "Admin", "password": "Admin@fbm12345"}


def _login(client):
    resp = client.post("/login", data=ADMIN, follow_redirects=False)
    assert resp.status_code in (302, 303)


def _seed_story(app):
    """Client with a 200-bag booking @ 500: 50k advance paid at booking,
    30k more paid later, 120 bags dispatched."""
    from models import Booking, BookingItem, Client, Entry, Payment, db

    with app.app_context():
        client_row = Client(code="DL-1", name="DL Client", category="General", is_active=True)
        db.session.add(client_row)
        db.session.flush()

        booking = Booking(
            client_name=client_row.name, amount=100_000.0, paid_amount=50_000.0,
            date_posted=datetime.now() - timedelta(days=10), is_void=False,
        )
        db.session.add(booking)
        db.session.flush()
        db.session.add(BookingItem(booking_id=booking.id, material_name="Cement",
                                   qty=200, price_at_time=500))

        db.session.add(Payment(
            client_id=client_row.id, client_name=client_row.name,
            amount=30_000.0, payment_type="Receipt", method="Cash",
        ))
        # Non-cash settlement must NOT count as money given.
        db.session.add(Payment(
            client_id=client_row.id, client_name=client_row.name,
            amount=5_000.0, payment_type="Material Return", method="Material Return",
        ))

        db.session.add(Entry(
            date=datetime.now().strftime("%Y-%m-%d"),
            time="10:00:00", type="OUT", material="Cement",
            booked_material="Cement", client=client_row.name,
            client_code=client_row.code, client_category="Booking Delivery",
            qty=120, bill_no="DL-BILL-1", nimbus_no="Booking Delivery",
            created_by="Admin",
        ))
        db.session.commit()


def test_decision_ledger_shows_money_story(app, client):
    _seed_story(app)
    _login(client)

    page = client.get("/decision_ledger")
    assert page.status_code == 200
    body = page.get_data(as_text=True)

    # Money given = 50,000 booking advance + 30,000 cash.  The 5,000
    # Material Return row must NOT be counted as money.
    assert "80,000" in body
    assert "Money Given" in body
    # Booking-rate equivalents: booked 200x500=100,000; dispatched
    # 120x500=60,000; remaining 80x500=40,000.
    assert "100,000" in body
    assert "60,000" in body
    assert "40,000" in body
    # Smart reading lines exist.
    assert "Advance with shop" in body          # 80k given - 60k delivered
    assert "expected pending" in body.lower()   # 100k - 80k = 20k after delivery


def test_decision_ledger_overall_table_has_values_and_total(app, client):
    _seed_story(app)
    _login(client)

    body = client.get("/decision_ledger").get_data(as_text=True)
    assert "ALL MATERIALS" in body          # collective footer row
    assert "Dispatch Progress" in body      # per-material progress column
    assert "Remaining Value" in body
    # Sorted overall table still shows per-material rows.
    assert "Cement" in body


def test_delivery_gap_negative_when_deliveries_ahead(app, client):
    """Client paid 10k but 60k delivered at booking rates -> warning line."""
    from models import Booking, BookingItem, Client, Entry, db

    with app.app_context():
        client_row = Client(code="DL-2", name="DL Behind Client",
                            category="General", is_active=True)
        db.session.add(client_row)
        db.session.flush()
        booking = Booking(client_name=client_row.name, amount=100_000.0,
                          paid_amount=10_000.0,
                          date_posted=datetime.now() - timedelta(days=5),
                          is_void=False)
        db.session.add(booking)
        db.session.flush()
        db.session.add(BookingItem(booking_id=booking.id, material_name="Sand",
                                   qty=200, price_at_time=500))
        db.session.add(Entry(
            date=datetime.now().strftime("%Y-%m-%d"), time="11:00:00",
            type="OUT", material="Sand", booked_material="Sand",
            client=client_row.name, client_code=client_row.code,
            client_category="Booking Delivery", qty=120, bill_no="DL-BILL-2",
            nimbus_no="Booking Delivery", created_by="Admin",
        ))
        db.session.commit()

    _login(client)
    body = client.get("/decision_ledger").get_data(as_text=True)
    assert "Deliveries ahead of money" in body
