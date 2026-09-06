#!/usr/bin/env python3
"""Drop any 'official' hit whose channel is a developer/community brand match by substring only.
Developer-branded names (Babcock Ranch, Ave Maria, The Villages...) must match the channel exactly."""
import json, re, sys

EXACT_BRANDS = ["babcock ranch", "ave maria", "the villages", "lakewood ranch", "400 central",
                "bella collina", "alina residences", "alton", "arden", "avenir"]
SUFFIXES = ["", " florida", " fl", " residences", " homes", " official", " inc"]

def norm(s): return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()

def ok(r):
    ch = norm(r["channel"])
    for b in EXACT_BRANDS:
        if b in ch:
            return ch in {b + suf for suf in SUFFIXES}
    return True  # builder-company channels (Lennar, Kolter, Mattamy...) already validated upstream

def main(src, out):
    rows = json.load(open(src))
    kept = [r for r in rows if ok(r)]
    dropped = [r["channel"] for r in rows if not ok(r)]
    json.dump(kept, open(out, "w"), indent=2)
    print(f"kept {len(kept)}, dropped {len(dropped)}: {sorted(set(dropped))}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
