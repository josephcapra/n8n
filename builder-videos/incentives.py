#!/usr/bin/env python3
"""Builder incentives: pull the sheet, apply common-sense rules, emit only what is safe to publish.

Rules (Joe, 2026-09-06/07):
  - expired incentives are never published; no end date = not publishable (goes to review)
  - exact mortgage rates are replaced with "Builder promotional interest rates may be offered"
  - agent commission / co-op figures and contact details are stripped
  - every published incentive carries the builder-set / subject-to-change disclaimer on the site
Runs identically here and inside the Cloud Run refresh job (sheet is readable via CSV export, no auth).
"""
import csv, datetime, io, json, os, re, urllib.request

SHEET_CSV = "https://docs.google.com/spreadsheets/d/1mnmuWCxSBJAeMPq_nC0m81Bifo3L4V3IiAxnbS9gtZc/export?format=csv"
RATE_PHRASE = "Builder promotional interest rates may be offered"
DISCLAIMER = ("Incentives are set by the builder and may change or end without notice. "
              "Terms, eligibility and availability vary by community and home; confirm current details with Paradise Realty FLA.")

RATE_CTX = re.compile(r"[^.;]*\b(rate|rates|apr|financ\w*|buy-?down|interest|fixed|fha|va|conventional|mortgage|arm)\b[^.;]*\d+(\.\d+)?\s*%[^.;]*[.;]?"
                      r"|[^.;]*\d+(\.\d+)?\s*%[^.;]*\b(rate|rates|apr|financ\w*|buy-?down|interest|fixed|fha|va|conventional|mortgage|arm)\b[^.;]*[.;]?", re.I)
COMMISSION = re.compile(r"[^.;]*\b(commission|co-?op|bonus to (the )?agent|agent bonus|broker bonus|selling agent)\b[^.;]*[.;]?", re.I)
CONTACT = re.compile(r"(\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}|\S+@\S+\.\w+)")

def fetch_csv():
    """requests in the cloud image; curl locally (this Mac's Python has no CA bundle)."""
    try:
        import requests
        return requests.get(SHEET_CSV, timeout=60).text
    except Exception:
        import subprocess
        r = subprocess.run(["curl", "-sL", "--max-time", "60", SHEET_CSV], capture_output=True, text=True)
        if r.returncode or not r.stdout: raise RuntimeError("could not fetch the incentive sheet")
        return r.stdout

def clean(s): return re.sub(r"[​‌‍﻿]", "", s or "").strip()

FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y")

def parse_dates(cell):
    """A cell can hold several dates — one sheet row often covers several communities with different
    end dates, and the pairing is not reliable. Return every date found, in order."""
    out = []
    for line in clean(cell).split("\n"):
        line = line.strip()
        for f in FORMATS:
            try: out.append(datetime.datetime.strptime(line, f).date()); break
            except ValueError: pass
    return out

def parse_date(cell):
    d = parse_dates(cell)
    return d[0] if d else None

def end_of_month(d):
    nxt = d.replace(day=28) + datetime.timedelta(days=4)
    return nxt - datetime.timedelta(days=nxt.day)

# Some sheet rows hold drafting scaffolding rather than final copy ("Here are options for...", "Option 1:").
PREFACE = re.compile(r"^\s*(here are (some )?options?[^:\n]*:|based on the provided text[^:\n]*:|"
                     r"the following[^:\n]*:|option\s*\d+\s*[:.\-]|suggested (copy|marquee)[^:\n]*:)\s*", re.I)
def strip_scaffolding(t):
    prev = None
    while t != prev:
        prev = t
        t = PREFACE.sub("", t).lstrip(" -–—•\n")
        # a leading line that is just a community/heading label, then the real copy on the next line
        first, _, rest = t.partition("\n")
        if rest and len(first) < 60 and not re.search(r"[.!?$%]", first): t = rest.lstrip()
    return t.strip()

def sanitize(text):
    t = strip_scaffolding(clean(text)); notes = []
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"\[link removed\]|Visit\s+[A-Z][\w .'&-]*\s*to\s*", "", t, flags=re.I)
    if RATE_CTX.search(t): t = RATE_CTX.sub(" " + RATE_PHRASE + ". ", t); notes.append("rate redacted")
    if COMMISSION.search(t): t = COMMISSION.sub(" ", t); notes.append("commission removed")
    if CONTACT.search(t): t = CONTACT.sub("", t); notes.append("contact removed")
    t = re.sub(r"\s{2,}", " ", t).strip(" .;")
    # redactions can leave a lowercase sentence start ("...may be offered. secure these savings")
    t = re.sub(r"(^|(?<=[.!?]) )([a-z])", lambda m: m.group(1) + m.group(2).upper(), t)
    return (t + "." if t and t[-1] not in ".!?" else t), notes

STOP = re.compile(r"^(the )?(communit(y|ies)|builder)\b", re.I)
def names_from(cell):
    t = clean(cell)
    if not t: return []
    out = re.findall(r"^\s*[-•*]\s*(.+?)\s*$", t, re.M)
    out += re.findall(r"communit(?:y|ies)(?: name)?(?: identified)?(?: is|are|:)\s*([^.\n(]+)", t, re.I)
    if not out and len(t) < 60 and "\n" not in t: out = [t]
    seen, res = set(), []
    for n in out:
        n = re.sub(r"\(.*?\)|\blocated in .*$", "", n, flags=re.I).strip(" .,-:*​")
        if 2 < len(n) < 60 and not STOP.match(n) and n.lower() not in seen:
            seen.add(n.lower()); res.append(n)
    return res

def builder_from(cell):
    t = clean(cell)
    m = re.search(r"builder(?: name)?(?: identified)?(?: is|:)\s*([A-Z][^.\n(,]+)", t)
    return (m.group(1).strip() if m else (t if len(t) < 40 and "\n" not in t else "")).strip(" .")

