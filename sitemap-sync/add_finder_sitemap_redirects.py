#!/usr/bin/env python3
"""Add RealGeeks 301 redirects for the Community Finder sitemaps. ADDS ONLY - never deletes.

Joe's rule: do not delete any of the old sitemaps or their redirects. sync-sitemaps.py cannot be used
for this, because its sync_redirects() builds a `desired` set of /sitemapN/ paths and then deletes every
existing /sitemapN/ redirect that is not in that set - running it with only the finder shards would
remove the live /sitemap1/ and /sitemap2/ redirects. So this script reuses that module's login, scrape
and upsert helpers and simply omits the cleanup pass.

Paths are prefixed "finder-" so they cannot collide with any of the 54 sitemaps already submitted
to Search Console.

Usage:  python3 add_finder_sitemap_redirects.py [--dry-run]
"""
import argparse, os, sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

import realgeeks_client as rg

GCS_BASE = "https://storage.googleapis.com/run-sources-paradise-automation-us-east1/sitemaps/"

# redirect path  ->  GCS object
REDIRECTS = {
    "/finder-sitemap-index/":            "finder-sitemap-index.xml",
    "/finder-sitemap-new-construction/": "finder-sitemap-new-construction.xml",
    "/finder-sitemap-resale-1/":         "finder-sitemap-resale-1.xml",
}

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    load_dotenv(Path(__file__).parent / ".env")

    desired = {src: GCS_BASE + obj for src, obj in REDIRECTS.items()}
    print(f"{'DRY RUN' if args.dry_run else 'APPLY'} - {len(desired)} redirect(s) to add/update\n")
    for s, t in desired.items():
        print(f"  {s:38} -> {t}")
    if args.dry_run:
        print("\nDry run - nothing written.")
        return 0

    login_url    = os.environ["REALGEEKS_LOGIN_URL"]
    redirects_url = os.environ["REALGEEKS_REDIRECTS_URL"]
    username     = os.environ["REALGEEKS_USER"]
    password     = os.environ.get("REALGEEKS_PASS", "")
    log_dir = Path(__file__).parent / "logs"; log_dir.mkdir(exist_ok=True)

    added = failed = 0
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=rg._browser_data_dir(), headless=True,
            args=["--disable-blink-features=AutomationControlled"])
        page = ctx.new_page()
        try:
            rg._ensure_logged_in(page, login_url, redirects_url, username, password)
            existing = rg._scrape_redirects(page, redirects_url)
            print(f"\n  {len(existing)} existing redirects found - none will be removed")
            for src, tgt in desired.items():
                try:
                    rg._upsert(page, redirects_url, src, tgt, existing)
                    added += 1; print(f"  OK   {src}")
                except Exception as e:
                    failed += 1; print(f"  FAIL {src}: {type(e).__name__}: {str(e)[:160]}")
            page.screenshot(path=str(log_dir / "finder-redirects.png"), full_page=True)
        finally:
            ctx.close()

    print(f"\nResult: {added} upserted, {failed} failed, 0 deleted")
    return 0 if failed == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
