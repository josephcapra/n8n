#!/usr/bin/env python3
"""Keep a builder-incentive block on each community's RealGeeks area page in sync with the sheet.

Design rules:
  - The tool owns ONE fenced block and nothing else on the page:
        <!-- prf-incentive:start v1 --> ... <!-- prf-incentive:end -->
    Hand-written page content is never read from, rewritten, or reordered.
  - Add when an unexpired incentive exists; REMOVE the block the day it expires or leaves the sheet.
  - Write only when the rendered block differs from what is already live. No change -> no login, no edit.
  - ASCII only: RealGeeks replaces non-ASCII characters with "?" on save (known platform behaviour).
  - Safety cap: if a run would change more than MAX_WRITES pages, it stops and reports instead.

Default mode is DRY RUN: it reports exactly what it would add, update or remove and writes nothing.
Pass --apply to actually save (that path requires the RealGeeks admin session and is gated separately).
"""
import argparse, difflib, html, json, os, re, subprocess, sys, datetime

BV = os.path.expanduser("~/builder-videos")
START, END = "<!-- prf-incentive:start v1 -->", "<!-- prf-incentive:end -->"
BLOCK_RE = re.compile(re.escape(START) + r".*?" + re.escape(END), re.S)
MAX_WRITES = 10          # blast-radius guard; a bigger change set stops for review
DISCLAIMER = ("Incentives are provided by the builder, are current as of the date shown, and may change or "
              "end without notice. Terms, eligibility and availability vary by community and by home and may "
              "require use of a preferred lender or title company. This is not an offer or a guarantee of savings. "
              "Confirm current details with Paradise Realty FLA before relying on them.")

def esc(s):
    """Escape markup but leave apostrophes readable in the page source."""
    return html.escape(str(s), quote=False)

GENERIC = {"the", "at", "of", "and", "a", "county", "north", "south", "east", "west", "palm", "beach",
           "port", "saint", "st", "fort", "ft", "lake", "lakes", "new", "city", "village", "park",
           "vero", "gardens", "coast", "bay", "river", "creek", "point", "pointe", "estates", "club"}
def slug_words(s):
    """Distinctive words only — 'palm beach' appearing in both a name and a URL proves nothing."""
    return {w for w in re.sub(r"[^a-z0-9]+", " ", s.lower()).split() if w not in GENERIC and len(w) > 2}

def ascii_only(s):
    """RealGeeks mangles non-ASCII on save; convert the few characters we actually produce."""
    return (s.replace("—", "&mdash;").replace("–", "&ndash;").replace("’", "&rsquo;")
             .replace("“", "&ldquo;").replace("”", "&rdquo;").replace("…", "..."))

def pretty_date(iso):
    try: return datetime.date.fromisoformat(iso).strftime("%B %-d, %Y")
    except Exception: return iso

def render(name, rec, nearby):
    """The complete block for one community, or '' when there is no publishable incentive."""
    if not rec: return ""
    builder = rec.get("builder") or "the builder"
    thru = f" &mdash; through {pretty_date(rec['expires'])}" if rec.get("expires") else ""
    near = ""
    if nearby:
        items = "".join(
            f"<li>{esc(x['name'])}{' (' + esc(x['builder']) + ')' if x['builder'] else ''}"
            f" &mdash; {'same area' if x['approx'] else str(x['miles']) + ' miles away'}</li>" for x in nearby[:3])
        near = ('<p style="margin:10px 0 4px;font-size:13px"><strong>Other nearby communities with current '
                f'incentives:</strong></p><ul style="margin:0 0 6px 18px;font-size:13px">{items}</ul>'
                '<p style="margin:0 0 6px;font-size:12px">Distances are approximate, based on community location. '
                'Ask us for a side-by-side comparison.</p>')
    body = (f'{START}\n'
            f'<div style="border:1px solid #C9A84C;background:#fdf8eb;border-radius:8px;padding:16px;margin:20px 0">'
            f'<p style="margin:0 0 6px;font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#8a6d1f">'
            f'Current builder incentive{thru}</p>'
            f'<p style="margin:0 0 8px;font-size:15px;line-height:1.55">{esc(rec["headline"])}</p>'
            f'{near}'
            f'<p style="margin:0;font-size:12px;line-height:1.5;color:#4a5568">Set by {esc(builder)}. {DISCLAIMER}</p>'
            f'</div>\n{END}')
    return ascii_only(body)

