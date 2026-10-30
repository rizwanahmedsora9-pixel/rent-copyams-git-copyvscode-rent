"""Live balances and GRN-backed supplier payments in New Transaction → Pay."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from uuid import uuid4

import pytest
from werkzeug.security import generate_password_hash

from app.services.account_setup import save_account
from app.services.financial_ledgers import build_supplier_financial_ledger
from app.services.payments_crud import ledger_balance, ledger_balances
from app.services.time_money import pk_now
from models import Account, AccountTransaction, Client, GRN, GRNItem, Material, Supplier, SupplierPayment, User, db

NEW = "/accounts/transactions/new"
ACCOUNT = "/accounts/transactions/account-balance"
SUPPLIER = "/accounts/transactions/supplier-dues"


def login(client, name="Admin", password="Admin@fbm12345"):
    response = client.post("/login", data={"username": name, "password": password})
    assert response.status_code in (302, 303)


def make_account(name="Till", opening="1000.25", kind="cash"):
    account, _ = save_account({
        "name": name, "account_type": kind, "opening_balance": opening,
        "account_number": "PK00001234" if kind == "bank" else "",
    }, actor=None)
    db.session.flush()
    return account


def transaction(mode, **kwargs):
    return {
        "entry_source": "accounts_workspace", "tx_mode": mode,
        "idempotency_key": uuid4().hex,
        "date_posted": pk_now().strftime("%Y-%m-%dT%H:%M"),
        **kwargs,
    }


def workspace_data(response):
    match = re.search(r"window\.AW_DATA\s*=\s*(\{.*?\});\s*</script>", response.get_data(as_text=True), re.S)
    assert match, "workspace data script not found"
    return json.loads(match.group(1))


def test_new_transaction_updates_after_a_real_grn_receipt_and_supplier_payment(app, client):
    with app.app_context():
        cash = make_account()
        supplier = Supplier(name="Cement Supplier", phone="03001112222", opening_balance=150.25, is_active=True)
        material = Material(code="CEM-LIVE", name="Live Cement", total=0.0)
        db.session.add_all([supplier, material])
        db.session.commit()
        cash_id, supplier_id = cash.id, supplier.id
    login(client)

    # Open the page before the receipt and GRN, as if they were posted in
    # another browser tab. The initial data must not omit account balances.
    page = client.get(NEW + "?mode=pay")
    assert page.status_code == 200
    data = workspace_data(page)
    assert data["accounts"][0]["current_balance_minor"] == 100025
    assert data["accounts"][0]["current_balance"] == 1000.25
    assert data["suppliers"][0]["code"] == str(supplier_id)
    assert data["suppliers"][0]["display_code"] == f"SUP-{supplier_id:04d}"
    assert data["accountBalanceUrl"] == ACCOUNT
    assert data["supplierDuesUrl"] == SUPPLIER
    assert b'id="tx-supplier-dues"' in page.data
    assert b'id="tx-refresh-balances"' in page.data
    assert client.get(f"{SUPPLIER}?supplier_id={supplier_id}").get_json()["balance"] == 150.25

    received = client.post(NEW, data=transaction(
        "receive", receive_source="other", receive_account_id=cash_id,
        amount="5000.10", party="Owner deposit",
    ))
    assert received.status_code == 302
    now = pk_now()
    grn = client.post("/grn", data={
        "action": "add", "supplier": "Cement Supplier", "supplier_id": str(supplier_id),
        "date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M"),
        "payment_type": "Credit", "manual_bill_no": "LIVE-GRN-1",
        "mat_name[]": ["Live Cement"], "qty[]": ["10"], "price[]": ["300"],
    }, follow_redirects=True)
    assert grn.status_code == 200 and b"GRN added successfully" in grn.data

    # The live endpoints see both changes without reloading the old page.
    account_response = client.get(f"{ACCOUNT}?account_id={cash_id}")
    supplier_response = client.get(f"{SUPPLIER}?supplier_id={supplier_id}")
    assert account_response.headers["Cache-Control"].startswith("no-store")
    assert supplier_response.headers["Cache-Control"].startswith("no-store")
    assert account_response.get_json()["balance_minor"] == 600035
    assert account_response.get_json()["ledger_balance_minor"] == 600035
    assert supplier_response.get_json()["balance_minor"] == 315025
    assert supplier_response.get_json()["status"] == "payable"

    payment = transaction("pay", pay_target="supplier", pay_from_account_id=cash_id,
                          supplier_id=supplier_id, amount="1200.25", note="GRN payment")
    assert client.post(NEW, data=payment).status_code == 302
    # Retrying the same request never pays the supplier twice.
    assert client.post(NEW, data=payment).status_code == 302
    assert client.get(f"{ACCOUNT}?account_id={cash_id}").get_json()["balance_minor"] == 480010
    assert client.get(f"{SUPPLIER}?supplier_id={supplier_id}").get_json()["balance_minor"] == 195000
    with app.app_context():
        account = db.session.get(Account, cash_id)
        supplier = db.session.get(Supplier, supplier_id)
        assert account.balance_minor == 480010
        assert account.balance == pytest.approx(4800.10)
        assert ledger_balance(cash_id) == pytest.approx(4800.10)
        assert build_supplier_financial_ledger(supplier)["closing_balance"] == pytest.approx(1950.00)
        assert SupplierPayment.query.filter_by(supplier_id=supplier_id, is_void=False).count() == 1
        assert AccountTransaction.query.filter_by(from_account_id=cash_id, source_type="SupplierPayment").count() == 1

    # A stale browser cannot overdraw: the server still validates current funds.
    rejected = client.post(NEW, data=transaction("pay", pay_target="supplier", pay_from_account_id=cash_id,
                                                supplier_id=supplier_id, amount="4800.11"))
    assert rejected.status_code == 422
    assert b"Insufficient balance" in rejected.data
    with app.app_context():
        assert SupplierPayment.query.filter_by(supplier_id=supplier_id, is_void=False).count() == 1
        assert db.session.get(Account, cash_id).balance_minor == 480010

    # Paying past the remaining GRN due is a valid supplier advance, not a
    # second purchase or an incorrectly signed payable.
    advance = transaction("pay", pay_target="supplier", pay_from_account_id=cash_id,
                          supplier_id=supplier_id, amount="2000.50")
    assert client.post(NEW, data=advance).status_code == 302
    assert client.get(f"{ACCOUNT}?account_id={cash_id}").get_json()["balance_minor"] == 279960
    supplier_result = client.get(f"{SUPPLIER}?supplier_id={supplier_id}").get_json()
    assert supplier_result["balance_minor"] == -5050
    assert supplier_result["status"] == "advance" and supplier_result["advance"] == 50.50


def test_supplier_dues_follow_the_same_ledger_including_legacy_and_void_rows(app, client):
    with app.app_context():
        owing = Supplier(name="Owing Supplier", opening_balance=100.25, is_active=True)
        advance = Supplier(name="Advance Supplier", opening_balance=-75.25, is_active=True)
        settled = Supplier(name="Settled Supplier", opening_balance=0, is_active=True)
        inactive = Supplier(name="Inactive Supplier", opening_balance=9, is_active=False)
        db.session.add_all([owing, advance, settled, inactive]); db.session.flush()
        linked = GRN(supplier_id=owing.id, supplier=owing.name, paid_amount=200,
                     loading_cost=20, discount=5, tax_amount=5, is_void=False)
        legacy = GRN(supplier_id=owing.id, supplier=owing.name, paid_amount=50, is_void=False)
        void = GRN(supplier_id=owing.id, supplier=owing.name, is_void=True)
        db.session.add_all([linked, legacy, void]); db.session.flush()
        db.session.add_all([
            GRNItem(grn_id=linked.id, mat_name="Cement", qty=2, price_at_time=500),
            GRNItem(grn_id=linked.id, mat_name="Voided item", qty=2, price_at_time=5000, is_void=True),
            GRNItem(grn_id=legacy.id, mat_name="Steel", qty=1, price_at_time=500),
            GRNItem(grn_id=void.id, mat_name="Voided GRN", qty=1, price_at_time=9999),
            SupplierPayment(supplier_id=owing.id, amount=200, payment_type="Payment", source_type="GRN", source_id=linked.id, is_void=False),
            SupplierPayment(supplier_id=owing.id, amount=9999, payment_type="Payment", is_void=True),
        ])
        db.session.commit()
        ids = [owing.id, advance.id, settled.id, inactive.id]
        expected = build_supplier_financial_ledger(owing)["closing_balance"]
    login(client)
    owing, advance, settled, inactive = [client.get(f"{SUPPLIER}?supplier_id={sid}").get_json() for sid in ids]
    # 100.25 opening + 1,020 GRN - 200 linked payment + 500 GRN - 50 legacy payment.
    assert owing["balance"] == expected == 1370.25
    assert owing["balance_minor"] == 137025 and owing["payable"] == 1370.25
    assert advance["balance"] == -75.25 and advance["advance"] == 75.25 and advance["status"] == "advance"
    assert settled["balance"] == 0 and settled["status"] == "settled"
    assert inactive["is_active"] is False
    assert client.get(f"{SUPPLIER}?supplier_code={ids[0]}").get_json()["balance"] == 1370.25
    for value in ("", "x", "-1", "999999"):
        assert client.get(f"{SUPPLIER}?supplier_id={value}").get_json() == {"found": False}
        assert client.get(f"{ACCOUNT}?account_id={value}").get_json() == {"found": False}


def test_unavailable_supplier_projection_never_reports_a_fake_zero(app, client, monkeypatch):
    from app.services import financial_ledgers
    with app.app_context():
        supplier = Supplier(name="Supplier", opening_balance=400, is_active=True)
        db.session.add(supplier); db.session.commit()
        supplier_id = supplier.id
    login(client)
    def fail(_supplier):
        raise RuntimeError("ledger temporarily unavailable")
    monkeypatch.setattr(financial_ledgers, "build_supplier_financial_ledger", fail)
    result = client.get(f"{SUPPLIER}?supplier_id={supplier_id}").get_json()
    assert result["found"] is True
    assert result["balance"] is None and result["balance_minor"] is None
    assert result["payable"] is None and result["status"] == "unavailable"


def test_account_balance_uses_minor_units_and_surfaces_existing_ledger_drift(app, client):
    with app.app_context():
        cash = make_account(opening="1000.00")
        other = make_account("Bank", "900.00", "bank")
        # Simulate legacy drift directly: current ORM writes normally keep the
        # legacy float and the authoritative minor-unit mirror in sync.
        cash_id, other_id = cash.id, other.id
        db.session.execute(db.text("UPDATE account SET balance = 9999, balance_minor = 25000 WHERE id = :id"), {"id": cash_id})
        db.session.commit()
        db.session.expire_all()
        assert ledger_balances([cash_id]) == {cash_id: 1000.0}
        assert ledger_balances([]) == {}
        assert ledger_balances([other_id])[other_id] == 900.0
    login(client)
    data = workspace_data(client.get(NEW + "?mode=pay"))
    row = next(a for a in data["accounts"] if a["id"] == cash_id)
    assert row["current_balance_minor"] == 25000
    response = client.get(f"{ACCOUNT}?account_id={cash_id}").get_json()
    assert response["balance"] == 250.0 and response["balance_minor"] == 25000
    assert response["ledger_balance"] == 1000.0 and response["ledger_balance_minor"] == 100000
    with app.app_context():
        account = db.session.get(Account, cash_id)
        assert account.balance == 9999 and account.balance_minor == 25000
        account.is_active = False
        db.session.commit()
    response = client.get(f"{ACCOUNT}?account_id={cash_id}").get_json()
    assert response["found"] is True and response["is_active"] is False and response["status"] == "suspended"


def test_live_lookups_require_finance_access(app, client):
    with app.app_context():
        cash_id = make_account().id
        supplier = Supplier(name="Supplier", is_active=True)
        finance_user = User(username="pay-user", password_hash=generate_password_hash("Pay@12345"), role="user",
                            can_manage_accounts=False, can_manage_payments=True)
        outsider = User(username="outsider", password_hash=generate_password_hash("Out@12345"), role="user",
                        can_manage_accounts=False, can_manage_payments=False, can_manage_account_masters=False,
                        can_view_cash_flow=False, can_view_supplier_ledger=False)
        db.session.add_all([supplier, finance_user, outsider]); db.session.commit()
        supplier_id = supplier.id
    assert client.get(f"{ACCOUNT}?account_id={cash_id}").status_code in (302, 401)
    assert client.get(f"{SUPPLIER}?supplier_id={supplier_id}").status_code in (302, 401)
    login(client, "outsider", "Out@12345")
    assert client.get(f"{ACCOUNT}?account_id={cash_id}").status_code in (302, 403)
    assert client.get(f"{SUPPLIER}?supplier_id={supplier_id}").status_code in (302, 403)
    client.get("/logout")
    login(client, "pay-user", "Pay@12345")
    assert client.get(f"{ACCOUNT}?account_id={cash_id}").get_json()["balance"] == 1000.25
    assert client.get(f"{SUPPLIER}?supplier_id={supplier_id}").get_json()["found"] is True


def test_real_transaction_page_live_balance_browser_behavior(app, client, tmp_path):
    """Exercise the real rendered markup and JS, not a hand-written form fixture."""
    node = shutil.which("node")
    if not node or subprocess.run([node, "-e", "require('jsdom')"], capture_output=True).returncode:
        pytest.skip("Node and jsdom are required for the optional browser DOM test")
    with app.app_context():
        make_account("A Till", "1000.25")
        make_account("B Bank", "500.00", "bank")
        db.session.add_all([
            Supplier(name="Alpha Cement", phone="0300111", is_active=True),
            Supplier(name="Beta Steel", phone="0300222", is_active=True),
            Client(code="CL-01", name="Client One", is_active=True),
        ])
        db.session.commit()
    login(client)
    page = client.get(NEW + "?mode=pay")
    assert page.status_code == 200
    html = tmp_path / "workspace.html"
    html.write_text(page.get_data(as_text=True))
    script = Path(__file__).parent / "frontend" / "test_accounts_pay_live_balances_dom.js"
    result = subprocess.run([node, str(script), str(html)], capture_output=True, text=True,
                            cwd=Path(__file__).resolve().parents[1], timeout=30, env=os.environ.copy())
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
