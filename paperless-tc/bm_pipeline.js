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

const CLOSED_RE = /clos|cancel|archiv|expired|withdraw|terminat|dead|fell/i;
function normalize(t) {
  const num = (v) => { if (v == null) return 0; const n = parseFloat(String(v).replace(/[^0-9.\-]/g, '')); return isNaN(n) ? 0 : n; };
  return {
    address: [t.address, t.city, [t.state, t.zip].filter(Boolean).join(' ')].filter(Boolean).join(', ') || t.transaction_name || '(unnamed deal)',
    sale_price: num(t.price),
    commission: num(t.total_gross_commission != null ? t.total_gross_commission : t.gci),
    close_date: t.buyer_expiration_date || t.closing_date || null,
    status: (t.status || '').toString(),
    owner: t.owner || null,
  };
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

    const res = await page.evaluate(async (today) => {
      const r = await fetch('/transactions?reference_date=' + today + '&per_page=500&page=1',
        { credentials: 'include', headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' } });
      const ct = r.headers.get('content-type') || '';
      if (!/json/i.test(ct)) return { httpError: r.status, ct };
      return { data: await r.json() };
    }, TODAY).catch((e) => ({ jsErr: String(e).slice(0, 160) }));

    if (res.jsErr || res.httpError || !res.data || !Array.isArray(res.data.entries)) {
      emit({ ok: false, error: 'fetch_failed', detail: res.jsErr || `HTTP ${res.httpError || '?'} ${res.ct || ''}` });
      await ctx.close(); await browser.close(); process.exit(1);
    }
    const all = res.data.entries;
    const fieldsSeen = all[0] ? Object.keys(all[0]) : [];
    const pipeline = all.map(normalize).filter((d) => !CLOSED_RE.test(d.status));
    log(`fetched ${all.length} txns; ${pipeline.length} in pipeline (non-closed)`);
    // keep the session warm
    try { await ctx.storageState({ path: STATE }); } catch (e) {}
    emit({ ok: true, pipeline, raw_count: all.length, fields_seen: fieldsSeen });
    await ctx.close(); await browser.close(); process.exit(0);
  } catch (e) {
    log('FATAL', String(e).slice(0, 200));
    emit({ ok: false, error: 'fetch_failed', detail: String(e).slice(0, 200) });
    try { await ctx.close(); await browser.close(); } catch (_) {}
    process.exit(1);
  }
})();
