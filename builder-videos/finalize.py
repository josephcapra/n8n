#!/usr/bin/env python3
"""
One-shot finalize after a cloud crawl has published:
  1. pull the crawl outputs (idx photos w/ live counts, dead list, extras, registry) from GCS inputs
  2. rebuild dataset + page locally with the CURRENT build scripts and publish them to gs://.../finder/
  3. acceptance check (every 100th page), regenerate sitemaps + LLMs.txt from the published dataset
  4. upload LLMs.txt to its bucket and sitemaps to gs://.../finder/sitemaps/ (NOT submitted anywhere)
Prints a JSON summary for the hand-off email.
"""
import json, os, re, subprocess, sys, datetime
HOME = os.path.expanduser("~"); BV = f"{HOME}/builder-videos"; ST = f"{HOME}/paradise-staging/finder-test"
IN = "gs://paradise-realty-images/finder/inputs"; OUT = "gs://paradise-realty-images/finder"
def sh(cmd, check=True): return subprocess.run(cmd, shell=True, check=check, capture_output=True, text=True).stdout.strip()

sh(f"gsutil -m -q cp {IN}/idx_photos.json {IN}/dead_subdivision_urls.json {IN}/extra_neighborhoods.json {IN}/community_urls_master.json {BV}/")
print("pulled crawl outputs from GCS")
print(sh(f"cd {BV} && python3 build_dataset.py | grep -E 'hygiene|idx photos|extra resale|\"total\"|gzipped'"))
print(sh(f"cd {BV} && python3 build_finder.py"))
qa = subprocess.run(f"cd {BV} && python3 qa_every_100.py", shell=True, capture_output=True, text=True); print(qa.stdout.strip())
if qa.returncode != 0: sys.exit("every-100th check failed — not publishing")

stats = json.load(open(f"{ST}/communities_all.stats.json")); stats["refreshed"] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"); stats["published_by"] = "finalize.py"
json.dump(stats, open(f"{ST}/communities_all.stats.json", "w"), indent=1)
sh(f'gsutil -q -h "Content-Encoding:gzip" -h "Content-Type:application/javascript" -h "Cache-Control:public, max-age=300" cp {ST}/communities_all.js.gz {OUT}/communities_all.js.gz')
sh(f'gsutil -q -h "Cache-Control:public, max-age=300" cp {ST}/index.html {OUT}/index.html')
sh(f'gsutil -q -h "Cache-Control:no-cache" cp {ST}/communities_all.stats.json {OUT}/stats.json')
print("published page + data to GCS")

print(sh(f"cd {BV} && python3 make_sitemaps.py"))
print(sh(f"cd {BV} && python3 make_llms.py"))
sh(f"gsutil -m -q cp {HOME}/paradise-staging/sitemaps/*.xml {OUT}/sitemaps/")
sh(f"gsutil -q cp gs://site-map-dynamic-page3/LLMs.txt gs://paradise-realty-backups/finder/LLMs-before-{datetime.date.today().isoformat()}.txt", check=False)
sh(f"gsutil -q -h 'Content-Type:text/plain; charset=utf-8' -h 'Cache-Control:public, max-age=3600' cp {HOME}/paradise-staging/llms/LLMs.txt gs://site-map-dynamic-page3/LLMs.txt")
print("sitemaps -> finder/sitemaps/ ; LLMs.txt updated in site-map-dynamic-page3 (previous copy backed up)")

d = json.loads(re.search(r"const DATA=(\[.*\]);\n", open(f"{ST}/communities_all.js").read(), re.S).group(1))
summary = {**stats, "unique_urls": len({r["u"] for r in d}), "cards_with_extra_areas": sum(1 for r in d if r.get("ys")),
           "live_pages": sum(1 for r in d if not r.get("dl")), "pages_currently_404": sum(1 for r in d if r.get("dl")),
           "idx_photos": sum(1 for r in d if r.get("ph")), "live_listing_counts": sum(1 for r in d if r.get("lv")), "incentives": sum(1 for r in d if r.get("i"))}
json.dump(summary, open(f"{BV}/final_summary.json", "w"), indent=1); print(json.dumps(summary, indent=1))
