"""
Daily Bing Webmaster Tools management.

What this module does on each run:
  1. Pull yesterday's traffic / query / page stats
  2. HEAD-check the top-impression URLs to detect 404s and off-site redirects
  3. Submit up to 500 fresh sitemap URLs to Bing (uses 5% of daily quota)
  4. Ping IndexNow with the same URLs (best-effort)
  5. Identify "opportunity queries" — pos 4-15 with ≥2 impressions
  6. Detect missing meta description / canonical / JSON-LD on top pages
  7. Build & email an HTML daily report

Reuses ``bing_client._call`` for authenticated Bing API calls.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

import area_changes
import bing_client
import bing_seo_scan
import gsc_analytics
import gsc_client

logger = logging.getLogger(__name__)

INDEXNOW_ENDPOINT = "https://www.bing.com/indexnow"
USER_AGENT = "ParadiseRealty-BingManager/1.0"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _ms_to_date(s: str):
    m = re.search(r"(\d+)", s)
    if not m:
        return None
    return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).date()


def _agg_query_stats(rows: list[dict]) -> list[dict]:
    """Aggregate raw QueryStats rows into per-query totals with weighted avg position."""
    by_q: dict[str, dict] = defaultdict(
        lambda: {"impr": 0, "clicks": 0, "pos_sum": 0.0, "n": 0}
    )
    for r in rows:
        q = r["Query"]
        by_q[q]["impr"] += r["Impressions"]
        by_q[q]["clicks"] += r["Clicks"]
        if r["AvgImpressionPosition"] > 0:
            by_q[q]["pos_sum"] += r["AvgImpressionPosition"] * r["Impressions"]
            by_q[q]["n"] += r["Impressions"]
    out = []
    for q, d in by_q.items():
        out.append(
            {
                "query": q,
                "impressions": d["impr"],
                "clicks": d["clicks"],
                "avg_position": (d["pos_sum"] / d["n"]) if d["n"] else None,
            }
        )
    return out


# ── Step 1: pull metrics ─────────────────────────────────────────────────────


def pull_traffic(api_key: str, site_url: str) -> dict:
    """Return last-30-day totals and the most recent day."""
    resp = bing_client._call(
        "get",
        "GetRankAndTrafficStats",
        api_key,
        params={"siteUrl": site_url},
    )
    rows = resp.get("d", []) or []
    parsed = [
        {
            "date": _ms_to_date(r["Date"]),
            "impressions": r["Impressions"],
            "clicks": r["Clicks"],
        }
        for r in rows
    ]
    parsed = [p for p in parsed if p["date"]]
    parsed.sort(key=lambda p: p["date"])
    last30 = parsed[-30:]
    return {
        "all_rows": parsed,
        "last_30_impressions": sum(r["impressions"] for r in last30),
        "last_30_clicks": sum(r["clicks"] for r in last30),
        "yesterday": parsed[-1] if parsed else None,
        "day_before": parsed[-2] if len(parsed) >= 2 else None,
    }


def pull_query_stats(api_key: str, site_url: str) -> list[dict]:
    resp = bing_client._call(
        "get",
        "GetQueryStats",
        api_key,
        params={"siteUrl": site_url},
    )
    return _agg_query_stats(resp.get("d", []) or [])


def pull_page_stats(api_key: str, site_url: str) -> list[dict]:
    """Top pages by impressions. The 'Query' field in PageStats is actually the URL."""
    resp = bing_client._call(
        "get",
        "GetPageStats",
        api_key,
        params={"siteUrl": site_url},
    )
    rows = resp.get("d", []) or []
    by_url: dict[str, dict] = defaultdict(
        lambda: {"impressions": 0, "clicks": 0, "pos_sum": 0.0, "n": 0}
    )
    for r in rows:
        u = r["Query"]
        by_url[u]["impressions"] += r["Impressions"]
        by_url[u]["clicks"] += r["Clicks"]
        if r["AvgImpressionPosition"] > 0:
            by_url[u]["pos_sum"] += r["AvgImpressionPosition"] * r["Impressions"]
            by_url[u]["n"] += r["Impressions"]
    out = []
    for url, d in by_url.items():
        out.append(
            {
                "url": url,
                "impressions": d["impressions"],
                "clicks": d["clicks"],
                "avg_position": (d["pos_sum"] / d["n"]) if d["n"] else None,
            }
        )
    out.sort(key=lambda r: -r["impressions"])
    return out


# ── Step 2: HEAD-check top URLs for broken redirects / 404s ─────────────────


def check_top_urls(top_urls: list[str], limit: int = 50) -> list[dict]:
    """
    Detect off-site redirects (like the wrongpage.io issue) and hard 404s.
    Returns one record per URL with issue category.
    """
    issues: list[dict] = []
    for url in top_urls[:limit]:
        try:
            r = requests.get(
                url,
                allow_redirects=False,
                timeout=10,
                headers={"User-Agent": USER_AGENT},
            )
            status = r.status_code
            location = r.headers.get("Location", "")
            if status in (301, 302, 307, 308):
                final = requests.get(
                    url, allow_redirects=True, timeout=15,
                    headers={"User-Agent": USER_AGENT},
                ).url
                final_host = urlparse(final).hostname or ""
                orig_host = urlparse(url).hostname or ""
                if final_host and orig_host and final_host != orig_host:
                    issues.append({
                        "url": url, "issue": "offsite_redirect",
                        "status": status, "destination": final,
                    })
            elif status >= 400:
                issues.append({"url": url, "issue": f"http_{status}", "status": status})
        except requests.RequestException as e:
            issues.append({"url": url, "issue": "fetch_error", "error": str(e)})
    return issues


def check_on_page_seo(urls: list[str], limit: int = 25) -> list[dict]:
    """
    For top-impression pages: detect missing meta description, canonical, JSON-LD.
    Only checks pages that return 200 (skips broken ones — those are caught above).
    """
    findings: list[dict] = []
    for url in urls[:limit]:
        try:
            r = requests.get(
                url, allow_redirects=True, timeout=15,
                headers={"User-Agent": USER_AGENT},
            )
            if r.status_code != 200 or not r.text:
                continue
            html = r.text
            problems = []
            if not re.search(r'<meta\s+name=["\']description["\']', html, re.I):
                problems.append("no_meta_description")
            if not re.search(r'<link\s+rel=["\']canonical["\']', html, re.I):
                problems.append("no_canonical")
            if 'application/ld+json' not in html.lower():
                problems.append("no_json_ld")
            if problems:
                findings.append({"url": url, "problems": problems})
        except requests.RequestException:
            continue
    return findings


# ── Step 3: submit URLs to Bing (10k/day quota) ─────────────────────────────


def submit_urls(api_key: str, site_url: str, urls: list[str]) -> dict:
    """SubmitUrlBatch — Bing accepts max 500 URLs per call. Returns submitted count."""
    if not urls:
        return {"submitted": 0, "errors": []}
    submitted = 0
    errors: list[str] = []
    # 500-URL batches
    for i in range(0, len(urls), 500):
        chunk = urls[i : i + 500]
        try:
            bing_client._call(
                "post",
                "SubmitUrlBatch",
                api_key,
                json_body={"siteUrl": site_url, "urlList": chunk},
            )
            submitted += len(chunk)
            logger.info(f"  SubmitUrlBatch: {len(chunk)} URLs submitted")
        except Exception as e:
            logger.error(f"  SubmitUrlBatch failed for chunk {i}: {e}")
            errors.append(str(e))
    return {"submitted": submitted, "errors": errors}


def get_url_submission_quota(api_key: str, site_url: str) -> dict:
    resp = bing_client._call(
        "get",
        "GetUrlSubmissionQuota",
        api_key,
        params={"siteUrl": site_url},
    )
    d = resp.get("d", {}) or {}
    return {
        "daily": d.get("DailyQuota", 0),
        "monthly": d.get("MonthlyQuota", 0),
    }


# ── Step 4: IndexNow ─────────────────────────────────────────────────────────


def indexnow_ping(host: str, key: str, key_location: str, urls: list[str]) -> dict:
    """
    Ping IndexNow. key_location is a public URL on the same host that returns
    the key as plain text. If the key file isn't hosted yet, IndexNow will
    return 4xx — we log and continue (Bing API submission already happened).
    """
    if not urls:
        return {"submitted": 0, "status": "noop"}
    body = {
        "host": host,
        "key": key,
        "keyLocation": key_location,
        "urlList": urls[:10000],
    }
    try:
        r = requests.post(
            INDEXNOW_ENDPOINT,
            json=body,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            timeout=20,
        )
        return {
            "submitted": len(urls),
            "status": r.status_code,
            "ok": 200 <= r.status_code < 300,
            "body": r.text[:200] if r.text else "",
        }
    except requests.RequestException as e:
        return {"submitted": 0, "status": "error", "error": str(e)}


# ── Step 5: opportunity queries ──────────────────────────────────────────────


def find_opportunity_queries(
    queries: list[dict], min_impr: int = 3, pos_lo: float = 4.0, pos_hi: float = 15.0
) -> list[dict]:
    out = [
        q for q in queries
        if q["avg_position"] is not None
        and pos_lo <= q["avg_position"] <= pos_hi
        and q["impressions"] >= min_impr
    ]
    out.sort(key=lambda q: -q["impressions"])
    return out


# ── Step 6: load fresh URLs to submit ────────────────────────────────────────


def load_fresh_urls_from_gcs(
    gcs_uri: str, project: str, limit: int = 500
) -> list[str]:
    """
    Read the most-recently-updated URLs from the sitemap CSV in GCS.
    The CSV has a 'url' column (from sitemap_builder.collect_urls_from_csv).
    For daily Bing submission we take a rotating window of `limit` URLs
    so all URLs eventually get re-submitted within a quarter.
    """
    import csv
    import tempfile
    from google.cloud import storage

    bucket_part, blob_part = gcs_uri.replace("gs://", "").split("/", 1)
    client = storage.Client(project=project)
    tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
    client.bucket(bucket_part).blob(blob_part).download_to_filename(tmp.name)

    urls: list[str] = []
    with open(tmp.name, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            u = (row.get("url") or "").strip()
            if u.startswith("http"):
                urls.append(u)

    # Daily rotation: skip ahead based on day-of-year so each URL gets a turn
    if not urls:
        return []
    day_of_year = datetime.now(ZoneInfo("America/New_York")).timetuple().tm_yday
    start = (day_of_year * limit) % len(urls)
    end = start + limit
    if end <= len(urls):
        return urls[start:end]
    return urls[start:] + urls[: end - len(urls)]


# ── HTML report ──────────────────────────────────────────────────────────────


def _kpi_block(traffic: dict, quota: dict) -> str:
    y = traffic["yesterday"]
    db = traffic["day_before"]
    y_str = f"{y['impressions']} impr / {y['clicks']} clicks" if y else "—"
    if y and db and db["impressions"] > 0:
        delta = (y["impressions"] - db["impressions"]) / db["impressions"] * 100
        sign = "+" if delta >= 0 else ""
        delta_str = f"{sign}{delta:.0f}% vs day before"
    else:
        delta_str = ""
    return f"""
    <div>
      <span class="kpi">Bing yesterday: <b>{y_str}</b> {delta_str}</span>
      <span class="kpi">Bing 30d: <b>{traffic['last_30_impressions']:,}</b> impr / <b>{traffic['last_30_clicks']:,}</b> clicks</span>
      <span class="kpi">Bing quota: <b>{quota['daily']:,}</b>/day · <b>{quota['monthly']:,}</b>/mo</span>
    </div>
    """


def _gsc_kpi_block(gsc_traffic: dict, area: dict) -> str:
    if not gsc_traffic:
        return "<div><span class='kpi warn'>Google data unavailable this run</span></div>"
    impr = gsc_traffic["impressions"]
    clicks = gsc_traffic["clicks"]
    pos = gsc_traffic["avg_position"]
    pos_str = f"{pos:.1f}" if pos else "—"
    ctr_pct = gsc_traffic["ctr"] * 100
    area_str = (
        f"{area['area_pages_with_impressions']:,} area pages indexed "
        f"({area['area_total_impressions']:,} impr / {area['area_total_clicks']:,} clicks, {area['window_days']}d)"
        if area else "Area pages: —"
    )
    return f"""
    <div>
      <span class="kpi">Google 30d: <b>{impr:,}</b> impr / <b>{clicks:,}</b> clicks / CTR <b>{ctr_pct:.1f}%</b> / avg pos <b>{pos_str}</b></span>
      <span class="kpi"><b>{area_str}</b></span>
    </div>
    """


def build_area_section(area_diff: dict | None) -> str:
    """Render the 'changes to area pages' section from a snapshot diff."""
    if not area_diff:
        return (
            "<h2>🏘 Area-page changes</h2>"
            "<p class='warn'>Area-page tracking unavailable this run "
            "(no tracked-URL list or fetch failed).</p>"
        )

    tracked = area_diff.get("tracked", 0)
    ok = area_diff.get("ok", 0)

    # First run: no prior snapshot to compare against.
    if not area_diff.get("prev_at"):
        return f"""
