"""
Bing Webmaster SEO recommendation scan.

Microsoft RETIRED the legacy SEO-report endpoints — `GetSeoReports` and
`GetSeoReportFullDetails` both return HTTP 404 (verified 2026-05-22), and the
modern "Site Scan / Recommendations" panel is not exposed on the public
``api.svc`` surface either (GetSiteScan / GetRecommendations / GetSeoIssues all
404). So this module *synthesizes* the equivalent — "recommendations and ways
to improve SEO traffic" — from the Bing Webmaster endpoints that still work,
plus the on-page + opportunity data the daily job already gathers:

  - GetCrawlStats   -> crawl-health snapshot (4xx / 5xx / 301 / blocked / other)
  - GetBlockedUrls  -> URLs the owner has blocked from Bing indexing
  - GetQueryStats   -> opportunity queries pos 4-15  (passed in; already pulled)
  - GetPageStats    -> top pages by impressions      (passed in; already pulled)
  - on-page findings (missing meta/canonical/schema)  (passed in)

It returns a prioritized list of recommendation dicts and renders an HTML
section. The single highest-value signal it computes is the cross-reference of
*blocked* URLs against *ranking* queries — i.e. pages you're telling Bing not to
index even though you already rank page-1 for their topic.
"""
from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlparse

import requests

import bing_client

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; ParadiseSEOBot/1.0; +https://www.paradiserealtyfla.com)"


def _ignore_patterns() -> list[str]:
    """
    URL-path substrings for redirects/blocks that are INTENTIONAL and must NOT be
    flagged as SEO problems. The `atlantic-fields-*` pages deliberately redirect
    off-site (to wrongpage.io) and stay blocked in Bing for LEGAL reasons — never
    "fix" or unblock them. Override/extend via env SEO_SCAN_IGNORE_PATTERNS
    (comma-separated path substrings).
    """
    raw = os.environ.get("SEO_SCAN_IGNORE_PATTERNS", "atlantic-fields")
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def _is_ignored(url: str) -> bool:
    path = (urlparse(url).path or "").lower()
    return any(p in path for p in _ignore_patterns())

# Stopwords stripped when tokenising a slug or query for topic matching.
_STOP = {
    "the", "at", "of", "in", "and", "fl", "florida", "for", "new", "homes",
    "home", "real", "estate", "county", "community", "communities",
}


def _ms_from_date(s: str) -> int:
    """Parse Bing's '/Date(1773315683461-0700)/' into epoch ms (0 on failure)."""
    m = re.search(r"/Date\((\d+)", s or "")
    return int(m.group(1)) if m else 0


def _tokens(text: str) -> set[str]:
    words = re.split(r"[^a-z0-9]+", (text or "").lower())
    return {w for w in words if w and w not in _STOP and len(w) > 2}


# ── Bing pulls ───────────────────────────────────────────────────────────────


def pull_crawl_stats(api_key: str, site_url: str) -> dict:
    """Most-recent GetCrawlStats row, normalised to the fields we report on."""
    resp = bing_client._call("get", "GetCrawlStats", api_key, params={"siteUrl": site_url})
    rows = resp.get("d", []) or []
    if not rows:
        return {}
    rows.sort(key=lambda r: _ms_from_date(r.get("Date", "")))
    r = rows[-1]
    return {
        "as_of_ms": _ms_from_date(r.get("Date", "")),
        "code_2xx": r.get("Code2xx", 0),
        "code_301": r.get("Code301", 0),
        "code_302": r.get("Code302", 0),
        "code_4xx": r.get("Code4xx", 0),
        "code_5xx": r.get("Code5xx", 0),
        "crawl_errors": r.get("CrawlErrors", 0),
        "blocked_by_robots": r.get("BlockedByRobotsTxt", 0),
        "all_other_codes": r.get("AllOtherCodes", 0),
        "in_index": r.get("InIndex", 0),
        "in_links": r.get("InLinks", 0),
        "crawled_pages": r.get("CrawledPages", 0),
    }


def pull_blocked_urls(api_key: str, site_url: str) -> list[dict]:
    """URLs the owner has blocked from Bing indexing (BWT 'Block URLs' tool)."""
    resp = bing_client._call("get", "GetBlockedUrls", api_key, params={"siteUrl": site_url})
    out = []
    for b in resp.get("d", []) or []:
        out.append({
            "url": b.get("Url", ""),
            "days_to_expire": b.get("DaysToExpire"),
            "entity_type": b.get("EntityType"),
        })
    return out


# ── The headline signal: blocked URLs that you actually rank for ──────────────


