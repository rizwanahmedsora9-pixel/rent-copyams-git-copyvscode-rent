"""Manage Categories is owner-defined and starts empty (rebuilt workspace)."""
from __future__ import annotations

from models import db, AccountCategory, CashFlowCategory

ADMIN = {"username": "Admin", "password": "Admin@fbm12345"}


def _login(client):
    return client.post("/login", data=ADMIN, follow_redirects=True)


def test_manage_categories_page_unauthenticated(client):
    resp = client.get("/accounts/categories")
    assert resp.status_code == 302
    assert "login" in resp.headers["Location"]


def test_manage_categories_crud_flow(client, app):
    assert _login(client).status_code == 200
    page = client.get("/accounts/categories")
    assert page.status_code == 200
    assert b"No categories yet" in page.data
    with app.app_context():
        assert CashFlowCategory.query.count() == 0
        assert AccountCategory.query.count() == 0

    resp = client.post("/accounts/setup/categories", data={"name": "Investment", "direction": "both", "notes": "Capital"}, follow_redirects=True)
    assert resp.status_code == 200 and b"Category created." in resp.data
    with app.app_context():
        cat = CashFlowCategory.query.filter_by(name="Investment").one()
        assert cat.notes == "Capital" and cat.is_active is True
        cat_id = cat.id

    resp = client.post("/accounts/setup/categories", data={"category_id": cat_id, "name": "Investments", "direction": "both", "notes": "Updated"}, follow_redirects=True)
    assert resp.status_code == 200 and b"Category updated." in resp.data
    with app.app_context():
        assert db.session.get(CashFlowCategory, cat_id).name == "Investments"

    client.post(f"/accounts/setup/categories/{cat_id}/status", data={"active": "0"})
    with app.app_context():
        assert db.session.get(CashFlowCategory, cat_id).is_active is False
    client.post(f"/accounts/setup/categories/{cat_id}/status", data={"active": "1"})
    with app.app_context():
        assert db.session.get(CashFlowCategory, cat_id).is_active is True

    resp = client.post(f"/accounts/setup/categories/{cat_id}/delete", follow_redirects=True)
    assert resp.status_code == 200 and b"Unused category deleted." in resp.data
    with app.app_context():
        assert db.session.get(CashFlowCategory, cat_id) is None
