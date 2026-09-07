"""
Google Analytics 4 (GA4) pulls for the daily SEO brief — the behavioral
counterpart to gsc_analytics.py.

Search Console tells us how Google *sees* the site (impressions, clicks,
position). GA4 tells us what visitors *do* once they arrive (sessions,
engagement, conversions, channel mix). Joining the two by landing-page path is
what lets the google-daily agent make wise decisions instead of reporting two
disconnected dashboards.

Read via the Analytics Data API (analyticsdata.googleapis.com, runReport).

Auth resolution (best-effort — the caller degrades gracefully if None):
  GA4_SA_CREDS (service-account JSON, the SA added as a GA4 property Viewer)
  -> ADC with the analytics.readonly scope (on Cloud Run = the runner SA's
     own identity; add that SA as a Viewer on the GA4 property).

Config:
  GA4_PROPERTY_ID  numeric GA4 property id (NOT the G-XXXX measurement id).
                   Find it in GA4 Admin -> Property Settings -> Property ID.
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

ANALYTICS_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
ORGANIC_CHANNEL = "Organic Search"  # GA4 default channel group value
DEFAULT_DAYS = 30


# ── Auth / service ────────────────────────────────────────────────────────────

def property_id() -> str | None:
    pid = os.environ.get("GA4_PROPERTY_ID", "").strip()
    # Tolerate a "properties/123" value or a stray G-XXXX (which is wrong).
    if pid.startswith("properties/"):
        pid = pid.split("/", 1)[1]
    return pid or None


def _build_service():
    """Build the GA4 Data API client, or None if no usable credentials.

    Auth resolution, in order:
      1. GA4_SA_CREDS (service-account key JSON) — optionally impersonating
         GA4_IMPERSONATE_SUBJECT via with_subject (domain-wide delegation).
      2. GA4_IMPERSONATE_SUBJECT set, no key → KEYLESS domain-wide delegation:
         the running service account signs (via IAM signBlob) a JWT asserting
         that subject, who has GA4 access. No key file, and the SA never has to
         be added as a GA4 user. Needs DWD authorized in the Workspace Admin
         console (SA client id + analytics.readonly) + the SA granted
         serviceAccountTokenCreator on itself (to sign).
      3. Plain ADC (the SA's own identity) — needs the SA added as a GA4 viewer.
    """
    from googleapiclient.discovery import build
    creds_json = os.environ.get("GA4_SA_CREDS", "").strip()
    subject = os.environ.get("GA4_IMPERSONATE_SUBJECT", "").strip()
    sa_email = os.environ.get("GA4_SA_EMAIL", "").strip()
    try:
        if creds_json:
            from google.oauth2 import service_account
            info = json.loads(creds_json)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=[ANALYTICS_SCOPE])
            if subject:
                creds = creds.with_subject(subject)
        elif subject:
            # Keyless domain-wide delegation — impersonate `subject` (a real GA4
            # user) using the runner SA's IAM signing capability.
            import google.auth
            from google.auth import iam
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account
            token_uri = "https://oauth2.googleapis.com/token"
            source_creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"])
            if not sa_email:
                sa_email = getattr(source_creds, "service_account_email", "") or ""
            signer = iam.Signer(Request(), source_creds, sa_email)
            creds = service_account.Credentials(
                signer, sa_email, token_uri,
                scopes=[ANALYTICS_SCOPE], subject=subject)
        else:
            import google.auth
            creds, _ = google.auth.default(scopes=[ANALYTICS_SCOPE])
        return build("analyticsdata", "v1beta", credentials=creds,
                     cache_discovery=False)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"  GA4 Data API auth unavailable: {e}")
        return None


def _organic_filter() -> dict:
    return {
        "filter": {
            "fieldName": "sessionDefaultChannelGroup",
            "stringFilter": {"value": ORGANIC_CHANNEL, "matchType": "EXACT"},
        }
    }


def _run(service, pid: str, body: dict) -> dict:
    return service.properties().runReport(
        property=f"properties/{pid}", body=body).execute()


def _num(s: str) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _metric_names(service, pid: str, candidates: list[str], probe: dict) -> list[str]:
    """Return the subset of `candidates` the property actually supports.

    GA4 renamed `conversions` -> `keyEvents` (2024-25) and some metrics are not
    available on every property. We probe once with a totals report and drop any
    metric the API rejects, so a single bad name never sinks the whole pull.
    """
    ok: list[str] = []
    for name in candidates:
        try:
            _run(service, pid, {**probe, "metrics": [{"name": name}], "limit": 1})
            ok.append(name)
        except Exception:  # noqa: BLE001 - metric not supported on this property
            continue
    return ok


# ── Pulls ─────────────────────────────────────────────────────────────────────

def pull_organic_kpis(service, pid: str, days: int = DEFAULT_DAYS) -> dict:
    """Organic-search KPIs for the last `days` days + the prior equal period,
    so the report can show week/period-over-period movement (the trend that
    flags a broad SEO regression)."""
    base_metrics = ["sessions", "engagedSessions", "engagementRate",
                    "averageSessionDuration", "totalUsers", "screenPageViews"]
    probe = {
        "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "yesterday"}],
        "dimensionFilter": _organic_filter(),
    }
    metrics = _metric_names(service, pid, base_metrics, probe)
    # conversions was renamed keyEvents; try the new name first, then the old.
    for conv in ("keyEvents", "conversions"):
        if _metric_names(service, pid, [conv], probe):
            metrics.append(conv)
            break

    def totals(start: str, end: str) -> dict:
        resp = _run(service, pid, {
            "dateRanges": [{"startDate": start, "endDate": end}],
            "dimensionFilter": _organic_filter(),
            "metrics": [{"name": m} for m in metrics],
        })
        rows = resp.get("rows", []) or []
        if not rows:
            return {m: 0.0 for m in metrics}
        vals = rows[0].get("metricValues", [])
        return {m: _num(vals[i].get("value")) for i, m in enumerate(metrics)}

    cur = totals(f"{days}daysAgo", "yesterday")
    prev = totals(f"{2 * days}daysAgo", f"{days + 1}daysAgo")

    def delta(key: str):
        c, p = cur.get(key, 0), prev.get(key, 0)
        if not p:
            return None
        return (c - p) / p

    conv_key = "keyEvents" if "keyEvents" in metrics else (
        "conversions" if "conversions" in metrics else None)
    return {
        "days": days,
        "metrics": metrics,
        "current": cur,
        "previous": prev,
        "sessions_delta": delta("sessions"),
        "conversions_delta": delta(conv_key) if conv_key else None,
        "conv_key": conv_key,
    }


def pull_organic_landing_pages(service, pid: str, days: int = DEFAULT_DAYS,
                               limit: int = 25) -> list[dict]:
    """Per-landing-page organic behavior — the table we join to GSC by path.

    `landingPage` returns the path (e.g. /melbourne-fl/...), which matches the
    path of GSC's full page URLs once normalized — that's the join key.
    """
    base_metrics = ["sessions", "engagedSessions", "engagementRate",
                    "averageSessionDuration"]
    probe = {
        "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "yesterday"}],
        "dimensions": [{"name": "landingPage"}],
        "dimensionFilter": _organic_filter(),
    }
    metrics = _metric_names(service, pid, base_metrics, probe)
    for conv in ("keyEvents", "conversions"):
        if _metric_names(service, pid, [conv], probe):
            metrics.append(conv)
            break

    resp = _run(service, pid, {
        "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "yesterday"}],
        "dimensions": [{"name": "landingPage"}],
        "dimensionFilter": _organic_filter(),
        "metrics": [{"name": m} for m in metrics],
        "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
        "limit": limit,
    })
    out = []
    for r in resp.get("rows", []) or []:
        dims = r.get("dimensionValues", [])
        vals = r.get("metricValues", [])
        path = dims[0].get("value") if dims else ""
        rec = {"landing_page": path}
        for i, m in enumerate(metrics):
            rec[m] = _num(vals[i].get("value")) if i < len(vals) else 0.0
        out.append(rec)
    return out


def pull_channel_mix(service, pid: str, days: int = DEFAULT_DAYS) -> list[dict]:
    """Sessions by default channel group — tracks organic's share of traffic."""
    resp = _run(service, pid, {
        "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "yesterday"}],
        "dimensions": [{"name": "sessionDefaultChannelGroup"}],
        "metrics": [{"name": "sessions"}],
        "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
        "limit": 15,
    })
    rows = resp.get("rows", []) or []
    total = sum(_num(r["metricValues"][0]["value"]) for r in rows) or 1.0
    return [
        {
            "channel": r["dimensionValues"][0]["value"],
            "sessions": _num(r["metricValues"][0]["value"]),
            "share": _num(r["metricValues"][0]["value"]) / total,
        }
        for r in rows
    ]


def pull_all(days: int = DEFAULT_DAYS, landing_limit: int = 25) -> dict:
    """One-shot convenience pull used by gsc_manager. Returns {} (with a `note`)
    if GA4 isn't configured yet, so the caller can degrade gracefully."""
    pid = property_id()
    if not pid:
        return {"configured": False,
                "note": "GA4_PROPERTY_ID not set — GA4 behavior data skipped. "
                        "Set the numeric property id + add the runner SA as a "
                        "GA4 property Viewer."}
    svc = _build_service()
    if svc is None:
        return {"configured": False,
                "note": "GA4 auth unavailable — enable analyticsdata.googleapis.com "
                        "and add the runner SA as a Viewer on the GA4 property."}
    try:
        return {
            "configured": True,
            "property_id": pid,
            "kpis": pull_organic_kpis(svc, pid, days=days),
            "landing_pages": pull_organic_landing_pages(svc, pid, days=days,
                                                        limit=landing_limit),
            "channels": pull_channel_mix(svc, pid, days=days),
        }
    except Exception as e:  # noqa: BLE001 - never block the report
        logger.error(f"  GA4 pull failed (continuing): {e}")
        return {"configured": False, "note": f"GA4 pull error: {str(e)[:160]}"}
