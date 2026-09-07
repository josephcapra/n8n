#!/usr/bin/env python3
"""
Nightly Community Finder refresh (Cloud Run Job).
  1. pull build inputs from GCS
  2. crawl every community page: liveness (404 -> reroute) + newest active listing photo/address/price
  3. rebuild communities_all.js(.gz) + index.html with the existing build scripts
  4. publish to gs://paradise-realty-images/finder/  (the web service reads from there)
"""
import json, os, re, sys, time, concurrent.futures as cf

HOME = "/tmp/home"                       # build scripts derive every path from $HOME
os.environ["HOME"] = HOME
BV = f"{HOME}/builder-videos"; ST = f"{HOME}/paradise-staging/finder-test"; PF = f"{HOME}/paradise-finder/cloudrun-deploy/static"
for d in (BV, f"{BV}/signs", ST, PF): os.makedirs(d, exist_ok=True)

from google.cloud import storage
import requests

BUCKET = "paradise-realty-images"
IN, OUT = "finder/inputs/", "finder/"
INPUT_FILES = ["all988.json", "best_all.json", "demo31.json", "rg_sign_images.json", "rg_sign_images_all.json",
               "link_fixes.json", "county_hubs.json", "dead_subdivision_urls.json", "idx_photos.json", "subdivisions_data.js", "template.src.html",
               "extra_neighborhoods.json", "sitemap_verbatim_urls.json", "incentives_clean.json", "community_urls_master.json"]
UA = {"User-Agent": "Mozilla/5.0 (ParadiseFinderRefresh)"}
CARD = re.compile(r'href="(/property/[^"]+)".{0,3000}?property-images\.realgeeks\.com/([a-z]+/[a-f0-9]+\.jpg)[^"]*"\s+alt="([^"]*)"(.{0,1500}?\$([\d,]{5,}))?', re.S)
# lenient fallback for area pages whose listing widgets are laid out differently: photo first, nearest listing link after it
LOOSE = re.compile(r'property-images\.realgeeks\.com/([a-z]+/[a-f0-9]+\.jpg)[^"]*"[^>]*?alt="([^"]*)".{0,4000}?href="(/property/[^"]+)"(.{0,1500}?\$([\d,]{5,}))?', re.S)
IMG_TAG = re.compile(r'<img\b[^>]*>', re.I)
SRC = re.compile(r'\bsrc="(https://property-images\.realgeeks\.com/[a-z]+/[a-f0-9]+\.jpg)[^"]*"', re.I)
ALT = re.compile(r'\balt="([^"]*)"', re.I)
LINK = re.compile(r'href="(/property/[^"#?]+)"')
PRICE = re.compile(r'\$([\d,]{5,})')
EMPTY = re.compile(r"No current listings|no listings (?:were )?found|check back later", re.I)

def _norm(m):
    """Return (listing_path, photo_path, address, price_str) from either regex."""
    g = m.groups()
    return (g[0], g[1], g[2], g[4]) if g[0].startswith("/property/") else (g[2], g[0], g[1], g[4])

def log(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)

def pull(bucket):
    for f in INPUT_FILES:
        dest = f"{PF}/subdivisions_data.js" if f == "subdivisions_data.js" else f"{BV}/{f}"
        b = bucket.blob(IN + f)
        if b.exists(): b.download_to_filename(dest)
    # the dataset builder decides GCS-vs-RealGeeks sign URL by checking for a local <slug>.jpg; mirror the bucket listing as empty stubs
    n = 0
    for b in bucket.list_blobs(prefix="community-signs/"):
        name = b.name.split("/")[-1]
        if name.endswith(".jpg"): open(f"{BV}/signs/{name}", "a").close(); n += 1
    log(f"inputs pulled; {n} sign stubs")

import random, threading
CODES = {}; _clock = threading.Lock(); _pause_until = [0.0]
def fetch(url):
    """Polite fetch: up to 3 attempts; on 429/403/5xx/timeout back off (and pause the whole pool on 429/403)."""
    for attempt in range(3):
        wait = _pause_until[0] - time.time()
        if wait > 0: time.sleep(wait)
        try:
            r = requests.get(url, headers=UA, timeout=25, allow_redirects=True)
            code = r.status_code
        except Exception:
            code = 0
        with _clock: CODES[code] = CODES.get(code, 0) + 1
        if code == 200: return url, 200, r.text
        if code in (404, 410): return url, code, ""
        if code in (429, 403):
            retry_after = 0
            try: retry_after = int(r.headers.get("Retry-After", "0"))
            except Exception: pass
            _pause_until[0] = max(_pause_until[0], time.time() + max(retry_after, 20 * (attempt + 1)))
        time.sleep(random.uniform(1, 3) * (attempt + 1))
    return url, code, ""

