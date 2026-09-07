"""
Daily Google Search Console management — the "google-daily" agent.

The Google-side sibling of bing_manager.py. On each run it:
  1. Pulls GSC performance (clicks / impressions / CTR / avg position, 30d)
     + "opportunity queries" — position 4-15 with traction (quick wins)
  2. Pulls GA4 behavior (organic sessions / engagement / conversions, 30d)
     via ga4_analytics — what visitors actually DO after they click
  3. Scans top pages for on-page SEO + AI/LLM-search readiness gaps
  4. Joins GSC × GA4 per landing page and asks Claude to interpret it into a
     prioritized corrective-action plan (owner=auto for safe nudges, owner=you
     for live content/meta/CTA edits that need approval)
  5. Submits fresh URLs + the plan's owner=auto URLs to Google's Indexing API
  6. (Re)submits the sitemaps to Search Console so Google re-crawls
  7. Researches the latest Google + reliable-source SEO / AI-search guidance
     (Claude web search) and grows an SEO playbook (GSC_LEARNINGS.md on GCS)
  8. Builds + emails an HTML daily report and saves it to the command center,
     and hands its top findings to other agents (Scout, sitemap-sync-weekly)
     via the Master relay (the worker output carries a `relay` block)

Reuses bing_manager's page helpers + gsc_client/gsc_analytics for auth + pulls,
so it stays consistent with the existing Bing job and the website report.
Action policy: safe nudges auto-applied; live edits surfaced for approval.

Runs two ways (like the other Level-B workers):
  * cron / manual:  python3 gsc_manager.py
  * agent-manager:  the Master triggers it with AGENTMGR_TASK_ID and reads the
                    TaskResult (worker name "google-daily").
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import ga4_analytics
import gsc_analytics
import gsc_client
# Reuse the page-health, on-page-scan, fresh-URL and email helpers from the
# Bing job so the two stay byte-for-byte consistent (no logic duplication).
from bing_manager import (
    check_on_page_seo,
    check_top_urls,
    load_fresh_urls_from_gcs,
    send_report,
)

logger = logging.getLogger(__name__)

INDEXING_SCOPE = "https://www.googleapis.com/auth/indexing"
SITEMAP_SHARDS = int(os.environ.get("GSC_SITEMAP_SHARDS", "16"))


# ── Indexing API (best-effort URL submission) ─────────────────────────────────

def _indexing_service():
    """Build the Google Indexing API client.

    Auth resolution: GOOGLE_INDEXING_CREDS (a service-account JSON, the SA must
    be added as an *owner* of the GSC property) -> ADC with the indexing scope.
    Returns None if no usable credentials — the caller degrades gracefully.
    """
    from googleapiclient.discovery import build
    creds_json = os.environ.get("GOOGLE_INDEXING_CREDS", "").strip()
    try:
        if creds_json:
            from google.oauth2 import service_account
            info = json.loads(creds_json)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=[INDEXING_SCOPE])
        else:
            import google.auth
            creds, _ = google.auth.default(scopes=[INDEXING_SCOPE])
        return build("indexing", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"  Indexing API auth unavailable: {e}")
        return None


def submit_urls_indexing_api(urls: list[str], limit: int = 100) -> dict:
    """Notify Google (URL_UPDATED) for up to `limit` fresh URLs. Best-effort.

    NOTE: Google's Indexing API is officially scoped to JobPosting /
    BroadcastEvent pages; for general pages it nudges crawl but isn't
    guaranteed. We submit anyway (it's harmless) and report what Google
    accepted. Needs the Indexing API enabled + the SA as a property owner.
    """
    out = {"attempted": 0, "succeeded": 0, "errors": [], "note": ""}
    urls = [u for u in (urls or []) if u][:limit]
    if not urls:
        out["note"] = "no fresh URLs to submit"
        return out
    svc = _indexing_service()
    if svc is None:
        out["note"] = ("Indexing API not configured — enable indexing.googleapis.com, "
                       "add the runner SA as a Search Console owner, and set the "
                       "auth/indexing scope (GOOGLE_INDEXING_CREDS).")
        return out
    for u in urls:
        out["attempted"] += 1
        try:
            svc.urlNotifications().publish(
                body={"url": u, "type": "URL_UPDATED"}).execute()
            out["succeeded"] += 1
        except Exception as e:  # noqa: BLE001
            msg = str(e)[:160]
            out["errors"].append({"url": u, "error": msg})
            # A permission/enablement error is systemic — stop hammering.
            if "PERMISSION_DENIED" in msg or "403" in msg or "has not been used" in msg:
                out["note"] = ("Indexing API rejected the call (permission/enablement). "
                               "Add the runner SA as a GSC owner + enable the API.")
                break
    return out


# ── Sitemap (re)submission to GSC ─────────────────────────────────────────────

def ping_sitemaps_gsc(site_url: str, n_shards: int = SITEMAP_SHARDS,
                      dry_run: bool = False) -> dict:
    """Resubmit the shard + index sitemaps so Google re-crawls. Reuses
    gsc_client.submit_sitemaps (its retry/cleanup logic)."""
    base = site_url.rstrip("/")
    shards = [(n, f"{base}/sitemap{n}/") for n in range(1, n_shards + 1)]
    try:
        return gsc_client.submit_sitemaps(
            shards, site_url, redirect_base=base, dry_run=dry_run)
    except Exception as e:  # noqa: BLE001 - never block the report
        logger.error(f"  GSC sitemap submit failed: {e}")
        return {"submitted": 0, "deleted": 0, "errors": [str(e)[:160]], "success": False}


# ── AI / LLM-search readiness (layered on the on-page scan) ───────────────────

def ai_search_findings(seo_findings: list[dict]) -> list[dict]:
    """Turn on-page gaps into AI/LLM-search-readiness notes. Pages that LLM
    answer engines (Google AI Overviews, ChatGPT, Perplexity) favor have
    structured data (JSON-LD) and clear, answer-shaped content. Missing JSON-LD
    is the single biggest AI-search miss, so we flag it explicitly."""
    notes = []
    for f in seo_findings or []:
        problems = f.get("problems", []) or []
        flags = []
        if any("json" in str(p).lower() or "ld+json" in str(p).lower() for p in problems):
            flags.append("no structured data (JSON-LD) — invisible to AI answer engines")
        if any("meta" in str(p).lower() and "desc" in str(p).lower() for p in problems):
            flags.append("no meta description — weak AI snippet/summary")
        if flags:
            notes.append({"url": f.get("url"), "ai_flags": flags})
    return notes


# ── Continuous improvement: SEO / AI-search research playbook ─────────────────

_PLAYBOOK_GCS = os.environ.get(
    "SEO_PLAYBOOK_GCS",
    "gs://site-map-dynamic-page3/seo-playbook/GSC_LEARNINGS.md")
_RESEARCH_MODEL = os.environ.get("GSC_RESEARCH_MODEL", "claude-haiku-4-5-20251001")

_RESEARCH_PROMPT = (
    "You are the SEO + AI-search analyst for a Southeast-Florida real-estate "
    "brokerage (paradiserealtyfla.com). Using web search, find the LATEST "
    "(past ~2 months) authoritative guidance for getting MORE high-quality "
    "organic traffic AND being surfaced by AI answer engines (Google AI "
    "Overviews / AI Mode, ChatGPT search, Perplexity, Gemini). Prioritize "
    "primary sources: Google Search Central (developers.google.com/search), "
    "the Google Search Status dashboard, and other reputable SEO sources "
    "(Search Engine Land, Search Engine Roundtable, Ahrefs, Moz). For each "
    "finding give: (1) what changed / the practice, (2) the concrete action "
    "for a local real-estate site, (3) the source URL. Cover: helpful-content "
    "/ E-E-A-T, structured data (JSON-LD) that helps AI answers, "
    "answer-engine optimization (AEO/GEO), local SEO, Core Web Vitals, and any "
    "ranking-system or AI-search updates. Return 6-10 tight bullets, each "
    "ending with its source URL. Skip generic advice."
)


def research_seo_playbook() -> str | None:
    """Ask Claude (with web search) for current SEO + AI-search best practices.
    Best-effort: returns a markdown digest, or None if no key / web search."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        logger.warning("  no ANTHROPIC_API_KEY — skipping SEO research")
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model=_RESEARCH_MODEL,
            max_tokens=1400,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
            messages=[{"role": "user", "content": _RESEARCH_PROMPT}],
        )
        text = "".join(
            getattr(b, "text", "") for b in resp.content
            if getattr(b, "type", "") == "text"
        ).strip()
        return text or None
    except Exception as e:  # noqa: BLE001 - research is best-effort
        logger.error(f"  SEO research failed (continuing without it): {e}")
        return None


