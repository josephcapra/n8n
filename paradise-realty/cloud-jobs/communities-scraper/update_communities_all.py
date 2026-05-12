#!/usr/bin/env python3
"""
Update all Communities-sheet rows with fresh pricing/listing data.

Logic per row:
  1. Skip rows where Status contains inactive/removed/deprecated/sold out/closed
  2. Source URL priority: Price Source URL → Builder URL → DuckDuckGo web search
  3. Scrape: paradise API (fast) for paradiserealtyfla.com, Playwright for all others
  4. Price Category: Attainable / Mid-Range / Upper Mid-Range / Luxury / Ultra-Luxury
  5. Batch-write to sheet; flush every 10 rows; neon-green highlight updated cells

Resumable: /tmp/update_communities_checkpoint.json
Usage:
    python3 update_communities_all.py           # resume from checkpoint
    python3 update_communities_all.py --fresh   # clear checkpoint, re-scrape all
"""

import re, json, time, os, sys, warnings
from pathlib import Path
from datetime import date, datetime
from statistics import median
from collections import Counter

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

warnings.filterwarnings("ignore")

# ── Config ─────────────────────────────────────────────────────────────────────
SHEET_ID        = "16vVPO8XLEq_KbdLoVOP3jtTyoxqr-Ohjzg_0lMbBFxo"
TAB             = "Communities"
# Cloud Run mounts the secret at /etc/secrets/sheets_token.json; fall back to local dev path
TOKEN_FILE      = (
    "/etc/secrets/sheets_token.json"
    if os.path.exists("/etc/secrets/sheets_token.json")
    else str(Path.home() / "paradise-realty/api/sheets_token.json")
)
CHECKPOINT_FILE = "/tmp/update_communities_checkpoint.json"
TODAY           = date.today().isoformat()
PARADISE_BASE   = "https://www.paradiserealtyfla.com"
NEON_GREEN      = {"red": 0.224, "green": 1.0, "blue": 0.078}
SCOPES          = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]
SKIP_STATUSES = {"inactive", "removed", "deprecated", "discontinued", "sold out", "closed"}

SESSION = requests.Session()
SESSION.verify = False
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
})


# ── Pure helpers ───────────────────────────────────────────────────────────────
def col_letter(idx):
    result = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        result = chr(65 + r) + result
    return result


def price_category(price):
    if not price or price <= 0:
        return ""
    if price < 300_000:   return "Attainable"
    if price < 500_000:   return "Mid-Range"
    if price < 750_000:   return "Upper Mid-Range"
    if price < 1_000_000: return "Luxury"
    return "Ultra-Luxury"


def market_hotness(avg_dom):
    if avg_dom is None: return ""
    if avg_dom < 30:    return "Hot"
    if avg_dom < 60:    return "Warm"
    if avg_dom < 90:    return "Balanced"
    return "Slow"


def mode_val(lst):
    return Counter(lst).most_common(1)[0][0] if lst else ""


def normalize_url(url):
    url = url.strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


# ── Sheets auth ────────────────────────────────────────────────────────────────
# The secret mount is read-only, so we copy to /tmp for writeback on refresh.
_TOKEN_WRITABLE = "/tmp/sheets_token_rw.json"

def _prepare_token():
    if not os.path.exists(_TOKEN_WRITABLE):
        import shutil
        shutil.copy2(TOKEN_FILE, _TOKEN_WRITABLE)


