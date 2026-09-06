#!/usr/bin/env python3
"""Permanent registry of every community/neighborhood URL ever seen. Append-only: entries are never removed,
URLs are never modified. The nightly job updates last_checked/last_live and appends newly discovered URLs."""
import json, re, os, datetime
HOME = os.path.expanduser("~"); BV = f"{HOME}/builder-videos"
REG = f"{BV}/community_urls_master.json"
today = datetime.date.today().isoformat()
reg = json.load(open(REG)) if os.path.exists(REG) else {}
def slug(u): m = re.search(r"/listings/subdivision/([^/?#]*)", u); return m.group(1).lower() if m else ""
def add(u, src, **meta):
    if not u or not u.startswith("http"): return
    e = reg.setdefault(u, {"url": u, "slug": slug(u), "sources": [], "first_seen": today, "last_checked": "", "last_live": "", "name": "", "county": "", "city": ""})
    if src not in e["sources"]: e["sources"].append(src)
    for k, v in meta.items():
        if v and not e.get(k): e[k] = v
subs = json.loads(re.search(r"const DATA\s*=\s*(\[.*?\]);", open(f"{HOME}/paradise-finder/cloudrun-deploy/static/subdivisions_data.js").read(), re.S).group(1))
for s in subs: add(s["ur"], "subdivisions_data.js", name=s["nm"], county=s["cn"], city=s.get("ct", ""))
for u in json.load(open(f"{BV}/sitemap_verbatim_urls.json")).values(): add(u, "sitemaps")
for u in json.load(open(f"{BV}/supabase_base_urls.json")): add(u, "supabase")
for e in json.load(open(f"{BV}/extra_neighborhoods.json")): add(e["url"], "sitemaps" if e.get("src") != "supabase" else "supabase", name=e["name"], county=e.get("county", ""), city=e.get("city", ""))
for c in json.load(open(f"{BV}/all988.json"))["communities"]:
    u = c.get("paradise_url") or ""
    if u and not u.startswith("http"): u = "https://www.paradiserealtyfla.com" + u
    if c.get("name", "").strip().lower() != "community name": add(u, "sheet-communities", name=c["name"], county=c["county"], city=c.get("city", ""))
dead = {u for u, _ in json.load(open(f"{BV}/dead_subdivision_urls.json"))}
verified_live = {e["url"] for e in json.load(open(f"{BV}/extra_neighborhoods.json"))}   # HEAD-checked 200 today
for u, e in reg.items():
    if u in dead: e["last_checked"] = today
    elif "subdivisions_data.js" in e["sources"] or u in verified_live:
        e["last_checked"] = e["last_checked"] or today; e["last_live"] = e["last_live"] or today
    if u in verified_live: e["finder_record"] = True   # once a neighborhood is a finder record it stays one
json.dump(reg, open(REG, "w"), indent=0)
from collections import Counter
print(f"registry: {len(reg):,} URLs (append-only) | by source: {Counter(s for e in reg.values() for s in e['sources'])}")
print(f"  with name/county: {sum(1 for e in reg.values() if e['name'] and e['county']):,} | {os.path.getsize(REG)//1024} KB")