def update_playbook(digest: str | None) -> dict:
    """Append today's research to the GCS-persisted SEO playbook. Returns
    {appended: bool, uri: str}. Never raises."""
    info = {"appended": False, "uri": _PLAYBOOK_GCS}
    if not digest:
        return info
    try:
        from google.cloud import storage
        m = _PLAYBOOK_GCS.replace("gs://", "").split("/", 1)
        bucket_name, blob_name = m[0], m[1]
        bucket = storage.Client().bucket(bucket_name)
        blob = bucket.blob(blob_name)
        prior = blob.download_as_text() if blob.exists() else "# SEO + AI-Search Playbook\n"
        stamp = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
        blob.upload_from_string(
            f"{prior}\n\n---\n## {stamp}\n{digest}\n", content_type="text/markdown")
        info["appended"] = True
    except Exception as e:  # noqa: BLE001
        logger.error(f"  playbook update failed: {e}")
    return info


# ── GSC × GA4 join + interpretation (the "wise decisions" step) ──────────────

def _path_of(url_or_path: str) -> str:
    """Normalize a GSC full URL or a GA4 landing path to a comparable path."""
    p = url_or_path or ""
    if p.startswith("http"):
        p = urlparse(p).path or "/"
    p = p.split("?", 1)[0].split("#", 1)[0]
    if len(p) > 1:
        p = p.rstrip("/")
    return p.lower() or "/"


