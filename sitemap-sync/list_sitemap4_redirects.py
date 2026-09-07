#!/usr/bin/env python3
"""List all sitemap4-related redirects in RG admin."""
import os
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()
DATA_DIR = str(Path(os.getenv("BROWSER_DATA_DIR", "~/.sitemap-sync-browser")).expanduser())
LIST_URL = "https://www.paradiserealtyfla.com/admin/redirects/redirect/?q=sitemap4"


def main() -> int:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=DATA_DIR, headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.new_page()
        try:
            page.goto(LIST_URL, wait_until="networkidle", timeout=30_000)
            if "login.realgeeks.com" in page.url:
                print(f"❌  Session expired. DATA_DIR={DATA_DIR}")
                return 2
            rows = page.query_selector_all("table#result_list tbody tr")
            for row in rows:
                cells = [c.inner_text().strip() for c in row.query_selector_all("td, th")]
                a = row.query_selector("th a, td a")
                href = a.get_attribute("href") if a else ""
                print(f"  {cells}  edit={href}")
        finally:
            ctx.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
