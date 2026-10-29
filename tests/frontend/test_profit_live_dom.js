/* Run with jsdom installed: node tests/frontend/test_profit_live_dom.js */
const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');
const assert = require('node:assert/strict');
const dom = new JSDOM(`<!doctype html><form id="plFilterForm" action="/profit_reports"></form>
<div id="plResultsRegion"><div data-pl-results><div class="pl-scroll">old</div><details open><summary>Costs</summary></details></div></div>`, {
    url: 'https://example.test/profit_reports?page=3&sort=revenue', runScripts: 'outside-only'
});
const w = dom.window;
let requested, resolveResponse, scroll;
w.scrollTo = pos => { scroll = pos; };
w.fetch = url => {
    requested = url;
    return new Promise(resolve => { resolveResponse = resolve; });
};
w.eval(fs.readFileSync(path.join(__dirname, '../../static/profit_report.js'), 'utf8'));
w.FBMProfitFilters.init({});
(async () => {
    const pending = w.FBMProfitFilters.refresh();
    assert.match(requested, /page=3/);
    assert.match(requested, /sort=revenue/);
    assert.match(requested, /partial=1/);
    assert.equal(w.document.querySelector('.is-loading'), null);
    // User moves while the request is in flight.
    w.pageYOffset = 1250;
    w.document.querySelector('.pl-scroll').scrollTop = 180;
    resolveResponse({ ok: true, text: async () => '<div data-pl-results><div class="pl-scroll">new records</div><details><summary>Costs</summary></details></div>' });
    assert.equal(await pending, true);
    assert.equal(scroll.top, 1250);
    assert.equal(w.document.querySelector('.pl-scroll').scrollTop, 180);
    assert.equal(w.document.querySelector('details').open, true);
    assert.equal(w.document.querySelector('.pl-scroll').textContent, 'new records');
    const failed = w.FBMProfitFilters.refresh();
    resolveResponse({ ok: true, text: async () => '<form>Login</form>' });
    assert.equal(await failed, false);
    assert.equal(w.document.querySelector('.pl-scroll').textContent, 'new records');
    assert.equal(w.document.querySelector('.pl-error'), null);
    console.log('Profit live update checks passed');
    w.close();
})().catch(error => { console.error(error); process.exitCode = 1; w.close(); });