def join_gsc_ga4(top_pages: list[dict] | None,
                 ga4_landing_pages: list[dict] | None) -> list[dict]:
    """Merge GSC search performance and GA4 behavior per landing-page path.

    The join key is the URL path. A page present in GSC but absent from GA4
    organic (or vice-versa) still appears — the gaps are themselves signal
    (search demand with no engagement, or engaged pages search can't see).
    """
    ga4_by_path: dict[str, dict] = {}
    for g in ga4_landing_pages or []:
        ga4_by_path[_path_of(g.get("landing_page", ""))] = g

    joined: list[dict] = []
    seen: set[str] = set()
    for p in top_pages or []:
        path = _path_of(p.get("page", ""))
        seen.add(path)
        g = ga4_by_path.get(path, {})
        joined.append({
            "path": path,
            "url": p.get("page"),
            "impressions": p.get("impressions", 0),
            "clicks": p.get("clicks", 0),
            "position": round(p.get("position", 0), 1),
            "ga4_sessions": int(g.get("sessions", 0)),
            "engagement_rate": round(g.get("engagementRate", 0), 3),
            "avg_session_sec": round(g.get("averageSessionDuration", 0), 1),
            "conversions": int(g.get("keyEvents", g.get("conversions", 0))),
            "in_ga4": bool(g),
        })
    # GA4-only organic pages (engaged but not in GSC's top set) — keep the
    # strongest few so the analyst sees pages search isn't surfacing.
    for path, g in ga4_by_path.items():
        if path in seen:
            continue
        joined.append({
            "path": path, "url": None, "impressions": 0, "clicks": 0,
            "position": 0, "ga4_sessions": int(g.get("sessions", 0)),
            "engagement_rate": round(g.get("engagementRate", 0), 3),
            "avg_session_sec": round(g.get("averageSessionDuration", 0), 1),
            "conversions": int(g.get("keyEvents", g.get("conversions", 0))),
            "in_ga4": True,
        })
    joined.sort(key=lambda r: -(r["clicks"] + r["ga4_sessions"]))
    return joined


_ANALYST_MODEL = os.environ.get("GSC_ANALYST_MODEL", "claude-haiku-4-5-20251001")

_ANALYST_PROMPT = (
    "You are the SEO analyst for paradiserealtyfla.com, a Southeast-Florida "
    "real-estate brokerage. You are given JOINED Google Search Console (search "
    "demand) + GA4 (on-site behavior) data per landing page, plus organic KPI "
    "trends, channel mix, opportunity queries, and on-page gaps. Decide the "
    "highest-leverage CORRECTIVE ACTIONS to improve Google organic performance.\n\n"
    "Reasoning patterns to apply:\n"
    "- High impressions + low CTR -> rewrite title/meta (owner: you).\n"
    "- Good organic clicks/sessions but LOW engagement_rate or short "
    "avg_session_sec -> content/intent mismatch, fix the page (owner: you).\n"
    "- Organic sessions but ZERO conversions -> CTA / lead-form problem "
    "(owner: you).\n"
    "- Ranks + has demand but weak/again: nudge recrawl + indexing "
    "(owner: auto — SAFE).\n"
    "- Falling organic session trend -> investigate broad regression "
    "(owner: you).\n\n"
    "POLICY: owner='auto' is ONLY for safe, reversible nudges (recrawl / "
    "indexing submit / sitemap priority). Any live content/meta/CTA edit MUST "
    "be owner='you' (needs human approval) — never 'auto'.\n\n"
    "Call the submit_corrective_plan tool with your plan: a <=2-sentence "
    "summary and up to 10 actions ordered by impact. owner='auto' ONLY for "
    "safe nudges (recrawl / indexing submit / sitemap priority); any live "
    "content/meta/CTA edit MUST be owner='you'. Be specific and cite the "
    "numbers in each rationale."
)

