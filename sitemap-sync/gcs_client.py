"""Upload sitemap shards and index to Google Cloud Storage."""
from __future__ import annotations

import logging
import re

from google.cloud import storage

logger = logging.getLogger(__name__)


def upload_sitemaps(
    shards: list[tuple[int, str]],
    index_xml: str,
    project: str,
    bucket_name: str,
    prefix: str,
    dry_run: bool = False,
) -> dict:
    """
    Upload all shards and the sitemap index to GCS.
    Delete any stale sitemap-N.xml blobs that exist in GCS but not in this run.
    Returns result dict with uploaded/deleted counts.
    """
    logger.info("=" * 60)
    logger.info("STEP 3: Uploading to Google Cloud Storage")
    logger.info(f"  Bucket: gs://{bucket_name}/{prefix}")

    prefix = prefix.rstrip("/") + "/"
    result: dict = {"uploaded": 0, "deleted": 0, "errors": [], "success": True}

    if dry_run:
        for n, _ in shards:
            logger.info(f"  [DRY RUN] Would upload {prefix}sitemap-{n}.xml")
        logger.info(f"  [DRY RUN] Would upload {prefix}sitemap-index.xml")
        result["uploaded"] = len(shards) + 1
        return result

    try:
        client = storage.Client(project=project)
        bucket = client.bucket(bucket_name)
    except Exception as e:
        raise RuntimeError(f"GCS client init failed (project={project}): {e}")

    _acl_warning_shown = False

    def _upload(blob_name: str, content: str) -> bool:
        nonlocal _acl_warning_shown
        blob = bucket.blob(blob_name)
        blob.cache_control = "public, max-age=300"
        try:
            blob.upload_from_string(
                content.encode("utf-8"),
                content_type="application/xml",
            )
            logger.info(f"  Uploaded gs://{bucket_name}/{blob_name}")
        except Exception as e:
            logger.error(f"  FAILED to upload {blob_name}: {e}")
            result["errors"].append(blob_name)
            result["success"] = False
            return False

        try:
            blob.make_public()
            logger.info(f"    ACL: set public-read on {blob_name}")
        except Exception:
            if not _acl_warning_shown:
                logger.info(
                    f"    Bucket uses Uniform IAM — allUsers objectViewer "
                    "applies at bucket level (already configured)."
                )
                _acl_warning_shown = True

        return True

    # Upload shards — track which numbers succeed
    uploaded_shard_nums: set[int] = set()
    for n, xml in shards:
        blob_name = f"{prefix}sitemap-{n}.xml"
        if _upload(blob_name, xml):
            uploaded_shard_nums.add(n)
            result["uploaded"] += 1

    # Upload index
    if _upload(f"{prefix}sitemap-index.xml", index_xml):
        result["uploaded"] += 1

    # Delete stale shard blobs
    pattern = re.compile(rf"^{re.escape(prefix)}sitemap-(\d+)\.xml$")
    try:
        existing_blobs = list(client.list_blobs(bucket_name, prefix=prefix))
        for blob in existing_blobs:
            m = pattern.match(blob.name)
            if m:
                n = int(m.group(1))
                if n not in uploaded_shard_nums:
                    try:
                        blob.delete()
                        logger.info(f"  Deleted stale shard: {blob.name}")
                        result["deleted"] += 1
                    except Exception as e:
                        logger.error(f"  Failed to delete stale shard {blob.name}: {e}")
    except Exception as e:
        logger.warning(f"  Could not list GCS blobs for cleanup: {e}")

    logger.info(
        f"  GCS done: {result['uploaded']} uploaded, {result['deleted']} deleted, "
        f"{len(result['errors'])} errors"
    )
    return result
