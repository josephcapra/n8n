"""
Click into the Website Settings + Emails sections of the Paradise
Realty Django admin and screenshot every form we find.
We're hunting for: default saved-search email frequency.
"""
import json, os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

DATA_DIR = Path(os.getenv("BROWSER_DATA_DIR", "~/.sitemap-sync-browser")).expanduser()
ts = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d-%H%M%S")
out = Path("logs") / f"website-settings-{ts}"
out.mkdir(parents=True, exist_ok=True)
print(f"Output: {out.resolve()}", flush=True)

def dump(page, label):
    safe = "".join(c if c.isalnum() else "-" for c in label)[:60]
    (out / f"{safe}.html").write_text(page.content())
    page.screenshot(path=str(out / f"{safe}.png"), full_page=True)
    print(f"  → {safe}.png  ({page.url})", flush=True)


with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(DATA_DIR),
        headless=False,
        viewport={"width": 1400, "height": 1100},
    )
    page = ctx.new_page()

    # Start on the admin index
    page.goto("https://www.paradiserealtyfla.com/admin/", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_load_state("networkidle", timeout=15_000)
    print(f"Admin: {page.url}", flush=True)

    # Click "Website Settings" card
    print("Clicking Website Settings", flush=True)
    page.get_by_text("Website Settings", exact=True).first.click()
    page.wait_for_load_state("networkidle", timeout=15_000)
    page.wait_for_timeout(2_500)
    dump(page, "01-website-settings")

    # Scan the form for any field mentioning daily/weekly/frequency/saved
    fields = page.eval_on_selector_all(
        "label, select, input, textarea, [class*='field'], [class*='form'], "
        "fieldset legend, .help, .helptext",
        "els => els.map(e => ({tag:e.tagName, text:(e.innerText||e.placeholder||e.name||'').trim().slice(0,120), name:e.name||null, value:e.value||null, type:e.type||null})).filter(o=>o.text)"
    )
    (out / "fields-website-settings.json").write_text(json.dumps(fields, indent=2))
    relevant = [f for f in fields if any(k in (f['text']+' '+(f['name'] or '')).lower()
                  for k in ('saved','search','daily','weekly','monthly','frequency','listing alert','email subscription'))]
    print(f"  Relevant fields: {len(relevant)}", flush=True)
    for r in relevant[:30]:
        print(f"    - [{r['tag']}] name={r['name']} text='{r['text'][:80]}'  value={r['value']}", flush=True)

    # Walk back to admin index, click each E-mail/Forms link for completeness
    for label in ("Custom Sign Up Forms", "Property Landing Sign Up Form Branding",
                  "Market Report Sign Up Form Branding", "Auto Responder Emails"):
        try:
            page.goto("https://www.paradiserealtyfla.com/admin/", wait_until="domcontentloaded", timeout=20_000)
            page.wait_for_timeout(1_500)
            link = page.get_by_text(label, exact=True).first
            if not link.is_visible():
                continue
            print(f"Clicking '{label}'", flush=True)
            link.click()
            page.wait_for_load_state("networkidle", timeout=15_000)
            page.wait_for_timeout(2_500)
            dump(page, f"02-{label}")
        except Exception as e:
            print(f"  '{label}': {e}", flush=True)

    # Mobile App Settings (Quick Links)
    try:
        page.goto("https://www.paradiserealtyfla.com/admin/", wait_until="domcontentloaded", timeout=20_000)
        page.wait_for_timeout(1_500)
        link = page.get_by_text("Mobile App Settings", exact=True).first
        if link.is_visible():
            link.click()
            page.wait_for_load_state("networkidle", timeout=15_000)
            page.wait_for_timeout(2_500)
            dump(page, "03-mobile-app-settings")
    except Exception as e:
        print(f"  Mobile App Settings: {e}", flush=True)

    print("DONE", flush=True)
    ctx.close()
