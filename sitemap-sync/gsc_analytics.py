"""
Google Search Console analytics pulls for the daily SEO brief.

Complements bing_manager.py's Bing pulls. Reuses gsc_client._build_service()
for authentication (OAuth refresh token in Secret Manager).

The Search Analytics API returns up to 25,000 rows per request and is the only
practical way to get per-query / per-page performance without scraping the UI.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Iterable

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 30
# GSC has a ~2-3 day data lag — most-recent days are partial.
RECENT_LAG_DAYS = 3
PAGE_PAGE_SIZE = 25000  # max rowLimit per request


def _date_range(days: int) -> tuple[str, str]:
    end = date.today() - timedelta(days=RECENT_LAG_DAYS)
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def _query(service, site_url: str, body: dict) -> dict:
    return service.searchanalytics().query(siteUrl=site_url, body=body).execute()


def pull_traffic(service, site_url: str, days: int = DEFAULT_DAYS) -> dict:
    """Aggregate totals + per-day series for the last `days` days (lagged)."""
    start, end = _date_range(days)
    by_day = _query(service, site_url, {
        "startDate": start, "endDate": end,
        "dimensions": ["date"],
        "rowLimit": days,
    })
    rows = by_day.get("rows", []) or []
    rows.sort(key=lambda r: r["keys"][0])

    total_impr = sum(r["impressions"] for r in rows)
    total_clicks = sum(r["clicks"] for r in rows)
    avg_pos = (
        sum(r["position"] * r["impressions"] for r in rows) / total_impr
        if total_impr else None
    )
    return {
        "start": start,
        "end": end,
        "days": days,
        "impressions": total_impr,
        "clicks": total_clicks,
        "ctr": (total_clicks / total_impr) if total_impr else 0,
        "avg_position": avg_pos,
        "latest_day": rows[-1] if rows else None,
        "prev_day": rows[-2] if len(rows) >= 2 else None,
    }


def pull_top_queries(service, site_url: str, days: int = DEFAULT_DAYS, limit: int = 20) -> list[dict]:
    start, end = _date_range(days)
    resp = _query(service, site_url, {
        "startDate": start, "endDate": end,
        "dimensions": ["query"],
        "rowLimit": limit,
    })
    return [
        {
            "query": r["keys"][0],
            "impressions": r["impressions"],
            "clicks": r["clicks"],
            "ctr": r.get("ctr", 0),
            "position": r.get("position", 0),
        }
        for r in resp.get("rows", []) or []
    ]


def pull_top_pages(service, site_url: str, days: int = DEFAULT_DAYS, limit: int = 20) -> list[dict]:
    start, end = _date_range(days)
    resp = _query(service, site_url, {
        "startDate": start, "endDate": end,
        "dimensions": ["page"],
        "rowLimit": limit,
    })
    return [
        {
            "page": r["keys"][0],
            "impressions": r["impressions"],
            "clicks": r["clicks"],
            "position": r.get("position", 0),
        }
        for r in resp.get("rows", []) or []
    ]


def find_opportunity_queries(
    service, site_url: str, days: int = DEFAULT_DAYS,
    min_impr: int = 5, pos_lo: float = 4.0, pos_hi: float = 15.0, limit: int = 25,
) -> list[dict]:
    """Queries ranking pos 4-15 with traction — quick wins for on-page tweaks."""
    start, end = _date_range(days)
    resp = _query(service, site_url, {
        "startDate": start, "endDate": end,
        "dimensions": ["query"],
        "rowLimit": 5000,
    })
    rows = resp.get("rows", []) or []
    out = [
        {
            "query": r["keys"][0],
            "impressions": r["impressions"],
            "clicks": r["clicks"],
            "position": r.get("position", 0),
        }
        for r in rows
        if r["impressions"] >= min_impr
        and pos_lo <= r.get("position", 0) <= pos_hi
    ]
    out.sort(key=lambda r: -r["impressions"])
    return out[:limit]


def count_area_pages(
    service, site_url: str, area_pattern: str, days: int = 90,
) -> dict:
    """
    Count pages matching `area_pattern` that Google has shown to users in the
    last `days` days. This is the best API-available proxy for "indexed area
    pages" — pages that get zero impressions over a 90-day window are either
    unindexed or unranked-for-anything (functionally invisible either way).

    Returns:
      {
        "total_pages_with_impressions": int,  # all pages with any impressions
        "area_pages_with_impressions": int,   # subset matching the pattern
        "area_total_impressions": int,
        "area_total_clicks": int,
        "top_area_pages": [...]               # top 20 area pages by impressions
      }
    """
    start, end = _date_range(days)
    pattern = re.compile(area_pattern)

    all_rows: list[dict] = []
    start_row = 0
    while True:
        resp = _query(service, site_url, {
            "startDate": start, "endDate": end,
            "dimensions": ["page"],
            "rowLimit": PAGE_PAGE_SIZE,
            "startRow": start_row,
        })
        rows = resp.get("rows", []) or []
        all_rows.extend(rows)
        if len(rows) < PAGE_PAGE_SIZE:
            break
        start_row += PAGE_PAGE_SIZE
        if start_row >= 100_000:  # safety cap
            logger.warning(f"  area-page counter: hit 100k row cap")
            break

    area_rows = [
        {
            "page": r["keys"][0],
            "impressions": r["impressions"],
            "clicks": r["clicks"],
            "position": r.get("position", 0),
        }
        for r in all_rows
        if pattern.search(r["keys"][0])
    ]
    area_rows.sort(key=lambda r: -r["impressions"])

    return {
        "window_days": days,
        "start": start, "end": end,
        "total_pages_with_impressions": len(all_rows),
        "area_pages_with_impressions": len(area_rows),
        "area_total_impressions": sum(r["impressions"] for r in area_rows),
        "area_total_clicks": sum(r["clicks"] for r in area_rows),
        "top_area_pages": area_rows[:20],
    }
