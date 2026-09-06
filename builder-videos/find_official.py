#!/usr/bin/env python3
"""
Find OFFICIAL builder videos on YouTube for a list of communities.
Official = the video's channel belongs to the builder. Realtor/agent channels are rejected.
"""
import json, re, subprocess, sys

# channel-name substrings that prove builder ownership (lowercase)
OFFICIAL = {
    "lennar": ["lennar"],
    "horton": ["d.r. horton", "dr horton", "drhorton", "d r horton"],
    "toll": ["toll brothers"],
    "kolter": ["kolter"],
    "taylor morrison": ["taylor morrison"],
    "gl homes": ["gl homes", "glhomes"],
    "pulte": ["pulte", "pultegroup"],
    "divosta": ["divosta", "pulte"],
    "del webb": ["del webb", "pulte"],
    "mattamy": ["mattamy"],
    "hovnanian": ["hovnanian"],
    "dream finders": ["dream finders"],
    "babcock": ["babcock ranch"],
    "ave maria": ["ave maria"],
    "villages": ["the villages"],
    "lakewood ranch": ["lakewood ranch"],
    "ar homes": ["ar homes", "arthur rutenberg"],
    "el-ad": ["el-ad", "elad"],
    "red apple": ["400 central", "red apple"],
    "davila": ["bella collina", "davila"],
}
STOP = {"at", "the", "of", "and", "by", "-", "&", "golf", "country", "club", "communities", "road", "residences"}

def official_keys(builder):
    b = builder.lower()
    keys = [k for k in OFFICIAL if k in b]
    if "multiple" in b or "various" in b or not b:
        return None
    return [s for k in keys for s in OFFICIAL[k]]

def tokens(name):
    return [t for t in re.findall(r"[a-z0-9']+", name.lower()) if t not in STOP and len(t) > 2]

def search(query, n=10):
    out = subprocess.run(
        ["yt-dlp", f"ytsearch{n}:{query}", "--flat-playlist",
         "--print", "%(channel)s\t%(id)s\t%(title)s\t%(duration)s"],
        capture_output=True, text=True, timeout=120).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            rows.append({"channel": parts[0], "id": parts[1], "title": parts[2],
                         "duration": parts[3] if len(parts) > 3 else ""})
    return rows

def find(community, builder, city=""):
    keys = official_keys(builder)
    name_tokens = tokens(community)
    if not name_tokens:
        return []
    # A community-branded channel counts ONLY when no builder channel is known, and only on an
    # exact name match (+ "Residences"/"Florida" suffix). Realtor channels like
    # "Living in Babcock Ranch" or "Aurora at Lakewood Ranch" (an agent's channel) are rejected.
    def norm(s): return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()
    cname = norm(community)
    brand_exact = {cname, f"{cname} residences", f"{cname} florida", f"{cname} fl"}
    hits, seen = [], set()
    for q in (f"{builder} {community} {city}", f"{community} {city} new homes {builder}"):
        for r in search(q):
            ch = r["channel"].lower()
            title = r["title"].lower()
            if keys:
                is_official = any(k in ch for k in keys)
            else:
                is_official = norm(r["channel"]) in brand_exact
            title_match = any(re.search(rf"\b{re.escape(t)}\b", title) for t in name_tokens)
            if is_official and title_match and r["id"] not in seen:
                seen.add(r["id"])
                hits.append({"video_id": r["id"], "title": r["title"], "channel": r["channel"],
                             "duration": r["duration"], "youtube_url": f"https://youtu.be/{r['id']}"})
    return hits

def main(path, out_path):
    src = json.load(open(path))
    comms = src["communities"] if isinstance(src, dict) else src
    results = []
    for c in comms:
        hits = find(c["name"], c.get("builder", ""), c.get("city", ""))
        flag = f"{len(hits)} official" if hits else "none"
        print(f"{c['name']:<40} {c.get('builder',''):<24} {flag}", flush=True)
        for h in hits:
            results.append({"community": c["name"], "slug": c.get("slug"), "builder": c.get("builder", ""),
                            "platform": "youtube", **h, "source": "official builder YouTube channel"})
    json.dump(results, open(out_path, "w"), indent=2)
    print(f"\n{len(results)} official videos across {len({r['community'] for r in results})} communities -> {out_path}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
