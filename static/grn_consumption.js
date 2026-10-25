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

        var consumedEl = el.querySelector("[data-grn-consumed]");
        if (consumedEl) consumedEl.textContent = formatQty(consumed);
        var remainingEl = el.querySelector("[data-grn-remaining]");
        if (remainingEl) remainingEl.textContent = formatQty(remaining);
        var receivedEl = el.querySelector("[data-grn-received]");
        if (receivedEl) receivedEl.textContent = formatQty(received);
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

    function applyServerData(payload) {
        if (!payload || !payload.items) return 0;
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

    function init() {
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
        paint: paint,
        paintAll: paintAll,
        applyServerData: applyServerData,
        refresh: refresh,
        percentOf: percentOf
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
