"""Submit sitemaps to Bing Webmaster Tools API."""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

BASE = "https://ssl.bing.com/webmaster/api.svc/json"
HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "User-Agent": "ParadiseRealty-SitemapSync/1.0",
}


def _call(
    method: str,
    endpoint: str,
    api_key: str,
    params: dict | None = None,
    json_body: dict | None = None,
) -> dict:
    """Make an authenticated Bing Webmaster API call with retry."""
    url = f"{BASE}/{endpoint}"
    query = {"apikey": api_key}
    if params:
        query.update(params)

    delays = [1, 2, 4]
    for attempt in range(4):
        try:
            if method == "get":
                resp = requests.get(url, params=query, headers=HEADERS, timeout=30)
            else:
                resp = requests.post(url, params=query, json=json_body, headers=HEADERS, timeout=30)

            if resp.status_code == 401:
                raise RuntimeError(
                    f"Bing API 401 Unauthorized — BING_API_KEY is wrong or the site "
                    f"is not registered. Endpoint: {endpoint}"
                )
            resp.raise_for_status()
            return resp.json() if resp.content else {}

        except RuntimeError:
            raise  # Auth failures — do not retry
        except requests.RequestException as e:
            if attempt < 3:
                wait = delays[attempt]
                logger.warning(
                    f"  Bing {endpoint} attempt {attempt + 1}/4 failed: {e}. Retry in {wait}s"
                )
                time.sleep(wait)
            else:
                raise RuntimeError(
                    f"Bing {endpoint} failed after 4 attempts: {e}"
                ) from e


def submit_sitemaps(
    shards: list[tuple[int, str]],
    api_key: str,
    site_url: str,
    redirect_base: str,
    dry_run: bool = False,
) -> dict:
    """
    Submit all shards + index to Bing Webmaster Tools.
    Removes stale /sitemapN/ feeds that are registered but not in this run.
    """
    logger.info("=" * 60)
    logger.info("STEP 6: Bing Webmaster Tools submission")
    logger.info(f"  Site: {site_url}")

    base = redirect_base.rstrip("/")
    this_run: set[str] = {f"{base}/sitemap{n}/" for n, _ in shards}
    this_run.add(f"{base}/sitemap-index/")

    result: dict = {"submitted": 0, "deleted": 0, "errors": [], "success": True}

    if dry_run:
        for feed in sorted(this_run):
            logger.info(f"  [DRY RUN] Would submit: {feed}")
        result["submitted"] = len(this_run)
        return result

    # Submit each feed
    submitted: set[str] = set()
    for feed_url in sorted(this_run):
        try:
            _call(
                "post",
                "SubmitFeed",
                api_key,
                json_body={"siteUrl": site_url, "feedUrl": feed_url},
            )
            logger.info(f"  Submitted: {feed_url}")
            submitted.add(feed_url)
            result["submitted"] += 1
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"  Bing submit FAILED for {feed_url}: {e}")
            result["errors"].append(feed_url)
            result["success"] = False

    # Confirm via GetFeeds and cleanup stale entries
    try:
        resp = _call(
            "get",
            "GetFeeds",
            api_key,
            params={"siteUrl": site_url},
        )
        # API returns {"d": [...]} or {"Feeds": [...]}
        feeds = resp.get("d") or resp.get("Feeds") or []
        logger.info(f"  GetFeeds: {len(feeds)} feed(s) currently registered")
        for f in feeds:
            logger.info(
                f"    feed: {f.get('FeedUrl') or f.get('feedUrl')} "
                f"active={f.get('IsActive') or f.get('isActive')}"
            )

        sitemap_pattern = re.compile(r"/sitemap\d+/$")
        for f in feeds:
            feed_url = f.get("FeedUrl") or f.get("feedUrl", "")
            if sitemap_pattern.search(feed_url) and feed_url not in this_run:
                try:
                    _call(
                        "post",
                        "RemoveFeed",
                        api_key,
                        json_body={"siteUrl": site_url, "feedUrl": feed_url},
                    )
                    logger.info(f"  Removed stale Bing feed: {feed_url}")
                    result["deleted"] += 1
                except Exception as e:
                    logger.error(f"  Failed to remove Bing feed {feed_url}: {e}")

    except RuntimeError:
        raise
    except Exception as e:
        logger.warning(f"  Could not confirm Bing feeds (GetFeeds failed): {e}")

    logger.info(
        f"  Bing done: {result['submitted']} submitted, {result['deleted']} deleted, "
        f"{len(result['errors'])} errors"
    )
    return result
