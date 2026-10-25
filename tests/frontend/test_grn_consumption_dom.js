/* Offline DOM test for static/grn_consumption.js — the GRN screen's compact
   records table with a collapsible lot-details row.

   Three things regressed/are worth pinning on that screen and all are DOM
   behaviour, not pixels:

     1. the collapsed row is one line: the indicator markup lives in a hidden
        <tr class="grn-details-row"> directly after it, not in the row itself;
     2. the expand arrow toggles that row and keeps aria-expanded / the icon /
        the title / the is-expanded row class in agreement;
     3. the numeric read-outs (received / consumed / remaining) sit *next to*
        the bar, so the repaint script must find them through data-grn-scope —
        and there is no per-GRN roll-up element any more.

   Run:  npm i jsdom   then   node tests/frontend/test_grn_consumption_dom.js
   Against the real rendered screen instead of the fixture below:
         GRN_PAGE_HTML=instance/grn_page.html node tests/frontend/test_grn_consumption_dom.js */
const fs = require("fs");
const path = require("path");
let JSDOM;
try { JSDOM = require("jsdom").JSDOM; }
catch (_) { JSDOM = require("/tmp/node_modules/jsdom").JSDOM; }

const code = fs.readFileSync(path.join(__dirname, "..", "..", "static", "grn_consumption.js"), "utf8");

let pass = 0, fail = 0;
function check(name, cond, extra) {
    if (cond) { pass++; console.log("  ok  -", name); }
    else { fail++; console.log("  FAIL-", name, extra === undefined ? "" : "-> " + extra); }
}

/* Mirrors templates/grn_wizard.html: one collapsed GRN row whose two lots and
   their figures live in the hidden details row, plus the whole-screen meter.
   No [data-grn-summary]: the per-GRN roll-up was removed on purpose. */
const FIXTURE = `<!doctype html><html><body>
<section class="grn-overview level-high" id="grnOverallMeter" data-received="4000" data-consumed="1000">
  <span class="grn-overview-title">Overall consumption</span>
  <span class="grn-overview-pct" data-overall-pct>25.0%</span>
  <div class="grn-overview-bar" role="progressbar" data-overall-bar aria-valuenow="25">
    <div class="grn-overview-fill" data-overall-fill style="width: 25%;"></div>
  </div>
  <b data-overall-received>Rs 4,000</b>
  <b data-overall-consumed>Rs 1,000</b>
  <b data-overall-remaining>Rs 3,000</b>
  <span data-overall-lots>2</span>
  <span data-overall-complete>0</span>
  <span data-overall-replay>0</span>
  <span data-overall-value>Rs 3,000</span>
</section>

<table>
  <tbody>
    <tr class="grn-row" data-grn-row="9">
      <td class="grn-td-toggle">
        <button type="button" class="grn-expand-btn" data-grn-expand="grnDetails9"
                aria-expanded="false" aria-controls="grnDetails9"
                data-label-show="Show lot details" data-label-hide="Hide lot details"
                data-grn-label="GRN 9" title="Show lot details"
                aria-label="Show lot details for GRN 9"><i class="bi bi-chevron-right" aria-hidden="true"></i></button>
      </td>
      <td class="grn-items-cell"><span class="grn-items-line"><span class="grn-item-name">CEMENT</span> <b>4,000.00</b></span></td>
    </tr>
    <tr class="grn-details-row" id="grnDetails9" hidden>
      <td colspan="9">
        <div class="grn-details">
          <div class="grn-lot-list">
            <div class="grn-item-consume" data-grn-scope>
              <div class="grn-consume-head"><span class="grn-consume-name">CEMENT A</span></div>
              <div class="grn-lot-meter">
                <div class="grn-lot-figures">
                  <span>Received <b data-grn-received>2,000.00</b></span>
                  <span>Consumed <b data-grn-consumed>500.00</b></span>
                  <span>Remaining <b data-grn-remaining>1,500.00</b></span>
                </div>
                <div class="grn-consume level-moderate" data-grn-consume data-grn-item-id="1"
                     data-received="2000" data-consumed="500" data-percent="25">
                  <div class="grn-consume-track" role="progressbar">
                    <div class="grn-consume-fill level-moderate" style="width: 25%;"></div>
                  </div>
                  <span class="grn-consume-pct">25.0%</span>
                </div>
              </div>
            </div>

            <div class="grn-item-consume" data-grn-scope>
              <div class="grn-consume-head"><span class="grn-consume-name">CEMENT B</span></div>
              <div class="grn-lot-meter">
                <div class="grn-lot-figures">
                  <span>Received <b data-grn-received>2,000.00</b></span>
                  <span>Consumed <b data-grn-consumed>500.00</b></span>
                  <span>Remaining <b data-grn-remaining>1,500.00</b></span>
                </div>
                <div class="grn-consume level-moderate" data-grn-consume data-grn-item-id="2"
                     data-received="2000" data-consumed="500" data-percent="25">
                  <div class="grn-consume-track" role="progressbar">
                    <div class="grn-consume-fill level-moderate" style="width: 25%;"></div>
                  </div>
                  <span class="grn-consume-pct">25.0%</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </td>
    </tr>
  </tbody>
</table>
</body></html>`;

