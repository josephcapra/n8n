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
               "extra_neighborhoods.json", "sitemap_verbatim_urls.json", "incentives_clean.json"]
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
            m = CARD.search(html[i:] if i >= 0 else html)
            if m:
                idx[u] = {"photo": f"https://property-images.realgeeks.com/{m.group(2)}", "address": m.group(3),
                          "listing_url": "https://www.paradiserealtyfla.com" + m.group(1),
                          "price": int(m.group(5).replace(",", "")) if m.group(5) else 0, "checked": time.strftime("%Y-%m-%d")}
            if n % 5000 == 0: log(f"  crawl {n}/{len(urls)}  dead={len(dead)} photos={len(idx)}  {int(time.time()-t0)}s")
    return dead, idx

def main():
    client = storage.Client(); bucket = client.bucket(BUCKET)
    pull(bucket)
    subs = json.loads(re.search(r"const DATA\s*=\s*(\[.*?\]);", open(f"{PF}/subdivisions_data.js").read(), re.S).group(1))
    curated = json.load(open(f"{BV}/all988.json"))["communities"]
    extras = json.load(open(f"{BV}/extra_neighborhoods.json")) if os.path.exists(f"{BV}/extra_neighborhoods.json") else []
    verbatim = json.load(open(f"{BV}/sitemap_verbatim_urls.json")) if os.path.exists(f"{BV}/sitemap_verbatim_urls.json") else {}
    def extra_url(e):
        m = re.search(r"/listings/subdivision/([^/?#]*)", e["url"]); return verbatim.get(m.group(1).lower(), e["url"]) if m else e["url"]
    urls = sorted({s["ur"] for s in subs} | {extra_url(e) for e in extras}
                  | {(c.get("paradise_url") or "") for c in curated if (c.get("paradise_url") or "").startswith("http")})
    scope = os.environ.get("CRAWL_SCOPE", "all")
    if scope != "all":  # e.g. CRAWL_SCOPE=200 for a smoke test
        urls = urls[: int(scope)]
    log(f"crawling {len(urls)} pages")
    dead, idx = crawl(urls)
    log(f"crawl done: dead={len(dead)} photos={len(idx)}")
    if scope == "all":
        json.dump(dead, open(f"{BV}/dead_subdivision_urls.json", "w"))
        json.dump(idx, open(f"{BV}/idx_photos.json", "w"))
    else:  # smoke test: merge into the prior full results instead of replacing them
        prev = json.load(open(f"{BV}/idx_photos.json")); prev.update(idx); json.dump(prev, open(f"{BV}/idx_photos.json", "w"))

    import build_dataset, build_finder
    build_finder.SRC = f"{BV}/template.src.html"
    build_dataset.main(); build_finder.main()

    for local, remote in ((f"{ST}/communities_all.js.gz", "communities_all.js.gz"), (f"{ST}/index.html", "index.html"),
                          (f"{ST}/communities_all.stats.json", "stats.json")):
        b = bucket.blob(OUT + remote)
        if remote.endswith(".gz"): b.content_encoding = "gzip"; b.content_type = "application/javascript"
        b.cache_control = "public, max-age=300"
        b.upload_from_filename(local)
    for f in ("dead_subdivision_urls.json", "idx_photos.json"):
        bucket.blob(IN + f).upload_from_filename(f"{BV}/{f}")
    stats = json.load(open(f"{ST}/communities_all.stats.json")); stats["refreshed"] = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    bucket.blob(OUT + "stats.json").upload_from_string(json.dumps(stats, indent=1), content_type="application/json")
    log("published:", json.dumps(stats))

if __name__ == "__main__":
    main()
