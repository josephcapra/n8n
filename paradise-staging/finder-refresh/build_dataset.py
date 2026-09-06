#!/usr/bin/env python3
"""
Merge the finder's 41k MLS subdivisions with the 988 curated Sheet communities
(+ official videos + sign images) into one compact JS data file for the finder page.

Record schema (compact):
  n  name          c  county        y  city          u  url (View Community)
  h  homes url     b  builder       p  price_from    x  price_to (subdivisions)
  l  active listings                d  description   t  tier: 1 = curated (Sheet), 0 = MLS subdivision
  i  incentive headline (or "")     v  video youtube url   vs video source channel
  g  sign image url                 k  key (dedupe)
"""
import json, os, re

HOME = os.path.expanduser("~")
SUBS = f"{HOME}/paradise-finder/cloudrun-deploy/static/subdivisions_data.js"
CURATED = f"{HOME}/builder-videos/all988.json"
VIDEOS = f"{HOME}/builder-videos/best_all.json"
SIGNS_31 = f"{HOME}/builder-videos/rg_sign_images.json"
SIGNS_ALL = f"{HOME}/builder-videos/rg_sign_images_all.json"
OUT_JS = f"{HOME}/paradise-staging/finder-test/communities_all.js"
OUT_STATS = f"{HOME}/paradise-staging/finder-test/communities_all.stats.json"
GCS_SIGN = "https://storage.googleapis.com/paradise-realty-images/community-signs/{}.jpg"
LINK_OVERRIDES = {"Lakewood Ranch": "https://www.paradiserealtyfla.com/manatee-county/",
                  "The Villages": "https://www.paradiserealtyfla.com/sumter-county/"}

def norm(s): return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
def key(county, name): return f"{norm(county)}|{norm(name)}"
def slug(name): return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

def load_subs():
    s = open(SUBS).read()
    m = re.search(r"const DATA\s*=\s*(\[.*?\]);", s, re.S)
    return json.loads(m.group(1))

