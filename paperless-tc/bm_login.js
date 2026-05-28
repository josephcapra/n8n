// Brokermint (my.brokermint.com) login with SMS 2FA. Headed real Chrome,
// persistent profile so the "remember this device" session sticks. Password via
// env (BM_PASS). 2FA: after submitting creds an SMS code is texted to Joe; this
// script polls /tmp/bm_2fa_code.txt for the code (the operator writes it there
// once Joe reads it off his phone), enters it, ticks "remember device", saves.
//   NODE_PATH=/Users/User/node_modules BM_EMAIL=... BM_PASS='...' node bm_login.js
const { chromium } = require('playwright');
const os = require('os');
const path = require('path');
const fs = require('fs');

const PROFILE = process.env.BM_PROFILE || path.join(os.homedir(), '.brokermint-browser');
const EMAIL = process.env.BM_EMAIL || 'joe@josephcapra.com';
const PASS = process.env.BM_PASS || '';
const SHOTS = path.join(os.homedir(), 'paperless-tc', 'shots');
const CODE_FILE = '/tmp/bm_2fa_code.txt';
const ST = '/tmp/bm_login_status.txt';
const st = (s) => { try { fs.writeFileSync(ST, s + '\n'); } catch (e) {} console.log('[bm]', s); };
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  try { fs.unlinkSync(CODE_FILE); } catch (e) {}
  const opts = { headless: false, viewport: null, args: ['--disable-blink-features=AutomationControlled', '--start-maximized'] };
  let ctx;
  try { ctx = await chromium.launchPersistentContext(PROFILE, { ...opts, channel: 'chrome' }); st('launched_chrome'); }
  catch (e) { ctx = await chromium.launchPersistentContext(PROFILE, opts); st('launched_chromium'); }
  const page = ctx.pages()[0] || (await ctx.newPage());
  await page.bringToFront().catch(() => {});

  st('navigating');
  await page.goto('https://my.brokermint.com/', { waitUntil: 'domcontentloaded', timeout: 45000 }).catch((e) => st('nav_err:' + String(e).slice(0, 80)));
  await sleep(3500);

  // If already signed in (transactions visible), we're done.
  if (/\/transactions|\/dashboard/.test(page.url()) && !(await page.$('input[type="password"]'))) {
    st('already_logged_in:' + page.url());
  } else {
    st('filling_credentials');
    const emailSel = 'input[type="email"], input[name="email"], input[name*="email" i], input#user_email, input[name="login"]';
    const passSel = 'input[type="password"], input#user_password';
    try { await page.waitForSelector(emailSel, { timeout: 15000 }); await page.fill(emailSel, EMAIL); } catch (e) { st('email_fill_err:' + String(e).slice(0,60)); }
    try { await page.fill(passSel, PASS); } catch (e) { st('pass_fill_err:' + String(e).slice(0,60)); }
    // Tick "Remember me" on the LOGIN page so the session cookie is PERSISTENT
    // (otherwise it's a session-only cookie, lost when the browser closes -> the
    // headless daily run would see "please sign in"). Match by label or the lone
    // checkbox on the form.
    try {
      // Target the VISIBLE checkbox only (Rails also renders a hidden
      // input[name=remember_me] value=0 — selecting that throws "not a checkbox").
      const cb = await page.$('input[type="checkbox"]');
      if (cb) {
        if (!(await cb.isChecked())) await cb.check({ force: true });
        st('remember_me_checked');
      } else {
        await page.click('text=/remember me/i', { timeout: 2500 });
        st('remember_me_label_clicked');
      }
    } catch (e) { st('remember_me_err:' + String(e).slice(0,60)); }
    await page.screenshot({ path: path.join(SHOTS, 'bm_login_filled.png') }).catch(() => {});
    const btn = await page.$('button[type="submit"], input[type="submit"], button:has-text("Sign in"), button:has-text("Log in"), button:has-text("Login")');
    if (btn) { await btn.click().catch(() => {}); } else { await page.keyboard.press('Enter'); }
    st('submitted_credentials');
    await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
    await sleep(4000);
    await page.screenshot({ path: path.join(SHOTS, 'bm_after_submit.png') }).catch(() => {});

    // Detect a 2FA / verification-code input.
    const codeSel = 'input[name*="code" i], input[name*="otp" i], input[name*="token" i], input[autocomplete="one-time-code"], input[type="tel"], input[name*="verif" i], input[placeholder*="code" i]';
    let needs2fa = await page.$(codeSel);
    const bodyTxt = (await page.evaluate(() => document.body.innerText).catch(() => '')) || '';
    if (needs2fa || /verification|two-?factor|2fa|enter the code|sent (a|you) (a )?code|text message/i.test(bodyTxt)) {
      st('awaiting_2fa');  // <-- operator: write the SMS code into /tmp/bm_2fa_code.txt
      let code = null;
      for (let i = 0; i < 360; i++) { // up to ~12 min
        if (fs.existsSync(CODE_FILE)) { code = fs.readFileSync(CODE_FILE, 'utf8').trim(); if (code) break; }
        await sleep(2000);
      }
      if (!code) { st('2fa_timeout_no_code'); try { await ctx.close(); } catch (e) {} process.exit(2); }
      st('entering_2fa_code');
      // tick a "remember/trust this device" checkbox if present
      try { const cb = await page.$('input[type="checkbox"]'); if (cb && !(await cb.isChecked())) await cb.check({ force: true }); } catch (e) {}
      try { await page.fill(codeSel, code); } catch (e) {
        // some forms split the code across multiple single-char inputs
        const boxes = await page.$$(codeSel);
        if (boxes.length > 1) { for (let j = 0; j < code.length && j < boxes.length; j++) await boxes[j].fill(code[j]); }
      }
      const vbtn = await page.$('button[type="submit"], button:has-text("Verify"), button:has-text("Confirm"), button:has-text("Submit"), button:has-text("Continue")');
      if (vbtn) { await vbtn.click().catch(() => {}); } else { await page.keyboard.press('Enter'); }
      await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
      await sleep(4000);
    } else {
      st('no_2fa_detected');
    }
  }

  // Land on transactions to confirm.
  await page.goto('https://my.brokermint.com/#/transactions', { waitUntil: 'domcontentloaded', timeout: 45000 }).catch(() => {});
  await sleep(5000);
  const finalUrl = page.url();
  const title = await page.title().catch(() => '');
  st('final_url:' + finalUrl + ' | title:' + title);
  await page.screenshot({ path: path.join(SHOTS, 'bm_transactions.png'), fullPage: false }).catch(() => {});
  const ok = /transactions/i.test(finalUrl) && !(await page.$('input[type="password"]'));
  st(ok ? 'LOGIN_OK' : 'LOGIN_UNCONFIRMED');

  if (ok) {
    // Diagnostic: where does BoldTrail keep its auth? (cookie vs local/session storage)
    try {
      const storeInfo = await page.evaluate(() => ({
        ls: Object.keys(localStorage), ss: Object.keys(sessionStorage),
      }));
      fs.writeFileSync('/tmp/bm_storage_keys.json', JSON.stringify(storeInfo, null, 2));
      st('storage_keys: ls=' + storeInfo.ls.length + ' ss=' + storeInfo.ss.length);
    } catch (e) {}
    // Snapshot cookies + localStorage for replay (survives session-only cookies).
    try {
      await ctx.storageState({ path: path.join(os.homedir(), 'paperless-tc', 'bm_state.json') });
      st('storage_state_saved');
    } catch (e) { st('storage_state_err:' + String(e).slice(0, 50)); }
  }
  await sleep(2500);
  try { await ctx.close(); } catch (e) {}
  st('done');
  process.exit(ok ? 0 : 3);
})();
