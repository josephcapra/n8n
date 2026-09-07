"""
Area-page change detection for the daily website report.

Area / community pages are authored in RealGeeks admin and published live at
``https://www.paradiserealtyfla.com/<county>/<slug>/``. The Cloud Run image has
no browser, so we can't log into the admin there. Instead we detect changes the
robust, auth-free way: each morning we fetch the live published pages, fingerprint
each one, and diff against yesterday's snapshot stored in GCS. Any edit made in
RealGeeks shows up the moment it goes live.

Two artifacts live in GCS (default bucket: site-map-dynamic-page3):
  - tracked URL list   gs://.../area-report/area-tracked-urls.txt   (one URL/line)
  - latest snapshot    gs://.../area-report/area-snapshot-latest.json

``build_tracked_urls_from_repo()`` is a LOCAL helper used at setup time to seed
the URL list from the repo's ``<link rel="canonical">`` tags. The daily Cloud Run
job never touches the repo — it only reads the GCS list.

RealGeeks content pages are server-rendered, while their listing widgets render
client-side via JS. Because we fetch with ``requests`` (no JS), the dynamic
listing blocks don't appear in the HTML — which conveniently keeps the diff
focused on authored content instead of churning on live inventory.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

USER_AGENT = "ParadiseRealty-WebsiteReport/1.0"
SITE = "https://www.paradiserealtyfla.com"

# Body-copy edits under this many words are treated as dynamic-content noise.
# Some community pages embed live IDX listing inventory in the body that
# refreshes periodically (observed drift up to ~24 words/run), so the bar for a
# body-only change is set above that. Title / meta / heading / schema edits are
# always reported regardless of size — those fields are stable and authored.
BODY_WORD_THRESHOLD = 30


# ── Fingerprinting ────────────────────────────────────────────────────────────


def _norm_ws(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _sha(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8", "replace"))
        h.update(b"\x1f")
    return h.hexdigest()[:16]


def fingerprint_html(html_text: str) -> dict:
    """
    Extract a stable, low-noise fingerprint of an area page's authored content.

    Splits the signal in two:
      - ``meta_hash``: title + meta description + canonical + all headings +
        JSON-LD. These are almost always hand-authored; a change here is a real
        SEO/content edit.
      - ``body_hash``: visible paragraph / list text (nav, header, footer,
        script, style stripped). Catches body-copy edits.
    """
    title = meta_desc = canonical = h1 = ""
    headings: list[str] = []
    body_text = ""
    json_ld_blobs: list[str] = []

    doc = None
    try:
        from lxml import html as lxml_html

        doc = lxml_html.fromstring(html_text)
    except Exception:
        doc = None

    if doc is not None:
        t = doc.findtext(".//title")
        title = _norm_ws(t)
        for m in doc.iter("meta"):
            if (m.get("name") or "").lower() == "description":
                meta_desc = _norm_ws(m.get("content"))
                break
        for link in doc.iter("link"):
            if (link.get("rel") or "").lower() == "canonical":
                canonical = _norm_ws(link.get("href"))
                break
        for s in doc.iter("script"):
            if (s.get("type") or "").lower() == "application/ld+json":
                json_ld_blobs.append(_norm_ws(s.text_content()))
        # Drop chrome so body text reflects authored content, not nav/footer.
        for bad in doc.xpath("//nav | //header | //footer | //script | //style | //svg | //noscript"):
            bad.getparent().remove(bad) if bad.getparent() is not None else None
        for hx in doc.xpath("//h1 | //h2 | //h3"):
            txt = _norm_ws(hx.text_content())
            if txt:
                headings.append(txt)
                if hx.tag == "h1" and not h1:
                    h1 = txt
        paras = [_norm_ws(p.text_content()) for p in doc.xpath("//p | //li")]
        body_text = " ".join(p for p in paras if p)
    else:
        # Regex fallback if lxml is unavailable / HTML is malformed.
        m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.I | re.S)
        title = _norm_ws(m.group(1)) if m else ""
        m = re.search(r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']', html_text, re.I | re.S)
        meta_desc = _norm_ws(m.group(1)) if m else ""
        m = re.search(r'<link\s+rel=["\']canonical["\']\s+href=["\'](.*?)["\']', html_text, re.I)
        canonical = _norm_ws(m.group(1)) if m else ""
        m = re.search(r"<h1[^>]*>(.*?)</h1>", html_text, re.I | re.S)
        h1 = _norm_ws(re.sub(r"<[^>]+>", " ", m.group(1))) if m else ""
        headings = [h1] if h1 else []
        body_text = _norm_ws(re.sub(r"<[^>]+>", " ", html_text))

    headings_blob = " | ".join(headings)
    meta_hash = _sha(title, meta_desc, canonical, headings_blob, " ".join(sorted(json_ld_blobs)))
    body_hash = _sha(body_text)
    return {
        "title": title,
        "meta_description": meta_desc,
        "canonical": canonical,
        "h1": h1,
        "heading_count": len(headings),
        "word_count": len(body_text.split()),
        "has_json_ld": bool(json_ld_blobs),
        "meta_hash": meta_hash,
        "body_hash": body_hash,
    }


def fetch_one(url: str, timeout: int = 20, retries: int = 1) -> dict:
    """
    Fetch a single live URL and return its fingerprint (+ status / redirect).
    Retries on transient network errors so a blip doesn't look like a change.
    status 0 means "could not fetch" (no reliable signal) — distinct from a
    definite HTTP error response.
    """
    fp: dict = {"url": url}
    last_err = ""
    for attempt in range(retries + 1):
        try:
            r = requests.get(
                url,
                timeout=timeout,
                allow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
            fp["status"] = r.status_code
            final_host = urlparse(r.url).hostname or ""
            orig_host = urlparse(url).hostname or ""
            fp["redirected_offsite"] = bool(final_host and orig_host and final_host != orig_host)
            fp["final_url"] = r.url if r.url != url else ""
            if r.status_code == 200 and r.text:
                fp.update(fingerprint_html(r.text))
                fp["bytes"] = len(r.content)
            return fp
        except requests.RequestException as e:
            last_err = str(e)[:200]
    fp["status"] = 0
    fp["error"] = last_err
    return fp


def fetch_snapshot(urls: list[str], workers: int = 20, prev_pages: dict | None = None) -> dict:
    """
    Concurrently fingerprint every tracked URL. Returns {url: fingerprint}.

    If a fetch fails (status 0) and we have a prior fingerprint for that URL,
    carry the prior forward (flagged ``stale``) so a transient network blip
    doesn't show up as a change or break.
    """
    prev_pages = prev_pages or {}
    pages: dict[str, dict] = {}
    carried = 0
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_one, u): u for u in urls}
        for fut in concurrent.futures.as_completed(futs):
            fp = fut.result()
            url = fp["url"]
            if fp.get("status") == 0 and url in prev_pages:
                carried_fp = dict(prev_pages[url])
                carried_fp["stale"] = True
                carried_fp["last_fetch_error"] = fp.get("error", "")
                pages[url] = carried_fp
                carried += 1
            else:
                pages[url] = fp
            done += 1
            if done % 200 == 0:
                logger.info(f"  area snapshot: {done}/{len(urls)} pages fetched")
    if carried:
        logger.info(f"  area snapshot: {carried} page(s) failed to fetch — carried prior fingerprint")
    return pages


# ── GCS persistence ─────────────────────────────────────────────────────────


def _split_gs(gs_uri: str) -> tuple[str, str]:
    bucket, blob = gs_uri.replace("gs://", "").split("/", 1)
    return bucket, blob


def _blob(gs_uri: str, project: str):
    from google.cloud import storage

    bucket, blob = _split_gs(gs_uri)
    return storage.Client(project=project).bucket(bucket).blob(blob)


def load_tracked_urls(gs_uri: str, project: str) -> list[str]:
    try:
        text = _blob(gs_uri, project).download_as_text()
    except Exception as e:
        logger.warning(f"  could not load tracked URLs from {gs_uri}: {e}")
        return []
    urls = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("http")]
    return urls


def save_tracked_urls(urls: list[str], gs_uri: str, project: str) -> None:
    _blob(gs_uri, project).upload_from_string(
        "\n".join(urls) + "\n", content_type="text/plain"
    )
    logger.info(f"  wrote {len(urls):,} tracked URLs to {gs_uri}")


def load_snapshot(gs_uri: str, project: str) -> dict:
    try:
        return json.loads(_blob(gs_uri, project).download_as_text())
    except Exception as e:
        logger.info(f"  no prior snapshot at {gs_uri} ({e}) — first run")
        return {}


def save_snapshot(snapshot: dict, gs_uri: str, project: str, keep_history: bool = True) -> None:
    payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    _blob(gs_uri, project).upload_from_string(payload, content_type="application/json")
    if keep_history:
        ts = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d-%H%M%S")
        bucket, blob = _split_gs(gs_uri)
        prefix = blob.rsplit("/", 1)[0] if "/" in blob else ""
        hist = f"gs://{bucket}/{prefix}/snapshots/area-snapshot-{ts}.json"
        try:
            _blob(hist, project).upload_from_string(payload, content_type="application/json")
        except Exception as e:
            logger.warning(f"  could not write history snapshot {hist}: {e}")
    logger.info(f"  saved snapshot ({len(snapshot.get('pages', {})):,} pages) to {gs_uri}")


# ── Diff ──────────────────────────────────────────────────────────────────────


_FIELD_LABELS = {
    "title": "title",
    "meta_description": "meta description",
    "h1": "H1",
    "heading_count": "heading count",
    "has_json_ld": "JSON-LD schema",
    "canonical": "canonical URL",
}


def diff_snapshots(prev: dict, cur: dict) -> dict:
    """
    Compare two snapshots ({"pages": {url: fp}}). Returns a structured diff:
      added / removed / changed / broke, plus counts.
    """
    p = prev.get("pages", {}) if prev else {}
    c = cur.get("pages", {}) if cur else {}
    p_urls, c_urls = set(p), set(c)

    added = sorted(c_urls - p_urls)
    removed = sorted(p_urls - c_urls)

    changed: list[dict] = []
    broke: list[dict] = []
    for url in sorted(p_urls & c_urls):
        a, b = p[url], c[url]
        a_status, b_status = a.get("status"), b.get("status")

        # status 0 = couldn't fetch reliably (and prev was carried forward if
        # available) — no trustworthy signal, so skip entirely.
        if b_status == 0 or a_status == 0:
            continue

        a_ok = a_status == 200 and not a.get("redirected_offsite")
        # A "break" requires a definite server response: HTTP error or off-site
        # redirect. Transient timeouts never reach here.
        b_broken = (b_status and b_status >= 400) or b.get("redirected_offsite")
        if b_broken:
            if a_ok:
                broke.append({
                    "url": url,
                    "status": b_status,
                    "offsite": b.get("redirected_offsite"),
                    "final_url": b.get("final_url", ""),
                    "error": b.get("error", ""),
                })
            continue
        if b_status != 200:
            continue
        if not a_ok:
            # Was a definite error, now 200 — recovered.
            changed.append({"url": url, "fields": ["recovered (now 200)"], "detail": {}})
            continue

        fields: list[str] = []
        detail: dict = {}
        if a.get("meta_hash") != b.get("meta_hash"):
            for key, label in _FIELD_LABELS.items():
                if a.get(key) != b.get(key):
                    fields.append(label)
                    detail[label] = {"from": a.get(key), "to": b.get(key)}
            if not fields:  # hash changed but no labelled field (e.g. h2/h3 text)
                fields.append("headings/schema text")
        if a.get("body_hash") != b.get("body_hash"):
            wc_a, wc_b = a.get("word_count", 0), b.get("word_count", 0)
            # Only report body edits past the noise threshold, unless something
            # in the meta/heading set also changed (then it's clearly authored).
            if abs(wc_b - wc_a) >= BODY_WORD_THRESHOLD or fields:
                delta = wc_b - wc_a
                fields.append(f"body copy ({wc_a}→{wc_b} words, {'+' if delta>=0 else ''}{delta})")
        if fields:
            changed.append({"url": url, "fields": fields, "detail": detail})

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "broke": broke,
        "tracked": len(c_urls),
        "ok": sum(1 for v in c.values() if v.get("status") == 200),
        "prev_at": (prev or {}).get("generated_at"),
        "cur_at": (cur or {}).get("generated_at"),
    }


def build_snapshot(urls: list[str], workers: int = 20, prev: dict | None = None) -> dict:
    prev_pages = (prev or {}).get("pages", {}) if prev else {}
    pages = fetch_snapshot(urls, workers=workers, prev_pages=prev_pages)
    return {
        "generated_at": datetime.now(ZoneInfo("America/New_York")).isoformat(),
        "pages": pages,
    }


# ── Local setup helper: seed tracked URLs from the repo ────────────────────────


def build_tracked_urls_from_repo(repo_dir: str) -> list[str]:
    """
    LOCAL ONLY. Walk the repo's county directories (excluding *-beta staging
    dirs) and pull each page's ``<link rel="canonical">`` live URL, plus the
    county landing page. Returns a sorted, de-duplicated list.
    """
    root = Path(repo_dir)
    canon_re = re.compile(
        r'<link\s+rel=["\']canonical["\']\s+href=["\'](https://www\.paradiserealtyfla\.com/[^"\']+)["\']',
        re.I,
    )
    urls: set[str] = set()
    counties: set[str] = set()
    for county_dir in sorted(root.glob("*-county")):
        if county_dir.name.endswith("-beta"):
            continue
        for html_file in county_dir.glob("*.html"):
            try:
                text = html_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            m = canon_re.search(text)
            if m:
                urls.add(m.group(1).rstrip())
                counties.add(county_dir.name)
    for c in counties:
        urls.add(f"{SITE}/{c}/")
    return sorted(urls)
