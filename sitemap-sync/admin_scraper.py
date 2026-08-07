"""
Collect DynamicPage URLs from the RealGeeks Django admin changelist.

URL pattern (verified against VIEW ON SITE links):
  /listings/{attribute}/{value.replace(' ','-')}/{facet_label.replace(' ','-')}/

Auth: uses the persistent Playwright browser profile (stays logged in).
Speed: extracts session cookies then uses concurrent requests for pagination.
"""
from __future__ import annotations

import concurrent.futures
import csv
import logging
import os
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

logger = logging.getLogger(__name__)


def _browser_data_dir() -> str:
    raw = os.getenv("BROWSER_DATA_DIR", "~/.sitemap-sync-browser")
    return str(Path(raw).expanduser())


# ── HTML parser ───────────────────────────────────────────────────────────────

class _AdminListParser(HTMLParser):
    """
    Parse Django admin changelist pages for DynamicPage.
    Extracts (attribute, value, facet_label) from the three class-tagged cells:
      <th class="field-value"><a href="...">VALUE</a></th>
      <td class="field-attribute">ATTRIBUTE</td>
      <td class="field-facet_label">FACET</td>
    """

    def __init__(self):
        super().__init__()
        self._in_tbody = False
        self._in_row = False
        self._current_field: str = ""
        self._in_field: bool = False
        self._field_text: list[str] = []

        self._cur_attr: str = ""
        self._cur_val: str = ""
        self._cur_facet: str = ""

        self.records: list[tuple[str, str, str]] = []  # (attribute, value, facet)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "")

        if tag == "tbody":
            self._in_tbody = True
        elif tag == "tr" and self._in_tbody:
            self._in_row = True
            self._cur_attr = self._cur_val = self._cur_facet = ""
        elif tag in ("td", "th") and self._in_row:
            if "field-value" in cls:
                self._current_field = "value"
                self._in_field = True
                self._field_text = []
            elif "field-attribute" in cls:
                self._current_field = "attribute"
                self._in_field = True
                self._field_text = []
            elif "field-facet_label" in cls:
                self._current_field = "facet"
                self._in_field = True
                self._field_text = []
            else:
                self._in_field = False
                self._current_field = ""

    def handle_endtag(self, tag):
        if tag == "tbody":
            self._in_tbody = False
        elif tag == "tr" and self._in_row:
            if self._cur_attr and self._cur_val and self._cur_facet:
                self.records.append((self._cur_attr, self._cur_val, self._cur_facet))
            self._in_row = False
        elif tag in ("td", "th") and self._in_field:
            text = "".join(self._field_text).strip()
            if self._current_field == "value":
                self._cur_val = text
            elif self._current_field == "attribute":
                self._cur_attr = text
            elif self._current_field == "facet":
                self._cur_facet = text
            self._in_field = False
            self._current_field = ""

    def handle_data(self, data):
        if self._in_field:
            self._field_text.append(data)


# ── URL construction ──────────────────────────────────────────────────────────

def _make_url(site_url: str, attribute: str, value: str, facet: str) -> str:
    slug = lambda s: s.replace(" ", "-")
    return f"{site_url.rstrip('/')}/listings/{attribute}/{slug(value)}/{slug(facet)}/"


def _parse_total_and_pagesize(html_text: str) -> tuple[Optional[int], int]:
    """
    "1 – 100 of 612,258 dynamic pages" → (612258, 100)
    """
    m = re.search(r"1\s*[–\-]\s*(\d+)\s+of\s+([\d,]+)", html_text)
    if m:
        return int(m.group(2).replace(",", "")), int(m.group(1))
    m = re.search(r"([\d,]+)\s+(?:dynamic\s*page|result)", html_text, re.IGNORECASE)
    if m:
        return int(m.group(1).replace(",", "")), 100
    return None, 100


# ── Public API ────────────────────────────────────────────────────────────────

