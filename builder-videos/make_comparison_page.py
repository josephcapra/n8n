#!/usr/bin/env python3
"""Build a readable incentive-comparison page: every community running an offer, side by side with
competing offers within ~15 miles. Published to GCS for Joe; regenerated whenever incentives refresh."""
import datetime, html, json, os, subprocess

BV = os.path.expanduser("~/builder-videos")
OUT = f"{BV}/incentive-comparison.html"
GCS = "gs://paradise-realty-images/finder/reports/incentive-comparison.html"

def esc(s): return html.escape(str(s or ""), quote=False)
def pretty(iso):
    try: return datetime.date.fromisoformat(iso).strftime("%b %-d, %Y")
    except Exception: return iso or "no end date"
def days_left(iso, today):
    try: return (datetime.date.fromisoformat(iso) - today).days
    except Exception: return None

def main():
    today = datetime.date.today()
    inc = json.load(open(f"{BV}/incentives_clean.json"))
    near = json.load(open(f"{BV}/nearby_incentives.json"))
    comms = {c["name"]: c for c in json.load(open(f"{BV}/all988.json"))["communities"]}

    rows = []
    for name, v in sorted(inc.items(), key=lambda kv: (kv[1].get("expires") or "9999", kv[0])):
        c = comms.get(name, {})
        rivals = [x for x in near.get(name, []) if x["name"] in inc]
        d = days_left(v.get("expires"), today)
        urgency = "ends-soon" if d is not None and d <= 7 else ("ends-month" if d is not None and d <= 31 else "")
        url = c.get("paradise_url") or ""
        if url and not url.startswith("http"): url = "https://www.paradiserealtyfla.com" + url
        rivals_html = "".join(
            f'<li><span class="rv-name">{esc(x["name"])}</span>'
            f'<span class="rv-meta">{esc(x["builder"]) + " &middot; " if x["builder"] else ""}'
            f'{"same area" if x["approx"] else str(x["miles"]) + " mi"}</span>'
            f'<span class="rv-offer">{esc(x["headline"][:150])}</span>'
            f'<span class="rv-thru">through {pretty(x.get("expires"))}</span></li>' for x in rivals
        ) or '<li class="none">No other community within 15 miles is running an incentive right now &mdash; this is a clear differentiator.</li>'
        rows.append(f'''<article class="row {urgency}">
  <div class="mine">
    <h2>{esc(name)}{f' <a class="pg" href="{esc(url)}" target="_blank" rel="noopener">area page &rarr;</a>' if url else ''}</h2>
    <div class="where">{esc(c.get("city",""))}{", " if c.get("city") else ""}{esc(c.get("county",""))} County
      &middot; {esc(v.get("builder") or "builder not identified")}</div>
    <div class="thru {urgency}">Through {pretty(v.get("expires"))}{f" &middot; {d} days left" if d is not None else ""}</div>
    <p class="offer">{esc(v["headline"])}</p>
  </div>
  <div class="rivals"><h3>Competing incentives within 15 miles ({len(rivals)})</h3><ul>{rivals_html}</ul></div>
</article>''')

    total_near = len(near)
    head_to_head = len([n for n in inc if any(x["name"] in inc for x in near.get(n, []))])
    page = f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>Builder Incentive Comparison | Paradise Realty FLA</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap">
