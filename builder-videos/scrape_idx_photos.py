#!/usr/bin/env python3
"""Pull the first *active* listing's IDX photo (+ address, price, listing link) from each community page.
Re-runnable: a nightly run refreshes every entry, so a sold listing drops out automatically."""
import json, os, re, subprocess, sys, time, concurrent.futures as cf
HOME = os.path.expanduser("~")
DATA_JS = f"{HOME}/paradise-staging/finder-test/communities_all.js"
OUT = f"{HOME}/builder-videos/idx_photos.json"
COUNTIES = set(sys.argv[1].split(",")) if len(sys.argv) > 1 else {"Martin", "St. Lucie", "Indian River", "Palm Beach"}
CARD = re.compile(r'href="(/property/[^"]+)".{0,3000}?property-images\.realgeeks\.com/([a-z]+/[a-f0-9]+\.jpg)[^"]*"\s+alt="([^"]*)"(.{0,1500}?\$([\d,]{5,}))?', re.S)

def scrape(rec):
    u = rec["u"]
    try:
        html = subprocess.run(["curl", "-sL", "-A", "Mozilla/5.0", "--max-time", "25", u], capture_output=True, text=True, timeout=40).stdout
    except Exception:
        return u, None
    i = html.find("Newest Listings")
    m = CARD.search(html[i:] if i >= 0 else html)
    if not m: return u, None
    return u, {"photo": f"https://property-images.realgeeks.com/{m.group(2)}", "address": m.group(3), "listing_url": "https://www.paradiserealtyfla.com" + m.group(1),
               "price": int(m.group(5).replace(",", "")) if m.group(5) else 0, "checked": time.strftime("%Y-%m-%d")}

def main():
    d = json.loads(re.search(r"const DATA=(\[.*\]);\n", open(DATA_JS).read(), re.S).group(1))
    targets = [r for r in d if not r.get("g") and "search/results" not in r["u"] and (r["t"] == 1 or r["c"] in COUNTIES)]
    print(f"{len(targets)} pages to check ({sum(r['t'] for r in targets)} curated w/o sign + MLS in {sorted(COUNTIES)})", flush=True)
    out, t0 = {}, time.time()
    with cf.ThreadPoolExecutor(20) as ex:
        for n, (u, hit) in enumerate(ex.map(scrape, targets), 1):
            if hit: out[u] = hit
            if n % 500 == 0:
                json.dump(out, open(OUT, "w")); print(f"  {n}/{len(targets)} pages, {len(out)} photos, {int(time.time()-t0)}s", flush=True)
    json.dump(out, open(OUT, "w"), indent=1)
    print(f"DONE: {len(out)} communities got an IDX photo out of {len(targets)} checked")
if __name__ == "__main__": main()
