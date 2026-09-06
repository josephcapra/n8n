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

def fetch(url):
    try:
        r = requests.get(url, headers=UA, timeout=25, allow_redirects=True)
        return url, r.status_code, (r.text if r.status_code == 200 else "")
    except Exception:
        return url, 0, ""

def crawl(urls):
    """Returns (dead_list, idx_photos). One pass gives both liveness and the newest-listing photo."""
    dead, idx, t0 = [], {}, time.time()
    with cf.ThreadPoolExecutor(24) as ex:
        for n, (u, code, html) in enumerate(ex.map(fetch, urls), 1):
            if code != 200:
                dead.append([u, str(code)]); continue
            i = html.find("Newest Listings")
            seg = html[i:] if i >= 0 else html
            m = CARD.search(seg)
            # live listing count: prefer an explicit "N Homes/Properties/Listings for Sale" total, else count listing cards
            tot = re.search(r"(\d[\d,]*)\s+(?:Homes?|Properties|Listings|Results)\s+(?:for Sale|Found|Available)", html, re.I)
            count = int(tot.group(1).replace(",", "")) if tot else len(re.findall(r'href="/property/[^"]+"', seg))
            if m:
                idx[u] = {"photo": f"https://property-images.realgeeks.com/{m.group(2)}", "address": m.group(3),
                          "listing_url": "https://www.paradiserealtyfla.com" + m.group(1),
                          "price": int(m.group(5).replace(",", "")) if m.group(5) else 0, "count": count, "checked": time.strftime("%Y-%m-%d")}
            elif count:
                idx[u] = {"count": count, "checked": time.strftime("%Y-%m-%d")}
            if n % 5000 == 0: log(f"  crawl {n}/{len(urls)}  dead={len(dead)} photos={len(idx)}  {int(time.time()-t0)}s")
    return dead, idx

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
    dead, idx = crawl(urls)
    log(f"crawl done: dead={len(dead)} photos={len(idx)}")
    dead_set = {u for u, _ in dead}
    for u in urls:
        e = reg[u]; e["last_checked"] = today
        if u not in dead_set: e["last_live"] = today
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