def crawl(urls, prev_dead=frozenset(), prev_idx=None):
    """Returns (dead_list, idx_photos). One pass gives both liveness and the newest-listing photo.
    Only a definitive 404/410 marks a page dead. Timeouts, 429s and 5xx are 'unknown' — the page keeps
    its previous state (and previous photo) so a slow night at RealGeeks can't blank thousands of cards."""
    dead, idx, live, unknown, t0 = [], {}, set(), 0, time.time()
    prev_idx = prev_idx or {}
    with cf.ThreadPoolExecutor(8) as ex:
        for n, (u, code, html) in enumerate(ex.map(fetch, urls), 1):
            if code in (404, 410):
                dead.append([u, str(code)]); continue
            if code != 200:
                unknown += 1
                if u in prev_dead: dead.append([u, f"prev-{code}"])
                elif u in prev_idx: idx[u] = prev_idx[u]
                continue
            live.add(u)   # only a real 200 counts as live
            i = html.find("Newest Listings"); seg = html[i:] if i >= 0 else html
            if EMPTY.search(html):
                count, photo, alt, lp, price = 0, None, None, None, 0
            else:
                links = list(dict.fromkeys(LINK.findall(seg))); count = len(links)
                photo = alt = None
                for tag in IMG_TAG.findall(seg):
                    s = SRC.search(tag)
                    if s:
                        photo = s.group(1); a = ALT.search(tag); alt = a.group(1) if a else ""; break
                lp = links[0] if links else None
                price = 0
                if photo:
                    j = seg.find(photo); mp = PRICE.search(seg, max(0, j - 4000), j + 4000) or PRICE.search(seg)
                    if mp: price = int(mp.group(1).replace(",", ""))
            if photo and count:
                idx[u] = {"photo": photo, "address": alt or "", "listing_url": "https://www.paradiserealtyfla.com" + (lp or ""),
                          "price": price, "count": count, "checked": time.strftime("%Y-%m-%d")}
            else:
                idx[u] = {"count": count, "checked": time.strftime("%Y-%m-%d")}
            if n % 5000 == 0: log(f"  crawl {n}/{len(urls)}  dead={len(dead)} photos={len(idx)} unknown={unknown}  {int(time.time()-t0)}s")
    log(f"  crawl unknown (timeout/429/5xx, state carried forward): {unknown}; response codes seen: {dict(sorted(CODES.items(), key=lambda kv: -kv[1]))}")
    return dead, idx, live