<h2>🏘 Area-page changes</h2>
<p class="good">Baseline established — now tracking <b>{tracked:,}</b> live area pages
({ok:,} returning 200). Day-over-day change detection starts with the next run.</p>
"""

    changed = area_diff.get("changed", [])
    added = area_diff.get("added", [])
    removed = area_diff.get("removed", [])
    broke = area_diff.get("broke", [])
    since = (area_diff.get("prev_at") or "")[:16].replace("T", " ")

    kpis = (
        f"<span class='kpi'>Tracked: <b>{tracked:,}</b></span>"
        f"<span class='kpi'>Changed: <b>{len(changed)}</b></span>"
        f"<span class='kpi'>New: <b>{len(added)}</b></span>"
        f"<span class='kpi'>Removed: <b>{len(removed)}</b></span>"
        f"<span class='kpi {'bad' if broke else ''}'>Broken: <b>{len(broke)}</b></span>"
    )

    broke_rows = "".join(
        f"<tr><td><code>{urlparse(b['url']).path}</code></td>"
        f"<td class='bad'>{('offsite → ' + b.get('final_url','')) if b.get('offsite') else ('HTTP ' + str(b.get('status'))) }"
        f"{(' · ' + b['error']) if b.get('error') else ''}</td></tr>"
        for b in broke[:25]
    )
    broke_html = (
        f"<h3 class='bad'>⚠ Broke since last report</h3><table>"
        f"<tr><th>Page</th><th>Problem</th></tr>{broke_rows}</table>"
        if broke else ""
    )

    changed_rows = "".join(
        f"<tr><td><code>{urlparse(c['url']).path}</code></td>"
        f"<td>{', '.join(c['fields'])}</td></tr>"
        for c in changed[:40]
    )
    changed_html = (
        f"<h3>Edited pages</h3><table>"
        f"<tr><th>Page</th><th>What changed</th></tr>{changed_rows}</table>"
        if changed else ""
    )

    add_rows = "".join(f"<li><code>{urlparse(u).path}</code></li>" for u in added[:40])
    rem_rows = "".join(f"<li><code>{urlparse(u).path}</code></li>" for u in removed[:40])
    add_html = f"<h3>New pages ({len(added)})</h3><ul>{add_rows}</ul>" if added else ""
    rem_html = (
        f"<h3>Removed / no longer reachable ({len(removed)})</h3><ul>{rem_rows}</ul>"
        if removed else ""
    )

    if not (changed or added or removed or broke):
        body = "<p class='good'>No area-page changes detected since the last report ✓</p>"
    else:
        body = broke_html + changed_html + add_html + rem_html

    note = (
        "<p style='font-size:11px;color:#888;margin-top:6px'>"
        "Title / meta / heading / schema edits are exact. Body-copy deltas can "
        "include live IDX listing inventory that refreshes on its own, not just authored text."
        "</p>"
    )
    return f"""
