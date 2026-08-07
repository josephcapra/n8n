"""Fetch, parse, shard, and generate sitemap XML files."""
from __future__ import annotations

import csv
import html as _html
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ParadiseRealty-SitemapSync/1.0)"}
MAX_URLS = 40_000
MAX_BYTES = 50 * 1024 * 1024  # 50 MB

# Stale URLs that must never appear in our sitemaps. The root-level
# /atlantic-fields-* pages 301 → wrongpage.io for legal reasons (see
# bing_seo_scan.py); the matching blog posts and the Esplanade 404 are dead
# pages that Google/Bing still remember from earlier crawls. Filter them out
# of any URL list before sharding so they can never reappear, even if a stale
# source (RG admin scraper, CSV, or sheet) reintroduces them.
#
# Use path-substring matching so future variants of the same content
# (e.g. /atlantic-fields-foo/, /blog/atlantic-fields-bar/) are filtered too.
# This is intentionally broad — paths under /martin-county/atlantic-fields-*
# are the canonical replacements and are unaffected (the bad paths are all
# root-level or under /blog/).
BLOCKED_PATH_PREFIXES = (
    "/atlantic-fields-",
    "/esplanade-at-tradition-port-st-lucie",
)
BLOCKED_PATH_SUBSTRINGS = (
    "/blog/5-good-things-about-atlantic-fields",
    "/blog/atlantic-fields-",
    "/blog/top-50-questions-about-atlantic-fields",
)


def _is_blocked(url: str) -> bool:
    """True if `url`'s path is on the permanent deny-list."""
    from urllib.parse import urlparse
    path = (urlparse(url).path or "").lower().rstrip("/") + "/"
    if any(path.startswith(p) for p in BLOCKED_PATH_PREFIXES):
        return True
    if any(s in path for s in BLOCKED_PATH_SUBSTRINGS):
        return True
    return False


def _drop_blocked(urls: list[str]) -> list[str]:
    """Filter out deny-listed URLs, logging each drop once."""
    kept: list[str] = []
    dropped = 0
    for u in urls:
        if _is_blocked(u):
            logger.info(f"  Dropping deny-listed URL: {u}")
            dropped += 1
        else:
            kept.append(u)
    if dropped:
        logger.info(f"  Deny-list filtered {dropped} URL(s)")
    return kept


def _fetch(url: str, max_attempts: int = 4) -> bytes:
    delays = [1, 2, 4]
    for attempt in range(max_attempts):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.content
        except requests.RequestException as e:
            if attempt == max_attempts - 1:
                raise RuntimeError(f"Failed to fetch {url} after {max_attempts} attempts: {e}")
            wait = delays[min(attempt, len(delays) - 1)]
            logger.warning(f"  Fetch attempt {attempt + 1}/{max_attempts} failed: {e}. Waiting {wait}s")
            time.sleep(wait)


def _local_tag(elem) -> str:
    tag = elem.tag
    return tag.split("}")[-1] if "}" in tag else tag


