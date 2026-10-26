/* Offline DOM test for static/ledger_live.js — the live GRN / P&L watcher.

   Run:  npm i jsdom   then   node tests/frontend/test_ledger_live_dom.js
*/
const fs = require("fs");
const path = require("path");
let JSDOM;
try { JSDOM = require("jsdom").JSDOM; }
catch (_) { JSDOM = require("/tmp/node_modules/jsdom").JSDOM; }

const code = fs.readFileSync(
    path.join(__dirname, "..", "..", "static", "ledger_live.js"),
    "utf8"
);

const dom = new JSDOM(`<!doctype html><html><body>
  <span id="live" data-ledger-live-status>Live</span>
  <div data-grn-consume data-grn-item-id="1"></div>
</body></html>`, { url: "http://localhost/", runScripts: "outside-only" });
const w = dom.window;

Object.defineProperty(w.document, "visibilityState", {
    value: "visible",
    configurable: true
});

let pass = 0, fail = 0;
function check(name, cond, extra) {
    if (cond) { pass++; console.log("  ok  -", name); }
    else { fail++; console.log("  FAIL-", name, extra === undefined ? "" : "-> " + extra); }
}
function wait(ms) {
    return new Promise(resolve => setTimeout(resolve, ms || 20));
}

let revision = "revision-one";
let revisionCalls = 0;
let grnRefreshCalls = 0;
let profitRefreshCalls = 0;
w.fetch = function (url, options) {
    revisionCalls += 1;
    check("revision request uses no-store", !options || options.cache === "no-store");
    return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ ok: true, revision: revision })
    });
};
w.FBMGrnConsumption = {
    refresh: function (url) {
        grnRefreshCalls += 1;
        check("GRN refresh uses the authoritative endpoint", url === "/api/grn_consumption");
        return Promise.resolve({ items: {} });
    }
};
w.FBMProfitFilters = {
    refresh: function () { profitRefreshCalls += 1; return Promise.resolve(true); }
};

w.eval(code);
check("script exposes FBMLedgerLive", !!w.FBMLedgerLive && typeof w.FBMLedgerLive.init === "function");
const api = w.FBMLedgerLive.init({ grn: true, profit: true, status: "#live", interval: 100000 });
check("init returns a controller", !!api && typeof api.check === "function");

(async function () {
    await wait();
    check("initial revision recorded", api.revision() === "revision-one", api.revision());
    check("baseline does not repaint unchanged data", grnRefreshCalls === 0 && profitRefreshCalls === 0);
    check("baseline status is live", /Live/.test(w.document.getElementById("live").textContent));

    revision = "revision-two";
    await api.check(false);
    await wait();
    check("GRN projection refreshed after ledger change", grnRefreshCalls === 1, grnRefreshCalls);
    check("Profit report refreshed after ledger change", profitRefreshCalls === 1, profitRefreshCalls);
    check("new revision saved", api.revision() === "revision-two", api.revision());
    check("status reports updated", /updated/.test(w.document.getElementById("live").textContent),
        w.document.getElementById("live").textContent);

    await api.check(false);
    await wait();
    check("same revision does not repaint again", grnRefreshCalls === 1 && profitRefreshCalls === 1);
    check("revision endpoint was polled", revisionCalls >= 3, revisionCalls);

    console.log(fail === 0 ? "\nALL " + pass + " CHECKS PASSED" : "\n" + fail + " FAILED / " + pass + " passed");
    process.exit(fail === 0 ? 0 : 1);
})();
