"""GRN screen layout: one compact row per GRN, lot meters behind an arrow.

The indicator's *numbers* are already pinned by ``test_grn_consumption_indicator``.
These tests pin the presentation contract around them:

1. a record is one line: the collapsed ``<tr>`` carries no progress bar and no
   consumption figures at all, so a page of GRNs stays scannable;
2. every row has a real expand button (``aria-expanded`` / ``aria-controls``)
   that opens a server-rendered ``grn-details-row``; the row is ``hidden``
   until the operator asks for it;
3. there is no aggregate report per GRN: the old *Overall consumption* column
   and its ``data-grn-summary`` element are gone.  A GRN with several items
   lists each item's own consumption inside the expanded panel;
4. the whole-screen *Overall consumption* summary is one slim strip, not the
   old two-column panel with four stat cards;
5. the row actions and toolbar keep the contract from the previous pass.

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


def row_pairs(html):
    """[(collapsed row, details row)] — the details row follows its main row."""
    table = records_table(html)
    body = table[table.index("<tbody>"):]
    return re.findall(
        r'(<tr class="grn-row"[^>]*>.*?</tr>)\s*(<tr class="grn-details-row"[^>]*>.*?</tr>)',
        body,
        re.S,
    )


def main_rows(html):
    return [pair[0] for pair in row_pairs(html)]


def details_rows(html):
    return [pair[1] for pair in row_pairs(html)]


def overview_section(html):
    match = re.search(r'<section class="grn-overview[^"]*" id="grnOverallMeter".*?</section>', html, re.S)
    assert match, "the whole-screen overall meter was not rendered"
    return match.group(0)


def lot_count(details_row):
    return details_row.count("data-grn-consume data-grn-item-id=")


# --------------------------------------------------------------------------- #
# 1. Collapsed rows are one compact line; details are hidden until asked
# --------------------------------------------------------------------------- #
def test_collapsed_row_is_one_line_and_has_an_expand_button(grn_screen):
    _app, html, data = grn_screen
    headers = header_cells(html)
    assert len(headers) == 9, headers
    assert not any("overall consumption" in name.lower() for name in headers), headers

    pairs = row_pairs(html)
    assert len(pairs) == len(data["grns"])
    for main, details in pairs:
        assert main.count("<td") == len(headers), "row does not match the header"
        # The collapsed row is only identity + quantity: no bar, no figures.
        assert "grn-consume-track" not in main
        assert "data-grn-consume" not in main
        assert "data-grn-scope" not in main
        assert "data-grn-summary" not in main
        assert "grn-consume-pct" not in main
        assert main.count('class="grn-items-line"') + main.count('class="grn-items-empty"') == 1

        # A real button controls the hidden details row.
        assert main.count('<button type="button" class="grn-expand-btn"') == 1
        assert 'aria-expanded="false"' in main
        target = re.search(r'aria-controls="([^"]+)"', main).group(1)
        assert 'data-grn-expand="%s"' % target in main
        assert 'id="%s" hidden' % target in details
        assert 'colspan="9"' in details

        # Everything about every lot waits inside the details row.
        lots = lot_count(details)
        assert lots >= 1
        assert details.count('class="grn-item-consume" data-grn-scope') == lots
        assert details.count("data-grn-summary") == 0, "per-GRN aggregate is not wanted"

    # One details row per record, and the toggle script is loaded by the page.
    assert len(details_rows(html)) == len(main_rows(html)) == len(data["grns"])
    assert "grn_consumption.js" in html


def test_every_lot_and_meter_can_be_repainted(grn_screen):
    """Each lot's read-outs sit in its own data-grn-scope block."""
    _app, html, _data = grn_screen
    for details in details_rows(html):
        lots = lot_count(details)
        assert details.count("data-grn-scope") == lots
        assert details.count("data-grn-received") == lots
        assert details.count("data-grn-consumed") == lots
        assert details.count("data-grn-remaining") == lots


# --------------------------------------------------------------------------- #
# 2. The whole-screen overall meter is one slim strip over the server numbers
# --------------------------------------------------------------------------- #
def test_overview_is_one_slim_strip_aggregating_the_server_projection(grn_screen):
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

    # Slim: just the bar track + its fill are <div>s; the four stat cards,
    # caption row and separate legend row are gone.
    assert section.count("<div") == 2
    assert section.count("grn-overview-bar") == 1
    assert "grn-overview-stat" not in section
    assert "grn-overview-stats" not in section
    assert "grn-overview-caption" not in section
    assert "grn-overview-legend" not in section
    overview_css = re.search(r"\.grn-overview \{[^}]*\}", html).group(0)
    assert "display: flex" in overview_css and "flex-wrap: wrap" in overview_css


# --------------------------------------------------------------------------- #
# 3. Row actions: one uniform, icon-only strip
# --------------------------------------------------------------------------- #
def test_row_actions_are_one_uniform_strip(grn_screen):
    _app, html, _data = grn_screen
    rows = main_rows(html)
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