def main():
    client = storage.Client(); bucket = client.bucket(BUCKET)
    pull(bucket)
    subs = json.loads(re.search(r"const DATA\s*=\s*(\[.*?\]);", open(f"{PF}/subdivisions_data.js").read(), re.S).group(1))
    curated = json.load(open(f"{BV}/all988.json"))["communities"]
    # The permanent URL registry is the crawl universe: every community URL ever seen, verbatim, append-only.
    # Pages come and go with IDX listings, so a URL that is dead today is still checked every run.
    reg_path = f"{BV}/community_urls_master.json"
    reg = json.load(open(reg_path)) if os.path.exists(reg_path) else {}
    today = time.strftime("%Y-%m-%d")
    def reg_add(u, src, **meta):
        if not u or not u.startswith("http"): return
        e = reg.setdefault(u, {"url": u, "slug": (re.search(r"/listings/subdivision/([^/?#]*)", u) or [None, ""])[1].lower() if "/listings/subdivision/" in u else "",
                               "sources": [], "first_seen": today, "last_checked": "", "last_live": "", "name": "", "county": "", "city": ""})
        if src not in e["sources"]: e["sources"].append(src)
        for k, v in meta.items():
            if v and not e.get(k): e[k] = v
    for s in subs: reg_add(s["ur"], "subdivisions_data.js", name=s["nm"], county=s["cn"], city=s.get("ct", ""))
    for c in curated:
        u = c.get("paradise_url") or ""
        if u and not u.startswith("http"): u = "https://www.paradiserealtyfla.com" + u
        if (c.get("name") or "").strip().lower() != "community name": reg_add(u, "sheet-communities", name=c.get("name", ""), county=c.get("county", ""), city=c.get("city", ""))
    urls = sorted(reg.keys())
    scope = os.environ.get("CRAWL_SCOPE", "all")
    if scope != "all":  # e.g. CRAWL_SCOPE=200 for a smoke test
        urls = urls[: int(scope)]
    log(f"crawling {len(urls)} pages from the registry ({len(reg)} URLs total)")
    prev_dead = {u for u, _ in (json.load(open(f"{BV}/dead_subdivision_urls.json")) if os.path.exists(f"{BV}/dead_subdivision_urls.json") else [])}
    prev_idx = json.load(open(f"{BV}/idx_photos.json")) if os.path.exists(f"{BV}/idx_photos.json") else {}
    dead, idx, live = crawl(urls, prev_dead, prev_idx)
    log(f"crawl done: dead={len(dead)} photos={len(idx)}")
    for u in urls:
        e = reg[u]; e["last_checked"] = today
        if u in live: e["last_live"] = today   # never stamp live on a timeout — that once invented 15k phantom communities
        if u in idx and idx[u].get("address") and not e.get("city"): e["city"] = idx[u]["address"].rsplit(",", 1)[-1].strip()
    json.dump(reg, open(reg_path, "w"))
    if scope == "all":
        json.dump(dead, open(f"{BV}/dead_subdivision_urls.json", "w"))
        json.dump(idx, open(f"{BV}/idx_photos.json", "w"))
    else:  # smoke test: merge into the prior full results instead of replacing them
        prev = json.load(open(f"{BV}/idx_photos.json")); prev.update(idx); json.dump(prev, open(f"{BV}/idx_photos.json", "w"))
    # neighborhoods that are live but absent from subdivisions_data.js become finder records (never removed once added)
    known = {s["ur"] for s in subs}
    from collections import Counter
    c2c = {}
    for s in subs:
        if s.get("ct") and s.get("cn"): c2c.setdefault(s["ct"].strip().lower(), Counter())[s["cn"]] += 1
    # NEVER SHRINK: every neighborhood that has ever been a finder record stays one (flagged while its page is down).
    prev_path = f"{BV}/extra_neighborhoods.json"
    extras = {e["url"]: e for e in (json.load(open(prev_path)) if os.path.exists(prev_path) else [])}
    for u in extras: reg.setdefault(u, {"url": u, "slug": "", "sources": ["extras"], "first_seen": today, "last_checked": "", "last_live": "", "name": "", "county": "", "city": ""})["finder_record"] = True
    import urllib.parse
    for u, e in reg.items():
        if u in known or u in extras or "/listings/subdivision/" not in u or "sheet-communities" in e["sources"]: continue
        if not (e.get("last_live") or e.get("finder_record")): continue
        name = e.get("name") or re.sub(r"\s+", " ", urllib.parse.unquote(e.get("slug", "")).replace("-", " ")).strip(" -:*'\"").title()
        county = e.get("county") or (c2c[e["city"].strip().lower()].most_common(1)[0][0] if e.get("city") and e["city"].strip().lower() in c2c else "")
        extras[u] = {"url": u, "name": name, "city": e.get("city", ""), "county": county, "desc": ""}; e["finder_record"] = True
    for u, x in extras.items():   # fill county later if the crawl learned the city
        if not x.get("county") and reg.get(u, {}).get("city") and reg[u]["city"].strip().lower() in c2c:
            x["county"] = c2c[reg[u]["city"].strip().lower()].most_common(1)[0][0]
    json.dump(list(extras.values()), open(prev_path, "w")); json.dump(reg, open(reg_path, "w"))
    log(f"registry: {len(reg)} URLs; neighborhoods carried as finder records beyond the base file: {len(extras)} (never fewer than last run)")

    # builder incentives straight from the sheet every run: expired dropped, rates redacted, contacts stripped
    try:
        import incentives
        pub, rev = incentives.build(curated, out_dir=BV)
        log(f"incentives: {len(pub)} publishable | expired {len(rev['expired'])} | no end date {len(rev['no_end_date'])} | unmatched {len(rev['unmatched'])}")
        bucket.blob(IN + "incentives_clean.json").upload_from_filename(f"{BV}/incentives_clean.json")
        bucket.blob(OUT + "reports/incentives_review.json").upload_from_filename(f"{BV}/incentives_review.json")
    except Exception as e:
        log(f"incentives: sheet refresh failed ({e}); keeping the previous published set")

    import build_dataset, build_finder
    build_finder.SRC = f"{BV}/template.src.html"
    build_dataset.main(); build_finder.main()

    for local, remote in ((f"{ST}/communities_all.js.gz", "communities_all.js.gz"), (f"{ST}/index.html", "index.html"),
                          (f"{ST}/communities_all.stats.json", "stats.json")):
        b = bucket.blob(OUT + remote)
        if remote.endswith(".gz"): b.content_encoding = "gzip"; b.content_type = "application/javascript"
        b.cache_control = "public, max-age=300"
        b.upload_from_filename(local)
    for f in ("dead_subdivision_urls.json", "idx_photos.json", "extra_neighborhoods.json", "community_urls_master.json"):
        bucket.blob(IN + f).upload_from_filename(f"{BV}/{f}")
    # dated, immutable copy of the registry in the backups bucket — the URLs are the constant asset
    client.bucket("paradise-realty-backups").blob(f"finder/community_urls_master-{today}.json").upload_from_filename(reg_path)
    stats = json.load(open(f"{ST}/communities_all.stats.json")); stats["refreshed"] = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    bucket.blob(OUT + "stats.json").upload_from_string(json.dumps(stats, indent=1), content_type="application/json")
    log("published:", json.dumps(stats))

if __name__ == "__main__":
    main()