def cross_ref_blocked_opportunities(
    blocked: list[dict], opportunities: list[dict], min_overlap: int = 2
) -> list[dict]:
    """
    Find blocked URLs whose topic matches a query you already rank for (pos 4-15).
    These are self-inflicted: you're page-1 for the term but blocking the page.
    """
    findings = []
    for b in blocked:
        if _is_ignored(b["url"]):
            continue  # intentionally blocked (e.g. atlantic-fields, legal) — skip
        path = urlparse(b["url"]).path
        slug_tokens = _tokens(path)
        if not slug_tokens:
            continue
        matched = []
        for q in opportunities:
            qt = _tokens(q["query"])
            if len(qt & slug_tokens) >= min_overlap:
                matched.append(q)
        if matched:
            matched.sort(key=lambda q: -q["impressions"])
            findings.append({
                "url": b["url"],
                "days_to_expire": b.get("days_to_expire"),
                "queries": matched[:5],
                "top_impr": matched[0]["impressions"],
            })
    findings.sort(key=lambda f: -f["top_impr"])
    return findings


# ── Off-site redirect leak detector ───────────────────────────────────────────


def _root_host(h: str) -> str:
    return h[4:] if h.startswith("www.") else h


def detect_offsite_redirects(urls: list[str], limit: int = 40, max_hops: int = 5) -> list[dict]:
    """
    Manually walk each URL's redirect chain (Location headers, no auto-follow) and
    flag the FIRST hop that lands on a different domain — e.g. the `wrongpage.io`
    leak. A ranking page that redirects off-domain hands its Bing/Google
    impressions to someone else (the worst kind of SEO leak). We read the Location
    header rather than fetching the destination, so detection still works even
    when the off-site target is unreachable or loops.
    """
    from urllib.parse import urljoin

    out: list[dict] = []
    seen: set[str] = set()
    for url in list(urls)[:limit]:
        if not url or url in seen:
            continue
        seen.add(url)
        if _is_ignored(url):
            continue  # intentional off-site redirect (e.g. atlantic-fields, legal)
        orig_root = _root_host(urlparse(url).hostname or "")
        cur = url
        try:
            for _ in range(max_hops):
                r = requests.get(
                    cur, allow_redirects=False, timeout=12,
                    headers={"User-Agent": USER_AGENT},
                )
                if r.status_code not in (301, 302, 303, 307, 308):
                    break
                loc = r.headers.get("Location", "")
                if not loc:
                    break
                dest = urljoin(cur, loc)
                dest_root = _root_host(urlparse(dest).hostname or "")
                if dest_root and orig_root and dest_root != orig_root:
                    out.append({"url": url, "destination": dest})
                    break
                cur = dest
        except requests.RequestException:
            continue
    return out


# ── Recommendation synthesis ──────────────────────────────────────────────────


