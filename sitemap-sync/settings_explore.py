"""
Explore RealGeeks settings to find the default saved-search email
frequency. Do NOT change anything — just screenshot + dump HTML so
we can decide where the toggle is.

Checks (in order):
  1. CRM settings tab (tab-settings)
  2. Direct deep links inside leads.realgeeks.com that might host
     site / saved-search defaults
  3. paradiserealtyfla.com Django admin (already used for redirects)
"""
from __future__ import annotations
import json, os, sys, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

DATA_DIR = Path(os.getenv("BROWSER_DATA_DIR", "~/.sitemap-sync-browser")).expanduser()
ts = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d-%H%M%S")
out = Path("logs") / f"settings-explore-{ts}"
out.mkdir(parents=True, exist_ok=True)
print(f"Output: {out.resolve()}", flush=True)

net_log = (out / "network.jsonl").open("w")

def interesting(url):
    p = urlparse(url)
    if any(p.path.endswith(ext) for ext in
           (".js",".css",".png",".jpg",".svg",".woff",".woff2",
            ".gif",".ico",".map",".webmanifest")):
        return False
    return "realgeeks" in p.netloc or "paradiserealty" in p.netloc

def dump(page, label):
    safe = "".join(c if c.isalnum() else "-" for c in label)[:60]
    (out / f"{safe}.html").write_text(page.content())
    page.screenshot(path=str(out / f"{safe}.png"), full_page=True)
    print(f"  → {safe}.png  ({page.url})", flush=True)

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(DATA_DIR),
        headless=False,
        viewport={"width": 1400, "height": 900},
    )
    page = ctx.new_page()

    def on_response(resp):
        if not interesting(resp.url): return
        e = {"method": resp.request.method, "status": resp.status,
             "url": resp.url, "ct": resp.headers.get("content-type","")}
        if "json" in e["ct"]:
            try:
                b = resp.text()
                if len(b) < 250_000: e["body"] = b
            except: pass
        net_log.write(json.dumps(e)+"\n"); net_log.flush()
    page.on("response", on_response)

    # Get past site picker if shown
    page.goto("https://leads.realgeeks.com/", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_load_state("networkidle", timeout=20_000)
    if "login/list" in page.url:
        page.get_by_role("button", name="Log in as User").first.click()
        page.wait_for_load_state("networkidle", timeout=20_000)

    print(f"Dashboard at: {page.url}", flush=True)
    dump(page, "01-dashboard")

    # Click side-nav settings tab
    print("Clicking tab-settings", flush=True)
    page.click("[data-cy='tab-settings']")
    page.wait_for_load_state("networkidle", timeout=15_000)
    page.wait_for_timeout(4_000)
    dump(page, "02-settings-default")
    print(f"After settings click: {page.url}", flush=True)

    # Enumerate visible sub-nav links
    sublinks = page.eval_on_selector_all(
        "a, [role='menuitem'], button",
        "els => els.map(e => ({text:(e.innerText||'').trim().slice(0,80), href:e.href||null, dataCy:e.getAttribute('data-cy')})).filter(o => o.text && o.text.length < 60)"
    )
    (out / "settings-sublinks.json").write_text(json.dumps(sublinks, indent=2))
    print(f"  {len(sublinks)} sub-link/buttons captured → settings-sublinks.json", flush=True)

    # Look for anything mentioning saved search / email / default / frequency
    candidates = [s for s in sublinks if any(k in (s['text'] or '').lower()
                  for k in ('saved search','frequency','email','default','listing alert',
                            'notification','subscription','search subscribe','daily','weekly'))]
    print(f"  {len(candidates)} candidates matched keywords:", flush=True)
    for c in candidates[:30]:
        print(f"    - '{c['text']}'  href={c['href']}", flush=True)

    # Also try some likely deep links inside leads.realgeeks.com
    for path in ("/secure/djbar", "/settings", "/secure/settings",
                 "/secure/settings/saved-searches", "/admin",
                 "/secure/site_settings"):
        url = "https://leads.realgeeks.com" + path
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=15_000)
            page.wait_for_timeout(2_500)
            dump(page, f"03-deeplink{path.replace('/','_')}")
        except Exception as e:
            print(f"  deeplink {path} failed: {e}", flush=True)

    # Also check the Django admin on paradiserealtyfla.com — it may have a site config
    try:
        page.goto("https://www.paradiserealtyfla.com/admin/", wait_until="domcontentloaded", timeout=20_000)
        page.wait_for_timeout(3_000)
        dump(page, "04-pf-django-admin-index")
    except Exception as e:
        print(f"  pf admin failed: {e}", flush=True)

    net_log.close()
    print("DONE", flush=True)
    ctx.close()
