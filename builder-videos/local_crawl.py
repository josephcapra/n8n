#!/usr/bin/env python3
"""Full crawl from this machine (residential egress — RealGeeks throttles Google Cloud IPs, not this one).
Mirrors refresh.py's rules: only 404/410 is dead; timeouts carry prior state forward; only a real 200 is live.
Writes dead_subdivision_urls.json + idx_photos.json and uploads them to the GCS inputs the build reads.
"""
import json, os, re, subprocess, time, random, threading, concurrent.futures as cf
from collections import Counter

BV = os.path.expanduser("~/builder-videos"); PF = os.path.expanduser("~/paradise-finder/cloudrun-deploy/static")
CARD = re.compile(r'href="(/property/[^"]+)".{0,3000}?property-images\.realgeeks\.com/([a-z]+/[a-f0-9]+\.jpg)[^"]*"\s+alt="([^"]*)"(.{0,1500}?\$([\d,]{5,}))?', re.S)
LOOSE = re.compile(r'property-images\.realgeeks\.com/([a-z]+/[a-f0-9]+\.jpg)[^"]*"[^>]*?alt="([^"]*)".{0,4000}?href="(/property/[^"]+)"(.{0,1500}?\$([\d,]{5,}))?', re.S)
def norm(m): g = m.groups(); return (g[0], g[1], g[2], g[4]) if g[0].startswith("/property/") else (g[2], g[0], g[1], g[4])

subs = json.loads(re.search(r"const DATA\s*=\s*(\[.*?\]);", open(f"{PF}/subdivisions_data.js").read(), re.S).group(1))
extras = json.load(open(f"{BV}/extra_neighborhoods.json"))
curated = json.load(open(f"{BV}/all988.json"))["communities"]
def cur_url(c):
    u = c.get("paradise_url") or ""
    return ("https://www.paradiserealtyfla.com" + u) if u and not u.startswith("http") else u
urls = sorted({s["ur"] for s in subs} | {e["url"] for e in extras} | {cur_url(c) for c in curated if cur_url(c).startswith("http")})
prev_dead = {u for u, _ in (json.load(open(f"{BV}/dead_subdivision_urls.json")) if os.path.exists(f"{BV}/dead_subdivision_urls.json") else [])}
prev_idx = json.load(open(f"{BV}/idx_photos.json")) if os.path.exists(f"{BV}/idx_photos.json") else {}
print(f"crawling {len(urls)} community pages (base {len(subs)} + extras {len(extras)} + curated)", flush=True)

codes = Counter(); lock = threading.Lock(); pause = [0.0]
def fetch(u):
    for attempt in range(3):
        w = pause[0] - time.time()
        if w > 0: time.sleep(w)
        r = subprocess.run(["curl", "-sL", "-A", "Mozilla/5.0", "--max-time", "25", "-w", "\n%{http_code}", u], capture_output=True, text=True)
        try: html, code = r.stdout.rsplit("\n", 1)
        except ValueError: html, code = "", "000"
        with lock: codes[code] += 1
        if code == "200": return u, 200, html
        if code in ("404", "410"): return u, int(code), ""
        if code in ("429", "403"):
            with lock: pause[0] = max(pause[0], time.time() + 20 * (attempt + 1))
        time.sleep(random.uniform(1, 3) * (attempt + 1))
    return u, 0, ""

dead, idx, live, unknown, t0 = [], {}, set(), 0, time.time()
with cf.ThreadPoolExecutor(12) as ex:
    for n, (u, code, html) in enumerate(ex.map(fetch, urls), 1):
        if code in (404, 410): dead.append([u, str(code)])
        elif code != 200:
            unknown += 1
            if u in prev_dead: dead.append([u, f"prev-{code}"])
            elif u in prev_idx: idx[u] = prev_idx[u]
        else:
            live.add(u)
            i = html.find("Newest Listings"); seg = html[i:] if i >= 0 else html
            m = CARD.search(seg) or CARD.search(html) or LOOSE.search(html)
            # count ONLY the listing links this community actually shows; a page that says it has none has none
            if re.search(r"No current listings|no listings (?:were )?found|check back later", html, re.I):
                count, m = 0, None
            else:
                count = len(set(re.findall(r'href="(/property/[^"]+)"', seg)))
            if m and count:
                lp, pp, addr, pr = norm(m)
                idx[u] = {"photo": f"https://property-images.realgeeks.com/{pp}", "address": addr, "listing_url": "https://www.paradiserealtyfla.com" + lp,
                          "price": int(pr.replace(",", "")) if pr else 0, "count": count, "checked": time.strftime("%Y-%m-%d")}
            else: idx[u] = {"count": count, "checked": time.strftime("%Y-%m-%d")}
        if n % 5000 == 0:
            print(f"  {n}/{len(urls)} live={len(live)} dead={len(dead)} photos={sum(1 for v in idx.values() if v.get('photo'))} unknown={unknown} {int(time.time()-t0)}s", flush=True)
            json.dump(dead, open(f"{BV}/dead_subdivision_urls.json", "w")); json.dump(idx, open(f"{BV}/idx_photos.json", "w"))

json.dump(dead, open(f"{BV}/dead_subdivision_urls.json", "w")); json.dump(idx, open(f"{BV}/idx_photos.json", "w"))
photos = sum(1 for v in idx.values() if v.get("photo"))
print(f"DONE {len(urls)} pages in {int(time.time()-t0)}s: live={len(live)} dead={len(dead)} photos={photos} unknown={unknown}")
print("codes:", dict(codes.most_common()))
reg = json.load(open(f"{BV}/community_urls_master.json")); today = time.strftime("%Y-%m-%d")
for u in urls:
    e = reg.get(u)
    if not e: continue
    e["last_checked"] = today
    if u in live: e["last_live"] = today
json.dump(reg, open(f"{BV}/community_urls_master.json", "w"))
subprocess.run(f"gsutil -m -q cp {BV}/dead_subdivision_urls.json {BV}/idx_photos.json {BV}/community_urls_master.json gs://paradise-realty-images/finder/inputs/", shell=True, check=True)
print("uploaded crawl outputs + registry to GCS inputs")