<h2>🏘 Area-page changes <span style="font-weight:normal;font-size:12px;color:#888">since {since} ET</span></h2>
<div>{kpis}</div>
{body}
{note if (changed or broke) else ''}
"""


def build_daily_report(
    traffic: dict,
    quota: dict,
    submitted: dict,
    indexnow: dict,
    page_issues: list[dict],
    seo_findings: list[dict],
    opportunities: list[dict],
    top_pages: list[dict],
    gsc_traffic: dict | None = None,
    gsc_top_queries: list[dict] | None = None,
    gsc_top_pages: list[dict] | None = None,
    gsc_opportunities: list[dict] | None = None,
    gsc_area: dict | None = None,
    area_diff: dict | None = None,
    seo_recs: list[dict] | None = None,
    crawl_stats: dict | None = None,
    blocked_urls: list[dict] | None = None,
    blocked_value: list[dict] | None = None,
    recrawl: dict | None = None,
) -> str:
    now = datetime.now(ZoneInfo("America/New_York"))
    area_section = build_area_section(area_diff)
    seo_section = bing_seo_scan.build_seo_section(
        seo_recs or [], crawl_stats or {}, blocked_urls or [],
        blocked_value or [], recrawl or {},
    )

    issue_rows = ""
    for i in page_issues[:25]:
        kind = i.get("issue", "?")
        dest = i.get("destination") or i.get("error") or ""
        issue_rows += (
            f"<tr><td><code>{i['url']}</code></td>"
            f"<td class='bad'>{kind}</td>"
            f"<td><code>{dest[:80]}</code></td></tr>"
        )
    if not issue_rows:
        issue_rows = (
            "<tr><td colspan='3' class='good'>"
            "No 404s or off-site redirects detected in the top 50 impression URLs ✓"
            "</td></tr>"
        )

    seo_rows = ""
    for f in seo_findings[:20]:
        probs = ", ".join(f["problems"])
        seo_rows += (
            f"<tr><td><code>{f['url']}</code></td>"
            f"<td class='warn'>{probs}</td></tr>"
        )
    if not seo_rows:
        seo_rows = (
            "<tr><td colspan='2' class='good'>"
            "All top pages have meta description, canonical, and JSON-LD ✓</td></tr>"
        )

    opp_rows = ""
    for q in opportunities[:20]:
        opp_rows += (
            f"<tr><td>{q['query']}</td>"
            f"<td>{q['avg_position']:.1f}</td>"
            f"<td>{q['impressions']}</td>"
            f"<td>{q['clicks']}</td></tr>"
        )
    if not opp_rows:
        opp_rows = "<tr><td colspan='4'>No new opportunities today.</td></tr>"

    top_rows = ""
    for p in top_pages[:10]:
        ctr = (p["clicks"] / p["impressions"] * 100) if p["impressions"] else 0
        pos = f"{p['avg_position']:.1f}" if p["avg_position"] else "—"
        top_rows += (
            f"<tr><td><code>{urlparse(p['url']).path}</code></td>"
            f"<td>{p['impressions']}</td>"
            f"<td>{p['clicks']}</td>"
            f"<td>{ctr:.1f}%</td>"
            f"<td>{pos}</td></tr>"
        )

    # Google sections (only render if data was pulled successfully)
    gsc_query_rows = ""
    for q in (gsc_top_queries or [])[:15]:
        gsc_query_rows += (
            f"<tr><td>{q['query']}</td>"
            f"<td>{q['impressions']:,}</td>"
            f"<td>{q['clicks']:,}</td>"
            f"<td>{q['ctr']*100:.1f}%</td>"
            f"<td>{q['position']:.1f}</td></tr>"
        )
    gsc_page_rows = ""
    for p in (gsc_top_pages or [])[:15]:
        path = urlparse(p["page"]).path or "/"
        gsc_page_rows += (
            f"<tr><td><code>{path}</code></td>"
            f"<td>{p['impressions']:,}</td>"
            f"<td>{p['clicks']:,}</td>"
            f"<td>{p['position']:.1f}</td></tr>"
        )
    gsc_opp_rows = ""
    for q in (gsc_opportunities or [])[:15]:
        gsc_opp_rows += (
            f"<tr><td>{q['query']}</td>"
            f"<td>{q['position']:.1f}</td>"
            f"<td>{q['impressions']:,}</td>"
            f"<td>{q['clicks']:,}</td></tr>"
        )
    gsc_area_rows = ""
    for p in (gsc_area or {}).get("top_area_pages", [])[:15]:
        path = urlparse(p["page"]).path or "/"
        gsc_area_rows += (
            f"<tr><td><code>{path}</code></td>"
            f"<td>{p['impressions']:,}</td>"
            f"<td>{p['clicks']:,}</td>"
            f"<td>{p['position']:.1f}</td></tr>"
        )

    google_section = ""
    if gsc_traffic:
        google_section = f"""
