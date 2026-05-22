#!/usr/bin/env python3
"""
Weekly newhome source updater for paradise-realty-communities sheet.

Runs every Monday via Cloud Scheduler → Cloud Run Job.

What it does:
  1. Queries showingnew.com/josephcapra API for all FL new-home communities
  2. Compares against 'communities' tab (skip anything already there)
  3. Diffs against 'newhome source' tab:
       - New entries  → fetch description, append
       - Still active → update Last Verified date
       - No longer in feed → mark Status = "Sold Out"
  4. Sends email report via SendGrid

Secrets (Google Secret Manager, project=paradise-automation):
  sheets-oauth-token   → OAuth token JSON for Google Sheets
  sendgrid-api-key     → SendGrid API key for email
"""

import os, sys, json, re, time, logging
from datetime import date
from bs4 import BeautifulSoup
import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from google.cloud import secretmanager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
SHEET_ID    = "16vVPO8XLEq_KbdLoVOP3jtTyoxqr-Ohjzg_0lMbBFxo"
SOURCE_TAB  = "newhome source"
COMM_TAB    = "communities"
GCP_PROJECT = "paradise-automation"
TODAY       = date.today().isoformat()
REPORT_TO   = "joe@paradiserealtyfla.com"
REPORT_FROM = "reports@paradiserealtyfla.com"

BASE_URL    = "https://www.showingnew.com"
AGENT_SLUG  = "josephcapra"
UA          = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# All FL markets on showingnew.com/josephcapra (area slug → MarketId)
FL_MARKETS = {
    "orlando":       74,
    "gainesville":   67,
    "panama-city":   75,
    "punta-gorda":   77,
    "naples":        72,
    "pensacola":     76,
    "fort-myers":    64,
    "tallahassee":   79,
    "daytona-beach": 62,
    "melbourne":     70,
    "ocala":         73,
}


# ── Secret Manager ────────────────────────────────────────────────────────────
def get_secret(name: str) -> str:
    sm = secretmanager.SecretManagerServiceClient()
    path = f"projects/{GCP_PROJECT}/secrets/{name}/versions/latest"
    return sm.access_secret_version(request={"name": path}).payload.data.decode()


def update_secret(name: str, payload: str):
    sm = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{GCP_PROJECT}/secrets/{name}"
    sm.add_secret_version(
        request={"parent": parent, "payload": {"data": payload.encode()}}
    )


# ── Google Sheets ─────────────────────────────────────────────────────────────
def get_sheets_service():
    raw = get_secret("sheets-oauth-token")
    data = json.loads(raw)
    creds = Credentials(
        token=data["token"],
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes"),
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        data["token"] = creds.token
        update_secret("sheets-oauth-token", json.dumps(data))
    return build("sheets", "v4", credentials=creds)


def sheet_values(svc, tab, range_="A1:Z"):
    result = svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{tab}'!{range_}"
    ).execute()
    return result.get("values", [])


# ── showingnew.com API ────────────────────────────────────────────────────────
def fetch_all_fl_communities() -> dict:
    """Query all FL markets via the communities API. Returns dict of id → community."""
    session = requests.Session()
    # Seed session cookies
    session.get(f"{BASE_URL}/{AGENT_SLUG}/communities/florida/orlando",
                headers={"User-Agent": UA}, timeout=15)

    all_communities = {}
    for area, market_id in FL_MARKETS.items():
        try:
            resp = session.post(
                f"{BASE_URL}/{AGENT_SLUG}/communityresults/getresultscommunities",
                json={"page": 1, "pageSize": 500, "MarketId": market_id},
                headers={
                    "User-Agent": UA,
                    "Content-Type": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{BASE_URL}/{AGENT_SLUG}/communities/florida/{area}",
                },
                timeout=20,
            )
            data = resp.json().get("Data", [])
            new = 0
            for c in data:
                cid = c.get("Id")
                if cid and cid not in all_communities:
                    all_communities[cid] = c
                    new += 1
            log.info(f"{area}: {len(data)} communities ({new} unique new)")
        except Exception as e:
            log.warning(f"{area}: fetch failed — {e}")
        time.sleep(0.5)

    log.info(f"Total unique FL communities from API: {len(all_communities)}")
    return all_communities


