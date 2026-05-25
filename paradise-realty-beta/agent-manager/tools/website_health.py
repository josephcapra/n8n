"""Website health check for the live site (paradiserealtyfla.com).

Surfaces, as a red/yellow/green light for the command center, anything that
would stop a visitor from getting accurate, up-to-date info in a user-friendly
way: the site being down or erroring, a stale/empty sitemap (an update that
didn't land), broken pages, and missing SEO / AI best practices that need the
operator's attention. Read-only — it only fetches public URLs.

Mirrors tools/security_health.py: a ``run()`` that returns a grade, severity
counts, and a list of findings the GUI renders on click.
"""
from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

SITE = os.environ.get("AGENTMGR_SITE_URL", "https://www.paradiserealtyfla.com").rstrip("/")
_UA = {"User-Agent": "ParadiseHealthBot/1.0 (+command-center)"}
_TIMEOUT = 8
_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "ok": 4}


@dataclass
class Finding:
    id: str
    title: str
    severity: str          # critical | high | medium | low | ok
    detail: str
    fix: str = ""


def _get(url: str, allow_redirects: bool = True) -> requests.Response:
    return requests.get(url, headers=_UA, timeout=_TIMEOUT, allow_redirects=allow_redirects)


def _newest_date(stamps: list[str]) -> datetime | None:
    """Parse the freshest <lastmod> date from a sitemap (ISO date or datetime)."""
    best: datetime | None = None
    for s in stamps:
        s = s.strip()
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
            try:
                d = datetime.strptime(s.replace("Z", "+0000") if fmt.endswith("%z") else s, fmt)
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                if best is None or d > best:
                    best = d
                break
            except ValueError:
                continue
    return best


# --- individual checks ----------------------------------------------------

def _check_homepage() -> tuple[list[Finding], str | None]:
    """Homepage reachable + reasonably fast. Returns (findings, html-or-None)."""
    try:
        r = _get(SITE)
    except Exception as exc:  # noqa: BLE001
        return ([Finding("homepage", "Homepage unreachable", "critical",
                         f"Couldn't load {SITE}: {exc}",
                         "Check the site is up and DNS resolves.")], None)
    if r.status_code != 200:
        return ([Finding("homepage", "Homepage not returning 200", "critical",
                         f"{SITE} → HTTP {r.status_code}",
                         "The site may be down — check hosting / RealGeeks status.")], None)
    secs = r.elapsed.total_seconds()
    out = [Finding("homepage", "Homepage reachable", "ok", f"{SITE} → 200 in {secs:.1f}s")]
    if secs > 4:
        out.append(Finding("speed", "Homepage is slow to load", "low",
                           f"Took {secs:.1f}s — visitors expect under 3s.",
                           "Compress hero images and review third-party scripts."))
    return out, r.text


def _check_https() -> Finding:
    host = SITE.split("//", 1)[-1]
    try:
        r = requests.get("http://" + host, headers=_UA, timeout=_TIMEOUT, allow_redirects=True)
        if r.url.startswith("https"):
            return Finding("https", "HTTPS enforced", "ok", "http:// redirects to https://")
        return Finding("https", "No HTTPS redirect", "high",
                       "http:// does not redirect to https:// — visitors can land on an insecure page.",
                       "Force HTTPS at the host/CDN level.")
    except Exception as exc:  # noqa: BLE001
        return Finding("https", "Couldn't check HTTPS redirect", "low", str(exc)[:120])


_SITEMAP_CAP = 1_000_000   # read at most ~1 MB — enough to validate + sample


def _read_capped(url: str) -> tuple[str, int, str | None]:
    """Stream a URL, returning (text-up-to-cap, total-bytes-if-known, error).
    Sitemaps can be tens of MB; we only need the head to validate and sample,
    so we stop early instead of blocking on a full multi-MB download."""
    try:
        r = requests.get(url, headers=_UA, timeout=(8, 25), stream=True)
    except Exception as exc:  # noqa: BLE001
        return "", 0, str(exc)[:120]
    try:
        if r.status_code != 200:
            return "", 0, f"HTTP {r.status_code}"
        size = int(r.headers.get("Content-Length") or 0)
        buf = bytearray()
        for chunk in r.iter_content(65536):
            buf += chunk
            if len(buf) >= _SITEMAP_CAP:
                break
        return bytes(buf).decode("utf-8", "ignore"), size or len(buf), None
    finally:
        r.close()


