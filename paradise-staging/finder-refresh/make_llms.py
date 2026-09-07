#!/usr/bin/env python3
"""Regenerate LLMs.txt from the live finder dataset. Static brand/compliance sections are kept from the current file;
the community, structure and data sections are rebuilt from data so counts never go stale."""
import json, re, os, datetime
from collections import Counter, defaultdict
DATA = os.environ.get("LLMS_DATA", os.path.expanduser("~/paradise-staging/finder-test/communities_all.js"))
CUR = os.environ.get("LLMS_CURRENT", os.path.expanduser("~/paradise-staging/llms/LLMs.current.txt"))
OUT = os.environ.get("LLMS_OUT", os.path.expanduser("~/paradise-staging/llms/LLMs.txt"))
d = json.loads(re.search(r"const DATA=(\[.*\]);\n", open(DATA).read(), re.S).group(1))
cur = open(CUR).read()
def section(title):
    m = re.search(rf"(^## {re.escape(title)}\n.*?)(?=^## |\Z)", cur, re.S | re.M); return m.group(1).rstrip() + "\n" if m else ""
live = [r for r in d if not r.get("dl")]; nc = [r for r in d if r["t"] == 1]
by_c = defaultdict(list)
for r in nc: by_c[r["c"]].append(r)
today = datetime.date.today().isoformat()
lines = ["# Paradise Realty FLA — AI Agent Reference", f"_Updated {today}. Community counts below come from the nightly-refreshed Community Finder dataset._", ""]
lines += [section("About Paradise Realty FLA"), section("Business Model"), section("Service Area"), section("Core Value Proposition")]
lines += ["## Community Finder (authoritative community data)", "",
          f"- **{len(d):,} Florida communities** across **{len({r['c'] for r in d if r.get('c')})} counties**: {len(nc)} curated **new construction** communities (builder, price-from, description, incentives, official builder video, entrance sign) plus {len(d)-len(nc):,} **resale neighborhoods** from the MLS.",
          f"- {sum(1 for r in d if r.get('v'))} communities carry an **official builder video tour** (builder's own YouTube channel only; no third-party or agent videos).",
          f"- {sum(1 for r in d if r.get('g'))} communities carry a **community entrance sign photo**; {sum(1 for r in d if r.get('ph'))} show the photo of a **current active listing** (refreshed nightly; removed automatically when the listing sells).",
          f"- **Builder incentives** are published only while unexpired; exact mortgage rates are never quoted — the phrase used is \"Builder promotional interest rates may be offered\". {sum(1 for r in d if r.get('i'))} communities currently show an incentive.",
          "- Data is refreshed **twice daily (3:30 AM and 3:30 PM ET)** from a permanent registry of every community URL. URLs are stable and never rewritten; a neighborhood page that has no active listing today is flagged rather than removed and returns automatically when a home is listed.",
          "- Community Finder: https://search.paradiserealtyfla.com/ — searchable directory of every community below.",
          "- Current builder incentives: https://search.paradiserealtyfla.com/incentives — every community running an unexpired builder offer, with the date it runs through. Rebuilt twice daily; offers disappear the day they expire.", ""]
lines += ["## New Construction Communities by County (curated)", ""]
for county in sorted(by_c, key=lambda c: -len(by_c[c])):
    rows = sorted(by_c[county], key=lambda r: -(r.get("l") or 0))[:8]
    items = ", ".join(f"{r['n']}" + (f" ({r['b']})" if r.get("b") else "") + (f" from ${r['p']//1000}K" if r.get("p") else "") for r in rows)
    lines.append(f"- **{county} County** ({len(by_c[county])}): {items}")
lines += ["", "## Website Structure", "",
          "- `/` — Homepage", "- `/communities/` — Florida Community Finder (all communities, filters by county, price, incentive, video, new construction)",
          "- `/<county>-county/` — County hub pages (66 counties)", "- `/<county>-county/<community-slug>/` — New construction community pages",
          "- `/listings/subdivision/<Subdivision-Slug>/` — Resale neighborhood pages with live MLS listings; filter variants live beneath each (e.g. `/Condos-for-Sale/`, `/home-values/`)",
          "- `/search/results/?subdivision=<name>` — Live listing search for any community", "- `/blog/` — Market insights", ""]
lines += [section("Compliance Notes"), section("Contact")]
open(OUT, "w").write("\n".join(l for l in lines))
print(f"LLMs.txt written: {os.path.getsize(OUT)//1024} KB, {len(lines)} lines")
