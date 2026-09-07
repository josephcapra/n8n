#!/usr/bin/env python3
"""Generate community sitemaps from the finder dataset for Joe to submit MANUALLY (never auto-submitted).
- URLs are emitted exactly as stored (verbatim); nothing is normalized.
- Only pages that returned 200 in the latest crawl are included; currently-404 pages go to a separate CSV report.
- Split at 45,000 URLs per file (sitemap protocol max is 50,000) with an index file."""
import json, re, os, csv, datetime, gzip
from xml.sax.saxutils import escape
DATA = os.path.expanduser("~/paradise-staging/finder-test/communities_all.js")
OUT = os.path.expanduser("~/paradise-staging/sitemaps"); os.makedirs(OUT, exist_ok=True)
d = json.loads(re.search(r"const DATA=(\[.*\]);\n", open(DATA).read(), re.S).group(1))
today = datetime.date.today().isoformat()
live = [r for r in d if not r.get("dl")]; dead = [r for r in d if r.get("dl")]
new_con = [r for r in live if r["t"] == 1]; resale = [r for r in live if r["t"] == 0]
def write(name, rows, prio):
    body = "".join(f"  <url><loc>{escape(r['u'])}</loc><lastmod>{today}</lastmod><changefreq>weekly</changefreq><priority>{prio}</priority></url>\n" for r in rows)
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + body + "</urlset>\n"
    open(f"{OUT}/{name}", "w", encoding="utf-8").write(xml); return name
# "finder-" prefix so these can never collide with the 54 sitemaps already submitted to GSC
files = [write("finder-sitemap-new-construction.xml", new_con, "0.8")]
for i in range(0, len(resale), 45000):
    files.append(write(f"finder-sitemap-resale-{i//45000+1}.xml", resale[i:i+45000], "0.6"))
idx = '<?xml version="1.0" encoding="UTF-8"?>\n<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + "".join(
    # served from the finder host, which we control; valid for the sc-domain property
    f"  <sitemap><loc>https://search.paradiserealtyfla.com/{f}</loc><lastmod>{today}</lastmod></sitemap>\n" for f in files) + "</sitemapindex>\n"
open(f"{OUT}/finder-sitemap-index.xml", "w").write(idx)
with open(f"{OUT}/currently-404-pages.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["community", "county", "tier", "url (unchanged)", "fallback shown on site"])
    for r in dead: w.writerow([r["n"], r["c"], "new construction" if r["t"] else "resale", r["u"], r.get("f", "")])
print(f"live URLs: {len(live):,} ({len(new_con)} new construction + {len(resale):,} resale) -> {files}")
print(f"currently 404 (excluded, reported): {len(dead):,} -> currently-404-pages.csv")
for f in os.listdir(OUT): print(f"  {f}: {os.path.getsize(f'{OUT}/{f}')//1024} KB")