const pagePath = process.env.GRN_PAGE_HTML;
const source = pagePath ? fs.readFileSync(pagePath, "utf8") : FIXTURE;
if (pagePath) console.log("  (document:", pagePath + ")");

const dom = new JSDOM(source, { url: "http://localhost/grn", runScripts: "outside-only", pretendToBeVisual: true });
const w = dom.window, d = w.document;

w.eval(code);

const api = w.FBMGrnConsumption;
check("script exposes FBMGrnConsumption", !!api);
check("script exposes the row expander", typeof api.initExpanders === "function" && typeof api.setExpanded === "function");

/* The script may have attached itself already; init() is idempotent and makes
   the expander checks independent of JSDOM's readyState. */
api.init();

check("no per-GRN roll-up element", d.querySelector("[data-grn-summary]") === null);

/* ---- 1. initial paint keeps the server numbers ------------------------ */
const lot1 = d.querySelector('[data-grn-consume][data-grn-item-id="1"]');
const lot2 = d.querySelector('[data-grn-consume][data-grn-item-id="2"]');
function bandOf(pct) {
    return pct >= 100 ? "complete" : pct >= 75 ? "critical" : pct >= 50 ? "high" : pct >= 25 ? "moderate" : "low";
}
check("lot indicators found", !!lot1 && !!lot2);
/* Works against the fixture and against a real rendered page, whose numbers
   are whatever the server projected. */
const pct1Before = api.percentOf(lot1.getAttribute("data-received"), lot1.getAttribute("data-consumed"));
check("paint preserves the server percentage",
    lot1.querySelector(".grn-consume-pct").textContent === pct1Before.toFixed(1) + "%",
    lot1.querySelector(".grn-consume-pct").textContent + " vs " + pct1Before);
check("paint preserves the server band", lot1.classList.contains("level-" + bandOf(pct1Before)),
    lot1.className);

/* ---- 2. the read-outs beside the bar are repainted (the scope fix) ---- */
let painted = api.applyServerData({
    items: {
        "1": { received_qty: 2000, consumed_qty: 2000, received_value: 2400000, consumed_value: 2400000, remaining_value: 0, fully_consumed: true, status: "recorded" },
        "2": { received_qty: 2000, consumed_qty: 1500, received_value: 2600000, consumed_value: 1950000, remaining_value: 650000, fully_consumed: false, status: "reconstructed" },
        /* Not rendered on this screen (eg. an active filter filters it out).
           It must not leak into the "on this screen" strip.  A synthetic id
           keeps the check true against the fixture and a real /grn page. */
        "999999": { received_qty: 9000, consumed_qty: 9000, received_value: 9000000, consumed_value: 9000000, remaining_value: 0, fully_consumed: true, status: "recorded" }
    },
    grns: { "9": { received_qty: 4000, consumed_qty: 3500 } }
});
check("both lots repainted", painted >= 2, painted);

const fig1 = lot1.closest("[data-grn-scope]");
check("consumed read-out beside bar 1 repainted", fig1.querySelector("[data-grn-consumed]").textContent === "2,000",
    fig1.querySelector("[data-grn-consumed]").textContent);
check("remaining read-out beside bar 1 repainted", fig1.querySelector("[data-grn-remaining]").textContent === "0",
    fig1.querySelector("[data-grn-remaining]").textContent);
check("bar 1 fill set to 100%", lot1.querySelector(".grn-consume-fill").style.width === "100%",
    lot1.querySelector(".grn-consume-fill").style.width);
check("bar 1 label 100.0%", lot1.querySelector(".grn-consume-pct").textContent === "100.0%");
check("bar 1 band complete", lot1.classList.contains("level-complete") && !lot1.classList.contains("level-moderate"));
check("bar 1 aria updated", lot1.querySelector(".grn-consume-track").getAttribute("aria-valuenow") === "100");

/* scope isolation: lot 2 must not inherit lot 1's numbers */
const fig2 = lot2.closest("[data-grn-scope]");
check("lot 2 keeps its own consumed figure", fig2.querySelector("[data-grn-consumed]").textContent === "1,500",
    fig2.querySelector("[data-grn-consumed]").textContent);
