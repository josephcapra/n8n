#!/usr/bin/env python3
"""Probe the RealGeeks media library for a sign image for every community in a JSON feed."""
import json, re, subprocess, sys, concurrent.futures as cf

BASE = "https://u.realgeeks.media/paradiserealtyfla/"
FOLDERS = ["community_signs"] + [f"community_signs_batch_{i}" for i in range(1, 9)] + ["signs", "community_images"]

def norm_slug(name): return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
def title_us(name): return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")

def candidates(c):
    n = c["name"]; county = re.sub(r"[^a-z0-9]+", "_", c.get("county", "").lower()).strip("_")
    slugs = list(dict.fromkeys([norm_slug(n), c.get("slug", ""), re.sub(r"-(at|the|of)-", "-", norm_slug(n))]))
    forms = [f"{title_us(n)}_Community_Sign", title_us(n)] + [s for s in slugs if s]
    folders = FOLDERS + [f"{county}_county_images", f"{county}_images"]
    out = []
    for f in folders:
        for form in forms:
            for ext in (".png", ".jpg", ".jpeg", ".webp"):
                out.append(BASE + f"{f}/{form}{ext}")
    return list(dict.fromkeys(out))

def head(u):
    r = subprocess.run(["curl", "-sIL", "-o", "/dev/null", "-w", "%{http_code} %{content_type}", "--max-time", "8", u],
                       capture_output=True, text=True).stdout
    return u, r

def probe(c):
    with cf.ThreadPoolExecutor(16) as ex:
        for u, r in ex.map(head, candidates(c)):
            if r.startswith("200") and "image" in r:
                return c["name"], u
    return c["name"], None

def main(src, out):
    comms = json.load(open(src))["communities"]
    hits = {}
    try: hits = json.load(open(out))
    except Exception: pass
    todo = [c for c in comms if c["name"] not in hits]
    print(f"{len(comms)} communities, {len(hits)} already known, probing {len(todo)}", flush=True)
    with cf.ThreadPoolExecutor(6) as ex:
        for i, (name, url) in enumerate(ex.map(probe, todo), 1):
            if url: hits[name] = url
            if i % 25 == 0:
                json.dump(hits, open(out, "w"), indent=2)
                print(f"  {i}/{len(todo)} probed, {len(hits)} found", flush=True)
    json.dump(hits, open(out, "w"), indent=2)
    print(f"done: {len(hits)}/{len(comms)} sign images")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
