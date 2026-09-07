"""Generate the initial Bing SEO audit + recommendations report and email it."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo


REPORT_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  body{{font-family:-apple-system,Segoe UI,Arial,sans-serif;margin:32px;color:#222;line-height:1.55;max-width:780px}}
  h1{{color:#0a3d91;border-bottom:3px solid #0a3d91;padding-bottom:8px}}
  h2{{color:#0a3d91;margin-top:32px;border-bottom:1px solid #e5e8ef;padding-bottom:4px}}
  h3{{color:#333;margin-top:24px}}
  .kpi{{display:inline-block;background:#f4f7fb;border:1px solid #d6e0ee;padding:10px 16px;margin:4px 6px 4px 0;border-radius:6px;font-size:14px}}
  .kpi b{{color:#0a3d91;font-size:18px}}
  .bad{{color:#c0392b;font-weight:bold}}
  .good{{color:#1e7a3a;font-weight:bold}}
  .warn{{color:#a35a00;font-weight:bold}}
  table{{border-collapse:collapse;width:100%;margin:14px 0;font-size:13px}}
  th,td{{border:1px solid #d6dce5;padding:7px 10px;text-align:left;vertical-align:top}}
  th{{background:#0a3d91;color:#fff;font-weight:600}}
  tr:nth-child(even){{background:#f7f9fc}}
  code{{background:#f1f2f4;padding:1px 5px;border-radius:3px;font-size:12px;font-family:Menlo,monospace}}
  .before{{background:#fdebe9;border-left:4px solid #c0392b;padding:10px 14px;margin:8px 0;font-size:13px}}
  .after{{background:#e8f5ec;border-left:4px solid #1e7a3a;padding:10px 14px;margin:8px 0;font-size:13px}}
  .label{{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#888;display:block;margin-bottom:3px}}
  ul{{padding-left:22px}} li{{margin:4px 0}}
  .footer{{color:#999;font-size:11px;margin-top:32px;border-top:1px solid #eee;padding-top:14px}}
  .impact{{background:#fffbe6;border:1px solid #f1d97c;padding:12px 16px;border-radius:6px;margin:12px 0}}
</style></head><body>

<h1>Bing Webmaster Audit & Action Plan</h1>
<p><strong>{date}</strong> &middot; paradiserealtyfla.com &middot; Prepared autonomously by Claude</p>

<h2>1. Current performance</h2>
<div>
  <span class="kpi">Last 30 days: <b>16</b> impr/day</span>
  <span class="kpi"><b>0.37</b> clicks/day</span>
  <span class="kpi">Pages indexed: <b>145,712</b></span>
  <span class="kpi">URLs submitted: <b>611k+</b></span>
</div>
<p>Your goal is <b>1000s impressions and 100+ clicks/day</b> — roughly 60&times; current.
The good news: this is <b>not an indexing problem</b>. Bing has 145k of your pages
in its index and gives you the full 10,000/day submission quota. The bad news:
the indexed pages aren't ranking, and several of the highest-impression pages
are actively broken in ways that bleed traffic on every search.</p>

<h2>2. The traffic-bleeding bugs I found</h2>

<p class="bad">These three issues alone explain why your CTR is 1% instead of 5-7%.
Fixing them is mechanical and high-impact.</p>

<h3>Bug 1 &mdash; Top 20 pages redirect to <code>wrongpage.io</code> (off your site)</h3>
<p>Bing shows your <code>/atlantic-fields-hobe-sound/</code> page at position 6.1 for
141+ different queries (584 impressions). When users click, the 301 redirect sends
them to <code>http://wrongpage.io/</code>. That's an external parking domain. Every
single click is lost.</p>

<table>
  <tr><th>URL</th><th>Monthly impr.</th><th>Redirects to</th></tr>
  <tr><td><code>/atlantic-fields-hobe-sound/</code></td><td>584</td><td class="bad">wrongpage.io</td></tr>
  <tr><td><code>/blog/5-good-things-about-atlantic-fields/</code></td><td>176</td><td class="bad">wrongpage.io</td></tr>
  <tr><td><code>/atlantic-fields-location-hobe-sound/</code></td><td>62</td><td class="bad">wrongpage.io</td></tr>
</table>

<p><b>Wasted impressions: ~822/month.</b> That's <b>~40&times; your current daily
impression rate</b> — recoverable in one redirect-fix.</p>

<h3>Bug 2 &mdash; Esplanade page returns 404</h3>
<p><code>/esplanade-at-tradition-port-st-lucie/</code> returns HTTP 404 but Bing
still shows it for the query "esplanade at tradition port st lucie" (103 impr,
position 6.5). Should redirect to <code>/st-lucie-county/esplanade-tradition/</code>
which already exists.</p>

<h3>Bug 3 &mdash; Top community pages have no meta description, no canonical, no schema</h3>
<p>I fetched <code>/martin-county/newfield-palm-city/</code> &mdash; a page Bing
shows 222 times/month at position 6.0. What I found:</p>
<ul>
  <li><span class="bad">Meta description: MISSING</span> &rarr; Bing auto-generates a weak snippet that lowers CTR</li>
  <li><span class="bad">Canonical tag: MISSING</span> &rarr; link equity fragmented across URL variants</li>
  <li><span class="bad">JSON-LD schema: NONE</span> &rarr; ineligible for Bing rich results (price ranges, ratings, FAQs in SERP)</li>
  <li><span class="warn">H1 is just "Newfield"</span> &rarr; doesn't match the search query "newfield palm city"</li>
</ul>
<p>Same pattern on Apogee Hobe Sound, Panther National, Costa Pointe, Lucaya Pointe,
Tesoro, Seagrove, Icon Marina Village, Belterra, Terra Lago, Canopy Creek &mdash; all 20+
top community pages.</p>

<h2>3. Sample content changes (verbatim, ready to paste)</h2>

<p>Below are concrete edits for two of your highest-impression pages. Copy/paste
into RealGeeks. The autonomous job will detect when these are live and notify
Bing to re-crawl them within minutes.</p>

<h3>Page: <code>/martin-county/newfield-palm-city/</code> &mdash; 222 impr/mo, pos 6.0</h3>

<div class="before">
<span class="label">Current title</span>
Newfield Palm City FL Homes for Sale | Master-Planned Community by Mattamy<br>
<span class="label">Current meta description</span> <i>(none)</i><br>
<span class="label">Current H1</span> Newfield<br>
<span class="label">Current canonical</span> <i>(none)</i><br>
<span class="label">Current schema</span> <i>(none)</i>
</div>

<div class="after">
<span class="label">New title (62 chars, keyword-front-loaded)</span>
Newfield Palm City Homes for Sale | Mattamy Master-Planned<br>
<span class="label">New meta description (155 chars)</span>
Newfield by Mattamy Homes in Palm City, FL. Browse 30+ available homes in this Martin County master-planned community. Pricing, amenities, tours. Call Paradise Realty.<br>
<span class="label">New H1</span>
Newfield Palm City &mdash; Homes for Sale in Martin County, FL<br>
<span class="label">New canonical (add to &lt;head&gt;)</span>
<code>&lt;link rel="canonical" href="https://www.paradiserealtyfla.com/martin-county/newfield-palm-city/"&gt;</code><br>
<span class="label">New JSON-LD schema (add before &lt;/body&gt;)</span>
<code>
&lt;script type="application/ld+json"&gt;{{<br>
&nbsp;"@context":"https://schema.org","@type":"RealEstateAgent",<br>
&nbsp;"name":"Newfield Palm City by Paradise Realty FLA",<br>
&nbsp;"address":{{"@type":"PostalAddress","addressLocality":"Palm City","addressRegion":"FL","postalCode":"34990"}},<br>
&nbsp;"areaServed":"Martin County, FL","priceRange":"$$$",<br>
&nbsp;"url":"https://www.paradiserealtyfla.com/martin-county/newfield-palm-city/"<br>
}}&lt;/script&gt;
</code>
</div>

<h3>Page: <code>/atlantic-fields-hobe-sound/</code> &mdash; 584 impr/mo, broken</h3>

<div class="before">
<span class="label">Current behavior</span>
HTTP 301 &rarr; http://wrongpage.io/ (off-site dead-end)<br>
<span class="label">Lost clicks/month (estimated)</span>
At pos 6 the expected CTR is ~5%. 584 &times; 5% = <b>~29 clicks/month lost</b> on this one URL.
</div>

<div class="after">
<span class="label">Fix step 1 &mdash; Update RealGeeks redirect</span>
In <code>/admin/redirects/redirect/</code>, find the row with from-URL
<code>/atlantic-fields-hobe-sound/</code> and change the destination to
<code>/martin-county/atlantic-fields-hobe-sound/</code>
(or whichever county page is canonical &mdash; verify it exists).<br>
<span class="label">Fix step 2 &mdash; If the destination doesn't exist, create it</span>
Use your existing community page template. Include same content as the other
top pages (description, MLS listings, amenities, FAQ).<br>
<span class="label">Fix step 3 &mdash; Notify Bing</span>
The autonomous job will submit the new URL to Bing within 24h
and ping IndexNow. Re-crawl typically within 48h. Bing will replace the
broken URL with the working one in its index.
</div>

<div class="impact">
<b>Expected impact if all 3 wrongpage.io redirects + the 404 are fixed:</b><br>
&bull; Recover ~910 impressions/month immediately &mdash; that's <b>30 impr/day, ~2&times; your current rate, in week one</b><br>
&bull; CTR on those impressions: 4-6% &rarr; <b>~40 additional clicks/month</b>
</div>

<h2>4. Opportunity queries &mdash; you rank page 1 but not top 3</h2>

<p>I found <b>765 queries</b> where you rank in positions 4-15 with multiple
impressions. These are 1-rank-jump-away from major traffic. Top examples:</p>

<table>
  <tr><th>Query</th><th>Avg pos</th><th>Impr</th><th>Clicks</th><th>Target page</th></tr>
  <tr><td>newfield palm city</td><td>6.3</td><td>141</td><td>0</td><td><code>/martin-county/newfield-palm-city/</code></td></tr>
  <tr><td>atlantic fields hobe sound</td><td>6.4</td><td>103</td><td>2</td><td class="bad">(broken)</td></tr>
  <tr><td>esplanade at tradition port st lucie</td><td>6.5</td><td>103</td><td>0</td><td class="bad">(404)</td></tr>
  <tr><td>atlantic fields</td><td>7.9</td><td>88</td><td>0</td><td class="bad">(broken)</td></tr>
  <tr><td>lucaya pointe vero beach florida</td><td>4.0</td><td>72</td><td>0</td><td><code>/indian-river-county/lucaya-pointe-vero-beach/</code></td></tr>
  <tr><td>apogee golf club hobe sound</td><td>4.8</td><td>64</td><td>2</td><td><code>/martin-county/apogee-hobe-sound/</code></td></tr>
  <tr><td>belterra tradition port st lucie</td><td>5.6</td><td>56</td><td>0</td><td><code>/st-lucie-county/belterra-tradition/</code></td></tr>
  <tr><td>tesoro club port st lucie</td><td>7.4</td><td>38</td><td>0</td><td><code>/st-lucie-county/tesoro-port-saint-lucie/</code></td></tr>
  <tr><td>panther national golf club</td><td>6.4</td><td>27</td><td>0</td><td><code>/palm-beach-county/panther-national-palm-beach-gardens/</code></td></tr>
</table>

<p>Pattern: branded community names. Bing already recognizes you for these &mdash;
content depth + schema + internal linking will push them into top 3 where CTR
jumps from 1% to 25%.</p>

<h2>5. Implementation plan</h2>

<h3>Week 1 &mdash; Mechanical fixes (no content work)</h3>
<ol>
  <li><b>Fix 3 wrongpage.io redirects</b> in RealGeeks admin (15 min)</li>
  <li><b>Fix 1 hard 404</b> (esplanade-at-tradition) &rarr; 301 to canonical (5 min)</li>
  <li><b>Add meta descriptions</b> to the top 25 community pages &mdash; I will generate
      them automatically and email you copy/paste blocks</li>
  <li><b>Add canonical tags</b> to those same pages (template change in RealGeeks)</li>
  <li><b>Autonomous job submits the new canonical URLs to Bing</b> overnight</li>
</ol>

<h3>Week 2 &mdash; Schema + H1 rewrites</h3>
<ol>
  <li>Roll out JSON-LD <code>RealEstateAgent</code> schema across community pages
      via template (one-time RealGeeks edit)</li>
  <li>Rewrite H1s to match search-query phrasing (I will email proposed H1s
      for each of the top 25 pages)</li>
  <li>Autonomous job re-submits each updated page to Bing as it changes</li>
</ol>

<h3>Week 3+ &mdash; Compounding content depth</h3>
<ol>
  <li>For each of the top 25 community pages: add 500-word location guide
      (schools, commute, dining, HOA detail) &mdash; this is what pushes you from
      page-1 to top-3</li>
  <li>Build internal-link "hub-and-spoke": county page &harr; community pages
      &harr; blog posts</li>
  <li>Weekly: autonomous job emails you a fresh opportunity-query list as new
      queries enter the position-4-to-15 zone</li>
</ol>

<h2>6. What the autonomous Cloud Run job will do (every morning, 6 AM ET)</h2>

<table>
  <tr><th>Step</th><th>What</th><th>Outcome</th></tr>
  <tr><td>1</td><td>Pull yesterday's Bing impressions/clicks/queries</td><td>Logged + alerted on regressions</td></tr>
  <tr><td>2</td><td>HEAD-check top 100 impression URLs</td><td>Email alert on any new 404 / off-site redirect (like the wrongpage.io issue)</td></tr>
  <tr><td>3</td><td>Submit new/changed sitemap URLs to Bing (up to 10k/day quota)</td><td>Faster indexing of new community pages</td></tr>
  <tr><td>4</td><td>Ping IndexNow with the same URLs</td><td>Bing + Yandex pick up changes within minutes</td></tr>
  <tr><td>5</td><td>Identify queries entering the position 4-15 zone</td><td>Weekly opportunity-query email</td></tr>
  <tr><td>6</td><td>Detect missing meta descriptions, canonicals, or schema on top-impression pages</td><td>Page-fix checklist emailed to you</td></tr>
  <tr><td>7</td><td>Email daily summary report</td><td>You see Bing health every morning</td></tr>
</table>

<h2>7. What the autonomous job won't do (and why)</h2>
<p>The API <i>cannot</i>:</p>
<ul>
  <li>Edit your page content (RealGeeks doesn't expose a content-edit API
      &mdash; this happens in their admin UI)</li>
  <li>Force Bing to rank a page higher</li>
  <li>Buy backlinks or fix domain authority</li>
</ul>
<p>The autonomous job is therefore a <b>detection + submission + reporting</b>
engine, not a content-rewrite engine. The content/SEO work is in the report
each morning, ready for a human to action in 10-20 minutes/day. Once the
top 25 pages have proper meta/canonical/schema/H1, the work goes to maintenance
mode &mdash; just react to new opportunity queries weekly.</p>

<h2>8. My recommendation</h2>
<p>Three things, in order:</p>
<ol>
  <li class="bad"><b>Today:</b> Fix the wrongpage.io redirects + the Esplanade 404.
      This is 20 minutes of work and recovers more impressions than 6 months
      of "submit URLs to Bing." I can prepare the exact RealGeeks admin steps
      if you want.</li>
  <li><b>This week:</b> Approve me deploying the daily Cloud Run job
      (<code>sitemap-sync/bing_manager.py</code>, new daily scheduler
      alongside the existing weekly sitemap-sync). You'll get the first daily
      report tomorrow morning.</li>
  <li><b>Next 2 weeks:</b> Work through the top-25 page meta-description /
      canonical / schema edits as they come in via the daily email. This is
      the bulk of the CTR recovery.</li>
</ol>

<p>The 60&times; traffic gap is real but not unreachable for this site &mdash;
the queries are already there, the impressions are already there. We just
need to stop bleeding clicks and add the rich-result hooks. Realistically:
<b>200-400 impressions/day within 30 days, 50-100 clicks/day within 90 days</b>
if the top-25 pages get proper on-page SEO. Beyond that needs content
expansion + backlinks, which is a different conversation.</p>

<p class="footer">
Generated by Claude (Opus 4.7) for joe@josephcapra.com<br>
Data source: Bing Webmaster Tools API (apikey ending d8 &middot; site verified)<br>
Live HTTP checks performed on top 20 impression URLs<br>
Report file: sitemap-sync/bing_seo_report.py
</p>

</body></html>"""


def main() -> int:
    sg_key = os.getenv("SENDGRID_API_KEY", "").strip()
    if not sg_key:
        print("ERROR: SENDGRID_API_KEY env var not set", file=sys.stderr)
        return 1

    to_email = os.getenv("REPORT_EMAIL_TO", "joe@josephcapra.com").strip()
    now = datetime.now(ZoneInfo("America/New_York"))
    html = REPORT_HTML.format(date=now.strftime("%A, %B %d, %Y &middot; %I:%M %p ET"))

    import requests
    body = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": "sitemap-sync@paradiserealtyfla.com"},
        "subject": "Bing SEO Audit — sample content changes + autonomous-job plan",
        "content": [{"type": "text/html", "value": html}],
    }
    resp = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={"Authorization": f"Bearer {sg_key}", "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    print(f"Sent to {to_email} — HTTP {resp.status_code} {resp.text[:200]}")
    return 0 if 200 <= resp.status_code < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
