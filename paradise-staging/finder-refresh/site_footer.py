#!/usr/bin/env python3
"""The paradiserealtyfla.com footer, rebuilt for the finder subdomain.

Joe's ask: keep the same footer as the main site so search.paradiserealtyfla.com reads as the same
company, not a bolted-on tool.

Why rebuilt rather than copied: the live footer is RealGeeks markup styled by their Tailwind build
(`bg-rg-footer`, `flex flex-col items-center`, ...). Pasting it here would render unstyled, and
pulling in their stylesheet would fight the finder's own CSS. So the structure, content, links and
colours are reproduced natively — same seven nav items, same monospace company block, the same 30
county links in the same order, the same #f0f5fe bottom bar.

Every link points at www.paradiserealtyfla.com absolutely, because these pages are served from a
different host and a root-relative href would resolve against the subdomain and 404.

Not reproduced: the floating call button, consultation pill and chat bubble. Those come from
RealGeeks > Settings > Custom Code > Footer HTML and carry the ?agent_id= attribution logic; they are
a separate widget with its own state, not part of this footer block.
"""

WWW = "https://www.paradiserealtyfla.com"
LOGO = "https://u.realgeeks.media/paradiserealtyfla/54d2b9efa49c-Transparent_Logo.png"
RG_LOGO = "https://cdn.realgeeks.com/static/designs/img/real-geeks-logo.svg?v=4"
RG_REF = ("https://www.realgeeks.com/?utm_campaign=client%20site%20ref&utm_source=client%20site%20referral"
          "&utm_content=www.paradiserealtyfla.com&utm_medium=referral")

NAV = [("Home", "/"), ("Advanced Search", "/search/advanced_search/"), ("Selling", "/seller/"),
       ("Buying", "/buyer/"), ("About Us", "/team/"), ("Blog", "/blog/"),
       ("Off-Market Properties", "/off-market/")]

# same order as the live site
# (url slug, display name) — the display name is NOT derivable from the slug: "Miami-Dade" keeps its
# hyphen while "St.-Lucie" and "Palm-Beach" lose theirs, so both are stated explicitly.
COUNTIES = [("Martin", "Martin"), ("St.-Lucie", "St. Lucie"), ("Palm-Beach", "Palm Beach"),
            ("Indian-River", "Indian River"), ("Seminole", "Seminole"), ("Sarasota", "Sarasota"),
            ("Orange", "Orange"), ("Manatee", "Manatee"), ("Hillsborough", "Hillsborough"),
            ("Sumter", "Sumter"), ("Pinellas", "Pinellas"), ("Pasco", "Pasco"),
            ("Charlotte", "Charlotte"), ("Hernando", "Hernando"), ("Polk", "Polk"), ("Lake", "Lake"),
            ("Volusia", "Volusia"), ("Osceola", "Osceola"), ("Marion", "Marion"), ("Flagler", "Flagler"),
            ("Miami-Dade", "Miami-Dade"), ("Okeechobee", "Okeechobee"), ("Brevard", "Brevard"),
            ("Guaynabo", "Guaynabo"), ("Dorado", "Dorado"), ("San-Juan", "San Juan"),
            ("Trujillo-Alto", "Trujillo Alto"), ("Carolina", "Carolina"), ("Citrus", "Citrus"),
            ("Highlands", "Highlands")]

INCENTIVE_NOTE = (
    "<strong>About builder incentives:</strong> incentives shown are provided by the builder, are current as of "
    "the date displayed, and may change or end without notice. Terms, eligibility, and availability vary by "
    "community and by home, and may require the use of a preferred lender or title company. Nothing here is an "
    "offer, a guarantee of savings, or a commitment to lend. Confirm current details with Paradise Realty FLA "
    "before relying on them. Register with us before visiting a builder sales office to preserve your right to "
    "representation.")

CSS = """
.sfoot{background:#fff;border-top:1px solid #e5e7eb;margin-top:40px}
.sfoot-nav{display:flex;flex-wrap:wrap;justify-content:space-between;gap:4px 18px;background:#f7f8f9;
  border-bottom:1px solid #e5e7eb;padding:14px 28px}
.sfoot-nav a{flex:1 1 auto;text-align:center;color:#1f2937;text-decoration:none;font-size:12.5px;
  font-weight:500;letter-spacing:.05em;text-transform:uppercase;white-space:nowrap;padding:4px 2px}
.sfoot-nav a:hover{color:#0D9488}
.sfoot-main{max-width:1280px;margin:0 auto;padding:34px 28px 30px;display:grid;
  grid-template-columns:minmax(220px,1fr) minmax(0,1.4fr);gap:28px 40px;align-items:start}
.sfoot-brand img{width:170px;height:auto;display:block;margin-bottom:20px}
.sfoot-brand pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px;
  line-height:1.65;color:#374151;margin:0;white-space:pre-wrap}
.sfoot-counties{display:grid;grid-template-columns:1fr 1fr;gap:2px 24px}
.sfoot-counties a{color:#6b7280;text-decoration:none;font-size:13px;text-align:center;padding:5px 2px}
.sfoot-counties a:hover{color:#0D9488;text-decoration:underline}
.sfoot-note{max-width:1280px;margin:0 auto;padding:0 28px 26px;font-size:11.5px;line-height:1.6;color:#6b7280}
.sfoot-note strong{color:#374151}
.sfoot-bottom{background:#f0f5fe;padding:14px 28px;display:flex;flex-wrap:wrap;align-items:center;
  gap:6px 14px;font-size:12.5px;color:#4b5563}
.sfoot-bottom a{color:#4b5563;text-decoration:none}
.sfoot-bottom a:hover{text-decoration:underline}
.sfoot-bottom img{height:15px;width:auto;vertical-align:middle;margin:0 2px}
.sfoot-copy{margin-left:auto;color:#6b7280}
@media (max-width:820px){.sfoot-main{grid-template-columns:1fr}.sfoot-nav{justify-content:flex-start}
  .sfoot-nav a{flex:0 0 auto}.sfoot-copy{margin-left:0;width:100%}}
"""

def html(include_incentive_note=True):
    nav = "".join(f'<a href="{WWW}{h}">{t}</a>' for t, h in NAV)
    counties = "".join(f'<a href="{WWW}/listings/county/{slug}/">{name} County</a>' for slug, name in COUNTIES)
    note = f'<div class="sfoot-note">{INCENTIVE_NOTE}</div>' if include_incentive_note else ""
    return f'''<footer class="sfoot">
  <nav class="sfoot-nav">{nav}</nav>
  <div class="sfoot-main">
    <div class="sfoot-brand">
      <a href="{WWW}"><img src="{LOGO}" alt="Paradise Realty FLA" loading="lazy"></a>
      <pre>Paradise Realty FLA
Joseph Capra
Licensed Real Estate Broker
"A Veteran-Owned Company"
6103 SE Federal Highway, Stuart, FL 34997
DRE#: 1066402</pre>
    </div>
    <div class="sfoot-counties">{counties}</div>
  </div>
  {note}
  <div class="sfoot-bottom">
    <span>IDX Site Powered by <a href="{RG_REF}" rel="nofollow noopener" target="_blank"><img src="{RG_LOGO}" alt="Real Geeks"></a></span>
    <a href="{WWW}/rg-accessibility">Accessibility</a>
    <a href="{WWW}/rg-terms">Terms</a>
    <a href="{WWW}/rg-privacy">Privacy</a>
    <span class="sfoot-copy">&copy; 2026 Paradise Realty FLA. All rights reserved.</span>
  </div>
</footer>'''