def build_recommendations(
    *,
    crawl: dict,
    blocked: list[dict],
    blocked_value: list[dict],
    opportunities: list[dict],
    seo_findings: list[dict],
    page_issues: list[dict],
    offsite_leaks: list[dict] | None = None,
) -> list[dict]:
    """Prioritised recommendation list. Each item:
    {priority, area, title, detail, action, owner}  owner ∈ {you, agent, auto}.
    """
    recs: list[dict] = []
    offsite_leaks = offsite_leaks or []
    leak_urls = {l["url"] for l in offsite_leaks}

    # 0) Off-site redirect leak — the worst: a ranking page handing its
    #    impressions to another domain (the wrongpage.io class of bug).
    if offsite_leaks:
        n = len(offsite_leaks)
        ex = offsite_leaks[0]
        recs.append({
            "priority": "HIGH", "area": "Redirect leak", "owner": "agent",
            "title": f"{n} ranking page(s) redirect OFF-SITE — traffic is leaking",
            "detail": (
                f"e.g. <code>{urlparse(ex['url']).path}</code> → "
                f"<code>{_esc(ex['destination'])}</code>. These rank in Bing but "
                f"send the visitor to another domain — the impressions are wasted."
            ),
            "action": (
                "Delete/repair the bad redirect in RealGeeks (Redirects model) so "
                "the URL serves its own content again, then unblock + resubmit."
            ),
        })

    # 1) Blocked-but-ranking (excluding ones that are really off-site leaks,
    #    which the rec above already covers) — high-value, low-effort.
    unblock = [b for b in blocked_value if b["url"] not in leak_urls]
    if unblock:
        n = len(unblock)
        top = unblock[0]
        ex_q = top["queries"][0]["query"] if top["queries"] else "?"
        recs.append({
            "priority": "HIGH", "area": "Indexing", "owner": "you",
            "title": f"Un-block {n} page(s) you rank for but block from Bing",
            "detail": (
                f"e.g. <code>{urlparse(top['url']).path}</code> is blocked, yet "
                f'you rank for "{ex_q}" ({top["top_impr"]} impr). Blocking the '
                f"landing page caps those impressions from ever converting."
            ),
            "action": (
                "Bing Webmaster Tools → Configure My Site → Block URLs → remove "
                "the block; confirm the page is not noindex; then resubmit it."
            ),
        })

    # 2) Server errors — never acceptable.
    if crawl.get("code_5xx", 0) > 0:
        recs.append({
            "priority": "HIGH", "area": "Crawl health", "owner": "you",
            "title": f"{crawl['code_5xx']:,} page(s) returned 5xx to Bing's crawler",
            "detail": "Server errors stop indexing and erode crawl trust.",
            "action": "Identify the failing URLs (server logs / BWT crawl info) and fix the 5xx.",
        })

    # 3) 4xx broken pages.
    if crawl.get("code_4xx", 0) > 0:
        live_404 = [i for i in page_issues if str(i.get("issue", "")).startswith("http_4")]
        extra = ""
        if live_404:
            extra = (
                " Among your top-impression pages, these 4xx were seen: "
                + ", ".join(f"<code>{urlparse(i['url']).path}</code>" for i in live_404[:5])
            )
        recs.append({
            "priority": "MED", "area": "Crawl health", "owner": "agent",
            "title": f"{crawl['code_4xx']:,} URL(s) returned 4xx in Bing's crawl",
            "detail": "Broken/removed pages waste crawl budget and lose any link equity." + extra,
            "action": "301-redirect the valuable ones to live equivalents; let truly dead ones 410.",
        })

    # 4) Large 'all other codes' bucket (soft-404s / timeouts / odd responses).
    if crawl.get("all_other_codes", 0) >= 1000:
        recs.append({
            "priority": "MED", "area": "Crawl health", "owner": "you",
            "title": f"{crawl['all_other_codes']:,} URL(s) returned non-standard codes",
            "detail": "Often soft-404s, redirect chains, or timeouts that quietly suppress indexing.",
            "action": "Spot-check a sample in BWT → URL Inspection; fix patterns (chains, soft-404s).",
        })

    # 5) Weak inbound-link profile — the structural ranking lever.
    in_links = crawl.get("in_links", 0)
    if in_links and in_links < 2000:
        recs.append({
            "priority": "MED", "area": "Authority", "owner": "you",
            "title": f"Only {in_links:,} inbound links to the whole site",
            "detail": (
                "Low domain authority is the main reason high-volume real-estate "
                "keywords don't rank — API submission can't fix this."
            ),
            "action": "Local-PR / builder & community backlinks / directory citations / guest posts.",
        })

    # 6) Opportunity queries — the biggest near-term click lever.
    if opportunities:
        top = opportunities[:6]
        lst = "; ".join(f'"{q["query"]}" (pos {q["avg_position"]:.0f}, {q["impressions"]} impr)' for q in top)
        recs.append({
            "priority": "MED", "area": "On-page", "owner": "agent",
            "title": f"{len(opportunities)} queries sit at position 4-15 — one nudge from clicks",
            "detail": f"Top: {lst}.",
            "action": (
                "Optimise each landing page's <title>/H1/intro to match the query, "
                "add internal links from related county/community pages."
            ),
        })

    # 7) On-page basics missing on top pages.
    if seo_findings:
        recs.append({
            "priority": "LOW", "area": "On-page", "owner": "agent",
            "title": f"{len(seo_findings)} top page(s) missing meta description / canonical / schema",
            "detail": "Basic on-page tags improve CTR and rich-result eligibility.",
            "action": "Add the missing <meta description>, <link canonical>, and JSON-LD.",
        })

    order = {"HIGH": 0, "MED": 1, "LOW": 2}
    recs.sort(key=lambda r: order.get(r["priority"], 9))
    return recs


# ── Safe auto-fix: priority recrawl of high-value live pages ──────────────────


