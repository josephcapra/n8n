#!/usr/bin/env python3
"""
wire_area_sitemap.py — One-time wiring of the dedicated area-page sitemap into
search engines. Run this once; afterwards the communities-scraper job keeps the
GCS object fresh and Google/Bing re-crawl it on their own schedule.

It performs three steps:
  1. Create a RealGeeks 301 redirect  /area-pages-sitemap/  ->  the GCS sitemap
     (so the sitemap is reachable under the verified paradiserealtyfla.com domain).
  2. Submit https://www.paradiserealtyfla.com/area-pages-sitemap/ to Google Search
     Console.
  3. Submit the same URL to Bing Webmaster Tools.

Prerequisites
-------------
* RealGeeks admin session at ~/paradise-realty/.rg_session.json (auto-refreshes
  via Gmail 2FA if expired — see create_redirects_southeast_fl.login).
* ADC with the webmasters scope for the GSC step:
      gcloud auth application-default login \
        --scopes=https://www.googleapis.com/auth/webmasters,https://www.googleapis.com/auth/cloud-platform
* BING_API_KEY in sitemap-sync/.env

Usage
-----
    python3 wire_area_sitemap.py            # do all three steps
    python3 wire_area_sitemap.py --dry-run  # show what it would do
"""

import argparse
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

SITE = "https://www.paradiserealtyfla.com"
REDIRECT_PATH = "/area-pages-sitemap/"
SITEMAP_URL = f"{SITE}{REDIRECT_PATH}"          # what we submit to Google + Bing
GCS_TARGET = "https://storage.googleapis.com/site-map-dynamic-page3/area-pages/sitemap.xml"

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")


def step1_redirect(dry_run: bool) -> bool:
    print("\n=== STEP 1: RealGeeks redirect ===")
    print(f"  {REDIRECT_PATH}  ->  {GCS_TARGET}")
    if dry_run:
        print("  [dry-run] skipped")
        return True
    sys.path.insert(0, str(Path.home() / "paradise-realty"))
    import create_redirects_southeast_fl as rg
    from playwright.sync_api import sync_playwright

    session_file = Path.home() / "paradise-realty/.rg_session.json"
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = (b.new_context(storage_state=str(session_file))
               if session_file.exists() else b.new_context())
        pg = ctx.new_page()
        pg.goto(f"{rg.ADMIN_URL}/", wait_until="domcontentloaded")
        if "login" in pg.url or "sign-in" in pg.url:
            rg.login(pg)
        result = rg.add_redirect(pg, REDIRECT_PATH, GCS_TARGET)
        ctx.storage_state(path=str(session_file))
        b.close()
    print(f"  add_redirect => {result}")
    return result in ("created", "duplicate")


def step1b_verify() -> bool:
    print("\n=== verify redirect resolves to the sitemap ===")
    try:
        r = requests.get(SITEMAP_URL, timeout=30, allow_redirects=True)
        n = r.text.count("<loc>")
        ok = r.status_code == 200 and n > 0
        print(f"  {SITEMAP_URL} -> HTTP {r.status_code}, {n} <loc> entries  "
              f"(final url: {r.url[:70]})")
        return ok
    except Exception as e:
        print(f"  verify failed: {e}")
        return False


def step2_gsc(dry_run: bool) -> bool:
    print("\n=== STEP 2: Google Search Console ===")
    if dry_run:
        print(f"  [dry-run] would submit {SITEMAP_URL} to {SITE}/")
        return True
    from gsc_client import _build_service
    try:
        svc = _build_service()
        svc.sitemaps().submit(siteUrl=f"{SITE}/", feedpath=SITEMAP_URL).execute()
        info = svc.sitemaps().get(siteUrl=f"{SITE}/", feedpath=SITEMAP_URL).execute()
        print(f"  submitted ✓  isPending={info.get('isPending')} "
              f"lastSubmitted={info.get('lastSubmitted','')}")
        return True
    except Exception as e:
        msg = str(e)
        print(f"  GSC submit FAILED: {msg[:200]}")
        if "insufficient" in msg.lower() or "403" in msg:
            print("  -> Re-auth ADC with the webmasters scope:")
            print("     gcloud auth application-default login --scopes="
                  "https://www.googleapis.com/auth/webmasters,"
                  "https://www.googleapis.com/auth/cloud-platform")
        return False


def step3_bing(dry_run: bool) -> bool:
    print("\n=== STEP 3: Bing Webmaster Tools ===")
    import os
    api_key = os.getenv("BING_API_KEY")
    if not api_key:
        print("  BING_API_KEY not set in .env — skipping")
        return False
    if dry_run:
        print(f"  [dry-run] would SubmitFeed {SITEMAP_URL}")
        return True
    from bing_client import _call
    try:
        _call("post", "SubmitFeed", api_key,
              json_body={"siteUrl": f"{SITE}/", "feedUrl": SITEMAP_URL})
        print(f"  submitted ✓  {SITEMAP_URL}")
        return True
    except Exception as e:
        print(f"  Bing submit FAILED: {str(e)[:200]}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print(f"Wiring dedicated area-page sitemap: {SITEMAP_URL}")
    ok1 = step1_redirect(args.dry_run)
    if ok1 and not args.dry_run:
        step1b_verify()
    ok2 = step2_gsc(args.dry_run)
    ok3 = step3_bing(args.dry_run)

    print("\n" + "=" * 50)
    print(f"  redirect: {'ok' if ok1 else 'FAILED'}")
    print(f"  GSC:      {'ok' if ok2 else 'FAILED'}")
    print(f"  Bing:     {'ok' if ok3 else 'FAILED'}")
    print("=" * 50)
    return 0 if (ok1 and ok2 and ok3) else 1


if __name__ == "__main__":
    sys.exit(main())
