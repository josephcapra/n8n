#!/usr/bin/env python3
"""Create new sitemap redirects in RG admin (idempotent: skips existing source paths)."""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

LIST_URL = "https://www.paradiserealtyfla.com/admin/redirects/redirect/?q=sitemap"
ADD_URL = "https://www.paradiserealtyfla.com/admin/redirects/redirect/add/"
GCS_BASE = "https://storage.googleapis.com/site-map-dynamic-page3/sitemaps"
DATA_DIR = str(Path(os.getenv("BROWSER_DATA_DIR", "~/.sitemap-sync-browser")).expanduser())

REDIRECTS = [
    ("/sitemap-13", f"{GCS_BASE}/sitemap-13.xml"),
    ("/sitemap-14", f"{GCS_BASE}/sitemap-14.xml"),
    ("/sitemap-15", f"{GCS_BASE}/sitemap-15.xml"),
]


def main() -> int:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=DATA_DIR,
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.new_page()
        try:
            # First scrape existing sources to avoid duplicates
            page.goto(LIST_URL, wait_until="networkidle", timeout=30_000)
            if "login.realgeeks.com" in page.url:
                print(f"❌  Session expired. DATA_DIR={DATA_DIR}")
                return 2
            existing = set()
            for row in page.query_selector_all("table#result_list tbody tr"):
                for a in row.query_selector_all("a"):
                    t = a.inner_text().strip()
                    if t.startswith("/sitemap"):
                        existing.add(t)
            print(f"  Existing /sitemap* redirects: {len(existing)}")

            for src, tgt in REDIRECTS:
                if src in existing:
                    print(f"⏭   Skip (already exists): {src}")
                    continue
                page.goto(ADD_URL, wait_until="networkidle", timeout=30_000)
                page.fill("#id_old_path", src)
                page.fill("#id_new_path", tgt)
                page.click("input[name='_save']")
                page.wait_for_load_state("networkidle", timeout=30_000)

                if "/add/" in page.url:
                    err = [e.inner_text() for e in page.query_selector_all(".errorlist li, .errornote")]
                    print(f"❌  Save failed for {src}: {err}")
                else:
                    print(f"✅  Created: {src} → {tgt}")
        finally:
            ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