def _parse(content: bytes, label: str) -> tuple[str, list[str]]:
    """Parse sitemap XML. Returns (root_kind, list_of_locs)."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise ValueError(f"XML parse error in {label}: {e}")

    kind = _local_tag(root)
    container = "sitemap" if kind == "sitemapindex" else "url"
    locs = []
    for child in root:
        if _local_tag(child) == container:
            for elem in child:
                if _local_tag(elem) == "loc" and elem.text and elem.text.strip():
                    locs.append(elem.text.strip())

    return kind, locs


def collect_urls(
    source_url: Optional[str] = None,
    source_file: Optional[str] = None,
) -> list[str]:
    """Fetch sitemap and return deduplicated, ordered list of page URLs."""
    logger.info("=" * 60)
    logger.info("STEP 1: Fetching source sitemap")

    if source_file:
        logger.info(f"  Source: file {source_file}")
        content = Path(source_file).read_bytes()
        label = source_file
    else:
        logger.info(f"  Source URL: {source_url}")
        content = _fetch(source_url)
        label = source_url

    kind, items = _parse(content, label)
    logger.info(f"  Root element: <{kind}> with {len(items)} entries")

    raw_urls: list[str] = []

    if kind == "sitemapindex":
        logger.info("  Sitemapindex detected — fetching child sitemaps")
        for child_url in items:
            logger.info(f"    Fetching child: {child_url}")
            try:
                child_content = _fetch(child_url)
                child_kind, child_locs = _parse(child_content, child_url)
                logger.info(f"      Got {len(child_locs)} URLs (<{child_kind}>)")
                raw_urls.extend(child_locs)
            except Exception as e:
                logger.error(f"    FAILED to fetch child sitemap {child_url}: {e}")
    else:
        raw_urls = items

    # Deduplicate preserving first-seen order
    seen: set[str] = set()
    urls: list[str] = []
    for u in raw_urls:
        u = u.strip()
        if u and u not in seen:
            seen.add(u)
            urls.append(u)

    dupes = len(raw_urls) - len(urls)
    logger.info(f"  Collected {len(urls):,} unique URLs (removed {dupes} duplicates)")
    urls = _drop_blocked(urls)
    return urls


def collect_urls_from_csv(csv_path: str) -> list[str]:
    """Read URLs from a CSV file that has a 'url' column (or use the first column)."""
    logger.info("=" * 60)
    logger.info("STEP 1: Reading URLs from CSV")
    logger.info(f"  Source: {csv_path}")

    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    seen: set[str] = set()
    urls: list[str] = []

    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        url_col = "url" if "url" in (reader.fieldnames or []) else (reader.fieldnames or ["url"])[0]
        for row in reader:
            u = row.get(url_col, "").strip()
            if u and u not in seen:
                seen.add(u)
                urls.append(u)

    logger.info(f"  Loaded {len(urls):,} unique URLs from column '{url_col}'")
    urls = _drop_blocked(urls)
    return urls


def build_shards(urls: list[str]) -> list[tuple[int, str]]:
    """
    Split URLs into shards of MAX_URLS each.
    Returns list of (shard_number, xml_string), 1-indexed.
    """
    logger.info("=" * 60)
    logger.info("STEP 2: Building sitemap shards")

    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    chunks = [urls[i : i + MAX_URLS] for i in range(0, max(len(urls), 1), MAX_URLS)]
    if not chunks:
        chunks = [[]]

    shards: list[tuple[int, str]] = []
    for i, chunk in enumerate(chunks):
        n = i + 1
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<urlset xmlns="{NS}">',
        ]
        for url in chunk:
            lines += [
                "  <url>",
                f"    <loc>{_html.escape(url)}</loc>",
                f"    <lastmod>{today}</lastmod>",
                "    <changefreq>weekly</changefreq>",
                "    <priority>0.6</priority>",
                "  </url>",
            ]
        lines.append("</urlset>")
        xml = "\n".join(lines)

        # Validate
        try:
            ET.fromstring(xml)
        except ET.ParseError as e:
            raise RuntimeError(f"Shard {n} is not valid XML: {e}")

        size = len(xml.encode("utf-8"))
        if size > MAX_BYTES:
            raise RuntimeError(
                f"Shard {n} is {size / 1024 / 1024:.1f} MB — exceeds 50 MB sitemap.org limit"
            )

        shards.append((n, xml))
        logger.info(f"  sitemap-{n}.xml: {len(chunk):,} URLs, {size / 1024:.0f} KB — valid")

    logger.info(f"  Generated {len(shards)} shard(s)")
    return shards


def build_index(shards: list[tuple[int, str]], site_url: str) -> str:
    """
    Build sitemap-index.xml.
    <loc> entries point to the redirect URLs on the site domain
    (which RealGeeks will redirect to the GCS objects).
    """
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    base = site_url.rstrip("/")

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<sitemapindex xmlns="{NS}">',
    ]
    for n, _ in shards:
        lines += [
            "  <sitemap>",
            f"    <loc>{base}/sitemap{n}/</loc>",
            f"    <lastmod>{today}</lastmod>",
            "  </sitemap>",
        ]
    lines.append("</sitemapindex>")
    return "\n".join(lines)
