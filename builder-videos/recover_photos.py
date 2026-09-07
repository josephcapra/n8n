#!/usr/bin/env python3
"""One-time recovery: for every live community page that lacks a listing photo after a throttled cloud crawl,
fetch the page from here (residential egress is not throttled) and merge photo/address/price/count into idx_photos.json.
Same parsers as refresh.py."""
import json, re, subprocess, time, concurrent.futures as cf
from collections import Counter
HOME = "/Users/User"; BV = f"{HOME}/builder-videos"
CARD = re.compile(r'href="(/property/[^"]+)".{0,3000}?property-images\.realgeeks\.com/([a-z]+/[a-f0-9]+\.jpg)[^"]*"\s+alt="([^"]*)"(.{0,1500}?\$([\d,]{5,}))?', re.S)
LOOSE = re.compile(r'property-images\.realgeeks\.com/([a-z]+/[a-f0-9]+\.jpg)[^"]*"[^>]*?alt="([^"]*)".{0,4000}?href="(/property/[^"]+)"(.{0,1500}?\$([\d,]{5,}))?', re.S)
def norm(m): g = m.groups(); return (g[0], g[1], g[2], g[4]) if g[0].startswith("/property/") else (g[2], g[0], g[1], g[4])

subprocess.run(f"gsutil -m -q cp gs://paradise-realty-images/finder/inputs/idx_photos.json gs://paradise-realty-images/finder/inputs/dead_subdivision_urls.json gs://paradise-realty-images/finder/inputs/community_urls_master.json {BV}/", shell=True, check=True)
idx = json.load(open(f"{BV}/idx_photos.json")); dead = {u for u, _ in json.load(open(f"{BV}/dead_subdivision_urls.json"))}
reg = json.load(open(f"{BV}/community_urls_master.json"))
targets = [u for u, e in reg.items() if u not in dead and not idx.get(u, {}).get("photo") and (e.get("finder_record") or "subdivisions_data.js" in e["sources"] or "sheet-communities" in e["sources"])]
print(f"idx has {len(idx)} entries; live pages without a photo to try: {len(targets)}")

def fetch(u):
    try:
        out = subprocess.run(["curl", "-sL", "-A", "Mozilla/5.0", "--max-time", "25", "-w", "\n%{http_code}", u], capture_output=True, text=True, timeout=40).stdout
        html, code = out.rsplit("\n", 1)
    except Exception: return u, "000", None
    if code != "200": return u, code, None
    i = html.find("Newest Listings"); seg = html[i:] if i >= 0 else html
    m = CARD.search(seg) or CARD.search(html) or LOOSE.search(html)
    tot = re.search(r"(\d[\d,]*)\s+(?:Homes?|Properties|Listings|Results)\s+(?:for Sale|Found|Available)", html, re.I)
    count = int(tot.group(1).replace(",", "")) if tot else len(re.findall(r'href="/property/[^"]+"', seg))
    if not m: return u, code, {"count": count} if count else None
    lp, pp, addr, pr = norm(m)
    return u, code, {"photo": f"https://property-images.realgeeks.com/{pp}", "address": addr, "listing_url": "https://www.paradiserealtyfla.com" + lp,
                     "price": int(pr.replace(",", "")) if pr else 0, "count": count, "checked": time.strftime("%Y-%m-%d")}
codes, got, newly_dead, t0 = Counter(), 0, [], time.time()
with cf.ThreadPoolExecutor(8) as ex:
    for n, (u, code, hit) in enumerate(ex.map(fetch, targets), 1):
        codes[code] += 1
        if hit and hit.get("photo"): idx[u] = {**idx.get(u, {}), **hit}; got += 1
        elif hit: idx[u] = {**idx.get(u, {}), **hit}
        if code in ("404", "410"): newly_dead.append([u, code])
        if n % 1000 == 0: print(f"  {n}/{len(targets)} photos+{got} codes={dict(codes)} {int(time.time()-t0)}s", flush=True)
json.dump(idx, open(f"{BV}/idx_photos.json", "w"))
if newly_dead:
    d = json.load(open(f"{BV}/dead_subdivision_urls.json")); d.extend(newly_dead); json.dump(d, open(f"{BV}/dead_subdivision_urls.json", "w"))
subprocess.run(f"gsutil -m -q cp {BV}/idx_photos.json {BV}/dead_subdivision_urls.json gs://paradise-realty-images/finder/inputs/", shell=True, check=True)
print(f"DONE: +{got} photos (idx now {sum(1 for v in idx.values() if v.get('photo'))} with photo); codes={dict(codes)}; newly confirmed 404: {len(newly_dead)}; uploaded to GCS inputs")
