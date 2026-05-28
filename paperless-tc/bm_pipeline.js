// Brokermint (BoldTrail BackOffice) pending-deal pipeline puller → Finance Agent.
//
// Returns pending/under-contract deals with COMMISSION (+ sale price) so the CFO
// can forecast short-term incoming revenue.
//
// ROBUST SESSION (fixed 2026-05-28): BoldTrail's auth is a SESSION-ONLY cookie
// (`_trakitweb2_session`, exp=-1) that the on-disk persistent profile drops on
// close — so reusing ~/.brokermint-browser headless lands on the login wall and
// re-prompts SMS 2FA every run. Instead we REPLAY the storageState snapshot
// (~/paperless-tc/bm_state.json, captured by bm_login.js) which restores that
// cookie, and we call the SPA's REAL endpoint `/transactions?reference_date=…`
// (the public /api/v1 path is dead). If the snapshot is missing/expired we emit
// needs_login so the daemon tells the operator to re-auth (node bm_login.js).
//
// Output: a SINGLE JSON object on STDOUT (diagnostics to STDERR):
//   {ok:true, pipeline:[{address, sale_price, commission, close_date, status}], raw_count, fields_seen}
//   {ok:false, error:"needs_login"|"fetch_failed"|"timeout", detail}
//   NODE_PATH=/Users/User/node_modules node bm_pipeline.js [pull|preview|status]
const { chromium } = require('playwright');
const os = require('os'), path = require('path'), fs = require('fs');

const STATE = path.join(os.homedir(), 'paperless-tc', 'bm_state.json');
const ACTION = (process.argv[2] || 'pull').trim();
const TODAY = new Date().toISOString().slice(0, 10);
const log = (...a) => console.error('[bm-pipeline]', ...a);
const emit = (obj) => { process.stdout.write(JSON.stringify(obj) + '\n'); };

// epoch-ms (or ISO string) -> 'YYYY-MM-DD' (or null). Brokermint dates are epoch ms.
function toISODate(v) {
  if (v == null || v === '') return null;
  const d = new Date(typeof v === 'number' || /^\d+$/.test(String(v)) ? Number(v) : v);
  return isNaN(d.getTime()) ? null : d.toISOString().slice(0, 10);
}

(async () => {
  setTimeout(() => { log('WATCHDOG timeout'); emit({ ok: false, error: 'timeout' }); process.exit(1); }, 180000);
  if (!fs.existsSync(STATE)) { emit({ ok: false, error: 'needs_login', detail: 'no bm_state.json — run: node ~/paperless-tc/bm_login.js (needs SMS code)' }); process.exit(1); }

  let browser;
  try { browser = await chromium.launch({ headless: true, channel: 'chrome' }); }
  catch (e) { browser = await chromium.launch({ headless: true }); }
  const ctx = await browser.newContext({ storageState: STATE });
  const page = await ctx.newPage();
  try {
    await page.goto('https://my.brokermint.com/#/transactions', { waitUntil: 'domcontentloaded', timeout: 45000 });
    await page.waitForTimeout(2500);
    const bodyHead = await page.evaluate(() => document.body.innerText.slice(0, 120)).catch(() => '');
    if (/sign in|sign up before continuing|remember me/i.test(bodyHead)) {
      emit({ ok: false, error: 'needs_login', detail: 'Brokermint session expired — re-auth: node ~/paperless-tc/bm_login.js' });
      await ctx.close(); await browser.close(); process.exit(1);
    }
    if (ACTION === 'status') { emit({ ok: true, pipeline: [], note: 'Brokermint session is authenticated.' }); await ctx.close(); await browser.close(); process.exit(0); }

    // Pull the transaction list, then ENRICH each non-closed deal from its detail
    // (/transactions/{id}): the list view lacks closing_date and often shows
    // commission 0, while the detail carries closing_date (epoch ms) +
    // total_gross_commission. Commission = the gross check the brokerage receives
    // at closing; fall back to the list's office_commissions_net when gross is 0.
    const res = await page.evaluate(async (today) => {
      const HDR = { credentials: 'include', headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' } };
      const CLOSED = /clos|cancel|archiv|expired|withdraw|terminat|dead|fell/i;
      const num = (v) => { const n = parseFloat(String(v == null ? '' : v).replace(/[^0-9.\-]/g, '')); return isNaN(n) ? 0 : n; };
      const lr = await fetch('/transactions?reference_date=' + today + '&per_page=500&page=1', HDR);
      if (!/json/i.test(lr.headers.get('content-type') || '')) return { httpError: lr.status };
      const entries = (await lr.json()).entries || [];
      const open = entries.filter((e) => !CLOSED.test(e.status || ''));
      const deals = [];
      for (const e of open) {
        let closing_ms = null, gross = 0;
        try {
          const dr = await fetch('/transactions/' + e.id, HDR);
          if (/json/i.test(dr.headers.get('content-type') || '')) {
            const d = await dr.json();
            closing_ms = d.closing_date || null;          // epoch ms (only on the detail)
            gross = num(d.total_gross_commission);
          }
        } catch (_) {}
        const net = num(e.office_commissions_net);
        deals.push({
          address: [e.address, e.city, [e.state, e.zip].filter(Boolean).join(' ')].filter(Boolean).join(', ') || e.transaction_name || '(unnamed deal)',
          sale_price: num(e.price),
          commission: gross > 0 ? gross : net,
          gross_commission: gross, office_net: net,
          close_date_ms: closing_ms,
          status: (e.status || '').toString(),
        });
      }
      return { entries_count: entries.length, open_count: open.length, deals, fields_seen: entries[0] ? Object.keys(entries[0]) : [] };
    }, TODAY).catch((e) => ({ jsErr: String(e).slice(0, 160) }));

    if (res.jsErr || res.httpError || !Array.isArray(res.deals)) {
      emit({ ok: false, error: 'fetch_failed', detail: res.jsErr || `HTTP ${res.httpError || '?'}` });
      await ctx.close(); await browser.close(); process.exit(1);
    }
    const pipeline = res.deals.map((d) => ({
      address: d.address, sale_price: d.sale_price, commission: d.commission,
      gross_commission: d.gross_commission, office_net: d.office_net,
      close_date: toISODate(d.close_date_ms), status: d.status,
    }));
    const dated = pipeline.filter((d) => d.close_date).length;
    log(`fetched ${res.entries_count} txns; ${res.open_count} open; ${dated} with a closing date`);
    try { await ctx.storageState({ path: STATE }); } catch (e) {}
    emit({ ok: true, pipeline, raw_count: res.entries_count, open_count: res.open_count, dated, fields_seen: res.fields_seen });
    await ctx.close(); await browser.close(); process.exit(0);
  } catch (e) {
    log('FATAL', String(e).slice(0, 200));
    emit({ ok: false, error: 'fetch_failed', detail: String(e).slice(0, 200) });
    try { await ctx.close(); await browser.close(); } catch (_) {}
    process.exit(1);
  }
})();