# ── Description scraper ───────────────────────────────────────────────────────
def fetch_description(url: str) -> str:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        for attrs in [
            {"class": re.compile(r"description", re.I)},
            {"class": re.compile(r"remark", re.I)},
            {"class": re.compile(r"overview", re.I)},
            {"class": re.compile(r"about", re.I)},
        ]:
            el = soup.find(attrs=attrs)
            if el:
                text = el.get_text(separator=" ", strip=True)
                if len(text) >= 80:
                    return text
        best = max(
            (p.get_text(separator=" ", strip=True) for p in soup.find_all("p")),
            key=len, default=""
        )
        return best if len(best) >= 80 else ""
    except Exception:
        return ""


# ── Price helpers ─────────────────────────────────────────────────────────────
def price_category(lo, hi) -> str:
    low = lo or hi
    if not low:
        return ""
    if low < 300_000:  return "Under $300K"
    if low < 500_000:  return "$300K–$500K"
    if low < 750_000:  return "$500K–$750K"
    return "$750K+"


def community_to_row(c: dict, desc: str) -> list:
    detail_url = BASE_URL + c.get("DetailUrl", "")
    return [
        c.get("Name", ""),
        f"{c.get('County', '')} County, FL",
        c.get("City", ""),
        c.get("GetBuilder", ""),
        "",           # builder_url
        "",           # paradise_url
        "",           # meta_desc
        desc,         # description (col H)
        price_category(c.get("PrLo") or 0, c.get("PrHi") or 0),
        detail_url,   # price_src
        TODAY,        # last_verified
        "Active",
        "",           # notes
        detail_url,   # url_src
    ]


