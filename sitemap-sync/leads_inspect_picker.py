"""
Open leads.realgeeks.com using the cached session, screenshot the
/login/list site picker page, and dump its HTML so we can see what
button to click.
"""
import os, sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

DATA_DIR = Path(os.getenv("BROWSER_DATA_DIR", "~/.sitemap-sync-browser")).expanduser()
ts = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d-%H%M%S")
out = Path("logs") / f"leads-picker-{ts}"
out.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(DATA_DIR),
        headless=True,
        viewport={"width": 1400, "height": 900},
    )
    page = ctx.new_page()
    page.goto("https://leads.realgeeks.com/leads", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_load_state("networkidle", timeout=20_000)
    print(f"URL: {page.url}")
    (out / "page.html").write_text(page.content())
    page.screenshot(path=str(out / "screenshot.png"), full_page=True)
    # Print clickable elements summary
    links = page.eval_on_selector_all(
        "a, button",
        "els => els.map(e => ({tag: e.tagName, text: (e.innerText||'').trim().slice(0,80), href: e.href||null, cls: e.className||null})).filter(o => o.text)"
    )
    print(f"\nFound {len(links)} clickable elements with text:")
    for l in links[:50]:
        print(f"  [{l['tag']}] '{l['text']}' href={l['href']}")
    ctx.close()
print(f"\nOutput: {out.resolve()}")
