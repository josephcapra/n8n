#!/usr/bin/env python3
"""Fix the /sitemap4 redirect to point at sitemap-4.xml in site-map-dynamic-page3."""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

REDIRECTS_LIST = "https://www.paradiserealtyfla.com/admin/redirects/redirect/?q=sitemap"
NEW_TARGET = "https://storage.googleapis.com/site-map-dynamic-page3/sitemaps/sitemap-4.xml"
DATA_DIR = str(Path(os.getenv("BROWSER_DATA_DIR", "~/.sitemap-sync-browser")).expanduser())


def main() -> int:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=DATA_DIR,
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.new_page()
        try:
            page.goto(REDIRECTS_LIST, wait_until="networkidle", timeout=30_000)
            if "login.realgeeks.com" in page.url or "sign-in" in page.url:
                print(f"❌  Session expired. Open this in browser pointed at profile and log in:\n   {DATA_DIR}")
                return 2

            rows = page.query_selector_all("table#result_list tbody tr")
            print(f"→  {len(rows)} rows on page")

            edit_url = None
            for row in rows:
                # Some Django admin tables show source in td.field-old_path or first td a
                # We need the row whose "old_path" / display source equals exactly "/sitemap4"
                links = row.query_selector_all("a")
                for link in links:
                    text = link.inner_text().strip()
                    if text == "/sitemap4":
                        href = link.get_attribute("href") or ""
                        if "/change/" in href:
                            edit_url = href if href.startswith("http") else f"https://www.paradiserealtyfla.com{href}"
                            break
                if edit_url:
                    break

            if not edit_url:
                # Fallback: dump first 30 row texts so we can see what's there
                print("→  Couldn't find exact match '/sitemap4'. Dumping rows:")
                for i, row in enumerate(rows[:30]):
                    cells = [c.inner_text().strip() for c in row.query_selector_all("td")]
                    print(f"  [{i}] {cells}")
                return 3

            print(f"→  Editing: {edit_url}")
            page.goto(edit_url, wait_until="networkidle", timeout=30_000)

            current = page.input_value("#id_new_path") or ""
            print(f"→  Current Redirect to: {current}")
            page.fill("#id_new_path", NEW_TARGET)
            print(f"→  Setting Redirect to: {NEW_TARGET}")

            page.click("input[name='_save']")
            page.wait_for_load_state("networkidle", timeout=30_000)

            page.goto(edit_url, wait_until="networkidle", timeout=30_000)
            after = page.input_value("#id_new_path") or ""
            print(f"→  Saved value: {after}")
            if after == NEW_TARGET:
                print("✅  /sitemap4 redirect updated successfully.")
                return 0
            else:
                print("⚠️   Save did not persist.")
                return 4
        finally:
            ctx.close()


if __name__ == "__main__":
    sys.exit(main())
