"""Submit sitemaps to Google Search Console via the Search Console API."""
from __future__ import annotations

import json
import logging
import os
import re
import time

import google.auth
import google.auth.exceptions
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/webmasters"


def _build_service():
    # Prefer stored OAuth credentials (joe@josephcapra.com) from Secret Manager.
    # GSC does not accept service accounts as property users, so we use the
    # verified owner's refresh token stored in GSC_OAUTH_CREDS env var.
    oauth_json = os.getenv("GSC_OAUTH_CREDS", "").strip()
    if oauth_json:
        from google.oauth2.credentials import Credentials
        info = json.loads(oauth_json)
        creds = Credentials(
            token=None,
            refresh_token=info["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=info["client_id"],
            client_secret=info["client_secret"],
            quota_project_id=info.get("quota_project_id", "paradise-automation"),
        )
        return build("searchconsole", "v1", credentials=creds)

    try:
        creds, _ = google.auth.default(scopes=[SCOPE])
    except google.auth.exceptions.DefaultCredentialsError as e:
        raise RuntimeError(
            f"Google auth failed: {e}\n"
            "Fix: gcloud auth application-default login "
            "--scopes=https://www.googleapis.com/auth/webmasters"
        )
    return build("searchconsole", "v1", credentials=creds)


def _submit_one(service, site_url: str, feedpath: str, result: dict) -> bool:
    delays = [1, 2, 4]
    for attempt in range(4):
        try:
            service.sitemaps().submit(siteUrl=site_url, feedpath=feedpath).execute()
            logger.info(f"  Submitted: {feedpath}")

            # Confirm acceptance
            try:
                info = service.sitemaps().get(siteUrl=site_url, feedpath=feedpath).execute()
                logger.info(
                    f"    GSC status: isPending={info.get('isPending')}, "
                    f"lastSubmitted={info.get('lastSubmitted')}, "
                    f"warnings={info.get('warnings', [])}, "
                    f"errors={info.get('errors', [])}"
                )
            except Exception as e:
                logger.warning(f"    Could not fetch GSC status for {feedpath}: {e}")

            return True

        except HttpError as e:
            if e.resp.status in (401, 403):
                raise RuntimeError(
                    f"GSC auth error ({e.resp.status}) for {feedpath}: {e}\n"
                    "Check that the site is verified in Search Console and "
                    "ADC credentials include the webmasters scope."
                )
            if attempt < 3:
                wait = delays[attempt]
                logger.warning(
                    f"  GSC submit attempt {attempt + 1}/4 failed ({e.resp.status}): {e}. "
                    f"Retry in {wait}s"
                )
                time.sleep(wait)
            else:
                logger.error(f"  GSC submit FAILED for {feedpath}: {e}")
                result["errors"].append(feedpath)
                result["success"] = False
                return False
        except Exception as e:
            logger.error(f"  GSC submit unexpected error for {feedpath}: {e}")
            result["errors"].append(feedpath)
            result["success"] = False
            return False


def submit_sitemaps(
    shards: list[tuple[int, str]],
    site_url: str,
    redirect_base: str,
    dry_run: bool = False,
) -> dict:
    """
    Submit all shards + index feedpaths to GSC.
    Deletes any stale /sitemapN/ feedpaths already registered in GSC.
    """
    logger.info("=" * 60)
    logger.info("STEP 5: Google Search Console submission")
    logger.info(f"  Site: {site_url}")

    base = redirect_base.rstrip("/")
    this_run: set[str] = {f"{base}/sitemap{n}/" for n, _ in shards}
    this_run.add(f"{base}/sitemap-index/")

    result: dict = {"submitted": 0, "deleted": 0, "errors": [], "success": True}

    if dry_run:
        for fp in sorted(this_run):
            logger.info(f"  [DRY RUN] Would submit: {fp}")
        result["submitted"] = len(this_run)
        return result

    service = _build_service()

    submitted: set[str] = set()
    for n, _ in shards:
        fp = f"{base}/sitemap{n}/"
        if _submit_one(service, site_url, fp, result):
            submitted.add(fp)
            result["submitted"] += 1

    index_fp = f"{base}/sitemap-index/"
    if _submit_one(service, site_url, index_fp, result):
        submitted.add(index_fp)
        result["submitted"] += 1

    # Cleanup stale /sitemapN/ entries
    sitemap_pattern = re.compile(r"/sitemap\d+/$")
    try:
        resp = service.sitemaps().list(siteUrl=site_url).execute()
        for sm in resp.get("sitemap", []):
            path = sm.get("path", "")
            if sitemap_pattern.search(path) and path not in this_run:
                try:
                    service.sitemaps().delete(siteUrl=site_url, feedpath=path).execute()
                    logger.info(f"  Deleted stale GSC sitemap: {path}")
                    result["deleted"] += 1
                except Exception as e:
                    logger.error(f"  Failed to delete GSC sitemap {path}: {e}")
    except Exception as e:
        logger.warning(f"  Could not list GSC sitemaps for cleanup: {e}")

    logger.info(
        f"  GSC done: {result['submitted']} submitted, {result['deleted']} deleted, "
        f"{len(result['errors'])} errors"
    )
    return result
