"""The sidebar is a set of collapsible, function-grouped modules.

Every top-level menu row is a button that expands its own submenu, and each
page opens exactly the group it belongs to with its own link highlighted.
These tests pin the structure so a future edit cannot:

* drop back to a flat list of ~25 nav links,
* leave a page with no group open (the user has no idea where they are),
* open the wrong group (e.g. "Ledger" highlighted on the *client* statement),
* render a group whose links the user is not permitted to open.
"""
from __future__ import annotations

import re

from werkzeug.security import generate_password_hash

from models import User, db

ADMIN = {"username": "Admin", "password": "Admin@fbm12345"}

# path -> (group id, label of the link that must be highlighted)
EXPECTED_ACTIVE = {
    "/": (None, "Dashboard"),
    "/clients": ("navAccountingGroup", "Client Ledger"),
    "/suppliers": ("navAccountingGroup", "Supplier Ledger"),
    "/accounts/": ("navAccountingGroup", "Accounts"),
    "/ledger/": ("navAccountingGroup", "Ledger"),
    "/ledger/journal": ("navAccountingGroup", "Ledger"),
    "/ledger/pnl": ("navAccountingGroup", "Ledger"),
    "/decision_ledger": ("navAccountingGroup", "Decision Ledger"),
    "/payments": ("navAccountingGroup", "Payments"),
    "/accounts/payments/suppliers": ("navAccountingGroup", "Pay Supplier"),
    "/pending_bills": ("navAccountingGroup", "Pending Bills"),
    "/current_payables": ("navAccountingGroup", "Current Payables"),
    "/cash_flow": ("navAccountingGroup", "Cash Flow"),
    "/cash_flow_differences": ("navAccountingGroup", "Cash Flow"),
    "/daily_reconciliation": ("navAccountingGroup", "Daily Reconciliation"),
    "/financial_tracker": ("navAccountingGroup", "Financial Tracker"),
    "/profit_reports": ("navAccountingGroup", "Profit Reports"),
    "/grn": ("navTxGroup", "GRN (Receiving)"),
    "/bookings": ("navTxGroup", "Bookings"),
    "/direct_sales": ("navTxGroup", "Sales"),
    "/material_returns": ("navTxGroup", "Material Return"),
    "/stock_summary": ("navInventoryGroup", "Stock Summary"),
    "/daily_transactions": ("navInventoryGroup", "Daily Breakdown"),
    "/tracking": ("navInventoryGroup", "History"),
    "/delivery_rents": ("navDeliveryGroup", "Delivery Rent"),
    "/delivery_persons": ("navDeliveryGroup", "Delivery Person Ledger"),
    "/materials": ("navDirectoryGroup", "Material Brands"),
    "/services": ("navDirectoryGroup", "Services & Charges"),
    "/notifications": ("navSystemGroup", "Notifications"),
    "/import_export/": ("navSystemGroup", "Import & Export"),
    "/legacy-migration/": ("navSystemGroup", "Legacy Data Migration"),
    "/settings": ("navSystemGroup", "Settings"),
    "/void_audit": ("navSystemGroup", "Settings"),
}

GROUP_IDS = (
    "navInventoryGroup",
    "navTxGroup",
    "navAccountingGroup",
    "navDeliveryGroup",
    "navDirectoryGroup",
    "navSystemGroup",
)

GROUP_TITLES = {
    "navInventoryGroup": "Inventory",
    "navTxGroup": "Transactions",
    "navAccountingGroup": "Accounting",
    "navDeliveryGroup": "Delivery",
    "navDirectoryGroup": "Directory",
    "navSystemGroup": "System",
}


def _csrf_token(client):
    page = client.get("/login").get_data(as_text=True)
    match = re.search(r'name="_csrf_token" value="([^"]+)"', page)
    assert match, "login page must carry the CSRF token"
    return match.group(1)


def _login(client, username="Admin", password="Admin@fbm12345"):
    resp = client.post(
        "/login",
        data={"username": username, "password": password, "_csrf_token": _csrf_token(client)},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)[:300]


