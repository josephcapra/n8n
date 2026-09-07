#!/usr/bin/env python3
"""
Wire RealGeeks sign-image URLs into the demo, then emit two builds:
  - staging (URLs as-is)  -> ~/paradise-staging/community-finder-v2.html   (what goes on the real site)
  - artifact (inlined)    -> scratchpad/community-finder-demo.html         (claude.ai preview; CSP blocks remote images)
Source of truth is scratchpad/community-finder-demo.src.html.
"""
import base64, json, os, re, shutil

SCRATCH = "/private/tmp/claude-501/-Users-User/ed61b43f-3353-4c6d-9ca3-6f8aac402470/scratchpad"
SRC = f"{SCRATCH}/community-finder-demo.src.html"
ARTIFACT = f"{SCRATCH}/community-finder-demo.html"
STAGING = os.path.expanduser("~/paradise-staging/community-finder-v2.html")
SIGNS = json.load(open(os.path.expanduser("~/builder-videos/rg_sign_images.json")))
SIGN_DIR = os.path.expanduser("~/builder-videos/signs_small")  # 480px thumbs: keeps the inlined artifact light

def slug(name): return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

def main():
    if not os.path.exists(SRC):
        shutil.copy(ARTIFACT, SRC)
    s = open(SRC).read()
    m = re.search(r"const DATA = (\{.*?\});", s, re.DOTALL)
    data = json.loads(m.group(1))
    # RealGeeks pages that 404 — send "View Community" to the county hub instead
    LINK_OVERRIDES = {
        "Lakewood Ranch": "https://www.paradiserealtyfla.com/manatee-county/",
        "The Villages": "https://www.paradiserealtyfla.com/sumter-county/",
    }
    n = 0
    for c in data["communities"]:
        if c["name"] in LINK_OVERRIDES:
            c["paradise_url"] = LINK_OVERRIDES[c["name"]]
        url = SIGNS.get(c["name"])
        if url:
            # serve the 880px/66KB copy from GCS; keep the RealGeeks original as source of record
            c["image_url"] = f"https://storage.googleapis.com/paradise-realty-images/community-signs/{slug(c['name'])}.jpg"
            c["image_source_url"] = url
            c["image_alt"] = f"{c['name']} community entrance sign"
            n += 1
        else:
            for k in ("image_url", "image_source_url", "image_alt"):
                c.pop(k, None)
    s = s.replace(m.group(0), "const DATA = " + json.dumps(data, separators=(",", ":")) + ";")
    open(SRC, "w").write(s)
    # test/staging outputs must never be indexed; the src stays clean for the eventual production cut-over
    NOINDEX = '<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n<meta name="robots" content="noindex, nofollow">\n'
    if 'name="robots"' not in s:
        s = s.replace('<meta name="description"', NOINDEX + '<meta name="description"', 1)
    open(STAGING, "w").write(s)

    NOTE = ('<div style="background:#C9A84C;color:#0A2540;text-align:center;padding:10px 16px;font:600 14px Inter,-apple-system,sans-serif">'
            'This is a 31-community preview. The full Community Finder (41,500+ communities, sign photos, live listing photos, video tours) is live at '
            '<a href="https://paradise-finder-3vuuwnsvua-ue.a.run.app/" style="color:#0A2540;text-decoration:underline">paradise-finder…run.app</a></div>\n')
    inl = s.replace('<header class="header">', NOTE + '<header class="header">', 1)
    for c in data["communities"]:
        if "image_url" not in c: continue
        p = f"{SIGN_DIR}/{slug(c['name'])}.jpg"
        if not os.path.exists(p): continue
        uri = "data:image/jpeg;base64," + base64.b64encode(open(p, "rb").read()).decode()
        inl = inl.replace(json.dumps(c["image_url"]), json.dumps(uri), 1)
    open(ARTIFACT, "w").write(inl)
    print(f"{n} communities carry RealGeeks sign URLs")
    print(f"staging  -> {STAGING} ({os.path.getsize(STAGING)//1024} KB, remote URLs)")
    print(f"artifact -> {ARTIFACT} ({os.path.getsize(ARTIFACT)//1024} KB, inlined)")

if __name__ == "__main__":
    main()