def main():
    subs = load_subs()
    cur = json.load(open(CURATED))["communities"]
    vids = json.load(open(VIDEOS)) if os.path.exists(VIDEOS) else {}
    signs = {}
    for p in (SIGNS_31, SIGNS_ALL):
        if os.path.exists(p): signs.update(json.load(open(p)))
    local_signs = {os.path.splitext(f)[0] for f in os.listdir(f"{HOME}/builder-videos/signs") if f.endswith(".jpg")}

    # incentives: the GCS feed carries none today; fall back to the last snapshot that had them (demo31.json)
    inc_fallback = {}
    p31 = f"{HOME}/builder-videos/demo31.json"
    if os.path.exists(p31):
        for c in json.load(open(p31))["communities"]:
            if c.get("incentives"): inc_fallback[c["name"]] = c["incentives"][0].get("headline", "")

    # sanitized builder incentives (sanitize_incentives.py): expired dropped, rates redacted, commission/contact removed
    inc_clean = jload_path = f"{HOME}/builder-videos/incentives_clean.json"
    inc_clean = json.load(open(jload_path)) if os.path.exists(jload_path) else {}
    for name, v in inc_clean.items():
        inc_fallback[name] = v["headline"]

    def sane(p):  # MLS feed has placeholder prices like 999,999,999,999
        p = p or 0
        return p if 10_000 <= p <= 100_000_000 else 0

    records, by_key = [], {}
    for s in subs:
        # subdivision rows stay minimal; the page derives homes-url and blurb from these fields
        r = {"n": s["nm"], "c": s["cn"], "y": s.get("ct", ""), "u": s["ur"],
             "p": sane(s.get("mn")), "x": sane(s.get("mx")), "l": s.get("lc") or 0, "t": 0, "k": key(s["cn"], s["nm"])}
        if s.get("ty"): r["ty"] = s["ty"]
        by_key[r["k"]] = r
        records.append(r)

    # resale neighborhoods found in the site's sitemaps but absent from subdivisions_data.js — added, never dropped.
    # URLs are used exactly as published (see sitemap_verbatim_urls.json); nothing is decoded, re-encoded or normalized.
    extra_path = f"{HOME}/builder-videos/extra_neighborhoods.json"
    if os.path.exists(extra_path):
        verbatim = json.load(open(f"{HOME}/builder-videos/sitemap_verbatim_urls.json")) if os.path.exists(f"{HOME}/builder-videos/sitemap_verbatim_urls.json") else {}
        n_extra = 0
        for e in json.load(open(extra_path)):
            m = re.search(r"/listings/subdivision/([^/?#]*)", e["url"]); slug_key = m.group(1).lower() if m else ""
            url = verbatim.get(slug_key, e["url"])
            k = key(e.get("county", ""), e["name"])
            if url in {r["u"] for r in records} or k in by_key: continue
            r = {"n": e["name"], "c": e.get("county", ""), "y": e.get("city", ""), "u": url, "p": 0, "x": 0, "l": 0, "t": 0, "k": k, "x_src": 1}
            if e.get("desc"): r["d"] = e["desc"]
            records.append(r); by_key[k] = r; n_extra += 1
        print(f"extra resale neighborhoods from sitemaps: +{n_extra}")

    # some Sheet rows carry an AI refusal instead of a description ("I appreciate your request, but…") — never show those
    REFUSAL = re.compile(r"^(I appreciate|I've reviewed|I'm unable|I need to|I cannot|I can't|As an AI|Unfortunately, the|The (provided )?content (you|appears)|I don't have)", re.I)
    def good_text(s): s = (s or "").strip(); return "" if not s or REFUSAL.match(s) else s

    enriched = added = 0
    for c in cur:
        c["description"] = good_text(c.get("description")); c["community_remarks"] = good_text(c.get("community_remarks"))
        k = key(c["county"], c["name"])
        url = LINK_OVERRIDES.get(c["name"]) or c.get("paradise_url") or ""
        if url and not url.startswith("http"): url = "https://www.paradiserealtyfla.com" + url
        v = vids.get(c["name"], {}).get("hero")
        sign_url = signs.get(c["name"])
        g = GCS_SIGN.format(slug(c["name"])) if (sign_url and slug(c["name"]) in local_signs) else (sign_url or "")
        inc = ((c.get("incentives") or [{}])[0].get("headline", "") if c.get("incentives") else "") or inc_fallback.get(c["name"], "")
        patch = {"n": c["name"], "c": c["county"], "y": c.get("city", ""), "u": url,
                 "h": f"https://www.paradiserealtyfla.com/search/results/?subdivision={c['name']}",
                 "b": c.get("builder", ""), "p": c.get("price_from") or 0, "l": c.get("active_listings") or 0,
                 "d": c.get("description") or (c.get("community_remarks") or "")[:220], "t": 1, "i": inc, "k": k}
        if v: patch["v"] = v["youtube_url"]; patch["vs"] = v["channel"]
        if g: patch["g"] = g
        if k in by_key:
            r = by_key[k]
            if not patch["p"] and r.get("p"): patch["p"] = r["p"]
            r.update({kk: vv for kk, vv in patch.items() if vv not in ("", 0, None)}); r["t"] = 1
            enriched += 1
        else:
            records.append(patch); by_key[k] = patch; added += 1

    # --- link hygiene: drop junk rows, reroute dead links, drop dead videos ---
    def jload(p, default):
        return json.load(open(p)) if os.path.exists(p) else default
    fixes = jload(f"{HOME}/builder-videos/link_fixes.json", {})
    hubs = jload(f"{HOME}/builder-videos/county_hubs.json", {})
    dead_subs = {u for u, _ in jload(f"{HOME}/builder-videos/dead_subdivision_urls.json", [])}
    dead_cur, dead_vid = set(fixes.get("dead_curated_links", [])), set(fixes.get("dead_videos", []))
    from urllib.parse import quote
    def search_url(name): return "https://www.paradiserealtyfla.com/search/results/?subdivision=" + quote(name)
    for r in records:  # curated "See Homes" links were built unencoded above
        if r["t"] == 1 and r.get("h", "").startswith("https://www.paradiserealtyfla.com/search/results/?subdivision="):
            r["h"] = search_url(r["n"])
    records = [r for r in records if r["n"].strip().lower() not in ("community name", "") and "Paradise URL" not in (r.get("u") or "")]
    # Joe's rule: NEVER alter a community's URL. A page that 404s today keeps its exact URL in `u`;
    # we only flag it (dl=1) and give the card a working fallback (`f`) until the nightly crawl sees it live again.
    flagged = 0
    for r in records:
        r.pop("dl", None); r.pop("f", None)
        if r["t"] == 1 and r["n"] in dead_cur:
            r["dl"] = 1; r["f"] = hubs.get(r["c"]) or search_url(r["n"]); flagged += 1
        elif r["t"] == 0 and r["u"] in dead_subs:
            r["dl"] = 1; r["f"] = search_url(r["n"]); flagged += 1
        if r.get("v") and r["n"] in dead_vid:
            r.pop("v", None); r.pop("vs", None)
    print(f"link hygiene: {flagged} pages currently 404 flagged (URLs untouched), {len(dead_vid)} dead videos dropped")

    # IDX photo fallback (first active listing on the community page), only where no sign image exists
    idx = jload(f"{HOME}/builder-videos/idx_photos.json", {})
    n_idx = 0
    for r in records:
        hit = idx.get(r["u"]) or idx.get(r.get("h", ""))
        if hit and not r.get("g"):
            r["ph"] = hit["photo"]; r["pa"] = hit["address"]; r["pu"] = hit["listing_url"]
            if hit.get("price"): r["pp"] = hit["price"]
            n_idx += 1
    print(f"idx photos attached: {n_idx}")

    records.sort(key=lambda r: (-r["t"], r["n"].lower()))
    counties = sorted({r["c"] for r in records if r["c"]})
    stats = {"total": len(records), "subdivisions": len(subs), "curated": len(cur), "curated_matched_existing": enriched,
             "curated_added_new": added, "with_video": sum(1 for r in records if r.get("v")),
             "with_sign": sum(1 for r in records if r.get("g")), "counties": len(counties)}
    os.makedirs(os.path.dirname(OUT_JS), exist_ok=True)
    for r in records: r.pop("k", None)
    body = ("const FINDER_META=" + json.dumps({"generated": "2026-09-06", **stats, "county_list": counties}, separators=(",", ":")) + ";\n"
            + "const DATA=" + json.dumps(records, separators=(",", ":"), ensure_ascii=False) + ";\n")
    with open(OUT_JS, "w") as f: f.write(body)
    import gzip
    with gzip.open(OUT_JS + ".gz", "wt", compresslevel=9) as f: f.write(body)
    print(f"gzipped: {os.path.getsize(OUT_JS + '.gz')//1024} KB")
    json.dump(stats, open(OUT_STATS, "w"), indent=2)
    print(json.dumps(stats, indent=2)); print(f"-> {OUT_JS} ({os.path.getsize(OUT_JS)//1024} KB)")

if __name__ == "__main__":
    main()