def norm(s): return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

def segment_for(name, details, others):
    """Pull out the passage of shared copy that belongs to `name`: from where it is first mentioned up to
    the next different community's mention. Returns '' when that yields nothing usable."""
    m = re.search(r"\b" + re.escape(name) + r"\b", details, re.I)
    if not m: return ""
    start = details.rfind("\n", 0, m.start()) + 1
    ends = [mm.start() for o in others if o != name
            for mm in re.finditer(r"\b" + re.escape(o) + r"\b", details, re.I) if mm.start() > m.end()]
    seg = details[start:min(ends)] if ends else details[start:]
    seg = re.sub(r"\s+", " ", seg).strip(" -–—:•\n")
    return seg[:180] if len(seg) >= 40 else ""

def build(communities, today=None, out_dir="."):
    """communities: list of dicts with a 'name'. Returns (published, review) and writes both files."""
    today = today or datetime.date.today()
    known = {norm(c["name"]): c["name"] for c in communities}
    # names distinctive enough that finding one in the copy really means the copy is about that community
    GENERIC = {"arden", "aria", "alton", "everton", "mosaic", "rivella", "watermark", "brookshire"}
    distinctive = {c["name"] for c in communities if len(c["name"]) >= 9 and c["name"].lower() not in GENERIC}
    def match(n):
        k = norm(n)
        if k in known: return known[k]
        for kk, v in known.items():
            if k and (kk.startswith(k) or k.startswith(kk)) and min(len(k), len(kk)) >= 6: return v
        return None

    rows = list(csv.DictReader(io.StringIO(fetch_csv())))

    published, review = {}, {"expired": [], "no_end_date": [], "no_community": [], "unmatched": [], "ambiguous": [], "redactions": []}
    for row in rows:
        ends = parse_dates(row.get("EndDate"))
        # Several end dates in one cell means several communities share the row; we cannot tell which date
        # belongs to which, so take the earliest. Ending an offer early is safe; over-claiming one is not.
        end = min(ends) if ends else None
        multi_dated = len(set(ends)) > 1
        posted = parse_date(row.get("Date"))
        assumed = False
        if not end and posted:
            # No end date given: assume it runs through the end of the month it was posted in.
            # Anchoring to the posted month (not today) means an old undated offer expires instead of
            # rolling forward forever, and the site says "Current for <Month>" rather than asserting a date.
            end, assumed = end_of_month(posted), True
        details, notes = sanitize(row.get("IncentiveDetails"))
        base = {"builder": builder_from(row.get("BuilderName")), "expires": end.isoformat() if end else "",
                "assumed_expiry": assumed, "shared_row_earliest_date": multi_dated,
                "headline": details[:180], "notes": notes,
                "sheet_date": (posted.isoformat() if posted else clean(row.get("Date")).split("\n")[0])}
        if not details: continue
        if end and end < today: review["expired"].append(base); continue
        if not end: review["no_end_date"].append({**base, "raw_community": clean(row.get("CommunityName"))[:80]}); continue
        names = names_from(row.get("CommunityName"))
        if not names: review["no_community"].append(base); continue
        # A row naming several communities usually carries several different offers in one cell. If the copy
        # names one of them specifically, that copy belongs to THAT community only — publishing it against a
        # sibling would put the wrong builder's offer on the wrong community. Only genuinely shared copy
        # (naming none of them) may go to all.
        named_in_copy = {kn for kn in distinctive if re.search(r"\b" + re.escape(kn) + r"\b", details, re.I)}
        for n in names:
            canon = match(n)
            if not canon: review["unmatched"].append({**base, "community_text": n}); continue
            if named_in_copy and canon not in named_in_copy:
                review["unmatched"].append({**base, "community_text": n,
                                            "why": f"copy names {sorted(named_in_copy)[0]}, not this community"})
                continue
            # Only publish where the offer-to-community pairing is unambiguous. These rows are AI-parsed
            # email prose: one row can list several communities and several different offers, and splitting
            # them heuristically produced wrong pairings three separate ways (a community showing another
            # community's discount). An incorrect dollar figure on a community page is a factual claim we
            # cannot make, so anything ambiguous goes to review for a human instead of onto the site.
            if len(names) > 1 and len(named_in_copy) != 1:
                review["ambiguous"].append({**base, "community_text": n, "row_lists": len(names),
                                            "why": "one row covers several communities and the copy does not "
                                                   "clearly belong to just one; needs a human to split"})
                continue
            rec = {**base, "community": canon}
            if notes: review["redactions"].append(rec)
            # Freshest information wins: the most recently posted row, then the later end date as a tie-break.
            prev = published.get(canon)
            if not prev or (rec["sheet_date"], rec["expires"]) > (prev["sheet_date"], prev["expires"]):
                published[canon] = rec

    json.dump(published, open(os.path.join(out_dir, "incentives_clean.json"), "w"), indent=1)
    json.dump(review, open(os.path.join(out_dir, "incentives_review.json"), "w"), indent=1)
    return published, review

if __name__ == "__main__":
    BV = os.path.expanduser("~/builder-videos")
    comms = json.load(open(f"{BV}/all988.json"))["communities"]
    pub, rev = build(comms, out_dir=BV)
    print(f"sheet rows -> publishable: {len(pub)} communities | expired {len(rev['expired'])} | "
          f"no end date {len(rev['no_end_date'])} | unmatched {len(rev['unmatched'])} | redactions {len(rev['redactions'])}")
    for n, v in list(pub.items())[:8]:
        print(f"  {n} ({v['builder'] or 'builder n/a'}, through {v['expires']}): {v['headline'][:96]}")
