#!/usr/bin/env python3
"""Common-sense pass over the Builder Incentive sheet before anything is published.
Rules (Joe, 2026-09-06): drop expired; redact exact mortgage rates -> 'Builder promotional interest rates may be offered';
never publish agent commission or contact details; unknown expiration is not publishable (goes to review)."""
import json, re, datetime, os
TODAY = datetime.date(2026, 9, 6)
RATE_PHRASE = "Builder promotional interest rates may be offered"
rows = json.load(open(os.path.expanduser("~/builder-videos/sheet/incentives_raw.json")))
def clean(s): return re.sub(r"[​‌‍﻿]", "", s or "").strip()
def parse(d):
    d = clean(d)
    for f in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y"):
        try: return datetime.datetime.strptime(d, f).date()
        except Exception: pass
    return None
RATE_CTX = re.compile(r"[^.;]*\b(rate|rates|apr|financ\w*|buy-?down|interest|fixed|fha|va|conventional|mortgage|arm)\b[^.;]*\d+(\.\d+)?\s*%[^.;]*[.;]?|[^.;]*\d+(\.\d+)?\s*%[^.;]*\b(rate|rates|apr|financ\w*|buy-?down|interest|fixed|fha|va|conventional|mortgage|arm)\b[^.;]*[.;]?", re.I)
COMMISSION = re.compile(r"[^.;]*\b(commission|co-?op|bonus to (the )?agent|agent bonus|broker bonus|selling agent)\b[^.;]*[.;]?", re.I)
CONTACT = re.compile(r"(\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}|\S+@\S+\.\w+)")
def sanitize(text):
    t = clean(text); notes = []
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)            # markdown links -> text
    t = re.sub(r"\[link removed\]|Visit\s+[A-Z][\w .'&-]*\s*to\s*", "", t, flags=re.I)
    if RATE_CTX.search(t): t = RATE_CTX.sub(" " + RATE_PHRASE + ". ", t); notes.append("rate redacted")
    if COMMISSION.search(t): t = COMMISSION.sub(" ", t); notes.append("commission removed")
    if CONTACT.search(t): t = CONTACT.sub("", t); notes.append("contact removed")
    t = re.sub(r"\s{2,}", " ", t).strip(" .;") ; return (t + ".") if t else "", notes
# The sheet is an email-parse: cells hold prose like "The community name is Cadence Townhomes." or a bullet list.
def names_from(cell):
    t = clean(cell)
    if not t: return []
    out = re.findall(r"^\s*[-•*]\s*(.+?)\s*$", t, re.M)                      # bullet list
    out += re.findall(r"communit(?:y|ies)(?: name)?(?: identified)?(?: is|are|:)\s*([^.\n(]+)", t, re.I)
    if not out and len(t) < 60 and "\n" not in t: out = [t]
    seen, res = set(), []
    for n in out:
        n = re.sub(r"\(.*?\)|\bin Port St\. Lucie\b|\blocated in .*$", "", n, flags=re.I).strip(" .,-:*")
        if 2 < len(n) < 60 and n.lower() not in seen: seen.add(n.lower()); res.append(n)
    return res
def builder_from(cell):
    t = clean(cell); m = re.search(r"builder(?: name)?(?: identified)?(?: is|:)\s*([A-Z][^.\n(,]+)", t)
    return (m.group(1).strip() if m else (t if len(t) < 40 and "\n" not in t else "")).strip(" .")
def norm(s): return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
known = {norm(c["name"]): c["name"] for c in json.load(open(os.path.expanduser("~/builder-videos/all988.json")))["communities"]}
def match(n):
    k = norm(n)
    if k in known: return known[k]
    for kk, v in known.items():
        if k and (kk.startswith(k) or k.startswith(kk)) and min(len(k), len(kk)) >= 6: return v
    return None
published, review = {}, {"expired": [], "no_end_date": [], "no_community": [], "unmatched": [], "redactions": []}
for r in rows:
    end = parse(r.get("EndDate")); builder = builder_from(r.get("BuilderName"))
    details, notes = sanitize(r.get("IncentiveDetails"))
    base = {"builder": builder, "expires": end.isoformat() if end else "", "headline": details[:160], "notes": notes, "sheet_row_date": clean(r.get("Date"))}
    if end and end < TODAY: review["expired"].append(base); continue
    if not end: review["no_end_date"].append({**base, "raw_community": clean(r.get("CommunityName"))[:80]}); continue
    names = names_from(r.get("CommunityName"))
    if not names: review["no_community"].append(base); continue
    for n in names:
        canon = match(n)
        if not canon: review["unmatched"].append({**base, "community_text": n}); continue
        rec = {**base, "community": canon}
        if notes: review["redactions"].append(rec)
        if canon not in published or published[canon]["expires"] < rec["expires"]: published[canon] = rec
json.dump(published, open("incentives_clean.json", "w"), indent=1); json.dump(review, open("incentives_review.json", "w"), indent=1)
print(f"rows {len(rows)} -> publishable {len(published)} communities | expired {len(review['expired'])} | no end date {len(review['no_end_date'])} | current but no community named {len(review['no_community'])} | with redactions {len(review['redactions'])}")
for n, v in list(published.items())[:6]: print(f"  {n} ({v['builder']}, until {v['expires']}): {v['headline'][:110]}  {v['notes']}")
