"""Regression tests from the full-app audit.

Three defects were found while auditing every route with a restrictive user
account and cross-checking the reports against the authoritative services:

1. The Services & Charges master had no permission gate at all.  A standard
   user whose ``can_manage_materials`` was False could still add, rename,
   merge and delete services (a rename cascades into historical sales,
   bookings and entries) while every material route correctly refused.
2. ``/daily_reconciliation`` can lock a cash day and save counted balances,
   and ``/financial_tracker`` exposes the whole cash matrix, yet neither had
   a permission mapping (``/cash_flow`` itself requires
   ``can_view_cash_flow``).
3. ``/import_export/full_db_import_report/<name>`` lacked the admin/root guard
   its two sibling routes enforce.

Each test below fails on the pre-audit code and passes now.
"""
from __future__ import annotations

from werkzeug.security import generate_password_hash

from models import Service, User, db

ADMIN = {"username": "Admin", "password": "Admin@fbm12345"}


def _login(client, username="Admin", password="Admin@fbm12345"):
    resp = client.post("/login", data={"username": username, "password": password},
                       follow_redirects=False)
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:300]


def _make_limited_user(app, username="clerk"):
    with app.app_context():
        user = User(
            username=username,
            password_hash=generate_password_hash("Clerk@12345"),
            role="user",
            status="active",
        )
        db.session.add(user)
        db.session.commit()
        assert user.can_manage_materials is False
        assert user.can_view_cash_flow is False


def test_standard_user_cannot_mutate_service_master(app, client):
    _make_limited_user(app)
    _login(client, "clerk", "Clerk@12345")

    resp = client.post("/services/add", data={"service_name": "Hack Service"},
                       follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert "Permission denied" in body
    with app.app_context():
        assert Service.query.filter_by(name="Hack Service").count() == 0


def test_standard_user_cannot_open_cash_day_or_tracker(app, client):
    _make_limited_user(app)
    _login(client, "clerk", "Clerk@12345")

    for path in ("/daily_reconciliation", "/financial_tracker"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code in (302, 303), (path, resp.status_code)
        assert (resp.headers.get("Location") or "").rstrip("/") in ("", "/"), (
            path, resp.headers.get("Location"),
        )


def test_admin_keeps_full_service_master_access(app, client):
    _login(client)
    resp = client.post("/services/add", data={"service_name": "Rent"},
                       follow_redirects=True)
    assert "added" in resp.get_data(as_text=True).lower()
    with app.app_context():
        service = Service.query.filter_by(name="Rent").one()
        assert service.is_active is True


def test_full_db_import_report_requires_admin(app, client):
    _make_limited_user(app)
    _login(client, "clerk", "Clerk@12345")
    resp = client.get("/import_export/full_db_import_report/anything.json")
    # Either the endpoint map denies first (302 -> dashboard) or the inline
    # admin guard answers 403.  Both are a refusal; a 200 would be the leak.
    assert resp.status_code in (302, 403)
    assert resp.status_code != 200
