#!/usr/bin/env python3
"""Search RG admin for any ContentPage records whose URL matches /sitemap*."""
import os
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()
DATA_DIR = str(Path(os.getenv("BROWSER_DATA_DIR", "~/.sitemap-sync-browser")).expanduser())
SEARCH = "https://www.paradiserealtyfla.com/admin/content/contentpage/?q=sitemap"


def main() -> int:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=DATA_DIR, headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.new_page()
        try:
            page.goto(SEARCH, wait_until="networkidle", timeout=30_000)
            if "login.realgeeks.com" in page.url:
                print(f"❌  Session expired. DATA_DIR={DATA_DIR}")
                return 2
            rows = page.query_selector_all("table#result_list tbody tr")
            print(f"  Found {len(rows)} ContentPage rows matching 'sitemap':")
            for row in rows:
                cells = row.query_selector_all("td, th")
                texts = [c.inner_text().strip() for c in cells]
                a = row.query_selector("th a, td a")
                href = a.get_attribute("href") if a else ""
                print(f"    {texts}  edit={href}")
        finally:
            ctx.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
