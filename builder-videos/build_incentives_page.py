#!/usr/bin/env python3
"""Build the public "Current Builder Incentives" page for buyers.

Source of truth is the FINISHED finder dataset (communities_all.js), not the sheet: whatever a community
card shows, this page shows. They cannot drift apart, and a community that failed the strict
offer-to-community pairing check in incentives.py never appears here either.

Self-maintaining, per Joe's rule: an offer is on this page only while it is unexpired and in the sheet.
When the last one expires the page renders its own empty state and the finder drops the nav link, so
nothing has to be taken down by hand at the end of a month.

Output: ~/paradise-staging/finder-test/incentives.html  (published to GCS by refresh.py, served at /incentives)
"""
import datetime, gzip, html, json, os, re

HOME = os.path.expanduser("~")
BV = f"{HOME}/builder-videos"
ST = f"{HOME}/paradise-staging/finder-test"
DATA_GZ = f"{ST}/communities_all.js.gz"
FINDER_HTML = f"{ST}/index.html"
OUT = f"{ST}/incentives.html"
CANONICAL = "https://search.paradiserealtyfla.com/incentives"
PHONE, PHONE_HREF = "(772) 247-7110", "tel:7722477110"
CONTACT = "https://www.paradiserealtyfla.com/contact/"
FINDER = "/"

# Joe's open question: whether the nearby-community comparison is buyer-facing or internal only.
# Buyer-facing framing is "here is what else is on offer near you"; the internal report keeps the
# competitive framing. Flip to False to strip it from the public page without touching anything else.
SHOW_NEARBY = True

def esc(s): return html.escape(str(s or ""), quote=False)
def attr(s): return html.escape(str(s or ""), quote=True)

def pretty(iso, fmt="%B %-d, %Y"):
    try: return datetime.date.fromisoformat(iso).strftime(fmt)
    except Exception: return ""

def days_left(iso, today):
    try: return (datetime.date.fromisoformat(iso) - today).days
    except Exception: return None

def grad(p):
    p = p or 0
    return ("linear-gradient(135deg,#0A2540 0%,#1a4a6e 50%,#0D9488 100%)" if p >= 1e6 else
            "linear-gradient(135deg,#134e4a 0%,#0D9488 50%,#2dd4bf 100%)" if p >= 5e5 else
            "linear-gradient(135deg,#1e3a5f 0%,#3b82f6 50%,#60a5fa 100%)" if p >= 3e5 else
            "linear-gradient(135deg,#065f46 0%,#10b981 50%,#6ee7b7 100%)")

def usd(n):
    try: return "$" + format(int(round(float(n))), ",")
    except Exception: return ""

def load_records():
    """Every community the finished dataset says has a live incentive."""
    raw = gzip.open(DATA_GZ, "rt").read()
    m = re.search(r"const DATA\s*=\s*(\[.*\])\s*;?\s*$", raw, re.S)
    if not m: raise RuntimeError("communities_all.js.gz: could not find the DATA array")
    return [r for r in json.loads(m.group(1)) if r.get("i")]

def logo_tag():
    """Reuse the finder's own header logo so the two pages are visibly the same site."""
    try:
        s = open(FINDER_HTML).read()
        m = re.search(r'<img src="(data:image/png;base64,[^"]+)" alt="">', s)
        if m: return f'<img src="{m.group(1)}" alt="">'
    except Exception: pass
    return ""

def price_line(r):
    p, x = r.get("p"), r.get("x")
    if p and x and x > p: return f"{usd(p)} to {usd(x)}"
    if p: return f"From {usd(p)}"
    return "Contact for pricing"

