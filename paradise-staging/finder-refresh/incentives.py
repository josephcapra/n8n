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

def parse_date(d):
    d = clean(d)
    for f in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y"):
        try: return datetime.datetime.strptime(d, f).date()
        except ValueError: pass
    return None

def sanitize(text):
    t = clean(text); notes = []
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

def build(communities, today=None, out_dir="."):
    """communities: list of dicts with a 'name'. Returns (published, review) and writes both files."""
    today = today or datetime.date.today()
    known = {norm(c["name"]): c["name"] for c in communities}
    def match(n):
        k = norm(n)
        if k in known: return known[k]
        for kk, v in known.items():
            if k and (kk.startswith(k) or k.startswith(kk)) and min(len(k), len(kk)) >= 6: return v
        return None

    rows = list(csv.DictReader(io.StringIO(fetch_csv())))

    published, review = {}, {"expired": [], "no_end_date": [], "no_community": [], "unmatched": [], "redactions": []}
    for row in rows:
        end = parse_date(row.get("EndDate"))
        details, notes = sanitize(row.get("IncentiveDetails"))
        base = {"builder": builder_from(row.get("BuilderName")), "expires": end.isoformat() if end else "",
                "headline": details[:180], "notes": notes, "sheet_date": clean(row.get("Date"))}
        if not details: continue
        if end and end < today: review["expired"].append(base); continue
        if not end: review["no_end_date"].append({**base, "raw_community": clean(row.get("CommunityName"))[:80]}); continue
        names = names_from(row.get("CommunityName"))
        if not names: review["no_community"].append(base); continue
        for n in names:
            canon = match(n)
            if not canon: review["unmatched"].append({**base, "community_text": n}); continue
            rec = {**base, "community": canon}
            if notes: review["redactions"].append(rec)
            if canon not in published or published[canon]["expires"] < rec["expires"]: published[canon] = rec

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
