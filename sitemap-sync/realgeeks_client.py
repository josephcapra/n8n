"""
Manage RealGeeks redirect rules via Playwright browser automation.

Uses a PERSISTENT browser profile (BROWSER_DATA_DIR) so the session stays alive
between runs — you only need to log in once.

RealGeeks uses OAuth via login.realgeeks.com. The login URL should be set to
https://www.paradiserealtyfla.com/admin/ — the script follows the OAuth redirect
automatically.

Redirects admin list URL pattern (Django admin):
  https://www.paradiserealtyfla.com/admin/redirects/redirect/
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Imported lazily inside functions — playwright is not installed in the Cloud Run image
# (Cloud Run uses --skip-redirects and never calls these functions)
try:
    from playwright.sync_api import Page, sync_playwright
    from playwright.sync_api import TimeoutError as PWTimeout
except ImportError:
    Page = None  # type: ignore
    sync_playwright = None  # type: ignore
    PWTimeout = Exception  # type: ignore

# ── Login selectors (login.realgeeks.com) ──────────────────────────────────────
SEL_EMAIL    = "input[type='email'], input[placeholder='Email']"
SEL_PASSWORD = "input[type='password'], input[placeholder='Password']"
SEL_SUBMIT   = "button[type='submit']"

# ── Redirects list (Django admin changelist) ───────────────────────────────────
SEL_TABLE_ROWS = "table#result_list tbody tr"
SEL_ADD_BTN    = "a.addlink, a[href$='/add/']"

# ── Redirect add/edit form ─────────────────────────────────────────────────────
SEL_OLD_PATH = "#id_old_path, input[name='old_path']"
SEL_NEW_PATH = "#id_new_path, input[name='new_path']"
SEL_STATUS   = (
    "#id_response_redirect_type, select[name='response_redirect_type'], "
    "#id_response_code, select[name='status_code']"
)
SEL_SAVE     = "input[name='_save'], input[type='submit'][value='Save']"
SEL_DEL_LINK = "a.deletelink, a[href*='/delete/']"


class RealGeeksError(Exception):
    pass


class RealGeeksAuthError(RealGeeksError):
    """Authentication failure — caller must not retry."""
    pass


def _browser_data_dir() -> str:
    raw = os.getenv("BROWSER_DATA_DIR", "~/.sitemap-sync-browser")
    return str(Path(raw).expanduser())


def sync_redirects(
    shards: list[tuple[int, str]],
    gcs_bucket: str,
    gcs_prefix: str,
    login_url: str,
    username: str,
    password: str,
    redirects_url: str,
    redirect_base: str,
    log_dir: Path,
    dry_run: bool = False,
) -> dict:
    """
    Upsert RealGeeks 301 redirects for every shard + index.
    Delete any stale /sitemapN/ redirects not in this run.
    """
    logger.info("=" * 60)
    logger.info("STEP 4: RealGeeks redirect sync")

    gcs_prefix = gcs_prefix.rstrip("/") + "/"
    gcs_base   = f"https://storage.googleapis.com/{gcs_bucket}/{gcs_prefix}"

    desired: dict[str, str] = {}
    for n, _ in shards:
        desired[f"/sitemap{n}/"] = f"{gcs_base}sitemap-{n}.xml"
    desired["/sitemap-index/"] = f"{gcs_base}sitemap-index.xml"

    result: dict = {"upserted": 0, "deleted": 0, "errors": [], "success": True}

    if dry_run:
        for src, tgt in desired.items():
            logger.info(f"  [DRY RUN] Would upsert: {src} → {tgt}")
        result["upserted"] = len(desired)
        return result

    ts = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d-%H%M%S")
    screenshot_path = log_dir / f"redirects-{ts}.png"
    data_dir = _browser_data_dir()
    logger.info(f"  Browser profile: {data_dir}")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=data_dir,
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.new_page()

        try:
            _ensure_logged_in(page, login_url, redirects_url, username, password)

            existing = _scrape_redirects(page, redirects_url)
            logger.info(f"  Found {len(existing)} existing redirects")

            for source, target in desired.items():
                try:
                    _upsert(page, redirects_url, source, target, existing)
                    result["upserted"] += 1
                    logger.info(f"  Upserted: {source} → {target}")
                except Exception as e:
                    logger.error(f"  FAILED to upsert {source}: {e}")
                    result["errors"].append(source)
                    result["success"] = False

            # Cleanup stale /sitemapN/ redirects
            sitemap_re = re.compile(r"^/sitemap\d+/$")
            for src in list(existing.keys()):
                if sitemap_re.match(src) and src not in desired:
                    try:
                        _delete(page, redirects_url, src, existing)
                        result["deleted"] += 1
                        logger.info(f"  Deleted stale redirect: {src}")
                    except Exception as e:
                        logger.error(f"  Failed to delete stale redirect {src}: {e}")

            # Screenshot
            page.goto(redirects_url, wait_until="networkidle")
            page.screenshot(path=str(screenshot_path), full_page=True)
            logger.info(f"  Screenshot: {screenshot_path}")

        finally:
            ctx.close()

    logger.info(
        f"  Redirects done: {result['upserted']} upserted, "
        f"{result['deleted']} deleted, {len(result['errors'])} errors"
    )
    return result


def _ensure_logged_in(
    page: Page,
    login_url: str,
    redirects_url: str,
    username: str,
    password: str,
) -> None:
    """
    Navigate to the redirects page. If we land on the login page,
    perform the OAuth login flow. Otherwise we're already authenticated.
    """
    logger.info(f"  Checking session — navigating to: {redirects_url}")
    page.goto(redirects_url, wait_until="networkidle")

    if "login.realgeeks.com" in page.url or "sign-in" in page.url or "verify-2fa" in page.url:
        logger.info("  Session expired — logging in via RealGeeks OAuth")
        _login(page, login_url, username, password)
    elif "paradiserealtyfla.com" not in page.url:
        logger.info("  Unexpected redirect — attempting login")
        _login(page, login_url, username, password)
    else:
        logger.info("  Already logged in (cached session)")

    # Confirm we're on the admin page
    if "login.realgeeks.com" in page.url or "sign-in" in page.url:
        raise RealGeeksAuthError(
            "Still on login page after auth attempt. "
            "Check REALGEEKS_USER and REALGEEKS_PASS. "
            "If 2FA is enabled, log in manually once at "
            f"{login_url} in a browser pointed at profile: {_browser_data_dir()}"
        )


def _login(page: Page, login_url: str, username: str, password: str) -> None:
    # Navigate to the site admin — this triggers the OAuth redirect to login.realgeeks.com
    page.goto(login_url, wait_until="networkidle")

    # If not yet on the login form, try navigating directly
    if not page.query_selector(SEL_EMAIL):
        logger.info(f"  Navigating to login.realgeeks.com (from {page.url})")
        page.wait_for_load_state("networkidle")

    try:
        page.wait_for_selector(SEL_EMAIL, timeout=10_000)
    except PWTimeout:
        raise RealGeeksAuthError(
            f"Email input not found on {page.url}. "
            "Check REALGEEKS_LOGIN_URL and that the admin is accessible."
        )

    # Detect 2FA gate (before credentials entry — shouldn't happen, but guard)
    if page.query_selector("input[name='2faMethod']"):
        raise RealGeeksAuthError(
            "2FA screen appeared before credentials were entered — unexpected state. "
            "Log in manually once to establish a session in the persistent profile, then rerun."
        )

    page.fill(SEL_EMAIL, username)
    page.fill(SEL_PASSWORD, password)
    page.click(SEL_SUBMIT)
    page.wait_for_load_state("networkidle")

    # Handle 2FA if triggered after credentials
    if page.query_selector("input[name='2faMethod']") or page.query_selector("input[name='code']"):
        raise RealGeeksAuthError(
            "2FA is enabled on this account. Automated login cannot complete 2FA.\n"
            "To fix: open a Chromium browser pointed at the same profile and log in manually:\n"
            f"  BROWSER_DATA_DIR={_browser_data_dir()}\n"
            "Then rerun — the session cookie will be reused automatically."
        )

    # Wait for OAuth callback redirect back to the site
    try:
        page.wait_for_url("*paradiserealtyfla.com*", timeout=15_000)
    except PWTimeout:
        pass  # May already be there

    logger.info(f"  Login complete — at: {page.url}")


def _scrape_redirects(page: Page, redirects_url: str) -> dict[str, str]:
    """
    Navigate to the redirects list and parse existing entries.
    Returns {source_path: absolute_edit_url}.
    Django admin changelist: table#result_list, first <td> has the edit link.
    """
    page.goto(redirects_url, wait_until="networkidle")
    existing: dict[str, str] = {}
    rows = page.query_selector_all(SEL_TABLE_ROWS)

    for row in rows:
        # Django admin: first column is a checkbox td with no link; edit link
        # lives in the first actual data td — select "td a" to skip the checkbox.
        link = row.query_selector("td a")
        if not link:
            continue
        source = link.inner_text().strip()
        href   = link.get_attribute("href") or ""
        if source and href:
            edit_url = href if href.startswith("http") else urljoin(page.url, href)
            existing[source] = edit_url

    return existing


def _upsert(
    page: Page,
    redirects_url: str,
    source: str,
    target: str,
    existing: dict[str, str],
) -> None:
    if source in existing:
        _update(page, existing[source], target)
    else:
        _create(page, redirects_url, source, target)


def _update(page: Page, edit_url: str, new_target: str) -> None:
    page.goto(edit_url, wait_until="networkidle")
    page.fill(SEL_NEW_PATH, new_target)
    page.click(SEL_SAVE)
    page.wait_for_load_state("networkidle")


def _create(page: Page, redirects_url: str, source: str, target: str) -> None:
    page.goto(redirects_url, wait_until="networkidle")
    add_btn = page.query_selector(SEL_ADD_BTN)
    if not add_btn:
        raise RealGeeksError(
            f"'Add redirect' button not found at {redirects_url}. "
            "Update SEL_ADD_BTN in realgeeks_client.py to match the page's HTML."
        )
    add_btn.click()
    page.wait_for_load_state("networkidle")

    page.fill(SEL_OLD_PATH, source)
    page.fill(SEL_NEW_PATH, target)

    status_el = page.query_selector(SEL_STATUS)
    if status_el:
        for val in ("301", "1"):
            try:
                page.select_option(SEL_STATUS, val)
                break
            except Exception:
                continue

    page.click(SEL_SAVE)
    page.wait_for_load_state("networkidle")


def _delete(
    page: Page,
    redirects_url: str,
    source: str,
    existing: dict[str, str],
) -> None:
    edit_url = existing.get(source)
    if not edit_url:
        return
    page.goto(edit_url, wait_until="networkidle")
    del_link = page.query_selector(SEL_DEL_LINK)
    if not del_link:
        raise RealGeeksError(f"Delete link not found for {source} at {edit_url}")
    del_link.click()
    page.wait_for_load_state("networkidle")
    confirm = page.query_selector("input[type='submit'], button[type='submit']")
    if confirm:
        confirm.click()
        page.wait_for_load_state("networkidle")


def pre_login(login_url: str, username: str, password: str) -> None:
    """
    Standalone helper: authenticate and save session to the persistent profile.
    Opens a visible browser. If 2FA appears, complete it in the browser window —
    the script waits up to 3 minutes for you to finish.

    Run via:
      python3 sitemap-sync/pre_login.py
    """
    data_dir = _browser_data_dir()
    print(f"Logging in and saving session to: {data_dir}")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=data_dir,
            headless=False,  # Visible — needed if 2FA prompt appears
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.new_page()
        page.goto(login_url, wait_until="networkidle")
        if "login.realgeeks.com" in page.url or "sign-in" in page.url:
            page.fill(SEL_EMAIL, username)
            page.fill(SEL_PASSWORD, password)
            page.click(SEL_SUBMIT)
            page.wait_for_load_state("networkidle")
            if page.query_selector("input[name='2faMethod']"):
                print("⚠️  2FA required — complete it in the browser window now.")
                print("    Waiting up to 3 minutes for you to finish...")
                # Wait for OAuth callback — user completes 2FA in the visible browser
                page.wait_for_url("*paradiserealtyfla.com/admin*", timeout=180_000)
        print(f"✅ Logged in — session saved. URL: {page.url}")
        ctx.close()
