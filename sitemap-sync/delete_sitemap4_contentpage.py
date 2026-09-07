#!/usr/bin/env python3
"""Delete the ContentPage with slug 'sitemap4' (id 198) that intercepts the
/sitemap4/ redirect and returns HTML instead of XML."""
import os
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()
DATA_DIR = str(Path(os.getenv("BROWSER_DATA_DIR", "~/.sitemap-sync-browser")).expanduser())
EDIT_URL = "https://www.paradiserealtyfla.com/admin/content/contentpage/198/change/"


def main() -> int:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=DATA_DIR, headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.new_page()
        try:
            page.goto(EDIT_URL, wait_until="networkidle", timeout=30_000)
            if "login.realgeeks.com" in page.url:
                print(f"❌  Session expired. DATA_DIR={DATA_DIR}")
                return 2

            # Confirm we're on the right page
            slug = page.input_value("#id_slug") if page.query_selector("#id_slug") else ""
            print(f"  Confirmed slug='{slug}' on edit page")
            if slug != "sitemap4":
                print(f"  ⚠️  Unexpected slug — aborting")
                return 3

            # Click Delete link
            del_link = page.query_selector("a.deletelink")
            if not del_link:
                print("❌  Delete link not found")
                return 4
            del_link.click()
            page.wait_for_load_state("networkidle", timeout=30_000)

            # Confirm deletion
            confirm = page.query_selector("input[type='submit']")
            if not confirm:
                print("❌  Delete confirm button not found")
                return 5
            confirm.click()
            page.wait_for_load_state("networkidle", timeout=30_000)
            print(f"✅  Deleted ContentPage 198 (slug='sitemap4')")
        finally:
            ctx.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
