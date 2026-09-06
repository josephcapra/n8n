#!/usr/bin/env python3
"""For every located RealGeeks sign image: download once, make 880px (site) + 480px (artifact) JPEGs, upload 880px to GCS."""
import json, os, re, subprocess, concurrent.futures as cf

HOME = os.path.expanduser("~")
SIGNS = json.load(open(f"{HOME}/builder-videos/rg_sign_images_all.json"))
BIG, SMALL = f"{HOME}/builder-videos/signs", f"{HOME}/builder-videos/signs_small"
os.makedirs(BIG, exist_ok=True); os.makedirs(SMALL, exist_ok=True)
def slug(n): return re.sub(r"[^a-z0-9]+", "-", n.lower()).strip("-")

def work(item):
    name, url = item
    s = slug(name); big, small, raw = f"{BIG}/{s}.jpg", f"{SMALL}/{s}.jpg", f"{BIG}/{s}.orig"
    if os.path.exists(big) and os.path.exists(small): return s, "have"
    if subprocess.run(["curl", "-sL", "--max-time", "60", "-o", raw, url]).returncode != 0: return s, "download-failed"
    ok1 = subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "78", "--resampleWidth", "880", raw, "--out", big], capture_output=True).returncode == 0
    ok2 = subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "62", "--resampleWidth", "480", raw, "--out", small], capture_output=True).returncode == 0
    if os.path.exists(raw): os.remove(raw)
    return s, "made" if ok1 and ok2 else "convert-failed"

def main():
    from collections import Counter
    res = Counter()
    with cf.ThreadPoolExecutor(8) as ex:
        for s, status in ex.map(work, SIGNS.items()): res[status] += 1
    print("local:", dict(res))
    r = subprocess.run(["gsutil", "-m", "-q", "rsync", "-r", "-x", r".*\.orig$", BIG, "gs://paradise-realty-images/community-signs/"], capture_output=True, text=True)
    subprocess.run(["gsutil", "-m", "-q", "setmeta", "-h", "Cache-Control:public, max-age=604800", "gs://paradise-realty-images/community-signs/*.jpg"], capture_output=True)
    n = len([f for f in os.listdir(BIG) if f.endswith(".jpg")])
    print(f"GCS community-signs/: {n} files synced" + (f" (rsync stderr: {r.stderr.strip()[:200]})" if r.returncode else ""))

if __name__ == "__main__":
    main()