<style>
:root{{--navy:#0A2540;--teal:#0D9488;--teal-d:#0f766e;--gold:#C9A84C;--goldbg:#fdf8eb;--bg:#f4f6f9;--bd:#e2e6ea;--tx:#0d1b2a;--tm:#4a5568;--tl:#8898aa;--w:#fff}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:Inter,-apple-system,sans-serif;background:var(--bg);color:var(--tx);line-height:1.55;-webkit-font-smoothing:antialiased}}
header{{background:var(--navy);border-bottom:3px solid var(--teal);color:#fff;padding:26px 24px}}
header .in{{max-width:1180px;margin:0 auto}}
h1{{font-size:24px;font-weight:700;letter-spacing:-.01em}}
.sub{{color:rgba(255,255,255,.75);font-size:14px;margin-top:6px}}
.stats{{max-width:1180px;margin:20px auto 0;padding:0 24px;display:flex;flex-wrap:wrap;gap:12px}}
.stat{{background:var(--w);border:1px solid var(--bd);border-radius:10px;padding:12px 16px;min-width:150px}}
.stat b{{display:block;font-size:22px;color:var(--teal-d)}}
.stat span{{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--tl)}}
main{{max-width:1180px;margin:0 auto;padding:20px 24px 40px}}
.row{{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:0;background:var(--w);border:1px solid var(--bd);border-radius:12px;overflow:hidden;margin-bottom:16px}}
.mine{{padding:18px 20px;border-right:1px solid var(--bd)}}
.rivals{{padding:18px 20px;background:#fafbfc}}
h2{{font-size:18px;font-weight:700}}
.pg{{font-size:12px;font-weight:600;color:var(--teal-d);text-decoration:none;white-space:nowrap}}
.pg:hover{{text-decoration:underline}}
.where{{font-size:12px;color:var(--tm);margin:3px 0 8px}}
.thru{{display:inline-block;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;
  background:var(--goldbg);color:#8a6d1f;border:1px solid var(--gold);border-radius:999px;padding:3px 10px;margin-bottom:9px}}
.thru.ends-soon{{background:#fee2e2;border-color:#dc2626;color:#991b1b}}
.offer{{font-size:14px;color:var(--tx)}}
h3{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--tl);margin-bottom:10px}}
.rivals ul{{list-style:none}}
.rivals li{{border-top:1px solid var(--bd);padding:9px 0}}
.rivals li:first-child{{border-top:0;padding-top:0}}
.rv-name{{display:block;font-weight:600;font-size:14px}}
.rv-meta{{display:block;font-size:11px;color:var(--tl);text-transform:uppercase;letter-spacing:.04em;margin:1px 0 3px}}
.rv-offer{{display:block;font-size:13px;color:var(--tm)}}
.rv-thru{{display:block;font-size:11px;color:var(--tl);margin-top:2px}}
.none{{font-size:13px;color:var(--teal-d);background:#e6f7f6;border:1px solid var(--teal);border-radius:8px;padding:10px}}
.note{{max-width:1180px;margin:0 auto;padding:0 24px 40px;font-size:12px;line-height:1.6;color:var(--tm)}}
.note strong{{color:var(--tx)}}
@media(max-width:820px){{.row{{grid-template-columns:1fr}}.mine{{border-right:0;border-bottom:1px solid var(--bd)}}}}
</style></head><body>
<header><div class="in"><h1>Builder Incentive Comparison</h1>
<div class="sub">Every community currently running an incentive, beside competing offers within about 15 miles &middot; generated {today.strftime("%B %-d, %Y")}</div></div></header>
<div class="stats">
  <div class="stat"><b>{len(inc)}</b><span>Communities with an offer</span></div>
  <div class="stat"><b>{head_to_head}</b><span>Facing a nearby competitor</span></div>
  <div class="stat"><b>{len(inc)-head_to_head}</b><span>No nearby competition</span></div>
  <div class="stat"><b>{total_near}</b><span>Communities near an offer</span></div>
</div>
<main>{"".join(rows)}</main>
<div class="note"><strong>How to read this.</strong> Incentives come from the Builder Incentive sheet; expired offers are
never shown and exact mortgage rates are replaced with "Builder promotional interest rates may be offered."
Incentives are set by the builder and may change or end without notice.<br><br>
<strong>About the distances.</strong> Community coordinates in the sheet are mostly city or county centroids, so these are
"same city / neighboring city" distances, not street-level. Communities sharing a centroid show as "same area" rather
than a false 0 miles. Treat them as context, not exact figures.<br><br>
Internal reference for Paradise Realty FLA. Not for publication.</div>
</body></html>'''
    open(OUT, "w").write(page)
    subprocess.run(["gsutil", "-q", "-h", "Content-Type:text/html; charset=utf-8",
                    "-h", "Cache-Control:no-cache", "cp", OUT, GCS], check=True)
    print(f"{len(inc)} communities, {head_to_head} head-to-head -> {OUT} ({os.path.getsize(OUT)//1024} KB)")
    print("https://storage.googleapis.com/paradise-realty-images/finder/reports/incentive-comparison.html")

if __name__ == "__main__":
    main()
