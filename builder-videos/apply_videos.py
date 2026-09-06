#!/usr/bin/env python3
"""Merge hero videos into the demo page: per-card video_url + one VideoObject JSON-LD per video."""
import json, re, sys, html

DEMO = "/private/tmp/claude-501/-Users-User/ed61b43f-3353-4c6d-9ca3-6f8aac402470/scratchpad/community-finder-demo.html"
KEEP_PARADISE = {"Avenir - GL Homes": "xteRgjnmQjI"}  # already re-hosted unlisted on Paradise channel

def video_object(c, v):
    return {
        "@context": "https://schema.org",
        "@type": "VideoObject",
        "name": v["title"],
        "description": f"Official {v['channel']} video for {c['name']}, a new construction community in {c['city']}, {c['county']} County, Florida. Homes from ${c['price_from']:,}." if c.get("price_from") else f"Official {v['channel']} video for {c['name']} in {c['city']}, Florida.",
        "thumbnailUrl": f"https://i.ytimg.com/vi/{v['video_id']}/hqdefault.jpg",
        "uploadDate": "2026-09-05",
        "contentUrl": f"https://www.youtube.com/watch?v={v['video_id']}",
        "embedUrl": f"https://www.youtube.com/embed/{v['video_id']}",
        "sourceOrganization": {"@type": "Organization", "name": v["channel"]},
        "publisher": {"@type": "RealEstateAgent", "name": "Paradise Realty FLA", "telephone": "+1-772-247-7110", "url": "https://www.paradiserealtyfla.com"},
        "about": {"@type": "Place", "name": c["name"], "address": {"@type": "PostalAddress", "addressLocality": c["city"], "addressRegion": "FL"}},
    }

def main(best_path):
    best = json.load(open(best_path))
    s = open(DEMO).read()
    m = re.search(r"const DATA = (\{.*?\});", s, re.DOTALL)
    data = json.loads(m.group(1))
    objs, n = [], 0
    for c in data["communities"]:
        for k in ("video_url", "video_title", "video_source", "video_count"):
            c.pop(k, None)
        b = best.get(c["name"])
        if c["name"] in KEEP_PARADISE:
            vid = KEEP_PARADISE[c["name"]]
            c.update(video_url=f"https://youtu.be/{vid}", video_source="GL Homes official (Paradise Realty channel)", video_count=1)
            objs.append(video_object(c, {"title": "Tour the Clubhouse at Apex at Avenir", "channel": "GL Homes", "video_id": vid}))
            n += 1
            continue
        if not b:
            continue
        v = b["hero"]
        c.update(video_url=v["youtube_url"], video_title=v["title"], video_source=f"{v['channel']} official", video_count=len(b["all"]))
        objs.append(video_object(c, v))
        n += 1
    s = s.replace(m.group(0), "const DATA = " + json.dumps(data, separators=(",", ":")) + ";")
    # replace any existing VideoObject blocks with the regenerated set
    s = re.sub(r'<script type="application/ld\+json">\s*\{\s*"@context": "https://schema.org",\s*"@type": "VideoObject".*?</script>\s*', "", s, flags=re.DOTALL)
    block = "".join(f'<script type="application/ld+json">\n{json.dumps(o, indent=2)}\n</script>\n\n' for o in objs)
    s = s.replace("<style>\n:root {", block + "<style>\n:root {", 1)
    open(DEMO, "w").write(s)
    print(f"{n} communities now carry an official video; {len(objs)} VideoObject blocks written")

if __name__ == "__main__":
    main(sys.argv[1])
