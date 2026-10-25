"""GRN screen layout: the consumption meters get real room, the row actions
stay one tidy strip.

The indicator's *numbers* are already pinned by ``test_grn_consumption_indicator``.
These tests pin the presentation contract around them, because three things were
wrong there:

1. the per-GRN roll-up meter was squeezed into the Total Qty column (a 190px
   box whose bar was clamped to 90px), so the one number that answers "is this
   GRN still in the warehouse?" was the least readable thing on the row;
2. the received / consumed / remaining read-outs sat *outside* the element the
   repaint script searches, so hitting "Recalculate" moved the bars and left
   the figures stale — they now live in a ``data-grn-scope`` block that
   ``static/grn_consumption.js`` falls back to;
3. the Actions cell mixed three icon buttons with a wide labelled *Delete* and
   an even wider *Shift* button, so the strip wrapped unevenly, and the refresh
   button toggled a ``.spin`` class that no stylesheet defines.

Only structure and class names are asserted here; the CSS itself lives in the
template and is checked by presence, not by pixels.
"""
from __future__ import annotations

import re
from datetime import datetime

import pytest

from app.services.grn_consumption import build_grn_consumption, consumption_level
from models import db
from tests.conftest import make_csrf_client
from tests.test_grn_consumption_indicator import add_grn, login, post_sale, seed

ACTIONS = ["View", "Print", "Edit", "Shift", "Delete"]


@pytest.fixture()
def grn_screen(app_factory, tmp_path):
    """(app, rendered /grn html, server projection) for a two-GRN screen."""
    app = app_factory(tmp_path / "grn_layout.db")
    app.config["TESTING"] = True
    client = make_csrf_client(app)
    with app.app_context():
        db.create_all()
        from app.services.schema import _ensure_default_admin
        _ensure_default_admin()
        db.session.commit()
    login(client)
    with app.app_context():
        material, customer, supplier = seed()
        add_grn(supplier, material, 2000, 1200, datetime(2026, 1, 1, 9, 0), "GRN-A")
        add_grn(supplier, material, 2000, 1300, datetime(2026, 1, 2, 9, 0), "GRN-B")
        db.session.commit()
        post_sale(client, customer, "2026-01-05", [("CEMENT", 2100, 1500)], "SALE-1")
        db.session.commit()
        response = client.get("/grn")
        assert response.status_code == 200
        return app, response.get_data(as_text=True), build_grn_consumption()


def records_table(html):
    start = html.index('<table class="table table-hover grn-table">')
    return html[start:html.index("</table>", start)]


def header_cells(html):
    table = records_table(html)
    head = table[: table.index("</thead>")]
    return [re.sub(r"<[^>]+>", "", cell).strip() for cell in re.findall(r"<th[\s>](.*?)</th>", head, re.S)]


def body_rows(html):
    table = records_table(html)
    return re.findall(r"<tr>(.*?)</tr>", table[table.index("<tbody>"):], re.S)


def overview_section(html):
    match = re.search(r'<section class="grn-overview[^"]*" id="grnOverallMeter".*?</section>', html, re.S)
    assert match, "the whole-screen overall meter was not rendered"
    return match.group(0)


# --------------------------------------------------------------------------- #
# 1. The per-GRN meter has its own column, and Total Qty is just a quantity
# --------------------------------------------------------------------------- #
def test_overall_meter_gets_its_own_column(grn_screen):
    _app, html, data = grn_screen
    headers = header_cells(html)
    assert any(name.startswith("Overall consumption") for name in headers), headers
    assert len(headers) == 9

    rows = body_rows(html)
    assert len(rows) == len(data["grns"])
    for row in rows:
        assert row.count("<td") == len(headers), "row does not match the header"
        assert row.count("data-grn-summary data-grn-id=") == 1
        assert 'class="grn-overall" data-grn-scope' in row
        # the roll-up bar moved out of the quantity cell
        qty_cell = re.search(r'<td class="grn-col-num">\s*<span class="grn-qty">.*?</td>', row, re.S)
        assert qty_cell and "grn-consume-track" not in qty_cell.group(0)
        assert "grn-consume-summary" not in row  # the old cramped wrapper is gone


def test_every_lot_and_meter_can_be_repainted(grn_screen):
    """The read-outs must sit inside a scope the repaint script searches."""
    _app, html, _data = grn_screen
    for row in body_rows(html):
        lots = row.count("data-grn-consume data-grn-item-id=")
        assert row.count('class="grn-item-consume" data-grn-scope') == lots
        assert row.count("data-grn-scope") == lots + 1  # + the overall meter
        assert row.count("data-grn-received") == lots
        assert row.count("data-grn-consumed") == lots + 1
        assert row.count("data-grn-remaining") == lots + 1