def recrawl_high_value_pages(
    api_key: str,
    site_url: str,
    top_pages: list[dict],
    page_issues: list[dict],
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    """
    SAFE auto-action: ask Bing to re-crawl the highest-impression *live* pages so
    on-page fixes get re-evaluated sooner. Reversible / no-harm; tiny quota use.
    Skips any URL flagged broken by the top-URL health check.
    """
    if limit is None:
        limit = int(os.environ.get("BING_RECRAWL_LIMIT", "50"))
    broken = {i["url"] for i in page_issues}
    urls = [p["url"] for p in top_pages if p.get("url") and p["url"] not in broken][:limit]
    if not urls:
        return {"submitted": 0, "urls": []}
    if dry_run:
        logger.info(f"  [DRY RUN] would resubmit {len(urls)} high-value pages for recrawl")
        return {"submitted": len(urls), "urls": urls, "dry_run": True}
    try:
        bing_client._call(
            "post", "SubmitUrlBatch", api_key,
            json_body={"siteUrl": site_url, "urlList": urls},
        )
        logger.info(f"  Resubmitted {len(urls)} high-value pages for priority recrawl")
        return {"submitted": len(urls), "urls": urls}
    except Exception as e:  # noqa: BLE001 - safe action, never block the report
        logger.warning(f"  Recrawl submit failed (non-fatal): {e}")
        return {"submitted": 0, "urls": [], "error": str(e)}


# ── HTML report section ────────────────────────────────────────────────────────


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


_PRIORITY_CLASS = {"HIGH": "bad", "MED": "warn", "LOW": ""}
_OWNER_LABEL = {
    "you": "You / broker",
    "agent": "Site-edit agent",
    "auto": "Auto (done)",
}


def build_seo_section(
    recs: list[dict],
    crawl: dict,
    blocked: list[dict],
    blocked_value: list[dict],
    recrawl: dict,
) -> str:
    if not crawl and not recs:
        return (
            "<h2>🔎 SEO recommendations</h2>"
            "<p class='warn'>Bing SEO-scan data unavailable this run.</p>"
        )

    rec_rows = ""
    for r in recs:
        cls = _PRIORITY_CLASS.get(r["priority"], "")
        rec_rows += (
            f"<tr><td class='{cls}'>{r['priority']}</td>"
            f"<td>{_esc(r['area'])}</td>"
            f"<td><b>{r['title']}</b><br><span style='color:#555'>{r['detail']}</span>"
            f"<br><span style='color:#0a3d91'>→ {r['action']}</span></td>"
            f"<td>{_OWNER_LABEL.get(r['owner'], r['owner'])}</td></tr>"
        )
    if not rec_rows:
        rec_rows = "<tr><td colspan='4' class='good'>No SEO issues surfaced this run ✓</td></tr>"

    crawl_block = ""
    if crawl:
        crawl_block = f"""
<h3 style="margin-top:18px;font-size:14px">Bing crawl-health snapshot</h3>
<table>
<tr><th>In index</th><th>Inbound links</th><th>301</th><th>4xx</th><th>5xx</th><th>Blocked by robots</th><th>Other codes</th></tr>
<tr>
  <td>{crawl['in_index']:,}</td>
  <td class="{'warn' if crawl['in_links'] < 2000 else ''}">{crawl['in_links']:,}</td>
  <td>{crawl['code_301']:,}</td>
  <td class="{'warn' if crawl['code_4xx'] else 'good'}">{crawl['code_4xx']:,}</td>
  <td class="{'bad' if crawl['code_5xx'] else 'good'}">{crawl['code_5xx']:,}</td>
  <td>{crawl['blocked_by_robots']:,}</td>
  <td>{crawl['all_other_codes']:,}</td>
</tr>
</table>"""

    blocked_block = ""
    if blocked:
        value_urls = {f["url"] for f in blocked_value}
        rows = ""
        for b in blocked:
            flag = " 🔴 <b>ranks for this!</b>" if b["url"] in value_urls else ""
            exp = f"{b['days_to_expire']}d" if b.get("days_to_expire") is not None else "—"
            rows += (
                f"<tr><td><code>{_esc(urlparse(b['url']).path)}</code>{flag}</td>"
                f"<td>{exp}</td></tr>"
            )
        blocked_block = f"""
<h3 style="margin-top:18px;font-size:14px">URLs you're blocking from Bing ({len(blocked)})</h3>
<table>
<tr><th>Blocked path</th><th>Block expires</th></tr>
{rows}
</table>"""

    recrawl_line = ""
    if recrawl:
        n = recrawl.get("submitted", 0)
        recrawl_line = (
            f"<p class='good' style='font-size:12px'>✓ Safe auto-action: resubmitted "
            f"<b>{n}</b> high-value live page(s) to Bing for priority recrawl.</p>"
        )

    return f"""
<h2>🔎 SEO recommendations <span style="font-weight:normal;font-size:12px;color:#888">synthesized from Bing Webmaster data</span></h2>
<p style="font-size:12px;color:#888;margin:2px 0 10px">
  Bing retired its packaged SEO-report API, so these are derived live from crawl
  stats, blocked-URL data, and your ranking queries. "Who acts" shows what's
  auto-done vs. what needs your approval before a live-site edit.
</p>
{recrawl_line}
<table>
<tr><th>Priority</th><th>Area</th><th>Recommendation &amp; action</th><th>Who acts</th></tr>
{rec_rows}
</table>
{crawl_block}
{blocked_block}
"""