def card(r, nearby, today):
    name, url = r["n"], r.get("u") or ""
    if r.get("dl"): url = r.get("f") or url          # page is 404 today: send buyers somewhere that works
    d = days_left(r.get("ix"), today)
    urgent = d is not None and d <= 7
    thru = pretty(r.get("ix"))
    chip = ""
    if thru:
        left = f' <span class="chip-days">&middot; {d} day{"" if d == 1 else "s"} left</span>' if d is not None and d >= 0 else ""
        chip = f'<p class="chip{" urgent" if urgent else ""}">Through {esc(thru)}{left}</p>'

    if r.get("ph"):
        media = (f'<img src="{attr(r["ph"])}?width=880&height=495&aspect_ratio=880:495" alt="{attr("Home for sale in " + name)}" loading="lazy">'
                 + (f'<a class="shot" href="{attr(r.get("pu"))}">Current listing &middot; {esc(r.get("pa"))}</a>' if r.get("pu") and r.get("pa") else ""))
    elif r.get("g"):
        media = f'<img src="{attr(r["g"])}" alt="{attr(name + " community entrance sign")}" loading="lazy">'
    else:
        media = f'<div class="ph" style="background:{grad(r.get("p"))}"></div>'

    # Attribute the OFFER only to the builder named on the incentive row (ib). The community's own builder
    # field (b) is who builds there, which is not always who is funding the promotion — Central Park Townhomes
    # is a DR Horton community running an RJM Custom Homes offer. Saying "Offered by DR Horton" there would be
    # a false claim, so with no ib we say "the builder" and let the copy name whoever it names.
    offer_builder = r.get("ib") or ""
    meta = " &middot; ".join(filter(None, [esc(r.get("y")), (esc(r["c"]) + " County") if r.get("c") else "",
                                           esc(r.get("b") + " community") if r.get("b") else ""]))
    facts = " &middot; ".join(filter(None, [price_line(r), f'{r["l"]:,} active listing{"" if r["l"] == 1 else "s"}' if r.get("l") and not (r.get("nl") or r.get("dl")) else ""]))

    near_html = ""
    if SHOW_NEARBY and nearby:
        items = "".join(f'<li><b>{esc(x["name"])}</b>{" &middot; " + esc(x["builder"]) if x["builder"] else ""}'
                        f'<span>{"nearby" if x["approx"] else str(x["miles"]) + " miles away"}</span></li>' for x in nearby[:3])
        near_html = ('<div class="near"><p class="near-h">Also running an incentive near here</p>'
                     f'<ul>{items}</ul>'
                     '<p class="near-n">Distances are approximate. Ask us for a side-by-side comparison of what each builder is offering.</p></div>')

    return f'''<article class="offer" id="{attr(re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"))}">
  <div class="media">{media}</div>
  <div class="body">
    <h2><a href="{attr(url)}">{esc(name)}</a></h2>
    <p class="meta">{meta}</p>
    {chip}
    <p class="copy">{esc(r["i"])}</p>
    {f'<p class="facts">{facts}</p>' if facts else ''}
    <p class="set-by">Offered by {esc(offer_builder or "the builder")}. Subject to change or cancellation without notice.</p>
    {near_html}
    <div class="cta"><a class="btn" href="{attr(url)}">View {esc(name)}</a>
      <a class="btn ghost" href="{attr(CONTACT)}">Ask about this incentive</a></div>
  </div>
</article>'''

