#!/usr/bin/env python3
"""Pick one hero video per community from official candidates: prefer community overviews over model walkthroughs."""
import json, re, sys

GOOD = re.compile(r"welcome|overview|community|tour the|vision|lifestyle|amenit|clubhouse|discover", re.I)
BAD = re.compile(r"update|sold|ad$|drone|pour|foundation|construction|#\d|no\.\d|\$\d", re.I)

def score(v):
    s = 0
    t = v["title"]
    if GOOD.search(t): s += 3
    if BAD.search(t): s -= 3
    try:
        d = int(v.get("duration") or 0)
    except ValueError:
        d = 0
    if 30 <= d <= 400: s += 2
    elif d < 20: s -= 1
    return s

def main(src, out):
    vids = json.load(open(src))
    by = {}
    for v in vids:
        by.setdefault(v["community"], []).append(v)
    best = {}
    for name, lst in by.items():
        lst.sort(key=score, reverse=True)
        best[name] = {"hero": lst[0], "all": lst}
        print(f"{name:<32} -> {lst[0]['video_id']}  {lst[0]['title'][:60]}  (+{len(lst)-1} more)")
    json.dump(best, open(out, "w"), indent=2)
    print(f"\n{len(best)} communities with a hero video -> {out}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
