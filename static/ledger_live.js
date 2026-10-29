/* =========================================================================
   Live FIFO ledger refresh for the GRN and Profit & Loss screens.

   The server remains the only place where stock and cost are calculated.  This
   script polls a tiny database revision token; when a sale, GRN, return,
   booking delivery or void is committed by this or another yard PC, it asks
   the authoritative endpoints for a fresh projection.  It never computes a
   quantity, rate or profit in the browser.
   ========================================================================= */
(function () {
    "use strict";

    var ENDPOINT = "/api/ledger_revision";
    var INTERVAL_MS = 12000;
    var state = null;

    function text(el, value) {
        if (el) el.textContent = value;
    }

    function clock() {
        try {
            return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
        } catch (error) {
            return new Date().toISOString().slice(11, 19);
        }
    }

    function setStatus(message, isError) {
        if (!state || !state.status) return;
        text(state.status, message);
        state.status.classList.toggle("text-danger", !!isError);
        state.status.classList.toggle("text-muted", !isError);
    }

    function userIsEditing() {
        var el = document.activeElement;
        if (!el) return false;
        var selection = window.getSelection && window.getSelection();
        if (selection && !selection.isCollapsed) return true;
        if (el.isContentEditable) return true;
        return !!(el.matches && el.matches("input, select, textarea, [contenteditable='true']"));
    }

    function refreshGrn() {
        if (!state.config.grn || !window.FBMGrnConsumption || !window.FBMGrnConsumption.refresh) {
            return Promise.resolve(true);
        }
        if (!document.querySelector("[data-grn-consume][data-grn-item-id]")) return Promise.resolve(true);
        return window.FBMGrnConsumption.refresh("/api/grn_consumption").then(function (payload) {
            return payload !== false;
        });
    }

    function refreshProfit() {
        if (!state.config.profit || !window.FBMProfitFilters || typeof window.FBMProfitFilters.refresh !== "function") {
            return Promise.resolve(true);
        }
        // The existing controller rebuilds the URL from the current filters,
        // fetches ?partial=1 and replaces only the results region.  Await its
        // result so "Live · updated" is never shown for a failed request.
        return Promise.resolve(window.FBMProfitFilters.refresh()).then(function (ok) {
            return ok !== false;
        });
    }

    function applyRevision(revision) {
        if (!state || state.refreshing) return;
        state.refreshing = true;
        setStatus("Live · updating…");
        Promise.all([refreshGrn(), refreshProfit()]).then(function (results) {
            var ok = results.every(Boolean);
            if (ok) state.revision = revision;
            setStatus(ok ? "Live · updated " + clock() : "Live · refresh failed " + clock(), !ok);
            document.dispatchEvent(new CustomEvent("ledger:refreshed", { detail: { revision: revision, ok: ok } }));
        }).catch(function () {
            setStatus("Live · refresh failed " + clock(), true);
        }).finally(function () {
            state.refreshing = false;
        });
    }

    function check(force) {
        if (!state || state.checking || document.visibilityState === "hidden") return Promise.resolve(false);
        if (!window.fetch) return Promise.resolve(false);
        state.checking = true;
        return fetch(ENDPOINT, {
            credentials: "same-origin",
            cache: "no-store",
            headers: { "Accept": "application/json" }
        }).then(function (response) {
            if (!response.ok) throw new Error("HTTP " + response.status);
            return response.json();
        }).then(function (payload) {
            if (!payload || !payload.ok || !payload.revision) throw new Error("bad revision");
            if (state.revision === null) {
                state.revision = payload.revision;
                setStatus("Live · synced " + clock());
            } else if (payload.revision !== state.revision || force) {
                // Never replace the results under a user who is typing a
                // filter.  Keep the old revision so the next poll retries.
                if (userIsEditing()) {
                    setStatus("Live · update pending");
                } else {
                    applyRevision(payload.revision);
                }
            }
            return true;
        }).catch(function () {
            setStatus("Live · offline", true);
            return false;
        }).finally(function () {
            state.checking = false;
        });
    }

    function init(config) {
        config = config || {};
        if (!config.grn && !config.profit) return null;
        state = {
            config: config,
            status: document.querySelector(config.status || "[data-ledger-live-status]"),
            revision: null,
            checking: false,
            refreshing: false
        };
        setStatus("Live · connecting…");
        check(false);
        window.setInterval(function () { check(false); }, config.interval || INTERVAL_MS);
        document.addEventListener("visibilitychange", function () {
            if (document.visibilityState === "visible") check(false);
        });
        window.addEventListener("focus", function () { check(false); });
        document.addEventListener("focusout", function () {
            window.setTimeout(function () { check(false); }, 0);
        });
        return {
            check: function (force) { return check(!!force); },
            revision: function () { return state && state.revision; }
        };
    }

    window.FBMLedgerLive = { init: init, check: check };
})();
