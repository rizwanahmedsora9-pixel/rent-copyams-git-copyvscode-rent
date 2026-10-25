/* =========================================================================
   GRN stock-consumption indicator.

   The server owns the numbers: every indicator is rendered from
   `app.services.grn_consumption` with data-received / data-consumed
   attributes.  This script only turns those numbers into a bar, a percentage
   and a colour, and repaints whenever the attributes change or a row is
   inserted by an existing workflow (page reload, AJAX refresh, filter).

   Colours communicate *consumption*, never profitability:
     0–24%   green          25–49%  light green
     50–74%  amber          75–99%  orange      100%  red (stock finished)
   ========================================================================= */
(function () {
    "use strict";

    var BANDS = [
        { max: 25, level: "low", label: "Barely used" },
        { max: 50, level: "moderate", label: "Partly used" },
        { max: 75, level: "high", label: "Mostly used" },
        { max: 100, level: "critical", label: "Nearly finished" },
        { max: Infinity, level: "complete", label: "Fully consumed" }
    ];

    function num(value) {
        var parsed = parseFloat(value);
        return isFinite(parsed) ? parsed : 0;
    }

    function band(percent) {
        for (var i = 0; i < BANDS.length; i += 1) {
            if (percent < BANDS[i].max) return BANDS[i];
        }
        return BANDS[BANDS.length - 1];
    }

    function percentOf(received, consumed) {
        if (!(received > 0)) return 0;
        var pct = (consumed / received) * 100;
        if (!isFinite(pct)) return 0;
        return Math.max(0, Math.min(100, Math.round(pct * 100) / 100));
    }

    function formatQty(value) {
        var rounded = Math.round(num(value) * 100) / 100;
        return rounded.toLocaleString(undefined, { maximumFractionDigits: 2 });
    }

    function formatMoney(value) {
        var rounded = Math.round(num(value));
        return "Rs " + rounded.toLocaleString(undefined, { maximumFractionDigits: 0 });
    }

    /* The numeric read-outs (received / consumed / remaining) sit next to the
       bar rather than inside it, so look in the bar first and then in the
       enclosing block marked with data-grn-scope.  Each scope holds exactly one
       indicator, which keeps a lot's numbers from leaking into its neighbour. */
    function scopeOf(el) {
        if (el.closest) {
            var scope = el.closest("[data-grn-scope]");
            if (scope) return scope;
        }
        return el.parentNode || el;
    }

    function setScopedText(el, selector, text) {
        var target = el.querySelector(selector) || scopeOf(el).querySelector(selector);
        if (target) target.textContent = text;
    }

    function paint(el, data) {
        if (!el) return;
        var received = data && data.received !== undefined ? num(data.received) : num(el.getAttribute("data-received"));
        var consumed = data && data.consumed !== undefined ? num(data.consumed) : num(el.getAttribute("data-consumed"));
        if (data && data.received !== undefined) el.setAttribute("data-received", received);
        if (data && data.consumed !== undefined) el.setAttribute("data-consumed", consumed);

        var remaining = Math.max(0, received - consumed);
        var pct = percentOf(received, consumed);
        var current = band(pct);

        el.setAttribute("data-percent", pct);
        el.setAttribute("data-level", current.level);
        el.classList.remove("level-low", "level-moderate", "level-high", "level-critical", "level-complete");
        el.classList.add("level-" + current.level);
        el.classList.toggle("is-complete", received > 0 && remaining <= 0 && pct >= 99.995);

        var fill = el.querySelector(".grn-consume-fill");
        if (fill) {
            fill.style.width = pct + "%";
            fill.classList.remove("level-low", "level-moderate", "level-high", "level-critical", "level-complete");
            fill.classList.add("level-" + current.level);
        }
        var label = el.querySelector(".grn-consume-pct");
        if (label) label.textContent = (Math.round(pct * 10) / 10).toFixed(1) + "%";

        var track = el.querySelector(".grn-consume-track");
        if (track) {
            track.setAttribute("role", "progressbar");
            track.setAttribute("aria-valuemin", "0");
            track.setAttribute("aria-valuemax", "100");
            track.setAttribute("aria-valuenow", String(pct));
            track.setAttribute("aria-valuetext", pct + "% consumed");
            track.setAttribute("title", current.label + " — " + formatQty(consumed) + " of " + formatQty(received) + " consumed");
        }

        setScopedText(el, "[data-grn-consumed]", formatQty(consumed));
        setScopedText(el, "[data-grn-remaining]", formatQty(remaining));
        setScopedText(el, "[data-grn-received]", formatQty(received));
        return { received: received, consumed: consumed, remaining: remaining, percent: pct, level: current.level };
    }

    function paintAll(root) {
        var nodes = (root || document).querySelectorAll("[data-grn-consume]");
        var results = [];
        Array.prototype.forEach.call(nodes, function (el) {
            results.push({ element: el, state: paint(el) });
        });
        return results;
    }

    /* The whole-screen overall meter, repainted from the same payload the bars
       use.  It aggregates exactly what the server template aggregates (received
       / consumed / remaining *value* of the lots on screen), so a refresh can
       only ever reproduce the number a page load would have printed. */
    function paintOverview(payload, itemIds) {
        var meter = document.getElementById("grnOverallMeter");
        if (!meter || !payload || !payload.items) return null;
        /* The strip says "across the GRNs on this screen", so aggregate only
           the lots the page is actually showing.  The endpoint returns the
           whole ledger; on a filtered screen that was more than the server had
           rendered. */
        var ids = (itemIds && itemIds.length) ? itemIds : Object.keys(payload.items);
        var totals = { received: 0, consumed: 0, remaining: 0, lots: 0, complete: 0, replay: 0 };
        ids.forEach(function (itemId) {
            var row = payload.items[itemId] || payload.items[String(itemId)];
            if (!row) return;
            totals.received += num(row.received_value);
            totals.consumed += num(row.consumed_value);
            totals.remaining += num(row.remaining_value);
            totals.lots += 1;
            if (row.fully_consumed) totals.complete += 1;
            if (row.status === "reconstructed") totals.replay += 1;
        });
        var pct = percentOf(totals.received, totals.consumed);
        var current = band(pct);

        meter.classList.remove("level-low", "level-moderate", "level-high", "level-critical", "level-complete");
        meter.classList.add("level-" + current.level);
        meter.setAttribute("data-received", totals.received);
        meter.setAttribute("data-consumed", totals.consumed);

        var text = (Math.round(pct * 10) / 10).toFixed(1) + "%";
        var pctEl = meter.querySelector("[data-overall-pct]");
        if (pctEl) pctEl.textContent = text;
        var fill = meter.querySelector("[data-overall-fill]");
        if (fill) fill.style.width = pct + "%";
        var bar = meter.querySelector("[data-overall-bar]");
        if (bar) {
            bar.setAttribute("aria-valuenow", String(pct));
            bar.setAttribute("aria-valuetext", text + " of received stock value consumed");
        }
        var receivedEl = meter.querySelector("[data-overall-received]");
        if (receivedEl) receivedEl.textContent = formatMoney(totals.received);
        var consumedEl = meter.querySelector("[data-overall-consumed]");
        if (consumedEl) consumedEl.textContent = formatMoney(totals.consumed);
        var remainingEl = meter.querySelector("[data-overall-remaining]");
        if (remainingEl) remainingEl.textContent = formatMoney(totals.remaining);
        var valueEl = meter.querySelector("[data-overall-value]");
        if (valueEl) valueEl.textContent = formatMoney(totals.remaining);
        var lotsEl = meter.querySelector("[data-overall-lots]");
        if (lotsEl) lotsEl.textContent = String(totals.lots);
        var completeEl = meter.querySelector("[data-overall-complete]");
        if (completeEl) completeEl.textContent = String(totals.complete);
        var replayEl = meter.querySelector("[data-overall-replay]");
        if (replayEl) replayEl.textContent = String(totals.replay);
        return { percent: pct, level: current.level, totals: totals };
    }

    function applyServerData(payload) {
        if (!payload || !payload.items) return 0;
        var visibleItemIds = [];
        Array.prototype.forEach.call(
            document.querySelectorAll("[data-grn-consume][data-grn-item-id]"),
            function (el) {
                var id = el.getAttribute("data-grn-item-id");
                if (id && visibleItemIds.indexOf(id) === -1) visibleItemIds.push(id);
            }
        );
        paintOverview(payload, visibleItemIds);
        var painted = 0;
        Object.keys(payload.items).forEach(function (itemId) {
            var row = payload.items[itemId];
            var el = document.querySelector('[data-grn-consume][data-grn-item-id="' + itemId + '"]');
            if (!el) return;
            paint(el, { received: row.received_qty, consumed: row.consumed_qty });
            painted += 1;
        });
        Object.keys(payload.grns || {}).forEach(function (grnId) {
            var grn = payload.grns[grnId];
            var el = document.querySelector('[data-grn-summary][data-grn-id="' + grnId + '"]');
            if (el) {
                paint(el, { received: grn.received_qty, consumed: grn.consumed_qty });
                painted += 1;
            }
        });
        return painted;
    }

    function refresh(url) {
        if (!window.fetch) return Promise.resolve(false);
        var target = url || "/api/grn_consumption";
        return fetch(target, { credentials: "same-origin", headers: { "Accept": "application/json" } })
            .then(function (response) {
                if (!response.ok) throw new Error("HTTP " + response.status);
                return response.json();
            })
            .then(function (payload) {
                applyServerData(payload);
                document.dispatchEvent(new CustomEvent("grn:consumption-refreshed", { detail: payload }));
                return payload;
            })
            .catch(function () {
                // Silent by design: the row values already on screen stay as the
                // server rendered them and remain authoritative.
                return false;
            });
    }

    /* ---------------------------------------------------------------------
       Collapsible GRN rows.

       The records table stays one compact line per GRN; the per-lot details
       live in a server-rendered <tr> that is simply hidden until the arrow is
       clicked.  Rendering them on the server keeps every figure available
       without JavaScript (and greppable by tests), while the toggle only
       changes visibility — it never computes a number.
       --------------------------------------------------------------------- */
    function detailsFor(button) {
        var id = button.getAttribute("data-grn-expand");
        return id ? document.getElementById(id) : null;
    }

    function setExpanded(button, open) {
        var details = detailsFor(button);
        if (!details) return false;
        button.setAttribute("aria-expanded", open ? "true" : "false");
        var showLabel = button.getAttribute("data-label-show") || "Show details";
        var hideLabel = button.getAttribute("data-label-hide") || "Hide details";
        var label = open ? hideLabel : showLabel;
        var reference = button.getAttribute("data-grn-label");
        button.setAttribute("title", label);
        button.setAttribute("aria-label", reference ? label + " for " + reference : label);
        var icon = button.querySelector("i");
        if (icon) {
            icon.classList.toggle("bi-chevron-right", !open);
            icon.classList.toggle("bi-chevron-down", open);
        }
        details.hidden = !open;
        details.classList.toggle("is-open", open);
        if (button.closest) {
            var row = button.closest("tr");
            if (row) row.classList.toggle("is-expanded", open);
        }
        // Bars inside a hidden row still carry the server width; repaint on
        // open so a row refreshed while collapsed is never stale when shown.
        if (open) paintAll(details);
        return true;
    }

    function toggleRow(button) {
        return setExpanded(button, button.getAttribute("aria-expanded") !== "true");
    }

    function initExpanders(root) {
        var buttons = (root || document).querySelectorAll("[data-grn-expand]");
        Array.prototype.forEach.call(buttons, function (button) {
            if (button.getAttribute("data-grn-expand-ready") === "1") return;
            button.setAttribute("data-grn-expand-ready", "1");
            button.addEventListener("click", function () { toggleRow(button); });
        });
        return buttons.length;
    }

    function init() {
        initExpanders(document);
        paintAll(document);
        // Recalculate indicators for rows added by an existing workflow
        // (filters, pagination, AJAX row insertion) without touching the data.
        if (window.MutationObserver) {
            var observer = new MutationObserver(function (mutations) {
                var touched = false;
                mutations.forEach(function (mutation) {
                    Array.prototype.forEach.call(mutation.addedNodes || [], function (node) {
                        if (node.nodeType !== 1) return;
                        if (node.matches && node.matches("[data-grn-consume]")) {
                            paint(node);
                            touched = true;
                        } else if (node.querySelectorAll) {
                            var inner = node.querySelectorAll("[data-grn-consume]");
                            if (inner.length) {
                                Array.prototype.forEach.call(inner, function (el) { paint(el); });
                                touched = true;
                            }
                        }
                    });
                });
                if (touched) document.dispatchEvent(new CustomEvent("grn:consumption-painted"));
            });
            observer.observe(document.body, { childList: true, subtree: true });
        }
    }

    window.FBMGrnConsumption = {
        init: init,
        initExpanders: initExpanders,
        setExpanded: setExpanded,
        toggleRow: toggleRow,
        paint: paint,
        paintAll: paintAll,
        applyServerData: applyServerData,
        paintOverview: paintOverview,
        refresh: refresh,
        percentOf: percentOf
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