# Structured-output tool so the model returns SDK-validated JSON (no fragile
# free-text JSON parsing — that broke when GA4 enriched the payload).
_PLAN_TOOL = {
    "name": "submit_corrective_plan",
    "description": "Submit the prioritized SEO corrective-action plan.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "<=2 sentences"},
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "imperative phrase"},
                        "target": {"type": "string", "description": "url, query, or 'site-wide'"},
                        "owner": {"type": "string", "enum": ["auto", "you"]},
                        "rationale": {"type": "string", "description": "cite the numbers"},
                        "impact": {"type": "string", "enum": ["high", "med", "low"]},
                    },
                    "required": ["action", "owner", "rationale", "impact"],
                },
            },
        },
        "required": ["summary", "actions"],
    },
}


def interpret_and_plan(*, joined, kpis, channels, opportunities,
                       seo_findings) -> dict:
    """Ask Claude to turn the joined data into a prioritized action plan.
    Best-effort: returns {summary, actions:[...]} or {} on any failure."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        logger.warning("  no ANTHROPIC_API_KEY — skipping interpretation")
        return {}
    payload = {
        "organic_kpis": kpis or {},
        "channel_mix": (channels or [])[:8],
        "pages": (joined or [])[:25],
        "opportunity_queries": [
            {"query": o["query"], "impressions": o["impressions"],
             "clicks": o["clicks"], "position": round(o["position"], 1)}
            for o in (opportunities or [])[:15]
        ],
        "on_page_gaps": [
            {"url": f.get("url"), "problems": f.get("problems", [])}
            for f in (seo_findings or [])[:15]
        ],
    }
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model=_ANALYST_MODEL,
            max_tokens=2000,
            system=_ANALYST_PROMPT,
            tools=[_PLAN_TOOL],
            tool_choice={"type": "tool", "name": "submit_corrective_plan"},
            messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
        )
        data = {}
        for b in resp.content:
            if getattr(b, "type", "") == "tool_use" and getattr(b, "name", "") == "submit_corrective_plan":
                data = b.input or {}
                break
        actions = data.get("actions", []) or []
        # Defensive: never let the model mark a live-edit as auto.
        for a in actions:
            act = (a.get("action") or "").lower()
            if a.get("owner") == "auto" and any(
                    w in act for w in ("rewrite", "edit", "add ", "update content",
                                        "change", "cta", "meta", "title", "rewrite")):
                a["owner"] = "you"
        return {"summary": data.get("summary", ""), "actions": actions}
    except Exception as e:  # noqa: BLE001 - interpretation is advisory
        logger.error(f"  interpretation failed (continuing): {e}")
        return {}


def safe_action_urls(plan: dict) -> list[str]:
    """Extract target URLs from owner='auto' actions so they can ride along
    with the Indexing-API submission (the only auto-action we execute)."""
    urls = []
    for a in (plan or {}).get("actions", []) or []:
        if a.get("owner") != "auto":
            continue
        tgt = (a.get("target") or "").strip()
        if tgt.startswith("http"):
            urls.append(tgt)
    return urls


# ── Report ────────────────────────────────────────────────────────────────────

def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _md_lite(md: str) -> str:
    """Minimal markdown -> HTML for the research digest (bullets + bold + links)."""
    out = []
    for raw in (md or "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        line = _esc(line)
        # links: [text](url) and bare urls
        import re
        line = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
                      r'<a href="\2">\1</a>', line)
        line = re.sub(r"(?<!\")(https?://[^\s<]+)", r'<a href="\1">\1</a>', line)
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        if line.lstrip("-* ").strip() != line:
            out.append(f"<li>{line.lstrip('-* ').strip()}</li>")
        elif line[:2].rstrip().isdigit() or line[:2] == "- ":
            out.append(f"<li>{line}</li>")
        else:
            out.append(f"<li>{line}</li>")
    return "<ul>" + "".join(out) + "</ul>" if out else "<p>No research this run.</p>"


def build_google_report(*, traffic, top_queries, top_pages, opportunities,
                        page_issues, seo_findings, ai_findings, indexing,
                        sitemaps, research, date_str, ga4=None, joined=None,
                        plan=None) -> str:
    def row(cells):
        return "<tr>" + "".join(f"<td style='padding:4px 8px'>{c}</td>" for c in cells) + "</tr>"

    t = traffic or {}
    kpi = (
        f"<b>{t.get('impressions', 0):,}</b> impressions · "
        f"<b>{t.get('clicks', 0):,}</b> clicks · "
        f"CTR <b>{(t.get('ctr', 0) * 100):.1f}%</b> · "
        f"avg position <b>{(t.get('avg_position') or 0):.1f}</b> (last {t.get('days', 30)}d)"
        if t else "GSC performance unavailable this run."
    )

    opp_rows = "".join(
        row([_esc(o["query"]), f"{o['impressions']:,}", o["clicks"], f"{o['position']:.1f}"])
        for o in (opportunities or [])[:15]
    ) or row(["<i>none</i>", "", "", ""])

    page_rows = "".join(
        row([f"<a href='{_esc(p['page'])}'>{_esc(p['page'])}</a>", f"{p['impressions']:,}",
             p["clicks"], f"{p.get('position', 0):.1f}"])
        for p in (top_pages or [])[:10]
    ) or row(["<i>none</i>", "", "", ""])

    gap_rows = "".join(
        row([f"<a href='{_esc(f['url'])}'>{_esc(f['url'])}</a>", _esc(", ".join(f.get('problems', [])))])
        for f in (seo_findings or [])[:15]
    ) or row(["<i>no gaps found</i>", ""])

    ai_rows = "".join(
        row([f"<a href='{_esc(f['url'])}'>{_esc(f['url'])}</a>", _esc("; ".join(f.get('ai_flags', [])))])
        for f in (ai_findings or [])[:10]
    ) or row(["<i>top pages look AI-ready</i>", ""])

    broken = "".join(f"<li><a href='{_esc(i.get('url'))}'>{_esc(i.get('url'))}</a> — {_esc(i.get('status') or i.get('issue'))}</li>"
                     for i in (page_issues or [])[:15]) or "<li>none ✅</li>"

    # GA4 organic KPI band (or a config note if GA4 isn't wired yet).
    def _arrow(d):
        if d is None:
            return ""
        sign, color = ("▲", "#15803d") if d >= 0 else ("▼", "#b91c1c")
        return f" <span style='color:{color};font-size:12px'>{sign}{abs(d) * 100:.0f}%</span>"

    g = ga4 or {}
    if g.get("configured"):
        k = g.get("kpis", {}) or {}
        cur = k.get("current", {}) or {}
        conv_key = k.get("conv_key")
        conv = cur.get(conv_key, 0) if conv_key else 0
        ga4_band = (
            "<p style='font:14px -apple-system,Arial;background:#f1f5f9;padding:8px 12px;border-radius:8px'>"
            f"<b>GA4 organic</b> (last {k.get('days', 30)}d): "
            f"<b>{int(cur.get('sessions', 0)):,}</b> sessions{_arrow(k.get('sessions_delta'))} · "
            f"engagement <b>{cur.get('engagementRate', 0) * 100:.0f}%</b> · "
            f"avg <b>{cur.get('averageSessionDuration', 0):.0f}s</b> · "
            f"<b>{int(conv):,}</b> conversions{_arrow(k.get('conversions_delta'))}</p>"
        )
    else:
        ga4_band = (
            "<p style='font:12px -apple-system,Arial;color:#94a3b8'>"
            f"GA4 behavior data not wired yet — {_esc(g.get('note', ''))}</p>"
        )

    # Joined GSC × GA4 landing-page table.
    join_rows = "".join(
        row([f"<a href='{_esc(j['url'])}'>{_esc(j['path'])}</a>" if j.get("url") else _esc(j["path"]),
             f"{j['impressions']:,}", j["clicks"], f"{j['position']:.1f}",
             f"{j['ga4_sessions']:,}", f"{j['engagement_rate'] * 100:.0f}%", j["conversions"]])
        for j in (joined or [])[:15]
    ) or row(["<i>no joined data — GA4 not wired</i>", "", "", "", "", "", ""])

    # Corrective-action plan (analyst output), split auto vs needs-approval.
    def _act_li(a):
        imp = (a.get("impact") or "med").lower()
        badge = {"high": "#b91c1c", "med": "#b45309", "low": "#64748b"}.get(imp, "#64748b")
        tgt = (a.get("target") or "").strip()
        tgt_html = (f" — <a href='{_esc(tgt)}'>{_esc(tgt)}</a>" if tgt.startswith("http")
                    else (f" — {_esc(tgt)}" if tgt else ""))
        return (f"<li style='margin-bottom:6px'><b>{_esc(a.get('action', ''))}</b>{tgt_html} "
                f"<span style='font-size:11px;color:{badge}'>[{_esc(imp)}]</span>"
                f"<br><span style='color:#475569;font-size:12px'>{_esc(a.get('rationale', ''))}</span></li>")

    pl = plan or {}
    acts = pl.get("actions", []) or []
    auto_html = "".join(_act_li(a) for a in acts if a.get("owner") == "auto") or "<li>none this run</li>"
    needs_html = "".join(_act_li(a) for a in acts if a.get("owner") != "auto") or "<li>none this run ✅</li>"
    plan_summary = _esc(pl.get("summary", ""))

    idx = indexing or {}
    sm = sitemaps or {}
    H = "font:700 15px -apple-system,Arial;color:#0f2744;margin:18px 0 6px"
    TH = "text-align:left;padding:4px 8px;color:#64748b;font-size:12px;border-bottom:1px solid #e2e8f0"
    return f"""<div style="max-width:760px;margin:0 auto;font-family:-apple-system,Segoe UI,Arial,sans-serif;color:#111">
  <div style="background:#0f2744;color:#fff;padding:18px 22px;border-radius:12px 12px 0 0">
    <div style="font:700 20px -apple-system,Arial">🔎 Google Daily — Search Console</div>
    <div style="font:13px -apple-system,Arial;color:#cbd5e1;margin-top:4px">{date_str} · paradiserealtyfla.com</div>
  </div>
  <div style="padding:16px 22px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px">
    <p style="font:14px -apple-system,Arial">{kpi}</p>
    {ga4_band}

    <div style="{H}">🧭 Corrective actions{(' — ' + plan_summary) if plan_summary else ''}</div>
    <div style="font-size:13px"><b style="color:#0f766e">Auto-applied (safe nudges):</b>
      <ul style="margin-top:4px">{auto_html}</ul></div>
    <div style="font-size:13px"><b style="color:#b45309">Needs your approval (live edits):</b>
      <ul style="margin-top:4px">{needs_html}</ul></div>

    <div style="{H}">Opportunity queries (position 4–15 — quick wins)</div>
    <table style="border-collapse:collapse;width:100%;font-size:13px">
      <tr><th style="{TH}">Query</th><th style="{TH}">Impr</th><th style="{TH}">Clicks</th><th style="{TH}">Pos</th></tr>
      {opp_rows}
    </table>

    <div style="{H}">Top pages (30d)</div>
    <table style="border-collapse:collapse;width:100%;font-size:13px">
      <tr><th style="{TH}">Page</th><th style="{TH}">Impr</th><th style="{TH}">Clicks</th><th style="{TH}">Pos</th></tr>
      {page_rows}
    </table>

    <div style="{H}">Organic landing pages — search demand × on-site behavior (GSC × GA4)</div>
    <table style="border-collapse:collapse;width:100%;font-size:13px">
      <tr><th style="{TH}">Page</th><th style="{TH}">Impr</th><th style="{TH}">Clicks</th>
          <th style="{TH}">Pos</th><th style="{TH}">Sessions</th><th style="{TH}">Engmt</th><th style="{TH}">Conv</th></tr>
      {join_rows}
    </table>

    <div style="{H}">On-page SEO gaps (top pages)</div>
    <table style="border-collapse:collapse;width:100%;font-size:13px">
      <tr><th style="{TH}">Page</th><th style="{TH}">Missing</th></tr>
      {gap_rows}
    </table>

    <div style="{H}">AI / LLM-search readiness</div>
    <table style="border-collapse:collapse;width:100%;font-size:13px">
      <tr><th style="{TH}">Page</th><th style="{TH}">Why AI engines may skip it</th></tr>
      {ai_rows}
    </table>

    <div style="{H}">Broken / redirected top pages</div>
    <ul style="font-size:13px">{broken}</ul>

    <div style="{H}">Actions taken</div>
    <ul style="font-size:13px">
      <li>Indexing API: {idx.get('succeeded', 0)}/{idx.get('attempted', 0)} URLs accepted{(' — ' + _esc(idx['note'])) if idx.get('note') else ''}</li>
      <li>Sitemaps resubmitted to GSC: {sm.get('submitted', 0)} ({len(sm.get('errors', []))} errors)</li>
    </ul>

    <div style="{H}">📈 SEO + AI-search playbook — this week's research</div>
    <div style="font-size:13px;line-height:1.6">{_md_lite(research)}</div>
    <p style="font:11px -apple-system,Arial;color:#94a3b8;margin-top:14px;border-top:1px solid #eef2f7;padding-top:8px">
      Generated by the google-daily agent. Research is advisory — review before acting.
      Findings accumulate in the SEO playbook so practices compound over time.</p>
  </div>
