#!/usr/bin/env python3
"""Repair after the 2026-09-07 bug where timed-out pages were stamped 'live', inventing ~15k phantom communities.

A URL keeps/gains finder_record status only with POSITIVE evidence that its page really rendered:
  - it is in subdivisions_data.js (the MLS base set), or
  - idx_photos.json has an entry for it (only written on a real 200 with listing content), or
  - it is a curated Sheet community.
Everything else stays in the registry (still crawled every run, can return) but is not a community card.
"""
import json, os, re, subprocess

BV = os.path.expanduser("~/builder-videos"); PF = os.path.expanduser("~/paradise-finder/cloudrun-deploy/static")
subprocess.run(f"gsutil -m -q cp gs://paradise-realty-images/finder/inputs/community_urls_master.json "
               f"gs://paradise-realty-images/finder/inputs/idx_photos.json "
               f"gs://paradise-realty-images/finder/inputs/extra_neighborhoods.json {BV}/", shell=True, check=True)
reg = json.load(open(f"{BV}/community_urls_master.json"))
idx = json.load(open(f"{BV}/idx_photos.json"))
extras = {e["url"]: e for e in json.load(open(f"{BV}/extra_neighborhoods.json"))}
subs = json.loads(re.search(r"const DATA\s*=\s*(\[.*?\]);", open(f"{PF}/subdivisions_data.js").read(), re.S).group(1))
base = {s["ur"] for s in subs}
evidence = set(idx.keys())

before_records = sum(1 for e in reg.values() if e.get("finder_record"))
before_extras = len(extras)
kept, dropped = {}, 0
for u, e in extras.items():
    if u in base or u in evidence:
        kept[u] = e
    else:
        dropped += 1
for u, e in reg.items():
    if e.get("finder_record") and u not in kept and u not in base:
        e.pop("finder_record", None)
        e.pop("last_live", None)          # the stamp was false; let a real 200 re-set it
for u in kept: reg[u]["finder_record"] = True

json.dump(list(kept.values()), open(f"{BV}/extra_neighborhoods.json", "w"))
json.dump(reg, open(f"{BV}/community_urls_master.json", "w"))
print(f"registry URLs: {len(reg)} (unchanged — nothing removed)")
print(f"finder_record flags: {before_records} -> {sum(1 for e in reg.values() if e.get('finder_record'))}")
print(f"extra neighborhoods: {before_extras} -> {len(kept)} (dropped {dropped} with no evidence of a live page)")
subprocess.run(f"gsutil -m -q cp {BV}/community_urls_master.json {BV}/extra_neighborhoods.json gs://paradise-realty-images/finder/inputs/", shell=True, check=True)
print("uploaded repaired registry + extras to GCS inputs")
