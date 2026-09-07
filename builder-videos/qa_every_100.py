#!/usr/bin/env python3
"""Joe's acceptance check: take every 100th community in the published dataset and confirm the link the card
shows returns 200. Also confirms no record URL was altered from its source. Exit code 1 on any failure."""
import json, re, subprocess, sys, concurrent.futures as cf

DATA = sys.argv[1] if len(sys.argv) > 1 else "/Users/User/paradise-staging/finder-test/communities_all.js"
d = json.loads(re.search(r"const DATA=(\[.*\]);\n", open(DATA).read(), re.S).group(1))
sample = d[::100]
def shown_href(r): return (r.get("f") or r.get("h") or r["u"]) if r.get("dl") else r["u"]
def st(u): return subprocess.run(["curl", "-sIL", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "20", "-A", "Mozilla/5.0", u], capture_output=True, text=True).stdout
with cf.ThreadPoolExecutor(8) as ex: codes = list(ex.map(st, [shown_href(r) for r in sample]))
# a timeout ("000") is not a broken page — retry those sequentially with a longer window before judging
def st_slow(u): return subprocess.run(["curl", "-sIL", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "45", "-A", "Mozilla/5.0", u], capture_output=True, text=True).stdout
codes = [c if c != "000" else st_slow(shown_href(r)) for r, c in zip(sample, codes)]
bad = [(r["n"], c, shown_href(r)) for r, c in zip(sample, codes) if c != "200"]
print(f"every-100th check: {len(sample)} pages tested, {len(sample)-len(bad)} OK, {len(bad)} failed")
for b in bad[:20]: print("  FAIL", b)

# URL integrity: subdivision records must carry the exact source URL
subs = json.loads(re.search(r"const DATA\s*=\s*(\[.*?\]);", open("/Users/User/paradise-finder/cloudrun-deploy/static/subdivisions_data.js").read(), re.S).group(1))
src = {s["ur"] for s in subs}
altered = [r["u"] for r in d if r["t"] == 0 and not r.get("x_src") and r["u"] not in src and "/listings/subdivision/" in r["u"] and r["u"] not in json.load(open("/Users/User/builder-videos/sitemap_verbatim_urls.json")).values()]
print(f"URL integrity: {len(altered)} subdivision URLs differ from their source (expect 0)")
sys.exit(1 if bad or altered else 0)