# ── Email via SendGrid ────────────────────────────────────────────────────────
def send_report(added: list, sold_out: list, still_active: int, total: int):
    try:
        api_key = get_secret("sendgrid-api-key")
    except Exception:
        log.warning("No sendgrid-api-key secret found — skipping email")
        return

    added_lines = "\n".join(
        f"  • {c['name']} ({c['county']}, {c['city']})" for c in added[:50]
    )
    if len(added) > 50:
        added_lines += f"\n  … and {len(added) - 50} more"

    sold_lines = "\n".join(f"  • {name}" for name in sold_out[:30])
    if len(sold_out) > 30:
        sold_lines += f"\n  … and {len(sold_out) - 30} more"

    body_text = f"""Paradise Realty — New Home Source Weekly Update
{TODAY}

SUMMARY
───────────────────────────────
  New communities added : {len(added)}
  Marked sold out       : {len(sold_out)}
  Still active          : {still_active}
  Total in tab          : {total}

{"NEW COMMUNITIES" if added else ""}
{"─"*40 if added else ""}
{added_lines if added else "  (none)"}

{"SOLD OUT / REMOVED FROM FEED" if sold_out else ""}
{"─"*40 if sold_out else ""}
{sold_lines if sold_out else "  (none)"}

View sheet: https://docs.google.com/spreadsheets/d/{SHEET_ID}
"""

    body_html = f"""<html><body style="font-family:sans-serif;max-width:600px">
<h2 style="color:#1a5276">Paradise Realty — New Home Source Update</h2>
<p style="color:#666">{TODAY}</p>
<table style="border-collapse:collapse;width:100%">
  <tr><td style="padding:8px;background:#eaf4fb"><strong>New communities added</strong></td>
      <td style="padding:8px;background:#eaf4fb;text-align:right;font-size:1.3em;color:#1a5276"><strong>{len(added)}</strong></td></tr>
  <tr><td style="padding:8px">Marked sold out</td>
      <td style="padding:8px;text-align:right">{len(sold_out)}</td></tr>
  <tr><td style="padding:8px;background:#f9f9f9">Still active</td>
      <td style="padding:8px;background:#f9f9f9;text-align:right">{still_active}</td></tr>
  <tr><td style="padding:8px"><strong>Total in tab</strong></td>
      <td style="padding:8px;text-align:right"><strong>{total}</strong></td></tr>
</table>
{"<h3>New Communities</h3><ul>" + "".join(f"<li><strong>{c['name']}</strong> — {c['county']}, {c['city']}</li>" for c in added[:50]) + ("" if len(added)<=50 else f"<li>…and {len(added)-50} more</li>") + "</ul>" if added else ""}
{"<h3>Sold Out / Removed From Feed</h3><ul>" + "".join(f"<li>{n}</li>" for n in sold_out[:30]) + ("" if len(sold_out)<=30 else f"<li>…and {len(sold_out)-30} more</li>") + "</ul>" if sold_out else ""}
<p><a href="https://docs.google.com/spreadsheets/d/{SHEET_ID}">View spreadsheet →</a></p>
</body></html>"""

    subject = f"New Home Source Update — {len(added)} added, {len(sold_out)} sold out ({TODAY})"
    try:
        import agentmgr_reports
        agentmgr_reports.save_report("newhome-source-updater", subject, body_html,
                                     source="newhome-source-updater")
    except Exception:  # noqa: BLE001 - report capture is best-effort
        pass

    payload = {
        "personalizations": [{"to": [{"email": REPORT_TO}]}],
        "from": {"email": REPORT_FROM, "name": "Paradise Realty Reports"},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": body_text},
            {"type": "text/html",  "value": body_html},
        ],
    }
    r = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        json=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=15,
    )
    if r.status_code in (200, 202):
        log.info(f"Email sent to {REPORT_TO}")
    else:
        log.warning(f"SendGrid returned {r.status_code}: {r.text[:200]}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=== newhome-source-updater starting ===")

    svc = get_sheets_service()

    # ── Load existing sheet data ───────────────────────────────────────────────
    log.info("Loading communities tab...")
    comm_rows = sheet_values(svc, COMM_TAB, "A2:B")
    existing_communities: set[tuple] = set()
    for row in comm_rows:
        name   = row[0].strip().lower() if len(row) > 0 else ""
        county = row[1].strip().lower() if len(row) > 1 else ""
        existing_communities.add((name, county))

    log.info("Loading newhome source tab...")
    source_rows = sheet_values(svc, SOURCE_TAB, "A2:N")
    # Index by (name, county) → row index (0-based within data, so sheet row = idx+2)
    source_index: dict[tuple, int] = {}
    for idx, row in enumerate(source_rows):
        name = row[0].strip().lower() if len(row) > 0 else ""
        county_raw = row[1].strip().lower() if len(row) > 1 else ""
        county = county_raw.replace(" county, fl", "").strip()
        source_index[(name, county)] = idx

    log.info(f"  {len(existing_communities)} in communities tab, "
             f"{len(source_index)} in newhome source tab")

    # ── Fetch all FL communities from API ──────────────────────────────────────
    api_communities = fetch_all_fl_communities()

    # Build a set of (name, county) from API for fast lookup
    api_keys: dict[tuple, dict] = {}
    for c in api_communities.values():
        key = (c.get("Name", "").strip().lower(), c.get("County", "").strip().lower())
        api_keys[key] = c

    # ── Diff ───────────────────────────────────────────────────────────────────
    to_add:      list[dict] = []  # in API, not in either tab
    to_sold_out: list[int]  = []  # sheet row indices where community left the feed
    to_refresh:  list[int]  = []  # sheet row indices to update Last Verified

    for key, c in api_keys.items():
        if key in existing_communities:
            continue  # already in communities tab — skip
        if key in source_index:
            to_refresh.append(source_index[key])  # still active, just update date
        else:
            to_add.append(c)

    for key, idx in source_index.items():
        row = source_rows[idx]
        status = row[11].strip().lower() if len(row) > 11 else ""
        if status in ("sold out", "closed", "inactive", "removed"):
            continue  # already marked — don't flip it back
        if key not in api_keys:
            to_sold_out.append(idx)

    log.info(f"  To add: {len(to_add)}, To mark sold out: {len(to_sold_out)}, "
             f"To refresh date: {len(to_refresh)}")

    # ── Fetch descriptions for new communities ─────────────────────────────────
    new_rows = []
    added_for_report = []
    for i, c in enumerate(to_add):
        detail_url = BASE_URL + c.get("DetailUrl", "")
        desc = fetch_description(detail_url)
        new_rows.append(community_to_row(c, desc))
        added_for_report.append({
            "name":   c.get("Name", ""),
            "county": c.get("County", ""),
            "city":   c.get("City", ""),
        })
        if (i + 1) % 10 == 0:
            log.info(f"  Fetched descriptions {i+1}/{len(to_add)}")
        time.sleep(0.6)

    # ── Write to sheet ─────────────────────────────────────────────────────────
    requests_batch = []
    source_gid = _get_sheet_gid(svc, SOURCE_TAB)  # fetch once, reuse below

    # 1. Append new rows
    if new_rows:
        svc.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=f"'{SOURCE_TAB}'!A2",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": new_rows},
        ).execute()
        log.info(f"Appended {len(new_rows)} new rows")

    # 2. Mark sold-out rows (col L = index 11 = "Status", col K = index 10 = "Last Verified")
    sold_out_names = []
    for idx in to_sold_out:
        sheet_row = idx + 2  # 1-based, skip header
        row_data = source_rows[idx]
        sold_out_names.append(row[0] if (row := row_data) else "?")
        requests_batch.append({
            "updateCells": {
                "range": {
                    "sheetId": source_gid,
                    "startRowIndex": sheet_row - 1,
                    "endRowIndex": sheet_row,
                    "startColumnIndex": 10,  # col K
                    "endColumnIndex": 12,    # through col L
                },
                "rows": [{"values": [
                    {"userEnteredValue": {"stringValue": TODAY}},
                    {"userEnteredValue": {"stringValue": "Sold Out"}},
                ]}],
                "fields": "userEnteredValue",
            }
        })

    # 3. Refresh Last Verified on still-active rows
    for idx in to_refresh:
        sheet_row = idx + 2
        requests_batch.append({
            "updateCells": {
                "range": {
                    "sheetId": source_gid,
                    "startRowIndex": sheet_row - 1,
                    "endRowIndex": sheet_row,
                    "startColumnIndex": 10,  # col K
                    "endColumnIndex": 11,
                },
                "rows": [{"values": [
                    {"userEnteredValue": {"stringValue": TODAY}},
                ]}],
                "fields": "userEnteredValue",
            }
        })

    if requests_batch:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": requests_batch},
        ).execute()
        log.info(f"Batch updated {len(requests_batch)} rows (sold-out + refresh)")

    total_in_tab = len(source_index) + len(new_rows)
    still_active = len(to_refresh) + len([
        k for k in source_index
        if source_rows[source_index[k]][11].strip().lower() == "active"
        if k in api_keys
    ])

    log.info(f"Done. Added={len(new_rows)}, SoldOut={len(to_sold_out)}, "
             f"Refreshed={len(to_refresh)}, Total={total_in_tab}")

    # ── Send email report ──────────────────────────────────────────────────────
    send_report(
        added=added_for_report,
        sold_out=sold_out_names,
        still_active=len(to_refresh),
        total=total_in_tab,
    )

    log.info("=== done ===")


def _get_sheet_gid(svc, tab_name: str) -> int:
    """Return the numeric sheetId for a tab by name."""
    meta = svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == tab_name:
            return s["properties"]["sheetId"]
    raise ValueError(f"Tab not found: {tab_name}")


def _run_as_agentmgr_worker(payload, task):
    """Agent-Manager worker entry: run the source update, return a summary."""
    args = payload.get("args")
    if args is not None:
        sys.argv = [sys.argv[0], *args]
    rc = main()
    if rc not in (0, None):
        raise RuntimeError(f"newhome-source-updater exited with code {rc}")
    return {"result": "newhome-source-updater completed", "exit_code": int(rc or 0)}


if __name__ == "__main__":
    import agentmgr_worker
    if agentmgr_worker.task_id():
        sys.exit(agentmgr_worker.run_as_worker(
            "newhome-source-updater", _run_as_agentmgr_worker))
    main()
