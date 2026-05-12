#!/usr/bin/env python3
"""
sync-sitemaps.py
Sync paradiserealtyfla.com sitemap → GCS → RealGeeks redirects → GSC → Bing.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

import admin_scraper
import bing_client
import email_report
import gcs_client
import gsc_client
import realgeeks_client
import sitemap_builder


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Sync paradiserealtyfla.com sitemap to GCS, RealGeeks, GSC, and Bing."
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--source-url", metavar="URL", help="Override source sitemap URL")
    src.add_argument("--source-file", metavar="PATH", help="Read sitemap from local XML file")
    src.add_argument(
        "--admin-url",
        metavar="URL",
        help="Pull URLs from Django admin changelist (default: ADMIN_URL env var)",
    )
    src.add_argument(
        "--source-csv",
        metavar="PATH",
        help="Read URLs from a CSV file with a 'url' column (fastest — skips admin scrape)",
    )
    src.add_argument(
        "--source-gcs-csv",
        metavar="GCS_PATH",
        help="Download CSV from GCS then use as source (e.g. gs://bucket/path/file.csv)",
    )
    p.add_argument("--email-to", metavar="ADDRESS", help="Send summary report to this email via SendGrid")
    p.add_argument("--dry-run", action="store_true", help="Log actions without writing anything")
    p.add_argument("--skip-gcs", action="store_true")
    p.add_argument("--skip-redirects", action="store_true")
    p.add_argument("--skip-gsc", action="store_true")
    p.add_argument("--skip-bing", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging(log_dir: Path, verbose: bool) -> tuple[logging.Logger, Path]:
    log_dir.mkdir(exist_ok=True)
    ts = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d-%H%M%S")
    log_file = log_dir / f"run-{ts}.log"

    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-8s %(message)s"

    logging.basicConfig(
        level=level,
        format=fmt,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("sitemap_sync"), log_file


# ── Env helpers ───────────────────────────────────────────────────────────────

def require_env(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        print(f"❌  Missing required env var: {key}")
        print(f"    Add it to .env — see .env.example for reference.")
        sys.exit(1)
    return val


# ── Summary ───────────────────────────────────────────────────────────────────

def _fmt(d: dict | None, count_key: str, del_key: str) -> str:
    if d is None:
        return "SKIPPED"
    ok = "✅" if d.get("success", True) else "❌"
    return f"{d.get(count_key, 0)} (deleted: {d.get(del_key, 0)}) {ok}"


def print_summary(
    url_count: int,
    shard_count: int,
    gcs: dict | None,
    redirects: dict | None,
    gsc: dict | None,
    bing: dict | None,
    runtime: float,
    log_file: Path,
    dry_run: bool,
) -> bool:
    mode = "  [DRY RUN]" if dry_run else ""
    W = 62
    print()
    print("=" * W)
    print(f"  Sitemap Sync Summary{mode}")
    print("=" * W)
    print(f"  {'URLs collected:':<22} {url_count:,}")
    print(f"  {'Shards generated:':<22} {shard_count}")
    print(f"  {'GCS uploaded:':<22} {_fmt(gcs, 'uploaded', 'deleted')}")
    print(f"  {'Redirects upserted:':<22} {_fmt(redirects, 'upserted', 'deleted')}")
    print(f"  {'GSC submitted:':<22} {_fmt(gsc, 'submitted', 'deleted')}")
    print(f"  {'Bing submitted:':<22} {_fmt(bing, 'submitted', 'deleted')}")
    print(f"  {'Total runtime:':<22} {runtime:.1f}s")
    print(f"  {'Log file:':<22} {log_file}")
    print("=" * W)

    failed_steps = [
        name
        for name, d in [("GCS", gcs), ("RealGeeks", redirects), ("GSC", gsc), ("Bing", bing)]
        if d is not None and not d.get("success", True)
    ]
    if failed_steps:
        print(f"❌  Failed steps: {', '.join(failed_steps)}")
        print()
        return False
    else:
        print("✅  All steps completed successfully")
        print()
        return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    args = parse_args()
    load_dotenv()

    log_dir = Path(__file__).parent / "logs"
    logger, log_file = setup_logging(log_dir, args.verbose)

    start = time.time()

    logger.info("=" * 60)
    logger.info("Paradise Realty — Sitemap Sync Pipeline")
    logger.info(f"  Mode:  {'DRY RUN — no writes' if args.dry_run else 'LIVE'}")
    logger.info(f"  Start: {datetime.now(ZoneInfo('America/New_York')).isoformat()}")
    logger.info("=" * 60)

    # ── Config ────────────────────────────────────────────────────────────────
    admin_url     = args.admin_url or os.getenv("ADMIN_URL", "")
    source_url    = args.source_url or os.getenv("SOURCE_SITEMAP_URL", "")
    gcs_project   = require_env("GCS_PROJECT")
    gcs_bucket    = require_env("GCS_BUCKET")
    gcs_prefix    = os.getenv("GCS_PREFIX", "sitemaps/")
    site_url      = require_env("SITE_URL")
    redirect_base = os.getenv("REDIRECT_BASE_URL", site_url)

    # ── GCS CSV download (Cloud Run path) ────────────────────────────────────
    source_csv_path = args.source_csv
    if args.source_gcs_csv:
        import tempfile
        from google.cloud import storage as _gcs
        gcs_uri = args.source_gcs_csv.replace("gs://", "")
        bucket_part, blob_part = gcs_uri.split("/", 1)
        logger.info(f"Downloading CSV from gs://{bucket_part}/{blob_part}")
        _client = _gcs.Client(project=os.getenv("GCS_PROJECT"))
        _tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        _client.bucket(bucket_part).blob(blob_part).download_to_filename(_tmp.name)
        source_csv_path = _tmp.name
        logger.info(f"  Downloaded to {source_csv_path}")

    # ── Steps 1 + 2: Fetch → Shard ────────────────────────────────────────────
    try:
        if source_csv_path:
            urls = sitemap_builder.collect_urls_from_csv(source_csv_path)
        elif admin_url and not args.source_url and not args.source_file:
            rg_login_url_step1 = require_env("REALGEEKS_LOGIN_URL")
            rg_user_step1      = require_env("REALGEEKS_USER")
            rg_pass_step1      = require_env("REALGEEKS_PASS")
            urls = admin_scraper.collect_urls_from_admin(
                admin_url=admin_url,
                site_url=site_url,
                login_url=rg_login_url_step1,
                username=rg_user_step1,
                password=rg_pass_step1,
            )
        else:
            effective_url = source_url or "http://paradiserealtyfla.com/sitemap.xml"
            urls = sitemap_builder.collect_urls(
                source_url=None if args.source_file else effective_url,
                source_file=args.source_file,
            )
        shards = sitemap_builder.build_shards(urls)
        index_xml = sitemap_builder.build_index(shards, site_url)
    except Exception as e:
        logger.error(f"FATAL — sitemap fetch/shard step failed: {e}")
        return 1

    logger.info(f"  {len(urls):,} URLs → {len(shards)} shard(s)")

    # ── Step 3: GCS ───────────────────────────────────────────────────────────
    gcs_result: dict | None = None
    if not args.skip_gcs:
        try:
            gcs_result = gcs_client.upload_sitemaps(
                shards=shards,
                index_xml=index_xml,
                project=gcs_project,
                bucket_name=gcs_bucket,
                prefix=gcs_prefix,
                dry_run=args.dry_run,
            )
        except Exception as e:
            logger.error(f"GCS step FAILED: {e}")
            gcs_result = {"success": False, "uploaded": 0, "deleted": 0, "errors": [str(e)]}

    # Only forward shards that uploaded successfully
    good_shards = shards
    if gcs_result and not args.dry_run and gcs_result.get("errors"):
        prefix_norm = gcs_prefix.rstrip("/") + "/"
        failed_blobs = set(gcs_result["errors"])
        good_shards = [
            (n, xml)
            for n, xml in shards
            if f"{prefix_norm}sitemap-{n}.xml" not in failed_blobs
        ]
        skipped = len(shards) - len(good_shards)
        if skipped:
            logger.warning(
                f"  {skipped} shard(s) failed GCS upload — "
                "those shards will be excluded from GSC/Bing submissions."
            )

    # ── Step 4: RealGeeks ─────────────────────────────────────────────────────
    rg_result: dict | None = None
    if not args.skip_redirects:
        rg_login_url    = require_env("REALGEEKS_LOGIN_URL")
        rg_user         = require_env("REALGEEKS_USER")
        rg_pass         = require_env("REALGEEKS_PASS")
        rg_redirects_url = require_env("REALGEEKS_REDIRECTS_URL")

        try:
            rg_result = realgeeks_client.sync_redirects(
                shards=good_shards,
                gcs_bucket=gcs_bucket,
                gcs_prefix=gcs_prefix,
                login_url=rg_login_url,
                username=rg_user,
                password=rg_pass,
                redirects_url=rg_redirects_url,
                redirect_base=redirect_base,
                log_dir=log_dir,
                dry_run=args.dry_run,
            )
        except realgeeks_client.RealGeeksAuthError as e:
            logger.error(f"❌  RealGeeks LOGIN FAILED: {e}")
            logger.error(
                "    Aborting pipeline — without working redirects, "
                "GSC/Bing submissions would point to 404 URLs."
            )
            runtime = time.time() - start
            print_summary(
                len(urls), len(shards), gcs_result, None, None, None, runtime, log_file, args.dry_run
            )
            return 1
        except Exception as e:
            logger.error(f"RealGeeks step FAILED: {e}")
            rg_result = {"success": False, "upserted": 0, "deleted": 0, "errors": [str(e)]}

    # ── Step 5: Google Search Console ─────────────────────────────────────────
    gsc_result: dict | None = None
    if not args.skip_gsc:
        try:
            gsc_result = gsc_client.submit_sitemaps(
                shards=good_shards,
                site_url=site_url,
                redirect_base=redirect_base,
                dry_run=args.dry_run,
            )
        except Exception as e:
            logger.error(f"GSC step FAILED: {e}")
            gsc_result = {"success": False, "submitted": 0, "deleted": 0, "errors": [str(e)]}

    # ── Step 6: Bing ──────────────────────────────────────────────────────────
    bing_result: dict | None = None
    if not args.skip_bing:
        bing_key = require_env("BING_API_KEY")
        try:
            bing_result = bing_client.submit_sitemaps(
                shards=good_shards,
                api_key=bing_key,
                site_url=site_url,
                redirect_base=redirect_base,
                dry_run=args.dry_run,
            )
        except Exception as e:
            logger.error(f"Bing step FAILED: {e}")
            bing_result = {"success": False, "submitted": 0, "deleted": 0, "errors": [str(e)]}

    # ── Summary ───────────────────────────────────────────────────────────────
    runtime = time.time() - start
    logger.info(f"Total runtime: {runtime:.1f}s | log: {log_file}")

    success = print_summary(
        url_count=len(urls),
        shard_count=len(shards),
        gcs=gcs_result,
        redirects=rg_result,
        gsc=gsc_result,
        bing=bing_result,
        runtime=runtime,
        log_file=log_file,
        dry_run=args.dry_run,
    )

    # ── Email report ──────────────────────────────────────────────────────────
    email_to = args.email_to or os.getenv("REPORT_EMAIL_TO", "")
    sendgrid_key = os.getenv("SENDGRID_API_KEY", "")
    if email_to and sendgrid_key and not args.dry_run:
        email_report.send_report(
            to_email=email_to,
            sendgrid_key=sendgrid_key,
            url_count=len(urls),
            shard_count=len(shards),
            gcs=gcs_result,
            redirects=rg_result,
            gsc=gsc_result,
            bing=bing_result,
            runtime=runtime,
            success=success,
        )

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