def fetch(url):
    r = subprocess.run(["curl", "-sL", "-A", "Mozilla/5.0", "--max-time", "30", "-w", "\n%{http_code}", url],
                       capture_output=True, text=True)
    try: body, code = r.stdout.rsplit("\n", 1)
    except ValueError: return "", "000"
    return body, code

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually save to RealGeeks (default is dry run)")
    args = ap.parse_args()

    comms = {c["name"]: c for c in json.load(open(f"{BV}/all988.json"))["communities"]}
    inc = json.load(open(f"{BV}/incentives_clean.json"))
    nearby = json.load(open(f"{BV}/nearby_incentives.json")) if os.path.exists(f"{BV}/nearby_incentives.json") else {}

    # Every community that either has an incentive now, or might still be carrying a block from last time.
    candidates = sorted(set(inc) | {n for n in comms if n in nearby and n in inc})
    plan = {"add": [], "update": [], "remove": [], "nochange": [], "unreachable": []}
    for name in candidates:
        c = comms.get(name) or {}
        url = c.get("paradise_url") or ""
        if url and not url.startswith("http"): url = "https://www.paradiserealtyfla.com" + url
        if not url:
            plan["unreachable"].append((name, "no area page URL in the sheet")); continue
        page, code = fetch(url)
        if code != "200":
            plan["unreachable"].append((name, f"page returned {code}")); continue
        # the sheet's community name should be recognisable in its own URL; if not, a human should look
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        if not (slug_words(name) & slug_words(slug)):
            plan["unreachable"].append((name, f"name does not match its page URL ({slug}) - verify before publishing")); continue
        current = (BLOCK_RE.search(page).group(0) if BLOCK_RE.search(page) else "")
        desired = render(name, inc.get(name), nearby.get(name, []))
        if current == desired: plan["nochange"].append((name, url))
        elif current and not desired: plan["remove"].append((name, url, current, desired))
        elif desired and not current: plan["add"].append((name, url, current, desired))
        else: plan["update"].append((name, url, current, desired))

    changes = plan["add"] + plan["update"] + plan["remove"]
    print(f"{'APPLY' if args.apply else 'DRY RUN'} - {len(candidates)} communities checked")
    print(f"  add {len(plan['add'])} | update {len(plan['update'])} | remove {len(plan['remove'])} | "
          f"no change {len(plan['nochange'])} | unreachable {len(plan['unreachable'])}")
    for kind in ("add", "update", "remove"):
        for item in plan[kind]:
            name, url = item[0], item[1]
            print(f"\n[{kind.upper()}] {name}\n  {url}")
            if kind != "remove":
                text = re.sub(r"<[^>]+>", " ", item[3]); print("  would show: " + re.sub(r"\s+", " ", text).strip()[:300])
            else:
                print("  would remove the existing incentive block (offer expired or no longer in the sheet)")
    for name, why in plan["unreachable"]: print(f"\n[SKIP] {name} - {why}")

    json.dump({k: [(i[0], i[1]) for i in v] if k != "unreachable" else v for k, v in plan.items()},
              open(f"{BV}/area_incentive_plan.json", "w"), indent=1)
    if len(changes) > MAX_WRITES:
        print(f"\nSTOP: {len(changes)} pages would change, over the {MAX_WRITES}-page safety cap. Review before applying.")
        return 2
    if args.apply:
        print("\n--apply is not wired to the RealGeeks admin session yet; this run changed nothing.")
        return 1
    print(f"\nDry run only - no page was modified. Plan saved to {BV}/area_incentive_plan.json")
    return 0

if __name__ == "__main__":
    sys.exit(main())
