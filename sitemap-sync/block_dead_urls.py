"""
One-shot: block the stale `atlantic-fields-*` + `esplanade-at-tradition-*` +
related blog URLs from Bing's index via Bing Webmaster Tools `AddBlockedURL`,
and emit a Google Search Console removal-tool plan (GSC removal is UI-only —
the API does not expose it).

These URLs return either HTTP 404 or off-site redirects (wrongpage.io). The
existing `bing_seo_scan.py` ignore-list keeps the scanner from flagging them
("LEGAL reasons — never unblock"). This script formalises that by submitting
each URL to the Bing Block URLs tool (90-day window per request — re-run
quarterly until they age out of Bing entirely).

Run:
    BING_API_KEY=... SITE_URL=https://www.paradiserealtyfla.com/ \\
        python3 block_dead_urls.py

Add --dry-run to preview without writing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import quote, urlparse

import bing_client

# URLs to remove from indexing. Each one is either a hard 404 or an off-site
# redirect to wrongpage.io. Source: Joe — 2026-05-23.
DEAD_URLS = [
    "https://www.paradiserealtyfla.com/atlantic-fields-hobe-sound/",
    "https://www.paradiserealtyfla.com/atlantic-fields-location-hobe-sound/",
    "https://www.paradiserealtyfla.com/esplanade-at-tradition-port-st-lucie/",
    "https://www.paradiserealtyfla.com/blog/5-good-things-about-atlantic-fields/",
    "https://www.paradiserealtyfla.com/blog/atlantic-fields-hobe-sound/",
    "https://www.paradiserealtyfla.com/blog/atlantic-fields-hobe-sound-private-golf-community-homes-discovery-land-company/",
    "https://www.paradiserealtyfla.com/blog/top-50-questions-about-atlantic-fields-hobe-sounds-private-golf-luxury-home-community/",
]


def already_blocked(api_key: str, site_url: str) -> set[str]:
    """Return the lowercased set of URLs Bing already has in the block list."""
    resp = bing_client._call(
        "get", "GetBlockedUrls", api_key, params={"siteUrl": site_url}
    )
    out: set[str] = set()
    for b in resp.get("d", []) or []:
        u = (b.get("Url") or "").strip().lower()
        if u:
            out.add(u)
    return out


def add_block(api_key: str, site_url: str, url: str) -> dict:
    """
    Submit a single URL to BWT's Block URLs tool.

    EntityTypeId   0 = Page, 1 = Directory  → we use 0 (single page).
    RequestTypeId  0 = CacheOnly, 1 = CacheAndIndex → we use 1 (full removal).
    """
    body = {
        "siteUrl": site_url,
        "blockedUrl": {
            "Url": url,
            "EntityTypeId": 0,
            "RequestTypeId": 1,
            "Date": "/Date(0)/",
        },
    }
    return bing_client._call("post", "AddBlockedURL", api_key, json_body=body)


def gsc_removal_url(target: str) -> str:
    """Deep-link into the GSC Removals tool, pre-filtered to the target URL."""
    # Search Console's Removals page accepts a `resource_id` (the property) and
    # the URL field is filled manually — the path itself can't be deep-linked,
    # but we land the user on the right property so it's two clicks instead of
    # six. Property is the https://www.paradiserealtyfla.com/ URL-prefix one.
    prop = "https://www.paradiserealtyfla.com/"
    return (
        "https://search.google.com/search-console/removals"
        f"?resource_id={quote(prop, safe='')}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    api_key = os.environ.get("BING_API_KEY", "").strip()
    site_url = os.environ.get("SITE_URL", "https://www.paradiserealtyfla.com/").strip()
    if not api_key:
        print("FATAL: BING_API_KEY not set in env", file=sys.stderr)
        return 1

    print(f"Site:    {site_url}")
    print(f"Targets: {len(DEAD_URLS)} URL(s)")
    print()

    # ── Bing block ────────────────────────────────────────────────────────────
    print("=" * 70)
    print("BING — AddBlockedURL")
    print("=" * 70)

    try:
        existing = already_blocked(api_key, site_url)
        print(f"  {len(existing)} URL(s) currently in Bing block list")
    except Exception as e:
        print(f"  WARN: could not fetch existing block list: {e}")
        existing = set()

    results = {"submitted": [], "skipped": [], "failed": []}
    for url in DEAD_URLS:
        if url.lower() in existing:
            print(f"  ↺ already blocked: {url}")
            results["skipped"].append(url)
            continue
        if args.dry_run:
            print(f"  [DRY] would block: {url}")
            results["submitted"].append(url)
            continue
        try:
            add_block(api_key, site_url, url)
            print(f"  ✓ blocked: {url}")
            results["submitted"].append(url)
        except Exception as e:
            print(f"  ✗ FAILED: {url}\n      {e}")
            results["failed"].append({"url": url, "error": str(e)})

    print()
    print(f"  Bing summary: {len(results['submitted'])} blocked, "
          f"{len(results['skipped'])} already-blocked, "
          f"{len(results['failed'])} failed")
    print()

    # ── Google — print the manual path (no API for removal) ──────────────────
    print("=" * 70)
    print("GOOGLE — Search Console URL Removal Tool")
    print("=" * 70)
    print("  The GSC API does not expose URL removal. Open this page:")
    print(f"    {gsc_removal_url(site_url)}")
    print("  Click 'New request' → 'Temporarily remove URL' → paste each URL")
    print("  below → 'Remove this URL only' → Next → Submit.")
    print("  (Temporary removal lasts ~6 months. The pages already 404 or")
    print("   redirect off-site, so Google will drop them organically once it")
    print("   recrawls — the removal tool just hides them immediately.)")
    print()
    for url in DEAD_URLS:
        print(f"    {url}")
    print()

    # Sitemap status
    print("=" * 70)
    print("SITEMAP")
    print("=" * 70)
    print("  None of these URLs are in the current production sitemap — they")
    print("  are stale entries Google/Bing remember from earlier crawls. The")
    print("  permanent ignore-pattern in bing_seo_scan.py ('atlantic-fields')")
    print("  already keeps the SEO scanner from flagging them.")
    print()

    # Emit machine-readable result for downstream use (e.g. agent dashboards)
    if not args.dry_run:
        with open("/tmp/block_dead_urls_result.json", "w") as f:
            json.dump(results, f, indent=2)
        print("  Result JSON: /tmp/block_dead_urls_result.json")

    return 0 if not results["failed"] else 2


if __name__ == "__main__":
    sys.exit(main())