def collect_urls_from_admin(
    admin_url: str,
    site_url: str,
    login_url: str,
    username: str,
    password: str,
) -> list[str]:
    """
    Pull all DynamicPage URLs from the Django admin changelist.
    Phase 1 (Playwright): auth + first page + get session cookies.
    Phase 2 (requests):   15-worker concurrent pagination.
    """
    logger.info("=" * 60)
    logger.info("STEP 1: Collecting URLs from Django admin")
    logger.info(f"  {admin_url}")

    from realgeeks_client import _ensure_logged_in

    # ── Phase 1: Playwright ──────────────────────────────────────────────────
    cookies: dict[str, str] = {}
    total: Optional[int] = None
    per_page: int = 100
    first_page_urls: list[str] = []

    from playwright.sync_api import sync_playwright  # not installed in Cloud Run image
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=_browser_data_dir(),
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.new_page()
        try:
            _ensure_logged_in(page, login_url, admin_url, username, password)
            page.goto(admin_url, wait_until="networkidle")

            html_text = page.content()
            cookies = {c["name"]: c["value"] for c in ctx.cookies()}

            total, per_page = _parse_total_and_pagesize(html_text)

            parser = _AdminListParser()
            parser.feed(html_text)
            first_page_urls = [_make_url(site_url, a, v, f) for a, v, f in parser.records]
            logger.info(
                f"  Page 0: {len(parser.records)} records parsed, "
                f"sample: {first_page_urls[0] if first_page_urls else '(none)'}"
            )

        finally:
            ctx.close()

    if total is None:
        logger.warning("  Could not detect total count — will paginate until empty")
        total = 800_000
    page_count = max(1, (total + per_page - 1) // per_page)
    logger.info(
        f"  Total: {total:,} records | {per_page}/page | {page_count} pages to fetch"
    )

    # ── Phase 2: concurrent requests ─────────────────────────────────────────
    import requests as req

    session = req.Session()
    for name, val in cookies.items():
        session.cookies.set(name, val)
    req_headers = {"User-Agent": "Mozilla/5.0 (compatible; ParadiseRealty-SitemapSync/1.0)"}

    all_urls: list[str] = list(first_page_urls)

    def fetch_page(p_idx: int) -> list[str]:
        page_url = f"{admin_url.rstrip('/')}/?p={p_idx}"
        for attempt in range(3):
            try:
                r = session.get(page_url, headers=req_headers, timeout=30)
                r.raise_for_status()
                if "login" in r.url or "sign-in" in r.url:
                    logger.error(f"  Session expired on page {p_idx}")
                    return []
                parser = _AdminListParser()
                parser.feed(r.text)
                return [_make_url(site_url, a, v, f) for a, v, f in parser.records]
            except Exception as e:
                if attempt == 2:
                    logger.error(f"  Page {p_idx} failed after 3 attempts: {e}")
                    return []
                time.sleep(1 << attempt)
        return []

    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        futures = {executor.submit(fetch_page, i): i for i in range(1, page_count)}
        for future in concurrent.futures.as_completed(futures):
            all_urls.extend(future.result())
            completed += 1
            if completed % 250 == 0 or completed == page_count - 1:
                logger.info(
                    f"  Progress: {completed + 1}/{page_count} pages | "
                    f"{len(all_urls):,} URLs collected"
                )

    # Deduplicate preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for u in all_urls:
        if u not in seen:
            seen.add(u)
            deduped.append(u)

    dupes = len(all_urls) - len(deduped)
    logger.info(f"  Collected {len(deduped):,} unique URLs ({dupes} duplicates removed)")
    # Apply the permanent deny-list (see sitemap_builder.BLOCKED_PATH_PREFIXES).
    import sitemap_builder
    deduped = sitemap_builder._drop_blocked(deduped)
    return deduped


def write_csv(urls: list[str], output_path: Path) -> None:
    """Write URL list to a single-column CSV."""
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["url"])
        for u in urls:
            writer.writerow([u])
    logger.info(f"  CSV written: {output_path} ({len(urls):,} rows)")