def _sidebar(client, path):
    body = client.get(path, follow_redirects=True).get_data(as_text=True)
    start = body.find('aria-label="Main navigation"')
    assert start != -1, f"{path} rendered without the sidebar"
    end = body.find("</nav>", start)
    return body[start:end]


def _open_groups(sidebar_html):
    return re.findall(r'<div id="(nav\w+)" class="nav-submenu show"', sidebar_html)


def _active_labels(sidebar_html):
    labels = []
    for chunk in sidebar_html.split("<a href=")[1:]:
        head, _, rest = chunk.partition(">")
        if "nav-link active" not in head:
            continue
        text = rest.split("</a>")[0]
        labels.append(" ".join(re.sub(r"<[^>]+>", " ", text).split()))
    return labels


def _plain(text):
    return text.replace("&amp;", "&")


def test_admin_sidebar_is_fully_grouped(app):
    client = app.test_client()
    _login(client)
    sidebar = _sidebar(client, "/")

    for group_id in GROUP_IDS:
        assert f'id="{group_id}"' in sidebar, group_id
        assert f'aria-controls="{group_id}"' in sidebar, group_id
        label = GROUP_TITLES[group_id]
        assert re.search(rf"<i [^>]*></i>\s*{re.escape(label)}", sidebar), label

    # Every expandable row is a button with a caret, not a bare link.
    toggles = re.findall(r'<button[^>]*nav-toggle-btn[^>]*>', sidebar)
    assert len(toggles) == len(GROUP_IDS)
    for tag in toggles:
        assert 'aria-expanded="' in tag
        assert "toggleNavGroup(" in tag
    assert sidebar.count('class="bi bi-chevron-down nav-caret') == len(GROUP_IDS)


def test_accounting_group_holds_every_finance_page(app):
    client = app.test_client()
    _login(client)
    sidebar = _sidebar(client, "/cash_flow")
    start = sidebar.find('id="navAccountingGroup"')
    end = sidebar.find('id="navDeliveryGroup"', start)  # next group starts here
    assert start != -1 and end > start
    block = sidebar[start:end]

    for label in (
        "Accounts", "Ledger", "Client Ledger", "Supplier Ledger", "Decision Ledger",
        "Payments", "Pay Supplier", "Pending Bills", "Current Payables",
        "Cash Flow", "Daily Reconciliation", "Financial Tracker", "Profit Reports",
    ):
        assert label in block, label
    # The three sub-sections are labelled so the 13 links stay readable.
    for section in ("Books &amp; Ledgers", "Payments &amp; Bills", "Cash &amp; Reports"):
        assert section in block, section


def test_each_page_opens_only_its_own_group(app):
    client = app.test_client()
    _login(client)

    for path, (group_id, label) in EXPECTED_ACTIVE.items():
        sidebar = _sidebar(client, path)
        open_groups = _open_groups(sidebar)
        active = [_plain(item) for item in _active_labels(sidebar)]
        if group_id is None:
            assert open_groups == [], path
            assert active == [label], (path, active)
            continue
        assert open_groups == [group_id], (path, open_groups)
        assert active == [label], (path, active)
        assert 'aria-expanded="true"' in sidebar, path


def test_group_without_permission_is_not_rendered(app):
    with app.app_context():
        user = User(
            username="clerk",
            password_hash=generate_password_hash("Clerk@12345"),
            role="user",
            status="active",
        )
        db.session.add(user)
        db.session.commit()
        assert user.can_manage_materials is False
        assert user.can_import_export is False
        assert user.can_access_settings is False

    client = app.test_client()
    _login(client, "clerk", "Clerk@12345")
    sidebar = _sidebar(client, "/")

    assert 'id="navDirectoryGroup"' not in sidebar          # no material master rights
    assert "Import &amp; Export" not in sidebar             # no import/export rights
    assert "Settings" not in sidebar                        # no settings rights
    assert 'id="navAccountingGroup"' in sidebar             # payments default is on
