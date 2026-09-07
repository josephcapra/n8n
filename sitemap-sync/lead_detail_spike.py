"""
Navigate directly to a lead detail page, capture every GraphQL
REQUEST + RESPONSE body, then attempt to open the saved-search
creation modal to discover the mutation + frequency options.

Usage:
  cd /Users/User/sitemap-sync && python3 lead_detail_spike.py [LEAD_ID]

Default LEAD_ID = 71357318 (lead "A a" with 3 properties viewed)
"""
from __future__ import annotations

import json, os, sys, time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

LEAD_ID = sys.argv[1] if len(sys.argv) > 1 else "71357318"
LEAD_URL = f"https://leads.realgeeks.com/leads/{LEAD_ID}"
DATA_DIR = Path(os.getenv("BROWSER_DATA_DIR", "~/.sitemap-sync-browser")).expanduser()

ts = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d-%H%M%S")
out = Path("logs") / f"lead-detail-{LEAD_ID}-{ts}"
out.mkdir(parents=True, exist_ok=True)
print(f"  Output: {out.resolve()}", flush=True)

net_log = (out / "network.jsonl").open("w")
gql_log = (out / "graphql.jsonl").open("w")


def interesting(url: str) -> bool:
    p = urlparse(url)
    if any(p.path.endswith(ext) for ext in
           (".js", ".css", ".png", ".jpg", ".svg", ".woff", ".woff2",
            ".gif", ".ico", ".map", ".webmanifest")):
        return False
    return "realgeeks" in p.netloc


with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(DATA_DIR),
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
        viewport={"width": 1400, "height": 900},
    )
    page = ctx.new_page()

    def on_request(req):
        if "graphql" in req.url:
            try:
                body = req.post_data or ""
                op_name = None
                try:
                    j = json.loads(body)
                    op_name = j.get("operationName")
                except Exception:
                    pass
                gql_log.write(json.dumps({
                    "phase": "request",
                    "operationName": op_name,
                    "body": body,
                }) + "\n")
                gql_log.flush()
            except Exception as e:
                gql_log.write(json.dumps({"phase":"request","err":str(e)}) + "\n")
                gql_log.flush()

    def on_response(resp):
        if not interesting(resp.url):
            return
        is_gql = "graphql" in resp.url
        entry = {
            "method": resp.request.method,
            "status": resp.status,
            "url": resp.url,
            "ct": resp.headers.get("content-type", ""),
        }
        if "json" in entry["ct"]:
            try:
                body = resp.text()
                if len(body) < 500_000:
                    entry["body"] = body
            except Exception as e:
                entry["body_err"] = str(e)
        net_log.write(json.dumps(entry) + "\n")
        net_log.flush()
        if is_gql:
            gql_log.write(json.dumps({
                "phase": "response",
                "status": entry["status"],
                "body": entry.get("body",""),
            }) + "\n")
            gql_log.flush()

    page.on("request", on_request)
    page.on("response", on_response)

    # Direct nav — session should be cached
    print(f"  → {LEAD_URL}", flush=True)
    page.goto(LEAD_URL, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_load_state("networkidle", timeout=30_000)
    print(f"  At: {page.url}", flush=True)

    # Handle picker if it appears
    if "login/list" in page.url:
        print("  Site picker — clicking 'Log in as User'", flush=True)
        page.get_by_role("button", name="Log in as User").first.click()
        page.wait_for_load_state("networkidle", timeout=20_000)
        page.goto(LEAD_URL, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_load_state("networkidle", timeout=30_000)
        print(f"  After picker: {page.url}", flush=True)

    print("  Settling 12s for lead detail render...", flush=True)
    page.wait_for_timeout(12_000)

    (out / "lead-detail.html").write_text(page.content())
    page.screenshot(path=str(out / "01-lead-detail.png"), full_page=True)

    # ── List all visible tabs and major buttons for inventory ─────────
    tabs = page.eval_on_selector_all(
        "[role='tab'], a[data-cy], button[data-cy], [data-cy^='tab-']",
        "els => els.map(e => ({tag:e.tagName, dataCy:e.getAttribute('data-cy'), text:(e.innerText||'').trim().slice(0,60), role:e.getAttribute('role')}))"
    )
    (out / "tabs-inventory.json").write_text(json.dumps(tabs, indent=2))
    print(f"  Tab inventory: {len(tabs)} items → tabs-inventory.json", flush=True)

    # ── Try to click "Activity" or "History" tab ─────────────────────
    for label in ("Activity", "History", "Property Activity", "Searches", "Saved Searches"):
        loc = page.get_by_role("tab", name=label).first
        try:
            if loc.is_visible():
                print(f"  Clicking tab: '{label}'", flush=True)
                loc.click()
                page.wait_for_load_state("networkidle", timeout=15_000)
                page.wait_for_timeout(4_000)
                page.screenshot(path=str(out / f"02-{label.lower().replace(' ','-')}.png"), full_page=True)
                (out / f"02-{label.lower().replace(' ','-')}.html").write_text(page.content())
        except Exception as e:
            print(f"    tab '{label}' not visible/clickable: {e}", flush=True)

    # ── Try to open the "Create saved search" flow ────────────────────
    print("  Looking for 'Saved Searches' or 'Add' button...", flush=True)
    for sel in [
        "button:has-text('Add Saved Search')",
        "button:has-text('New Saved Search')",
        "button:has-text('Create Saved Search')",
        "[data-cy*='saved-search'] button",
        "a:has-text('Saved Searches')",
        "button:has-text('Saved Searches')",
    ]:
        el = page.query_selector(sel)
        if el and el.is_visible():
            print(f"  Found via '{sel}' — clicking", flush=True)
            try:
                el.click()
                page.wait_for_timeout(4_000)
                page.screenshot(path=str(out / "03-saved-search-modal.png"), full_page=True)
                (out / "03-saved-search-modal.html").write_text(page.content())
                break
            except Exception as e:
                print(f"    click failed: {e}", flush=True)

    page.wait_for_timeout(2_000)
    net_log.close()
    gql_log.close()
    print(f"\n  Done → {out.resolve()}", flush=True)
    ctx.close()