# --------------------------------------------------------------------------- #
# 2. The whole-screen overall meter reports the same projection
# --------------------------------------------------------------------------- #
def test_overview_meter_aggregates_the_server_projection(grn_screen):
    _app, html, data = grn_screen
    section = overview_section(html)
    lots = list(data["items"].values())
    received = sum(lot["received_value"] for lot in lots)
    consumed = sum(lot["consumed_value"] for lot in lots)
    remaining = sum(lot["remaining_value"] for lot in lots)
    expected_pct = (consumed / received * 100) if received else 0.0

    assert float(re.search(r'data-received="([-\d.]+)"', section).group(1)) == pytest.approx(received)
    assert float(re.search(r'data-consumed="([-\d.]+)"', section).group(1)) == pytest.approx(consumed)

    def figure(hook):
        return re.search(r'data-overall-%s\b[^>]*>([^<]*)<' % hook, section).group(1).strip()

    assert figure("pct") == "{:,.1f}%".format(expected_pct)
    assert figure("received") == "Rs {:,.0f}".format(received)
    assert figure("consumed") == "Rs {:,.0f}".format(consumed)
    assert figure("remaining") == "Rs {:,.0f}".format(remaining)
    assert figure("value") == "Rs {:,.0f}".format(remaining)
    assert figure("lots") == str(len(lots))
    assert figure("complete") == str(sum(1 for lot in lots if lot["fully_consumed"]))
    assert figure("replay") == str(sum(1 for lot in lots if lot["status"] == "reconstructed"))

    # The colour band uses the same thresholds as the per-lot bands.
    assert 'level-%s"' % consumption_level(expected_pct) in section
    # ...and the meter is honest about what it measures.
    assert "valued at cost" in section


# --------------------------------------------------------------------------- #
# 3. Row actions: one uniform, icon-only strip
# --------------------------------------------------------------------------- #
def test_row_actions_are_one_uniform_strip(grn_screen):
    _app, html, data = grn_screen
    rows = body_rows(html)
    assert rows
    for row in rows:
        strip = re.search(r'<div class="grn-row-actions">(.*?)</div>', row, re.S).group(1)
        buttons = re.findall(r'<(?:a|button)\b[^>]*class="([^"]*\bbtn\b[^"]*)"', strip)
        assert len(buttons) == len(ACTIONS), buttons
        for classes in buttons:
            assert "btn-sm" in classes
            # exactly one variant, and an outline one: solid + outline together
            # used to fight over the colour because theme.css orders them.
            assert classes.count("btn-outline-") == 1, classes
            assert not re.search(r"\bbtn-(info|warning|danger|success|secondary|primary)\b", classes), classes
        assert len(re.findall(r'aria-label="[^"]+"', strip)) == len(ACTIONS)
        assert len(re.findall(r'title="[^"]+"', strip)) == len(ACTIONS)
        # the two labelled buttons are icon-only now, so the strip cannot wrap
        assert ">Delete<" not in strip and ">Shift<" not in strip
        # delete stays a confirmed POST; shift stays a modal trigger
        form = re.search(r'<form[^>]*class="grn-action-form"[^>]*>', row).group(0)
        assert 'method="POST"' in form and "confirm(" in form
        assert 'data-bs-toggle="modal"' in strip
    assert 'class="grn-actions-cell"' in records_table(html)


# --------------------------------------------------------------------------- #
# 4. The refresh affordance is wired to CSS that exists
# --------------------------------------------------------------------------- #
def test_refresh_button_feedback_actually_shows(grn_screen):
    _app, html, _data = grn_screen
    assert ".grn-refresh-spin {" in html, "the spin animation class is not defined on this page"
    assert "classList.add('grn-refresh-spin')" in html
    assert "classList.add('spin')" not in html  # the old dead class
    # .btn-outline-info is defined after .btn-outline-danger/-success in
    # theme.css, so the base class has to come off for feedback to be visible.
    assert "classList.remove('btn-outline-info')" in html

    button = re.search(r'<button[^>]*id="grnConsumptionRefresh".*?</button>', html, re.S).group(0)
    assert button.count("btn-outline-") == 1, button
    assert "data-refresh-label" in button


def test_toolbar_keeps_the_existing_controls(grn_screen):
    _app, html, _data = grn_screen
    toolbar = re.search(r'<form[^>]*class="grn-toolbar".*?</form>', html, re.S).group(0)
    for field in ('name="start_date"', 'name="end_date"', 'name="search"'):
        assert field in toolbar
    assert "url_for" not in toolbar and "/export_grn" in toolbar  # Excel export kept
    assert "grnConsumptionRefresh" in toolbar
    # no unlabelled icon-only buttons left in the toolbar
    for button in re.findall(r"<(?:a|button)\b[^>]*class=\"[^\"]*\bbtn\b[^\"]*\"[^>]*>(.*?)</(?:a|button)>", toolbar, re.S):
        assert re.sub(r"<[^>]+>", "", button).strip(), "toolbar button has no visible label"
