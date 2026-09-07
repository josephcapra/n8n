"""
LOCAL setup helper for the daily website report's area-page tracking.

Run from this machine (where the repo + ADC live). It:
  1. Extracts the live canonical URL of every area/community page in the repo
     (county dirs, excluding *-beta staging), plus county landing pages.
  2. Fetches each live page once to build a baseline fingerprint snapshot.
  3. Writes both the tracked-URL list and the baseline snapshot to GCS so the
     first scheduled 5 AM run has something to diff against.

Re-run this whenever you add new area pages in RealGeeks / the repo to expand
the tracked set.

    python3 sitemap-sync/seed_area_tracking.py
"""
from __future__ import annotations

import logging
import os
import sys
from collections import Counter

import area_changes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("seed")

REPO_DIR = os.environ.get("AREA_REPO_DIR", "/Users/User/paradise-realty-beta")
PROJECT = os.environ.get("GCS_PROJECT", "paradise-automation")
TRACKED_GCS = os.environ.get(
    "AREA_TRACKED_URLS_GCS",
    "gs://site-map-dynamic-page3/area-report/area-tracked-urls.txt",
)
SNAPSHOT_GCS = os.environ.get(
    "AREA_SNAPSHOT_GCS",
    "gs://site-map-dynamic-page3/area-report/area-snapshot-latest.json",
)


def main() -> int:
    urls = area_changes.build_tracked_urls_from_repo(REPO_DIR)
    log.info(f"Extracted {len(urls):,} canonical area-page URLs from {REPO_DIR}")
    if not urls:
        log.error("No URLs found — check AREA_REPO_DIR / canonical tags.")
        return 1

    log.info("Building baseline snapshot (fetching live pages)...")
    snap = area_changes.build_snapshot(urls, workers=int(os.environ.get("AREA_FETCH_WORKERS", "25")))

    statuses = Counter(p.get("status") for p in snap["pages"].values())
    log.info(f"Status distribution: {dict(statuses)}")
    offsite = [u for u, p in snap["pages"].items() if p.get("redirected_offsite")]
    if offsite:
        log.warning(f"{len(offsite)} page(s) redirect off-site, e.g. {offsite[:3]}")

    area_changes.save_tracked_urls(urls, TRACKED_GCS, PROJECT)
    area_changes.save_snapshot(snap, SNAPSHOT_GCS, PROJECT, keep_history=True)
    log.info("✅ Seed complete. First 5 AM run will diff against this baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
