#!/usr/bin/env python3
"""Which communities within ~15 miles are also running builder incentives.

Accuracy note: the Sheet's coordinates are mostly city/county centroids (977 communities share only 364
distinct points), so this is "same city / neighboring city" resolution, not street-level. Every output
says so. Distances are straight-line miles from those points.
"""
import json, math, os

BV = os.path.expanduser("~/builder-videos")
RADIUS_MI = 15.0

def miles(a, b):
    (la1, lo1), (la2, lo2) = a, b
    p = math.pi / 180
    h = (0.5 - math.cos((la2 - la1) * p) / 2
         + math.cos(la1 * p) * math.cos(la2 * p) * (1 - math.cos((lo2 - lo1) * p)) / 2)
    return 7917.5 * math.asin(math.sqrt(h))

def build(communities, incentives, radius=RADIUS_MI):
    """-> {community_name: [ {name, builder, miles, approx, headline, expires}, ... ] } for every community
    that has at least one *other* incentive community within the radius."""
    by_name = {c["name"]: c for c in communities}
    pts = {n: (c["lat"], c["lng"]) for n, c in by_name.items() if c.get("lat") and c.get("lng")}
    inc_pts = {n: pts[n] for n in incentives if n in pts}
    out = {}
    for name, p in pts.items():
        near = []
        for iname, ip in inc_pts.items():
            if iname == name: continue
            d = miles(p, ip)
            if d <= radius:
                v = incentives[iname]
                near.append({"name": iname, "builder": v.get("builder", ""), "miles": round(d, 1),
                             "approx": d < 0.05,          # same centroid: "same area", not "0 miles"
                             "headline": v["headline"], "expires": v.get("expires", ""),
                             "city": by_name[iname].get("city", ""), "county": by_name[iname].get("county", "")})
        if near:
            out[name] = sorted(near, key=lambda x: x["miles"])
    return out

if __name__ == "__main__":
    comms = json.load(open(f"{BV}/all988.json"))["communities"]
    inc = json.load(open(f"{BV}/incentives_clean.json"))
    near = build(comms, inc)
    json.dump(near, open(f"{BV}/nearby_incentives.json", "w"), indent=1)
    with_inc = {n: v for n, v in near.items() if n in inc}
    print(f"communities with >=1 incentive community within {RADIUS_MI:.0f} mi: {len(near)}")
    print(f"  of those, communities that themselves have an incentive (head-to-head): {len(with_inc)}")
    print(f"  communities with an incentive and NO nearby competitor: {len([n for n in inc if n not in near])}\n")
    for n, lst in list(with_inc.items())[:6]:
        mine = inc[n]
        print(f"{n} ({mine.get('builder') or 'builder n/a'}) — {mine['headline'][:70]}")
        for x in lst[:3]:
            where = "same area" if x["approx"] else f"{x['miles']} mi"
            print(f"    vs {x['name']} ({x['builder'] or 'builder n/a'}, {where}): {x['headline'][:62]}")
        print()