def get_sheets_service():
    _prepare_token()
    creds = Credentials.from_authorized_user_file(_TOKEN_WRITABLE, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(_TOKEN_WRITABLE, "w") as f:
            f.write(creds.to_json())
        # Push refreshed token back to Secret Manager so next run stays valid
        try:
            from google.cloud import secretmanager
            sm = secretmanager.SecretManagerServiceClient()
            sm.add_secret_version(
                parent="projects/paradise-automation/secrets/sheets-oauth-token",
                payload={"data": creds.to_json().encode()},
            )
            print("[auth] refreshed token saved to Secret Manager")
        except Exception as e:
            print(f"[auth] warning: could not update Secret Manager: {e}")
    return build("sheets", "v4", credentials=creds)


# ── paradiserealtyfla.com scraper (API-based, no Playwright needed) ────────────
def _get_mls_list(area_url):
    try:
        r = SESSION.get(area_url, timeout=20)
        return list(dict.fromkeys(re.findall(r'/property/([A-Z][A-Z0-9]+)/', r.text)))
    except Exception as e:
        print(f"  [warn] mls list: {e}")
        return []


def _fetch_listing_fields(mls):
    try:
        data = SESSION.get(
            f"{PARADISE_BASE}/api/v2/search/?mls_number={mls}", timeout=15
        ).json()
        item = data[0] if isinstance(data, list) else {}
        return item.get("fields", {})
    except Exception:
        return {}


def scrape_paradise_url(area_url):
    mls_list = _get_mls_list(area_url)
    if not mls_list:
        return None

    prices, doms, hoa_fees, hoa_freqs = [], [], [], []
    heatings, coolings, roofs, sewers, exteriors = [], [], [], [], []
    sampled = 0
    today   = date.today()

    for mls in mls_list:
        f = _fetch_listing_fields(mls)
        if not f:
            continue
        sampled += 1

        lp = f.get("list_price", {}).get("raw")
        if lp:
            prices.append(int(lp))

        ld = f.get("list_date", {}).get("data", "")
        if ld:
            try:
                doms.append((today - datetime.fromisoformat(ld).date()).days)
            except Exception:
                pass

        hoa_raw = f.get("hoa_fee", {}).get("data", "")
        if hoa_raw:
            num = re.sub(r"[^\d.]", "", hoa_raw)
            if num:
                hoa_fees.append(float(num))

        hf = f.get("hoa_freq", {}).get("data", "")
        if hf:
            hoa_freqs.append(hf)

        for key, lst in [
            ("heating",           heatings),
            ("cooling",           coolings),
            ("roof",              roofs),
            ("sewer",             sewers),
            ("exterior_features", exteriors),
        ]:
            v = f.get(key, {}).get("data", "")
            if v:
                lst.append(v)

        time.sleep(0.05)

    if sampled == 0:
        return None

    avg_dom   = round(sum(doms) / len(doms)) if doms else None
    med_price = int(median(prices)) if prices else None

    return {
        "Area_Median_Price":         med_price if med_price is not None else "",
        "Area_Active_Listings":      len(mls_list),
        "Area_Avg_DOM":              avg_dom if avg_dom is not None else "",
        "Area_Market_Hotness":       market_hotness(avg_dom),
        "Listing_HOA_Fee":           f"{sum(hoa_fees)/len(hoa_fees):.0f}" if hoa_fees else "",
        "Listing_HOA_Freq":          mode_val(hoa_freqs),
        "Listing_Heating":           mode_val(heatings),
        "Listing_Cooling":           mode_val(coolings),
        "Listing_Roof":              mode_val(roofs),
        "Listing_Sewer":             mode_val(sewers),
        "Listing_Exterior_Features": mode_val(exteriors),
        "Listing_Sample_Count":      sampled,
        "_price_for_category":       med_price,
    }


# ── Playwright scraper for external/builder URLs ───────────────────────────────
def _extract_prices(html):
    """Return list of plausible home prices found in raw HTML."""
    prices = []
    # "From $XXX,XXX" / "Starting at $XXX,XXX" / "Priced from $XXX,XXX"
    for m in re.findall(
        r"(?:from|starting(?:\s+at)?|priced\s+from)\s*\$\s*([\d,]+)", html, re.I
    ):
        n = int(re.sub(r"[^\d]", "", m))
        if 100_000 < n < 10_000_000:
            prices.append(n)
    # "$XXX,XXX – $XXX,XXX" price range
    for lo, hi in re.findall(r"\$\s*([\d,]+)\s*(?:–|-|to)\s*\$\s*([\d,]+)", html):
        for v in (lo, hi):
            n = int(re.sub(r"[^\d]", "", v))
            if 100_000 < n < 10_000_000:
                prices.append(n)
    # Fallback: standalone "$XXX,XXX"
    if not prices:
        for m in re.findall(r"\$\s*(\d{3},\d{3})", html):
            n = int(re.sub(r"[^\d]", "", m))
            if 100_000 < n < 10_000_000:
                prices.append(n)
    return prices


def scrape_external_url(url):
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    empty = {k: "" for k in [
        "Area_Median_Price", "Area_Active_Listings", "Area_Avg_DOM",
        "Area_Market_Hotness", "Listing_HOA_Fee", "Listing_HOA_Freq",
        "Listing_Heating", "Listing_Cooling", "Listing_Roof", "Listing_Sewer",
        "Listing_Exterior_Features", "Listing_Sample_Count",
    ]}
    empty["_price_for_category"] = None

    def _run(pw):
        browser = pw.chromium.launch(headless=True)
        ctx  = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)
        except Exception as nav_err:
            print(f"  [warn] page load: {type(nav_err).__name__}: {str(nav_err)[:80]}")
            browser.close()
            return None

        html = page.content()

        # Try to follow one "floor plans / pricing / homes" sub-page
        for link_text in ["floor plans", "floorplans", "pricing", "available homes", "homes"]:
            try:
                link = page.get_by_role("link", name=re.compile(link_text, re.I)).first
                if link.is_visible():
                    href = link.get_attribute("href") or ""
                    if href and not href.startswith("javascript") and href != "#":
                        full_href = (
                            href if href.startswith("http")
                            else url.rstrip("/") + "/" + href.lstrip("/")
                        )
                        sub = ctx.new_page()
                        try:
                            sub.goto(full_href, wait_until="domcontentloaded", timeout=25000)
                            sub.wait_for_timeout(2000)
                            html += sub.content()
                        except Exception:
                            pass
                        finally:
                            sub.close()
                    break
            except Exception:
                pass

        browser.close()
        return html

    html = None
    with sync_playwright() as pw:
        html = _run(pw)
        if html is None:
            print(f"  [retry] Playwright timeout, retrying once…")
            time.sleep(2)
            html = _run(pw)

    if not html:
        return empty

    prices    = _extract_prices(html)
    med_price = int(median(prices)) if prices else None

    # HOA fee
    hoa_fee = ""
    for pat in [
        r"hoa[^$\n]{0,50}\$\s*([\d,]+)",
        r"\$\s*([\d,]+)[^\n]{0,40}(?:per\s+month|monthly)\s+hoa",
        r"homeowners[^\$\n]{0,40}\$\s*([\d,]+)",
        r"association[^\$\n]{0,40}\$\s*([\d,]+)",
    ]:
        m = re.search(pat, html, re.I)
        if m:
            n = int(re.sub(r"[^\d]", "", m.group(1)))
            if 0 < n < 5000:
                hoa_fee = str(n)
                break

    # HOA frequency — scan near HOA-related text
    hoa_ctx_m = re.search(r"hoa.{0,300}", html, re.I | re.DOTALL)
    hoa_ctx   = hoa_ctx_m.group(0) if hoa_ctx_m else html[:5000]
    hoa_freq  = ""
    if re.search(r"\bmonthly\b",        hoa_ctx, re.I): hoa_freq = "Monthly"
    elif re.search(r"\bquarterly\b",    hoa_ctx, re.I): hoa_freq = "Quarterly"
    elif re.search(r"\bannual(?:ly)?\b",hoa_ctx, re.I): hoa_freq = "Annual"

    return {
        "Area_Median_Price":         med_price if med_price is not None else "",
        "Area_Active_Listings":      len(prices) if prices else "",
        "Area_Avg_DOM":              "",
        "Area_Market_Hotness":       "",
        "Listing_HOA_Fee":           hoa_fee,
        "Listing_HOA_Freq":          hoa_freq,
        "Listing_Heating":           "",
        "Listing_Cooling":           "",
        "Listing_Roof":              "",
        "Listing_Sewer":             "",
        "Listing_Exterior_Features": "",
        "Listing_Sample_Count":      len(prices),
        "_price_for_category":       med_price,
    }


