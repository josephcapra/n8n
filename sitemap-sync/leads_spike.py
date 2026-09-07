"""
Spike: log in to leads.realgeeks.com, navigate the site picker, and capture
network traffic on the leads list + one lead detail page.

Two-phase login:
  1. Submit email/password.
  2. If 2FA page appears, USER completes it in the visible browser window
     (script polls for navigation away from the 2FA page).
  3. If /login/list appears (site picker), click the only site / first row.
  4. Capture network on the leads dashboard.
  5. Click first lead, capture detail page.

Usage:
  cd /Users/User/sitemap-sync && python3 leads_spike.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

LEADS_URL = "https://leads.realgeeks.com/leads"
USER      = os.getenv("REALGEEKS_USER")
PASS      = os.getenv("REALGEEKS_PASS")
DATA_DIR  = Path(os.getenv("BROWSER_DATA_DIR", "~/.sitemap-sync-browser")).expanduser()

SEL_EMAIL    = "input[type='email'], input[placeholder='Email']"
SEL_PASSWORD = "input[type='password'], input[placeholder='Password']"
SEL_SUBMIT   = "button[type='submit']"


def interesting(url: str) -> bool:
    p = urlparse(url)
    if any(p.path.endswith(ext) for ext in
           (".js", ".css", ".png", ".jpg", ".svg", ".woff",
            ".woff2", ".gif", ".ico", ".map", ".webmanifest")):
        return False
    return "realgeeks" in p.netloc or "/api/" in p.path or "/leads" in p.path


def wait_for_url_contains(page, fragment: str, timeout_s: int = 180) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if fragment in page.url:
            return True
        page.wait_for_timeout(1000)
    return False


def main() -> int:
    if not USER or not PASS:
        print("ERROR: set REALGEEKS_USER and REALGEEKS_PASS", file=sys.stderr)
        return 2

    ts = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d-%H%M%S")
    out_dir = Path("logs") / f"leads-spike-{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"  Output dir: {out_dir.resolve()}", flush=True)

    net_log = (out_dir / "network.jsonl").open("w")
    captured = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(DATA_DIR),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1400, "height": 900},
        )
        page = ctx.new_page()

        def on_response(resp):
            if not interesting(resp.url):
                return
            entry = {
                "method": resp.request.method,
                "status": resp.status,
                "url": resp.url,
                "ct": resp.headers.get("content-type", ""),
            }
            if "json" in entry["ct"]:
                try:
                    body = resp.text()
                    if len(body) < 250_000:
                        entry["body"] = body
                except Exception as e:
                    entry["body_err"] = str(e)
            net_log.write(json.dumps(entry) + "\n")
            net_log.flush()
            captured.append(entry)

        page.on("response", on_response)

        try:
            print(f"  → {LEADS_URL}", flush=True)
            page.goto(LEADS_URL, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_load_state("networkidle", timeout=20_000)
            print(f"  Landed at: {page.url}", flush=True)

            # ── Sign-in form ─────────────────────────────────────────────
            if "sign-in" in page.url or page.query_selector(SEL_EMAIL):
                print("  Filling credentials...", flush=True)
                page.fill(SEL_EMAIL, USER)
                page.fill(SEL_PASSWORD, PASS)
                page.click(SEL_SUBMIT)
                page.wait_for_timeout(3000)
                print(f"  After signin click: {page.url}", flush=True)

            # ── 2FA ──────────────────────────────────────────────────────
            if "2fa" in page.url.lower() or page.query_selector("input[name='code']"):
                print("  ⚠  2FA REQUIRED — please complete in the browser window now.", flush=True)
                print("     Waiting up to 3 min for you to enter the code...", flush=True)
                if not wait_for_url_contains(page, "leads.realgeeks.com", timeout_s=180):
                    raise RuntimeError(f"Stuck on 2FA. Current URL: {page.url}")
                page.wait_for_load_state("networkidle", timeout=20_000)
                print(f"  Past 2FA: {page.url}", flush=True)

            # ── Site picker (/login/list) ───────────────────────────────
            if "login/list" in page.url:
                print("  Site picker shown — dumping HTML/screenshot", flush=True)
                (out_dir / "picker.html").write_text(page.content())
                page.screenshot(path=str(out_dir / "picker.png"), full_page=True)

                # The picker is a table with a "Log in as User" button per row
                candidate = page.get_by_role("button", name="Log in as User").first
                if not candidate or not candidate.is_visible():
                    candidate = page.get_by_text("Log in as User", exact=True).first
                text = (candidate.inner_text() or "").strip()[:60]
                print(f"  Clicking site: '{text}'", flush=True)
                candidate.click()
                page.wait_for_load_state("networkidle", timeout=20_000)

            print(f"  Now at: {page.url}", flush=True)

            # ── Re-navigate to /leads if not there ─────────────────────
            if "/leads" not in page.url:
                page.goto(LEADS_URL, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_load_state("networkidle", timeout=20_000)
                print(f"  Forced nav to leads: {page.url}", flush=True)

            # ── Capture leads list page ────────────────────────────────
            print("  Letting leads list settle (10s)...", flush=True)
            page.wait_for_timeout(10_000)
            (out_dir / "leads-list.html").write_text(page.content())
            page.screenshot(path=str(out_dir / "leads-list.png"), full_page=True)

            # ── Click into first lead row ──────────────────────────────
            first = page.query_selector(
                "table tbody tr a, [role='row'] a, a[href*='/leads/']"
            )
            if first:
                href = first.get_attribute("href")
                print(f"  Clicking first lead: {href}", flush=True)
                first.click()
                page.wait_for_load_state("networkidle", timeout=20_000)
                page.wait_for_timeout(8_000)
                (out_dir / "lead-detail.html").write_text(page.content())
                page.screenshot(path=str(out_dir / "lead-detail.png"), full_page=True)
                print(f"  Lead detail URL: {page.url}", flush=True)
            else:
                print("  No lead row link found", flush=True)

        finally:
            net_log.close()
            print(f"\n  Captured {len(captured)} interesting responses", flush=True)
            print(f"  → {out_dir.resolve()}", flush=True)
            time.sleep(2)
            ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