def _fetch_sitemap() -> tuple[list[str], datetime | None, int, float, str | None]:
    """Return (page_urls, newest_lastmod, total_bytes, fetch_seconds, error).
    Follows a sitemap index one level. Reads only a capped slice so a huge
    sitemap won't stall, but still records how long the server took to respond."""
    t0 = time.time()
    text, size, err = _read_capped(SITE + "/sitemap.xml")
    elapsed = time.time() - t0
    if err is not None:
        return [], None, 0, elapsed, err
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text)
    lastmods = re.findall(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", text)
    page_urls = locs
    # A sitemap index points at child sitemaps (.xml) — follow the first for real pages.
    if locs and all(u.lower().split("?")[0].endswith(".xml") for u in locs[:3]):
        ctext, _, _ = _read_capped(locs[0])
        child = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", ctext)
        if child:
            page_urls = child
            lastmods += re.findall(r"<lastmod>\s*([^<\s]+)\s*</lastmod>", ctext)
    return page_urls, _newest_date(lastmods), size, elapsed, None


def _check_sitemap(page_urls: list[str], newest: datetime | None,
                   size: int, elapsed: float, err: str | None) -> list[Finding]:
    if err is not None:
        return [Finding("sitemap", "Sitemap missing or unreadable", "high",
                        f"{SITE}/sitemap.xml → {err}",
                        "Publish a valid sitemap.xml so search engines/AI can find every page.")]
    if not page_urls:
        return [Finding("sitemap", "Sitemap is empty", "high",
                        "sitemap.xml has no URLs — an update may have failed to publish.",
                        "Re-run the sitemap-sync pipeline and confirm it writes URLs.")]
    capped = size >= _SITEMAP_CAP
    count = f"{len(page_urls)}+" if capped else str(len(page_urls))
    out = [Finding("sitemap", "Sitemap published", "ok", f"{count} URLs listed")]
    if newest is not None:
        age = (datetime.now(timezone.utc) - newest).days
        if age > 30:
            out.append(Finding("sitemap_fresh", "Sitemap looks stale", "medium",
                               f"Most recent <lastmod> is {age} days old — content updates may not be publishing.",
                               "Check the sitemap-sync job and area-page pipeline ran recently."))
    # The sitemap is generated on-the-fly; a slow response means crawlers may
    # time out before getting the full list of pages.
    if elapsed > 8:
        out.append(Finding("sitemap_slow", "Sitemap is slow to generate", "medium",
                           f"sitemap.xml took {elapsed:.0f}s to respond — Google/Bing may give up before it finishes.",
                           "Pre-build a static (gzipped) sitemap index instead of generating it per request."))
    return out


def _robots_blocks_all(body: str) -> bool:
    """True only if the wildcard (``User-agent: *``) group has a bare
    ``Disallow: /``. robots.txt is grouped per user-agent, so a 'Disallow: /'
    under a single bot (e.g. oodlebot) must NOT trip this."""
    cur: list[str] = []          # user-agents for the current group
    prev_was_directive = False
    for raw in body.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, val = (x.strip() for x in line.split(":", 1))
        k = key.lower()
        if k == "user-agent":
            if prev_was_directive:   # a UA line after directives starts a new group
                cur = []
                prev_was_directive = False
            cur.append(val)
        elif k in ("disallow", "allow", "crawl-delay", "sitemap"):
            prev_was_directive = True
            if k == "disallow" and val == "/" and "*" in cur:
                return True
    return False


def _check_robots() -> list[Finding]:
    try:
        r = _get(SITE + "/robots.txt")
    except Exception as exc:  # noqa: BLE001
        return [Finding("robots", "Couldn't check robots.txt", "low", str(exc)[:120])]
    if r.status_code != 200:
        return [Finding("robots", "robots.txt missing", "medium",
                        f"{SITE}/robots.txt → HTTP {r.status_code}",
                        "Add a robots.txt that points to your sitemap.")]
    body = r.text
    out: list[Finding] = []
    if _robots_blocks_all(body):
        out.append(Finding("robots_block", "robots.txt blocks all crawlers", "high",
                           "A site-wide 'Disallow: /' keeps the site out of Google/Bing and AI answers.",
                           "Remove the blanket Disallow so pages can be indexed."))
    if "sitemap" not in body.lower():
        out.append(Finding("robots_sitemap", "robots.txt has no Sitemap line", "low",
                           "Crawlers find the sitemap faster when robots.txt links it.",
                           f"Add 'Sitemap: {SITE}/sitemap.xml' to robots.txt."))
    if not out:
        out.append(Finding("robots", "robots.txt present", "ok", "found, no blocking rules"))
    return out


def _check_llms() -> Finding:
    try:
        r = _get(SITE + "/llms.txt")
        if r.status_code == 200 and r.text.strip():
            return Finding("llms", "llms.txt present", "ok", "AI assistants can read your site summary")
    except Exception:  # noqa: BLE001
        pass
    return Finding("llms", "No llms.txt", "low",
                   "AI assistants (ChatGPT, Claude, etc.) increasingly read /llms.txt to cite accurate info.",
                   "Publish an llms.txt summarizing the brokerage, areas served, and key pages.")


def _check_sample_pages(page_urls: list[str]) -> list[Finding]:
    if not page_urls:
        return []
    sample = page_urls[:6]

    def probe(u: str) -> str | None:
        try:
            r = _get(u)
            return f"{u} → {r.status_code}" if r.status_code >= 400 else None
        except Exception as exc:  # noqa: BLE001
            return f"{u} → {str(exc)[:40]}"

    with ThreadPoolExecutor(max_workers=len(sample)) as ex:
        broken = [b for b in ex.map(probe, sample) if b]
    if broken:
        return [Finding("pages", f"{len(broken)} of {len(sample)} sampled pages are erroring", "high",
                        "Visitors hitting these get an error instead of listing info:\n• "
                        + "\n• ".join(broken),
                        "Fix or remove the broken pages and re-publish the sitemap.")]
    return [Finding("pages", "Sampled pages load fine", "ok", f"{len(sample)} pages checked, all 200")]


def _check_seo_basics(html: str | None) -> list[Finding]:
    if not html:
        return []
    low = html.lower()
    out: list[Finding] = []

    def has(pattern: str) -> bool:
        return re.search(pattern, html, re.I | re.S) is not None

    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if not title or not title.group(1).strip():
        out.append(Finding("seo_title", "Missing page title", "high",
                           "The homepage has no <title> — search results show a blank/garbled headline.",
                           "Add a descriptive <title> with the brand + primary area."))
    if not has(r'<meta[^>]+name=["\']description["\']'):
        out.append(Finding("seo_desc", "Missing meta description", "medium",
                           "No meta description — Google writes its own snippet, often poorly.",
                           "Add a 150–160 char meta description."))
    if not has(r'<link[^>]+rel=["\']canonical["\']'):
        out.append(Finding("seo_canonical", "No canonical tag", "low",
                           "A canonical URL prevents duplicate-content dilution.",
                           "Add <link rel=\"canonical\"> to the homepage."))
    if 'name="viewport"' not in low and "name='viewport'" not in low:
        out.append(Finding("seo_viewport", "No mobile viewport tag", "medium",
                           "Without a viewport meta the site renders badly on phones — most buyers browse on mobile.",
                           "Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">."))
    if "og:title" not in low and "og:image" not in low:
        out.append(Finding("seo_og", "No Open Graph tags", "low",
                           "Shared links (Facebook/iMessage) show no preview image or title.",
                           "Add og:title, og:description and og:image meta tags."))
    if "application/ld+json" not in low and "schema.org" not in low:
        out.append(Finding("seo_schema", "No structured data (schema.org)", "medium",
                           "Without JSON-LD, Google/AI can't reliably read the brokerage, agents, or listings.",
                           "Add RealEstateAgent / Organization JSON-LD to the homepage."))
    if not out:
        out.append(Finding("seo", "SEO basics present", "ok",
                           "title, description, viewport, canonical, OG and structured data all found"))
    return out


def scan() -> list[Finding]:
    """Run the independent network checks concurrently so the whole scan is as
    fast as the slowest single check, not their sum."""
    findings: list[Finding] = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        f_home = ex.submit(_check_homepage)
        f_https = ex.submit(_check_https)
        f_sitemap = ex.submit(_fetch_sitemap)
        f_robots = ex.submit(_check_robots)
        f_llms = ex.submit(_check_llms)

        home, html = f_home.result()
        page_urls, newest, size, elapsed, err = f_sitemap.result()
        f_pages = ex.submit(_check_sample_pages, page_urls)  # depends on sitemap

        findings += home
        findings.append(f_https.result())
        findings += _check_sitemap(page_urls, newest, size, elapsed, err)
        findings += f_robots.result()
        findings.append(f_llms.result())
        findings += f_pages.result()
        findings += _check_seo_basics(html)
    return sorted(findings, key=lambda f: _RANK.get(f.severity, 9))


def _grade(findings: list[Finding]) -> tuple[str, dict]:
    counts = {s: 0 for s in _RANK}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    if counts["critical"]:
        return "F", counts
    score = 100 - counts["high"] * 18 - counts["medium"] * 7 - counts["low"] * 2
    if score >= 90:
        grade = "A"
    elif score >= 80:
        grade = "B"
    elif score >= 68:
        grade = "C"
    elif score >= 50:
        grade = "D"
    else:
        grade = "F"
    return grade, counts


def run(send: bool = False) -> dict:
    """Run all checks and return {grade, counts, checked, findings}. ``send`` is
    accepted for symmetry with security_health.run but this light doesn't email."""
    findings = scan()
    grade, counts = _grade(findings)
    return {
        "grade": grade,
        "counts": counts,
        "checked": SITE,
        "findings": [f.__dict__ for f in findings],
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run(), indent=2))