# ── Web search fallback (DuckDuckGo) ──────────────────────────────────────────
def search_builder_url(community_name, builder, city, county):
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        location = city or county or "Florida"
        builder_part = f'{builder} ' if builder else ""
        query = f'{builder_part}"{community_name}" {location} Florida new homes'
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
        b_slug = builder.lower().replace(" ", "") if builder else ""
        c_slug = community_name.lower().replace(" ", "-")
        for r in results:
            href = r.get("href", "")
            if not href:
                continue
            if (b_slug and b_slug in href.lower()) or c_slug in href.lower():
                return href
        return results[0].get("href", "") if results else ""
    except Exception as e:
        print(f"  [warn] web search: {e}")
        return ""


# ── Sheet write helpers ────────────────────────────────────────────────────────
def _fmt_req(tab_id, row_i, col_i):
    return {
        "repeatCell": {
            "range": {
                "sheetId":          tab_id,
                "startRowIndex":    row_i,
                "endRowIndex":      row_i + 1,
                "startColumnIndex": col_i,
                "endColumnIndex":   col_i + 1,
            },
            "cell":   {"userEnteredFormat": {"backgroundColor": NEON_GREEN}},
            "fields": "userEnteredFormat.backgroundColor",
        }
    }


def _flush(sheets, value_updates, fmt_requests):
    if value_updates:
        sheets.values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"valueInputOption": "RAW", "data": value_updates}
        ).execute()
    if fmt_requests:
        sheets.batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": fmt_requests}
        ).execute()


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    fresh = "--fresh" in sys.argv
    checkpoint = {}
    if not fresh and os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE) as f:
            checkpoint = json.load(f)
        print(f"[resume] {len(checkpoint)} rows already done")
    elif fresh and os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)
        print("[fresh] checkpoint cleared")

    service = get_sheets_service()
    sheets  = service.spreadsheets()
    resp    = sheets.values().get(spreadsheetId=SHEET_ID, range=f"{TAB}!A:ZZ").execute()
    rows    = resp.get("values", [])
    header  = list(rows[0])
    print(f"[sheet] {len(rows)-1} data rows, {len(header)} columns")

    # Map every column we need (adds to header list if somehow missing)
    def ci(col):
        if col in header:
            return header.index(col)
        header.append(col)
        return len(header) - 1

    name_col        = ci("Community Name")
    county_col      = ci("County")
    city_col        = ci("City")
    builder_col     = ci("Builder")
    builder_url_col = ci("Builder URL")
    price_src_col   = ci("Price Source URL")
    status_col      = ci("Status")
    price_cat_col   = ci("Price Category")
    med_price_col   = ci("Area_Median_Price")
    act_list_col    = ci("Area_Active_Listings")
    dom_col         = ci("Area_Avg_DOM")
    hotness_col     = ci("Area_Market_Hotness")
    hoa_fee_col     = ci("Listing_HOA_Fee")
    hoa_freq_col    = ci("Listing_HOA_Freq")
    heating_col     = ci("Listing_Heating")
    cooling_col     = ci("Listing_Cooling")
    roof_col        = ci("Listing_Roof")
    sewer_col       = ci("Listing_Sewer")
    exterior_col    = ci("Listing_Exterior_Features")
    sample_col      = ci("Listing_Sample_Count")
    scraped_col     = ci("Last_Scraped_Date")

    # Re-write header row (harmless if unchanged; covers any new columns)
    end_col = col_letter(len(header) - 1)
    sheets.values().update(
        spreadsheetId=SHEET_ID,
        range=f"{TAB}!A1:{end_col}1",
        valueInputOption="RAW",
        body={"values": [header]}
    ).execute()

    meta         = sheets.get(spreadsheetId=SHEET_ID).execute()
    tab_sheet_id = next(
        s["properties"]["sheetId"] for s in meta["sheets"]
        if s["properties"]["title"] == TAB
    )

    value_updates: list = []
    fmt_requests:  list = []
    processed = skipped = errors = 0
    total = len(rows) - 1

    for row_i, row in enumerate(rows[1:], start=1):
        def cell(c):
            return row[c].strip() if len(row) > c else ""

        name        = cell(name_col)
        status      = cell(status_col).lower()
        county      = cell(county_col)
        city        = cell(city_col)
        builder     = cell(builder_col)
        builder_url = cell(builder_url_col)
        price_src   = cell(price_src_col)

        if not name:
            continue

        # Skip inactive rows
        if any(s in status for s in SKIP_STATUSES):
            print(f"[{row_i}/{total}] SKIP ({status or 'no status'}): {name}")
            skipped += 1
            continue

        # Skip already checkpointed
        if name in checkpoint:
            print(f"[{row_i}/{total}] CACHED: {name}")
            continue

        print(f"\n[{row_i}/{total}] {name}", flush=True)

        # ── Step 1: determine source URL ──────────────────────────────────────
        write_price_src = False
        if price_src:
            source_url = price_src
            print(f"  url: {source_url[:100]}")
        elif builder_url:
            source_url = builder_url
            write_price_src = True          # Price Source URL was empty — fill it
            print(f"  url (builder): {source_url[:100]}")
        else:
            print(f"  searching: '{builder} {name} {city}'…", flush=True)
            source_url = search_builder_url(name, builder, city, county)
            if source_url:
                write_price_src = True
                print(f"  found: {source_url[:100]}")
            else:
                print(f"  [error] no URL found — skipping")
                errors += 1
                continue

        source_url = normalize_url(source_url)

        # ── Step 2: scrape ────────────────────────────────────────────────────
        if "paradiserealtyfla.com" in source_url:
            data = scrape_paradise_url(source_url)
        else:
            data = scrape_external_url(source_url)

        if not data:
            print(f"  [error] scrape returned nothing — skipping")
            errors += 1
            continue

        # Persist checkpoint
        checkpoint[name] = {"source_url": source_url, **{
            k: v for k, v in data.items() if not k.startswith("_")
        }}
        with open(CHECKPOINT_FILE, "w") as f:
            json.dump(checkpoint, f, default=str)

        # ── Step 3: price category ────────────────────────────────────────────
        med_price = data.get("_price_for_category") or data.get("Area_Median_Price")
        if isinstance(med_price, str):
            try:
                med_price = int(re.sub(r"[^\d]", "", med_price)) if med_price else None
            except Exception:
                med_price = None

        cat = price_category(med_price)
        if med_price:
            print(f"  listings={data.get('Area_Active_Listings','')}  "
                  f"median=${med_price:,}  [{cat}]")
        else:
            print(f"  listings={data.get('Area_Active_Listings','')}  "
                  f"no price  [{cat or '—'}]")

        # ── Step 4: queue write-back (skip empty values) ──────────────────────
        def queue(col_i, val):
            if val is None or val == "":
                return
            value_updates.append({
                "range":  f"{TAB}!{col_letter(col_i)}{row_i + 1}",
                "values": [[str(val)]]
            })
            fmt_requests.append(_fmt_req(tab_sheet_id, row_i, col_i))

        if write_price_src:
            queue(price_src_col, source_url)
        if cat:
            queue(price_cat_col, cat)

        queue(med_price_col, data.get("Area_Median_Price"))
        queue(act_list_col,  data.get("Area_Active_Listings"))
        queue(dom_col,       data.get("Area_Avg_DOM"))
        queue(hotness_col,   data.get("Area_Market_Hotness"))
        queue(hoa_fee_col,   data.get("Listing_HOA_Fee"))
        queue(hoa_freq_col,  data.get("Listing_HOA_Freq"))
        queue(heating_col,   data.get("Listing_Heating"))
        queue(cooling_col,   data.get("Listing_Cooling"))
        queue(roof_col,      data.get("Listing_Roof"))
        queue(sewer_col,     data.get("Listing_Sewer"))
        queue(exterior_col,  data.get("Listing_Exterior_Features"))
        queue(sample_col,    data.get("Listing_Sample_Count"))
        queue(scraped_col,   TODAY)

        processed += 1

        # Flush to sheet every 10 rows
        if processed % 10 == 0:
            _flush(sheets, value_updates, fmt_requests)
            value_updates.clear()
            fmt_requests.clear()
            print(f"  [sheet] flushed at row {row_i}")

        time.sleep(0.3)

    # Final flush
    if value_updates:
        _flush(sheets, value_updates, fmt_requests)
        print(f"[sheet] final flush — {len(value_updates)} cells written")

    print(f"\n[done]  processed={processed}  skipped={skipped}  errors={errors}")
    print(f"Checkpoint: {CHECKPOINT_FILE}")


if __name__ == "__main__":
    main()