<h2>🅖 Google Search Console — last 30 days</h2>
{_gsc_kpi_block(gsc_traffic, gsc_area)}

<h2>🅖 Top Google queries</h2>
<table>
<tr><th>Query</th><th>Impr</th><th>Clicks</th><th>CTR</th><th>Pos</th></tr>
{gsc_query_rows or '<tr><td colspan=5>no data</td></tr>'}
</table>

<h2>🅖 Top Google pages</h2>
<table>
<tr><th>Page</th><th>Impr</th><th>Clicks</th><th>Pos</th></tr>
{gsc_page_rows or '<tr><td colspan=4>no data</td></tr>'}
</table>

<h2>🅖 Google opportunity queries (pos 4-15)</h2>
<table>
<tr><th>Query</th><th>Avg pos</th><th>Impressions</th><th>Clicks</th></tr>
{gsc_opp_rows or '<tr><td colspan=4>None today.</td></tr>'}
</table>

<h2>🅖 Top area pages in Google (last 90d)</h2>
<table>
<tr><th>Page</th><th>Impr</th><th>Clicks</th><th>Pos</th></tr>
{gsc_area_rows or '<tr><td colspan=4>No area pages have received impressions in the last 90 days.</td></tr>'}
</table>
"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  body{{font-family:-apple-system,Segoe UI,Arial,sans-serif;margin:30px;color:#222;line-height:1.5;max-width:780px}}
  h1{{color:#0a3d91;border-bottom:3px solid #0a3d91;padding-bottom:6px;font-size:22px}}
  h2{{color:#0a3d91;margin-top:28px;font-size:17px;border-bottom:1px solid #e5e8ef;padding-bottom:4px}}
  .kpi{{display:inline-block;background:#f4f7fb;border:1px solid #d6e0ee;padding:8px 14px;margin:4px 6px 4px 0;border-radius:6px;font-size:13px}}
  .kpi b{{color:#0a3d91}}
  .bad{{color:#c0392b;font-weight:bold}}
  .good{{color:#1e7a3a;font-weight:bold}}
  .warn{{color:#a35a00;font-weight:bold}}
  table{{border-collapse:collapse;width:100%;margin:10px 0;font-size:12px}}
  th,td{{border:1px solid #d6dce5;padding:6px 9px;text-align:left;vertical-align:top}}
  th{{background:#0a3d91;color:#fff}}
  tr:nth-child(even){{background:#f7f9fc}}
  code{{font-family:Menlo,monospace;font-size:11px;background:#f1f2f4;padding:1px 4px;border-radius:3px}}
  .footer{{color:#999;font-size:11px;margin-top:32px;border-top:1px solid #eee;padding-top:14px}}
</style></head><body>

<h1>Paradise Daily SEO + Site Digest — paradiserealtyfla.com</h1>
<p>{now.strftime('%A, %B %d, %Y · %I:%M %p ET')} · Google Search Console · Bing · site health · area pages</p>

{_kpi_block(traffic, quota)}

{area_section}

{google_section}

{seo_section}

<h2>Bing job results</h2>
<table>
<tr><th>Step</th><th>Result</th></tr>
<tr><td>URLs submitted to Bing</td><td>{submitted['submitted']:,} {'✓' if not submitted['errors'] else '⚠'}</td></tr>
<tr><td>IndexNow ping</td><td>{indexnow.get('submitted', 0):,} URLs · HTTP {indexnow.get('status', '—')}</td></tr>
<tr><td>Top-50 page health checks</td><td>{len(page_issues)} issue(s) found</td></tr>
<tr><td>On-page SEO scan (top 25)</td><td>{len(seo_findings)} page(s) missing meta/canonical/schema</td></tr>
<tr><td>Opportunity queries (pos 4-15)</td><td>{len(opportunities)}</td></tr>
<tr><td>SEO recommendations</td><td>{len(seo_recs or [])} ({sum(1 for r in (seo_recs or []) if r['priority'] == 'HIGH')} high-priority)</td></tr>
<tr><td>Blocked URLs you rank for</td><td>{len(blocked_value or [])}</td></tr>
<tr><td>Pages resubmitted for recrawl</td><td>{(recrawl or {}).get('submitted', 0):,}</td></tr>
</table>

<h2>🅑 Broken pages costing Bing impressions</h2>
<table>
<tr><th>URL</th><th>Issue</th><th>Detail</th></tr>
{issue_rows}
</table>

<h2>🅑 Pages missing on-page SEO basics</h2>
<table>
<tr><th>URL</th><th>Missing</th></tr>
{seo_rows}
</table>

<h2>🅑 Bing opportunity queries (pos 4-15)</h2>
<table>
<tr><th>Query</th><th>Avg pos</th><th>Impressions</th><th>Clicks</th></tr>
{opp_rows}
</table>

<h2>🅑 Top Bing pages right now</h2>
<table>
<tr><th>Page</th><th>Impr</th><th>Clicks</th><th>CTR</th><th>Pos</th></tr>
{top_rows}
</table>

<p class="footer">
Autonomous job: <code>sitemap-sync/bing_manager.py</code> · Cloud Run Job <code>website-report</code> · paradise-automation/us-east1<br>
Runs 5:00 AM ET, Monday–Friday
</p>

</body></html>"""


def send_report(to_email: str, sg_key: str, subject: str, html: str) -> bool:
    body = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": "sitemap-sync@paradiserealtyfla.com"},
        "subject": subject,
        "content": [{"type": "text/html", "value": html}],
    }
    r = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={"Authorization": f"Bearer {sg_key}", "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    logger.info(f"Email send: HTTP {r.status_code}")
    return 200 <= r.status_code < 300


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        stream=sys.stdout,
    )

    api_key = os.environ["BING_API_KEY"]
    site_url = os.environ["SITE_URL"]
    sg_key = os.environ.get("SENDGRID_API_KEY", "")
    to_email = os.environ.get("REPORT_EMAIL_TO", "joe@josephcapra.com")

    # GSC: defaults match the URL-prefix property in Search Console.
    gsc_site_url = os.environ.get("GSC_SITE_URL", site_url)
    # Area pages = 1- or 2-segment paths that aren't site chrome or the
    # 600k+ auto-generated /listings/ filter pages. Covers /martin-county/,
    # /martin-county/willow-pointe-stuart/, and root-level community slugs
    # like /canopy-creek-palm-city/. Override via env if needed.
    area_pattern = os.environ.get(
        "AREA_PAGE_PATTERN",
        r"^https?://(www\.)?paradiserealtyfla\.com/"
        r"(?!listings/|blog/|buying/|selling/|about/|joseph-capra/|office-listings/|sitemap|search/|css/|js/)"
        r"[a-zA-Z0-9-]+/?([a-zA-Z0-9-]+/?)?$",
    )
    area_window_days = int(os.environ.get("AREA_WINDOW_DAYS", "90"))

    indexnow_key = os.environ.get("INDEXNOW_KEY", api_key)
    host = urlparse(site_url).hostname or "www.paradiserealtyfla.com"
    indexnow_key_location = os.environ.get(
        "INDEXNOW_KEY_LOCATION",
        f"https://{host}/{indexnow_key}.txt",
    )

    gcs_csv = os.environ.get(
        "DAILY_SUBMIT_GCS_CSV",
        "gs://site-map-dynamic-page3/sitemaps/communities_combined.csv",
    )
    gcs_project = os.environ.get("GCS_PROJECT", "paradise-automation")
    submit_limit = int(os.environ.get("DAILY_SUBMIT_LIMIT", "500"))

    area_tracked_gcs = os.environ.get(
        "AREA_TRACKED_URLS_GCS",
        "gs://site-map-dynamic-page3/area-report/area-tracked-urls.txt",
    )
    area_snapshot_gcs = os.environ.get(
        "AREA_SNAPSHOT_GCS",
        "gs://site-map-dynamic-page3/area-report/area-snapshot-latest.json",
    )
    area_workers = int(os.environ.get("AREA_FETCH_WORKERS", "20"))

    logger.info("=" * 60)
    logger.info("Daily website report — paradiserealtyfla.com")
    logger.info(f"  Site: {site_url}  Submit limit: {submit_limit}")
    logger.info("=" * 60)

    # 1. Metrics
    logger.info("[1/6] Pulling traffic/query/page stats")
    traffic = pull_traffic(api_key, site_url)
    queries = pull_query_stats(api_key, site_url)
    pages = pull_page_stats(api_key, site_url)
    quota = get_url_submission_quota(api_key, site_url)
    logger.info(
        f"  Yesterday: {traffic['yesterday']}  "
        f"L30 impr={traffic['last_30_impressions']} clicks={traffic['last_30_clicks']}"
    )

    # 2. Page health
    logger.info("[2/6] HEAD-checking top 50 impression URLs")
    top_urls = [p["url"] for p in pages[:50]]
    page_issues = check_top_urls(top_urls)
    # Drop intentionally-handled URLs (e.g. atlantic-fields legal off-site
    # redirect) so they don't show as "broken" noise every day.
    page_issues = [i for i in page_issues if not bing_seo_scan._is_ignored(i.get("url", ""))]
    for i in page_issues:
        logger.warning(f"  PAGE ISSUE: {i}")

    # 3. On-page SEO
    logger.info("[3/6] On-page SEO scan (top 25)")
    seo_findings = check_on_page_seo([p["url"] for p in pages[:25]])
    for f in seo_findings:
        logger.info(f"  SEO GAP: {f['url']} — {f['problems']}")

    # 4. Submit URLs
    logger.info("[4/6] Submitting URLs to Bing")
    try:
        fresh = load_fresh_urls_from_gcs(gcs_csv, gcs_project, limit=submit_limit)
        logger.info(f"  Loaded {len(fresh)} URLs from {gcs_csv}")
    except Exception as e:
        logger.error(f"  Could not load fresh URLs: {e}")
        fresh = []
    submitted = submit_urls(api_key, site_url, fresh)

    # 5. IndexNow
    logger.info("[5/6] IndexNow ping")
    indexnow = indexnow_ping(host, indexnow_key, indexnow_key_location, fresh)
    logger.info(f"  IndexNow: {indexnow}")

    # 6. Google Search Console pulls (best-effort — never block the Bing job)
    logger.info("[6/7] Pulling Google Search Console analytics")
    gsc_traffic = gsc_top_queries = gsc_top_pages = gsc_opps = gsc_area = None
    try:
        gsc = gsc_client._build_service()
        gsc_traffic = gsc_analytics.pull_traffic(gsc, gsc_site_url, days=30)
        gsc_top_queries = gsc_analytics.pull_top_queries(gsc, gsc_site_url, days=30, limit=20)
        gsc_top_pages = gsc_analytics.pull_top_pages(gsc, gsc_site_url, days=30, limit=20)
        gsc_opps = gsc_analytics.find_opportunity_queries(gsc, gsc_site_url, days=30)
        gsc_area = gsc_analytics.count_area_pages(
            gsc, gsc_site_url, area_pattern, days=area_window_days,
        )
        logger.info(
            f"  GSC 30d: {gsc_traffic['impressions']:,} impr / {gsc_traffic['clicks']:,} clicks · "
            f"area pages indexed: {gsc_area['area_pages_with_impressions']}/{gsc_area['total_pages_with_impressions']}"
        )
    except Exception as e:
        logger.error(f"  GSC pull failed (continuing without it): {e}")

    # 7. Area-page change detection (best-effort — never block the report)
    logger.info("[7/8] Detecting area-page changes vs. yesterday's snapshot")
    area_diff = None
    try:
        tracked = area_changes.load_tracked_urls(area_tracked_gcs, gcs_project)
        if tracked:
            prev = area_changes.load_snapshot(area_snapshot_gcs, gcs_project)
            cur = area_changes.build_snapshot(tracked, workers=area_workers, prev=prev)
            area_diff = area_changes.diff_snapshots(prev, cur)
            area_changes.save_snapshot(cur, area_snapshot_gcs, gcs_project)
            logger.info(
                f"  area: {area_diff['tracked']} tracked · {len(area_diff['changed'])} changed · "
                f"{len(area_diff['added'])} new · {len(area_diff['removed'])} removed · "
                f"{len(area_diff['broke'])} broke"
            )
        else:
            logger.warning(f"  no tracked URLs at {area_tracked_gcs} — skipping area changes")
    except Exception as e:
        logger.error(f"  area-change detection failed (continuing without it): {e}")

    # 8. Opportunities + report
    logger.info("[8/8] Opportunities + SEO recommendations + email report")
    opportunities = find_opportunity_queries(queries)

    # SEO recommendation scan: synthesize Bing's retired "SEO report" from the
    # endpoints that still work (crawl health, blocked URLs) + our opportunity
    # and on-page findings. Safe auto-action: resubmit top live pages to recrawl.
    crawl_stats = blocked_urls = blocked_value = seo_recs = recrawl = None
    try:
        crawl_stats = bing_seo_scan.pull_crawl_stats(api_key, site_url)
        blocked_urls = bing_seo_scan.pull_blocked_urls(api_key, site_url)
        blocked_value = bing_seo_scan.cross_ref_blocked_opportunities(
            blocked_urls, opportunities
        )
        # Off-site redirect leaks among ranking pages (the wrongpage.io class):
        # check the blocked-but-ranking URLs + the top live pages.
        leak_candidates = [b["url"] for b in (blocked_value or [])]
        leak_candidates += [p["url"] for p in pages[:40] if p.get("url")]
        offsite_leaks = bing_seo_scan.detect_offsite_redirects(leak_candidates)
        seo_recs = bing_seo_scan.build_recommendations(
            crawl=crawl_stats or {},
            blocked=blocked_urls or [],
            blocked_value=blocked_value or [],
            opportunities=opportunities,
            seo_findings=seo_findings,
            page_issues=page_issues,
            offsite_leaks=offsite_leaks,
        )
        recrawl = bing_seo_scan.recrawl_high_value_pages(
            api_key, site_url, pages, page_issues
        )
        logger.info(
            f"  SEO scan: {len(seo_recs)} recommendation(s), "
            f"{len(blocked_value)} blocked-but-ranking, "
            f"{len(offsite_leaks)} off-site redirect leak(s), "
            f"recrawl={recrawl.get('submitted', 0) if recrawl else 0}"
        )
        for r in (seo_recs or []):
            logger.info(f"    [{r['priority']}] {r['title']}")
    except Exception as e:  # noqa: BLE001 - never block the report
        logger.error(f"  SEO scan failed (continuing without it): {e}")

    html = build_daily_report(
        traffic=traffic,
        quota=quota,
        submitted=submitted,
        indexnow=indexnow,
        page_issues=page_issues,
        seo_findings=seo_findings,
        opportunities=opportunities,
        top_pages=pages,
        gsc_traffic=gsc_traffic,
        gsc_top_queries=gsc_top_queries,
        gsc_top_pages=gsc_top_pages,
        gsc_opportunities=gsc_opps,
        gsc_area=gsc_area,
        area_diff=area_diff,
        seo_recs=seo_recs,
        crawl_stats=crawl_stats,
        blocked_urls=blocked_urls,
        blocked_value=blocked_value,
        recrawl=recrawl,
    )
    try:
        import agentmgr_reports
        agentmgr_reports.save_report(
            "bing-daily",
            f"Website Report — {datetime.now(ZoneInfo('America/New_York')).strftime('%b %d, %Y')}",
            html, source="bing-daily")
    except Exception:  # noqa: BLE001 - report capture is best-effort
        pass
    if sg_key and to_email:
        google_bit = (
            f" · G {gsc_traffic['impressions']:,}/30d impr"
            if gsc_traffic else ""
        )
        if area_diff and area_diff.get("prev_at"):
            area_bit = (
                f" · {len(area_diff['changed'])} area edits"
                + (f" · {len(area_diff['broke'])} broke" if area_diff.get("broke") else "")
            )
        else:
            area_bit = ""
        now_et = datetime.now(ZoneInfo("America/New_York"))
        ok = send_report(
            to_email=to_email,
            sg_key=sg_key,
            subject=(
                f"Paradise SEO + Site Digest {now_et.strftime('%b %d')} — "
                f"B {traffic['last_30_impressions']:,}/30d impr · "
                f"{len(page_issues)} broken · {len(seo_recs or [])} SEO recs"
                f"{google_bit}{area_bit}"
            ),
            html=html,
        )
        if not ok:
            return 1
    else:
        logger.warning("No SENDGRID_API_KEY or REPORT_EMAIL_TO — skipping email")

    return 0


def _run_as_agentmgr_worker(payload, task):
    """Agent-Manager worker entry: run the daily Bing automation."""
    args = payload.get("args")
    if args is not None:
        sys.argv = [sys.argv[0], *args]
    rc = main()
    if rc not in (0, None):
        raise RuntimeError(f"bing-daily exited with code {rc}")
    return {"result": "bing-daily completed", "exit_code": int(rc or 0)}


if __name__ == "__main__":
    import agentmgr_worker
    if agentmgr_worker.task_id():
        sys.exit(agentmgr_worker.run_as_worker("bing-daily", _run_as_agentmgr_worker))
    sys.exit(main())