CSS = """
:root{color-scheme:light;--navy:#0A2540;--navy-mid:#1a3a5c;--navy-light:#e8eef5;--teal:#0D9488;--teal-dark:#0f766e;
--teal-light:#e6f7f6;--gold:#C9A84C;--gold-deep:#8a6d1f;--gold-light:#fdf8eb;--white:#fff;--cream:#f4f6f9;
--border:#e2e6ea;--text:#0d1b2a;--text-mid:#4a5568;--text-light:#8898aa;--red:#b91c1c;--red-bg:#fee2e2;
--shadow:0 1px 4px rgba(10,37,64,.06),0 6px 20px rgba(10,37,64,.06)}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;-webkit-font-smoothing:antialiased;
background:var(--cream);color:var(--text);line-height:1.6}
a{color:inherit}
.header{background:var(--navy);border-bottom:3px solid var(--teal);position:sticky;top:0;z-index:100}
.header-inner{max-width:1160px;margin:0 auto;padding:12px 24px;display:flex;align-items:center;justify-content:space-between;gap:16px}
.logo{display:flex;align-items:center;gap:11px;text-decoration:none;color:#fff}
.logo img{height:38px;width:auto;display:block}
.logo-text{font-size:16px;font-weight:700;letter-spacing:-.01em}
.logo-text span{color:var(--gold)}
.header-cta{display:flex;align-items:center;gap:14px}
.header-phone{color:#fff;text-decoration:none;font-weight:600;font-size:14px}
.header-phone:hover{color:var(--gold)}
.btn-consult{background:var(--teal);color:#fff;text-decoration:none;font-weight:600;font-size:13px;padding:9px 16px;border-radius:8px}
.btn-consult:hover{background:var(--teal-dark)}
.hero{background:var(--navy);color:#fff;padding:34px 24px 40px}
.hero-in{max-width:1160px;margin:0 auto}
.eyebrow{font-size:11px;font-weight:700;letter-spacing:.10em;text-transform:uppercase;color:var(--gold)}
h1{font-size:clamp(26px,4vw,38px);font-weight:800;letter-spacing:-.02em;line-height:1.15;margin:8px 0 12px;text-wrap:balance}
.lede{font-size:16px;line-height:1.65;color:rgba(255,255,255,.82);max-width:66ch}
.updated{margin-top:16px;font-size:12px;color:rgba(255,255,255,.6)}
.updated b{color:rgba(255,255,255,.9);font-weight:600}
main{max-width:1160px;margin:0 auto;padding:26px 24px 8px;display:flex;flex-direction:column;gap:18px}
.offer{display:grid;grid-template-columns:340px minmax(0,1fr);background:var(--white);border:1px solid var(--border);
border-radius:14px;overflow:hidden;box-shadow:var(--shadow)}
.media{position:relative;background:var(--navy-light);min-height:240px}
.media img,.media .ph{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block}
.shot{position:absolute;left:12px;top:12px;max-width:82%;background:rgba(10,37,64,.85);color:#fff;font-size:11px;
font-weight:600;padding:4px 10px;border-radius:4px;text-decoration:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.shot:hover{background:var(--teal)}
.body{padding:20px 22px;display:flex;flex-direction:column;gap:9px;align-items:flex-start}
h2{font-size:21px;font-weight:700;letter-spacing:-.01em;line-height:1.25}
h2 a{text-decoration:none}
h2 a:hover{color:var(--teal-dark);text-decoration:underline;text-underline-offset:3px}
.meta{font-size:12.5px;color:var(--text-mid)}
.chip{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;
background:var(--gold-light);color:var(--gold-deep);border:1px solid var(--gold);border-radius:999px;padding:4px 12px}
.chip.urgent{background:var(--red-bg);border-color:var(--red);color:var(--red)}
.chip-days{font-weight:600;opacity:.85}
.copy{font-size:15px;line-height:1.6}
.facts{font-size:13px;color:var(--text-mid);font-variant-numeric:tabular-nums}
.set-by{font-size:12px;line-height:1.5;color:var(--text-light)}
.near{width:100%;background:var(--cream);border:1px solid var(--border);border-radius:10px;padding:12px 14px;margin-top:2px}
.near-h{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--text-light);margin-bottom:7px}
.near ul{list-style:none;display:flex;flex-direction:column;gap:5px}
.near li{font-size:13px;color:var(--text-mid);display:flex;flex-wrap:wrap;gap:6px;align-items:baseline}
.near li b{color:var(--text);font-weight:600}
.near li span{font-size:11px;color:var(--text-light);text-transform:uppercase;letter-spacing:.04em}
.near-n{font-size:11.5px;color:var(--text-light);margin-top:8px;line-height:1.5}
.cta{display:flex;flex-wrap:wrap;gap:9px;margin-top:4px}
.btn{display:inline-block;background:var(--teal);color:#fff;text-decoration:none;font-weight:600;font-size:13.5px;
padding:10px 18px;border-radius:8px}
.btn:hover{background:var(--teal-dark)}
.btn.ghost{background:transparent;color:var(--teal-dark);border:1px solid var(--border)}
.btn.ghost:hover{background:var(--teal-light);border-color:var(--teal)}
.empty{background:var(--white);border:1px solid var(--border);border-radius:14px;padding:40px 28px;text-align:center;box-shadow:var(--shadow)}
.empty h2{font-size:20px;margin-bottom:8px}
.empty p{color:var(--text-mid);font-size:15px;max-width:56ch;margin:0 auto 18px}
.explain{max-width:1160px;margin:22px auto 0;padding:0 24px;display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
.ex{background:var(--white);border:1px solid var(--border);border-radius:12px;padding:18px 20px}
.ex h3{font-size:14px;font-weight:700;margin-bottom:6px}
.ex p{font-size:13.5px;color:var(--text-mid);line-height:1.6}
.faq{max-width:1160px;margin:26px auto 0;padding:0 24px}
.faq h2{font-size:20px;margin-bottom:12px}
.faq details{background:var(--white);border:1px solid var(--border);border-radius:10px;padding:14px 18px;margin-bottom:9px}
.faq summary{font-weight:600;font-size:14.5px;cursor:pointer;list-style:none}
.faq summary::-webkit-details-marker{display:none}
.faq summary::before{content:'+';color:var(--teal-dark);font-weight:700;margin-right:9px}
.faq details[open] summary::before{content:'\\2212'}
.faq p{font-size:14px;color:var(--text-mid);margin-top:9px;line-height:1.65}
.footer{background:var(--navy);color:rgba(255,255,255,.72);margin-top:34px;padding:26px 24px 30px}
.footer-in{max-width:1160px;margin:0 auto;font-size:12px;line-height:1.65}
.footer strong{color:rgba(255,255,255,.92)}
.footer a{color:var(--gold)}
.footer-bottom{margin-top:16px;padding-top:14px;border-top:1px solid rgba(255,255,255,.14);font-size:11.5px;color:rgba(255,255,255,.55)}
:focus-visible{outline:3px solid var(--teal);outline-offset:2px}
@media (max-width:860px){.offer{grid-template-columns:1fr}.media{min-height:210px;height:210px}}
@media (max-width:560px){.header-phone{display:none}.hero{padding:26px 20px 30px}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

FAQ = [
    ("What is a builder incentive?",
     "A builder incentive is a promotion offered directly by a new construction builder — for example money toward "
     "closing costs, a design-center allowance, an included upgrade package, or a price adjustment on a quick move-in "
     "home. The builder sets the offer, funds it, and decides when it ends."),
    ("Do I need an agent to get the builder's incentive?",
     "The incentive comes from the builder either way, and having your own representation does not reduce it. What "
     "changes is whether anyone is reviewing the contract, the site conditions, and the price on your behalf — the "
     "on-site sales agent works for the builder. Register with Paradise Realty FLA before your first visit to a builder "
     "sales office, because most builders will not allow representation to be added after that first registration."),
    ("How often is this page updated?",
     "It rebuilds automatically twice a day from our builder incentive records. Offers come off the page the day they "
     "expire, so nothing here is stale. If a community is not listed, we do not currently have a confirmed offer for it "
     "— that does not always mean there is not one, so it is worth asking."),
    ("Why do some offers not show an interest rate?",
     "Advertised financing rates change constantly and are tied to lender terms, credit, and timing we cannot verify "
     "here. Where a builder promotes a specific rate we say that builder promotional interest rates may be offered and "
     "let the builder and their lender quote you the current terms in writing."),
    ("Can these offers change?",
     "Yes. Incentives are set by the builder and may change, shrink, or end at any time without notice, and they often "
     "carry conditions such as using a preferred lender or title company, or applying only to selected homesites or "
     "quick move-in homes. Always confirm the current terms in writing before relying on them."),
]

def main():
    today = datetime.date.today()
    recs = sorted(load_records(), key=lambda r: (r.get("ix") or "9999-99-99", r["n"]))
    near_all = json.load(open(f"{BV}/nearby_incentives.json")) if os.path.exists(f"{BV}/nearby_incentives.json") else {}
    live = {r["n"] for r in recs}
    n = len(recs)

    if n:
        counties = sorted({r["c"] for r in recs if r.get("c")})
        where = (counties[0] + " County" if len(counties) == 1 else
                 " and ".join([", ".join(c + " County" for c in counties[:-1]), counties[-1] + " County"]) if len(counties) > 1 else "Florida")
        lede = (f"{n} Florida new construction communit{'y is' if n == 1 else 'ies are'} currently advertising a builder "
                f"incentive in {where}. Every offer below comes from the builder, shows the date it runs through, and "
                f"links straight to the community.")
        body = "".join(card(r, [x for x in near_all.get(r["n"], []) if x["name"] in live and x["name"] != r["n"]], today) for r in recs)
    else:
        # self-clearing: nothing is running, so the page says so rather than showing a stale offer
        lede = ("No builder in our coverage area is advertising a current incentive today. This page updates twice a "
                "day, and offers appear here the moment they are confirmed.")
        body = ('<div class="empty"><h2>No builder incentives are running right now</h2>'
                '<p>Builders usually release new offers at the start of a month and at the end of a quarter. We track '
                'them as they are announced — ask us and we will tell you the day something opens up in a community '
                f'you are watching.</p><a class="btn" href="{attr(CONTACT)}">Tell us what you are looking for</a></div>')

    items = "".join(
        f'{{"@type":"ListItem","position":{i+1},"item":{{"@type":"Offer","name":{json.dumps("Builder incentive at " + r["n"])},'
        f'"description":{json.dumps(r["i"][:300])},"category":"Builder incentive",'
        f'"availabilityEnds":{json.dumps(r.get("ix",""))},'
        + (f'"seller":{json.dumps({"@type":"Organization","name":r["ib"]})},' if r.get("ib") else "")
        +
        f'"url":{json.dumps(r.get("u",""))}}}}}' + ("," if i < n - 1 else "") for i, r in enumerate(recs))
    faq_ld = ",".join(f'{{"@type":"Question","name":{json.dumps(q)},"acceptedAnswer":{{"@type":"Answer","text":{json.dumps(a)}}}}}' for q, a in FAQ)
    ld = ('<script type="application/ld+json">{"@context":"https://schema.org","@graph":['
          f'{{"@type":"ItemList","name":"Current Florida builder incentives","numberOfItems":{n},"itemListElement":[{items}]}},'
          f'{{"@type":"FAQPage","mainEntity":[{faq_ld}]}},'
          '{"@type":"RealEstateAgent","name":"Paradise Realty FLA","telephone":"+1-772-247-7110",'
          '"address":{"@type":"PostalAddress","streetAddress":"6103 SE Federal Hwy","addressLocality":"Stuart",'
          '"addressRegion":"FL","postalCode":"34997","addressCountry":"US"}}]}</script>')

    faq_html = "".join(f"<details><summary>{esc(q)}</summary><p>{esc(a)}</p></details>" for q, a in FAQ)
    desc = (f"Current builder incentives at {n} Florida new construction communities, with the date each offer runs "
            f"through. Updated twice daily by Paradise Realty FLA.") if n else \
           "Florida builder incentives, tracked and updated twice daily by Paradise Realty FLA."

    page = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Current Builder Incentives | Paradise Realty FLA</title>
<meta name="description" content="{attr(desc)}">
<link rel="canonical" href="{attr(CANONICAL)}">
<meta property="og:title" content="Current Florida Builder Incentives">
<meta property="og:description" content="{attr(desc)}">
<meta property="og:type" content="website">
<meta property="og:url" content="{attr(CANONICAL)}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap">
<style>{CSS}</style>
{ld}
</head>
<body>
<header class="header"><div class="header-inner">
  <a href="https://www.paradiserealtyfla.com" class="logo">{logo_tag()}<span class="logo-text">Paradise Realty <span>FLA</span></span></a>
  <div class="header-cta">
    <a href="{attr(PHONE_HREF)}" class="header-phone">{esc(PHONE)}</a>
    <a href="{attr(CONTACT)}" class="btn-consult">Free Consultation</a>
  </div>
</div></header>

<section class="hero"><div class="hero-in">
  <p class="eyebrow">New construction &middot; Treasure Coast &amp; Palm Beaches</p>
  <h1>Current builder incentives</h1>
  <p class="lede">{esc(lede)}</p>
  <p class="updated">Updated <b>{today.strftime("%B %-d, %Y")}</b> &middot; rebuilt twice daily &middot;
    <a href="{attr(FINDER)}" style="color:var(--gold)">Browse all Florida communities &rarr;</a></p>
</div></section>

<main>{body}</main>

<section class="explain">
  <div class="ex"><h3>Where these come from</h3><p>Each offer is taken from the builder's own announcement to us and
    published with the end date the builder gave. We do not add offers we cannot attribute to a specific community.</p></div>
  <div class="ex"><h3>What we leave out</h3><p>Advertised financing rates are replaced with a note that builder
    promotional interest rates may be offered, because the real terms depend on the lender, your credit, and the day
    you lock. The builder's lender quotes those in writing.</p></div>
  <div class="ex"><h3>Register before you visit</h3><p>Most builders will not let you add your own representation after
    you have registered at their sales office. Tell us first and it costs you nothing — the incentive still comes
    from the builder.</p></div>
</section>

<section class="faq"><h2>Questions buyers ask about builder incentives</h2>{faq_html}</section>

<footer class="footer"><div class="footer-in">
  <strong>About builder incentives:</strong> incentives shown are provided by the builder, are current as of the date
  displayed, and may change or end without notice. Terms, eligibility, and availability vary by community and by home,
  and may require the use of a preferred lender or title company. Nothing on this page is an offer, a guarantee of
  savings, or a commitment to lend. Confirm current details with Paradise Realty FLA before relying on them. Register
  with us before visiting a builder sales office to preserve your right to representation.
  <div class="footer-bottom">Paradise Realty FLA &middot; 6103 SE Federal Hwy, Stuart, FL 34997 &middot;
    <a href="{attr(PHONE_HREF)}">{esc(PHONE)}</a> &middot; <a href="{attr(FINDER)}">Community Finder</a></div>
</div></footer>
</body>
</html>'''
    open(OUT, "w").write(page)
    print(f"incentives.html: {os.path.getsize(OUT)//1024} KB | {n} live offer{'' if n == 1 else 's'}"
          + (f" | earliest expiry {recs[0].get('ix')}" if n else " | empty state"))
    return n

if __name__ == "__main__":
    main()