check("lot 2 keeps its own remaining figure", fig2.querySelector("[data-grn-remaining]").textContent === "500",
    fig2.querySelector("[data-grn-remaining]").textContent);
check("lot 2 band critical", lot2.classList.contains("level-critical"));

/* ---- 3. the expand arrow toggles the details row ---------------------- */
const button = d.querySelector("[data-grn-expand]");
const details = d.getElementById(button.getAttribute("aria-controls"));
const row = button.closest("tr");
check("details start hidden", details.hidden === true);
check("collapse state announced", button.getAttribute("aria-expanded") === "false");
check("arrow points right", button.querySelector("i").classList.contains("bi-chevron-right"));

button.click();
check("click opens the details row", details.hidden === false && details.classList.contains("is-open"));
check("open state announced", button.getAttribute("aria-expanded") === "true");
check("arrow points down", button.querySelector("i").classList.contains("bi-chevron-down")
    && !button.querySelector("i").classList.contains("bi-chevron-right"));
check("title switches to hide", button.getAttribute("title") === button.getAttribute("data-label-hide"), button.getAttribute("title"));
check("aria-label switches to hide",
    button.getAttribute("aria-label") === button.getAttribute("data-label-hide") + " for " + button.getAttribute("data-grn-label"),
    button.getAttribute("aria-label"));
check("main row marked expanded", row.classList.contains("is-expanded"));

button.click();
check("second click collapses", details.hidden === true && button.getAttribute("aria-expanded") === "false");
check("arrow points right again", button.querySelector("i").classList.contains("bi-chevron-right"));
check("title switches back to show", button.getAttribute("title") === button.getAttribute("data-label-show"), button.getAttribute("title"));
check("aria-label switches back to show",
    button.getAttribute("aria-label") === button.getAttribute("data-label-show") + " for " + button.getAttribute("data-grn-label"),
    button.getAttribute("aria-label"));
check("main row marked collapsed", !row.classList.contains("is-expanded"));

check("setExpanded can open programmatically", api.setExpanded(button, true) && details.hidden === false);
api.setExpanded(button, false);

/* ---- 4. the whole-screen overall meter ------------------------------- */
const meter = d.getElementById("grnOverallMeter");
if (meter) {
    /* Server aggregation over the lots on the screen: received 5,000,000 /
       consumed 4,350,000 -> 87%.  Lot 999999 is in the payload but not on the
       page, so it must not move the strip. */
    check("overall meter percentage", meter.querySelector("[data-overall-pct]").textContent === "87.0%",
        meter.querySelector("[data-overall-pct]").textContent);
    check("overall meter fill width", meter.querySelector("[data-overall-fill]").style.width === "87%",
        meter.querySelector("[data-overall-fill]").style.width);
    check("overall meter band", meter.classList.contains("level-critical"));
    check("overall meter received money", meter.querySelector("[data-overall-received]").textContent === "Rs 5,000,000",
        meter.querySelector("[data-overall-received]").textContent);
    check("overall meter consumed money", meter.querySelector("[data-overall-consumed]").textContent === "Rs 4,350,000",
        meter.querySelector("[data-overall-consumed]").textContent);
    check("overall meter stock in hand", meter.querySelector("[data-overall-remaining]").textContent === "Rs 650,000",
        meter.querySelector("[data-overall-remaining]").textContent);
    check("overall meter lot count", meter.querySelector("[data-overall-lots]").textContent === "2",
        meter.querySelector("[data-overall-lots]").textContent);
    check("overall meter fully-consumed count", meter.querySelector("[data-overall-complete]").textContent === "1",
        meter.querySelector("[data-overall-complete]").textContent);
    check("overall meter replay count", meter.querySelector("[data-overall-replay]").textContent === "1",
        meter.querySelector("[data-overall-replay]").textContent);
    check("overall meter aria value", meter.querySelector("[data-overall-bar]").getAttribute("aria-valuenow") === "87",
        meter.querySelector("[data-overall-bar]").getAttribute("aria-valuenow"));
} else {
    console.log("  (no #grnOverallMeter in this document — overall meter checks skipped)");
}

/* ---- 5. guards ------------------------------------------------------- */
check("percentage never exceeds 100", api.percentOf(100, 140) === 100);
check("percentage is 0 without received quantity", api.percentOf(0, 50) === 0);

console.log(fail === 0 ? "\nALL " + pass + " CHECKS PASSED" : "\n" + fail + " FAILED / " + pass + " passed");
process.exit(fail === 0 ? 0 : 1);