</div>"""


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", stream=sys.stdout)
    site_url = os.environ.get("SITE_URL", "https://www.paradiserealtyfla.com/")
    gsc_site_url = os.environ.get("GSC_SITE_URL", site_url)
    sg_key = os.environ.get("SENDGRID_API_KEY", "")
    to_email = os.environ.get("REPORT_EMAIL_TO", "joe@josephcapra.com")
    gcs_csv = os.environ.get(
        "DAILY_SUBMIT_GCS_CSV",
        "gs://site-map-dynamic-page3/sitemaps/communities_combined.csv")
    gcs_project = os.environ.get("GCS_PROJECT", "paradise-automation")
    submit_limit = int(os.environ.get("GSC_INDEXING_LIMIT", "100"))
    now_et = datetime.now(ZoneInfo("America/New_York"))
    date_str = now_et.strftime("%b %d, %Y")

    logger.info("=" * 60)
    logger.info("google-daily — Google Search Console management")
    logger.info(f"  Site: {gsc_site_url}")
    logger.info("=" * 60)

    # 1. GSC performance (best-effort)
    logger.info("[1/8] GSC performance + opportunity queries")
    traffic = top_queries = top_pages = opportunities = None
    try:
        gsc = gsc_client._build_service()
        traffic = gsc_analytics.pull_traffic(gsc, gsc_site_url, days=30)
        top_queries = gsc_analytics.pull_top_queries(gsc, gsc_site_url, days=30, limit=20)
        top_pages = gsc_analytics.pull_top_pages(gsc, gsc_site_url, days=30, limit=20)
        opportunities = gsc_analytics.find_opportunity_queries(gsc, gsc_site_url, days=30)
        logger.info(f"  30d: {traffic['impressions']:,} impr / {traffic['clicks']:,} clicks · "
                    f"{len(opportunities)} opportunity queries")
    except Exception as e:  # noqa: BLE001
        logger.error(f"  GSC pull failed (continuing): {e}")

    # 2. GA4 behavior — organic sessions / engagement / conversions
    logger.info("[2/8] GA4 behavior — organic engagement + conversions")
    ga4 = ga4_analytics.pull_all(days=30, landing_limit=25)
    if ga4.get("configured"):
        _k = (ga4.get("kpis") or {}).get("current", {})
        logger.info(f"  GA4 organic 30d: {int(_k.get('sessions', 0)):,} sessions · "
                    f"engagement {(_k.get('engagementRate', 0) * 100):.0f}%")
    else:
        logger.info(f"  GA4 not wired: {ga4.get('note', '')}")

    # 3. Page health + on-page scan (top pages by impressions)
    logger.info("[3/8] Page health + on-page SEO scan")
    top_urls = [p["page"] for p in (top_pages or [])][:50]
    page_issues = check_top_urls(top_urls) if top_urls else []
    seo_findings = check_on_page_seo([p["page"] for p in (top_pages or [])][:25]) if top_pages else []
    ai_findings = ai_search_findings(seo_findings)

    # 4. Join GSC × GA4 + interpret -> prioritized corrective-action plan
    logger.info("[4/8] Joining GSC × GA4 + interpreting (analyst)")
    joined = join_gsc_ga4(top_pages, (ga4.get("landing_pages") if ga4 else None))
    plan = interpret_and_plan(
        joined=joined, kpis=(ga4.get("kpis") if ga4 else None),
        channels=(ga4.get("channels") if ga4 else None),
        opportunities=opportunities, seo_findings=seo_findings)
    _auto = sum(1 for a in plan.get("actions", []) if a.get("owner") == "auto")
    _appr = len(plan.get("actions", [])) - _auto
    logger.info(f"  plan: {len(plan.get('actions', []))} actions "
                f"({_auto} auto, {_appr} need approval)")

    # 5. Indexing API submission (fresh URLs + the analyst's safe auto URLs)
    logger.info("[5/8] Submitting fresh URLs to Google Indexing API")
    try:
        fresh = load_fresh_urls_from_gcs(gcs_csv, gcs_project, limit=submit_limit)
    except Exception as e:  # noqa: BLE001
        logger.error(f"  could not load fresh URLs: {e}")
        fresh = []
    # Union the analyst's owner='auto' URLs (deduped, fresh URLs first).
    submit_urls = list(dict.fromkeys([*fresh, *safe_action_urls(plan)]))
    indexing = submit_urls_indexing_api(submit_urls, limit=submit_limit)
    logger.info(f"  Indexing API: {indexing['succeeded']}/{indexing['attempted']} "
                f"{indexing.get('note', '')}")

    # 6. Sitemap (re)submission to GSC
    logger.info("[6/8] Resubmitting sitemaps to Search Console")
    sitemaps = ping_sitemaps_gsc(site_url)

    # 7. Continuous improvement: SEO + AI-search research -> playbook
    logger.info("[7/8] Researching SEO + AI-search best practices")
    research = research_seo_playbook()
    playbook = update_playbook(research)
    logger.info(f"  research: {'ok' if research else 'unavailable'} · "
                f"playbook appended: {playbook['appended']}")

    # 8. Report (save + email)
    logger.info("[8/8] Building + sending report")
    html = build_google_report(
        traffic=traffic, top_queries=top_queries, top_pages=top_pages,
        opportunities=opportunities, page_issues=page_issues,
        seo_findings=seo_findings, ai_findings=ai_findings,
        indexing=indexing, sitemaps=sitemaps, research=research, date_str=date_str,
        ga4=ga4, joined=joined, plan=plan,
    )
    try:
        import agentmgr_reports
        agentmgr_reports.save_report(
            "google-daily", f"Google Daily — {date_str}", html, source="google-daily")
    except Exception:  # noqa: BLE001
        pass
    # Stash this run's findings so the agent-manager worker entry can hand them
    # to other agents (Scout, sitemap-sync-weekly) through the Master.
    global _LAST_RUN
    _LAST_RUN = {"opportunities": opportunities, "seo_findings": seo_findings,
                 "research_captured": bool(research), "plan": plan,
                 "ga4_configured": ga4.get("configured", False)}

    if sg_key and to_email:
        impr = traffic["impressions"] if traffic else 0
        n_appr = sum(1 for a in plan.get("actions", []) if a.get("owner") != "auto")
        subject = (f"Google Daily {now_et.strftime('%b %d')} — "
                   f"{impr:,}/30d impr · {len(opportunities or [])} opps · "
                   f"{n_appr} actions to approve")
        if not send_report(to_email, sg_key, subject, html):
            return 1
    else:
        logger.warning("  no SENDGRID_API_KEY/REPORT_EMAIL_TO — skipping email")
    return 0


# Populated by main(); read by the agent-manager worker entry to build the relay.
_LAST_RUN: dict = {}


# Top findings handed to other agents through the Master (the worker output's
# `relay` block; the Master forwards it to the named agents).
def _relay_payload(opportunities, seo_findings, research, plan=None) -> dict:
    acts = (plan or {}).get("actions", []) or []
    n_appr = sum(1 for a in acts if a.get("owner") != "auto")
    return {
        "to": ["scout", "sitemap-sync-weekly"],
        "summary": (
            f"{len(opportunities or [])} GSC opportunity queries (pos 4-15); "
            f"{len(seo_findings or [])} pages missing meta/canonical/JSON-LD; "
            f"{len(acts)} corrective actions ({n_appr} need approval); "
            f"SEO research {'captured' if research else 'unavailable'} this run."
        ),
        "top_opportunities": [o["query"] for o in (opportunities or [])[:5]],
        "actions_needing_approval": [
            {"action": a.get("action"), "target": a.get("target"),
             "impact": a.get("impact")}
            for a in acts if a.get("owner") != "auto"
        ][:5],
    }


def _run_as_agentmgr_worker(payload, task):
    """Agent-Manager worker entry: run the daily Google automation, and return
    a `relay` block so the Master can forward the findings to other agents."""
    args = payload.get("args")
    if args is not None:
        sys.argv = [sys.argv[0], *args]
    rc = main()
    if rc not in (0, None):
        raise RuntimeError(f"google-daily exited with code {rc}")
    return {
        "result": "google-daily completed",
        "exit_code": int(rc or 0),
        "relay": _relay_payload(
            _LAST_RUN.get("opportunities"), _LAST_RUN.get("seo_findings"),
            _LAST_RUN.get("research_captured"), _LAST_RUN.get("plan")),
    }


if __name__ == "__main__":
    import agentmgr_worker
    if agentmgr_worker.task_id():
        sys.exit(agentmgr_worker.run_as_worker("google-daily", _run_as_agentmgr_worker))
    sys.exit(main())
