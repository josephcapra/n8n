#!/usr/bin/env python3
"""
Build the full Community Finder page (41k subdivisions + 988 curated) for the Cloud Run test service.
Input : scratchpad/community-finder-demo.src.html (theme + layout source of truth)
        ~/paradise-staging/finder-test/communities_all.js (from build_dataset.py)
Output: ~/paradise-staging/finder-test/index.html
"""
import json, os, re

HOME = os.path.expanduser("~")
SRC = "/private/tmp/claude-501/-Users-User/ed61b43f-3353-4c6d-9ca3-6f8aac402470/scratchpad/community-finder-demo.src.html"
DATA_JS = f"{HOME}/paradise-staging/finder-test/communities_all.js"
OUT = f"{HOME}/paradise-staging/finder-test/index.html"

FINDER_JS = r"""
const $ = id => document.getElementById(id);
const els = {q: $('search'), county: $('county'), city: $('city'), price: $('price'), inc: $('incentive'), lst: $('listings'),
             vid: $('video'), cur: $('curated'), sort: $('sort'), count: $('count'), grid: $('grid'), empty: $('empty'), more: $('more')};
const PAGE = 48;
let filtered = [], shown = 0;
// same amenity bitmask + price tiers as the previous finder, so nothing is lost in the cut-over
const AMEN = [[0,"Pool"],[1,"Gated"],[2,"Golf"],[3,"Waterfront"],[4,"Boat Access"],[5,"Clubhouse"],[6,"Fitness Center"],[7,"Tennis"],[8,"Pickleball"],[9,"Restaurant"],[10,"55+"],[11,"Playground"],[12,"Park"],[13,"Golf Carts OK"],[14,"Hurricane Shutters"],[15,"Outdoor Kitchen"],[16,"RV Parking"],[17,"Resort-Style"]];
const TIER = {A:"Attainable", M:"Mid-Range", U:"Upper Mid-Range", L:"Luxury", UL:"Ultra-Luxury"};
const hasBit = (af, bit) => !!((af || 0) & (1 << bit));
const lifestyleOn = () => [...document.querySelectorAll('.pill.life.active')].map(p => +p.dataset.bit);
const typesOn = () => [...document.querySelectorAll('.pill.type.active')].map(p => p.dataset.type.toLowerCase());

for (const c of DATA) c._s = [c.n, c.y, c.b || '', c.c].join(' ').toLowerCase();

const esc = s => String(s ?? '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const fmt = n => !n ? 'Contact for Price' : n >= 1e6 ? '$' + (n/1e6).toFixed(1).replace('.0','') + 'M' : '$' + Math.round(n/1e3) + 'K';
const grad = p => p >= 1e6 ? 'linear-gradient(135deg,#0A2540 0%,#1a4a6e 50%,#0D9488 100%)'
                : p >= 5e5 ? 'linear-gradient(135deg,#134e4a 0%,#0D9488 50%,#2dd4bf 100%)'
                : p >= 3e5 ? 'linear-gradient(135deg,#1e3a5f 0%,#3b82f6 50%,#60a5fa 100%)'
                :            'linear-gradient(135deg,#065f46 0%,#10b981 50%,#6ee7b7 100%)';

function card(c) {
  const curated = c.t === 1;
  const usd = n => '$' + Math.round(n).toLocaleString();
  const price = !c.p ? 'Contact for Price'
              : (c.x && c.x > c.p) ? `${usd(c.p)} <small>to</small> ${usd(c.x)}`
              : (curated ? `<small>from</small> ${usd(c.p)}` : usd(c.p));
  // every count shown was counted on that community's own page; anything unverified shows no count at all
  const noneNow = c.dl || c.nl;
  const sub = [noneNow ? 'No active listings' : (c.l > 0 ? `${c.l.toLocaleString()} listing${c.l === 1 ? '' : 's'}` : ''), c.pt && TIER[c.pt] ? TIER[c.pt] : ''].filter(Boolean).join(' · ');
  const homes = c.h || c.u;
  const view = c.dl ? (c.f || homes) : c.u;   // page currently 404 -> working fallback; the record's URL itself is never changed
  const desc = c.d ? c.d.slice(0, 120) + (c.d.length > 120 ? '…' : '') : (c.ty ? `${c.ty} subdivision in ${c.y}, ${c.c} County.` : '');
  let badges = '';
  if (c.i) badges += '<span class="badge badge-gold">Builder incentive</span>';
  const inc = c.i ? `<div class="inc">
      <div class="inc-head">Builder incentive${c.ix ? ` <span class="inc-thru">through ${new Date(c.ix + 'T12:00:00').toLocaleDateString('en-US', {month:'short', day:'numeric'})}</span>` : ''}</div>
      <p class="inc-text">${esc(c.i)}</p>
      <p class="inc-note">Set by ${esc(c.ib || 'the builder')} and subject to change without notice.</p>
    </div>` : '';
  badges += curated || c.nc ? '<span class="badge badge-teal">New Construction</span>' : '<span class="badge badge-ghost">Resale</span>';
  if (c.pt && TIER[c.pt]) badges += `<span class="badge badge-tier">${TIER[c.pt]}</span>`;
  const stats = (c.bd || c.ba || c.sf || c.hoa) ? `<div class="stats">
      <div><b>${c.bd ? (+c.bd).toFixed(c.bd % 1 ? 1 : 0) : '—'}</b><span>Beds avg</span></div>
      <div><b>${c.ba ? (+c.ba).toFixed(c.ba % 1 ? 1 : 0) : '—'}</b><span>Baths avg</span></div>
      <div><b>${c.sf ? Math.round(c.sf).toLocaleString() : '—'}</b><span>Sq ft avg</span></div>
      <div><b>${c.hoa ? '$' + Math.round(c.hoa).toLocaleString() : 'None'}</b><span>HOA / mo</span></div></div>` : '';
  const types = c.ty ? `<div class="types"><strong>Types:</strong> ${esc(c.ty)}${c.yr ? ` · Built ${c.yr}+` : ''}</div>` : '';
  const am = AMEN.filter(([b]) => hasBit(c.af, b)).map(([, l]) => l);
  const amen = am.length ? `<div class="amen">${am.slice(0, 6).map(l => `<span>${l}</span>`).join('')}${am.length > 6 ? `<span class="more">+${am.length - 6} more</span>` : ''}</div>` : '';
  if (noneNow) badges += '<span class="badge badge-muted" title="Checked on the community page — nothing listed today. It returns automatically when a home is listed.">No active listings right now</span>';
  else if (c.l > 0) badges += `<span class="badge badge-ghost" title="Counted on the community page at the last refresh">${c.l} Listed</span>`;
  // image priority (Joe): if the community has a listing, show that listing's photo; else its entrance sign; else a gradient
  const img = c.ph ? `<img src="${esc(c.ph)}?width=880&height=495&aspect_ratio=880:495" alt="Current listing in ${esc(c.n)}: ${esc(c.pa)}" loading="lazy">
                     <a href="${esc(c.pu)}" target="_blank" rel="noopener" class="card-idx-caption" title="Photo is from an active MLS listing and updates automatically">Current listing · ${esc(c.pa)}${c.pp ? ' · ' + fmt(c.pp) : ''}</a>`
            : c.g  ? `<img src="${esc(c.g)}" alt="${esc(c.n)} community entrance sign" loading="lazy">`
            :        `<div class="card-image-placeholder" style="background:${grad(c.p)}"></div>`;
  return `<article class="card" data-href="${esc(view || homes)}" role="link" tabindex="0" aria-label="Open ${esc(c.n)}">
    <div class="card-image">${img}
      ${c.v ? `<button type="button" class="card-video-badge" data-video="${esc(c.v.split('/').pop())}" data-title="${esc(c.n)} — ${esc(c.vs || 'official builder video')}" title="Plays here, no redirect">Watch Tour</button>` : ''}
      <div class="card-image-overlay"><h3 class="card-name">${esc(c.n.replace(/^0\d{3}\s+/, ''))}</h3><div class="card-location">${esc(c.y)}${c.ys && c.ys.length ? ` +${c.ys.length} more area${c.ys.length > 1 ? 's' : ''}` : ''}${c.y ? ', ' : ''}${esc(c.c)} County</div></div>
    </div>
    <div class="card-body">
      <div class="card-price">${price}</div>
      ${sub ? `<div class="card-sub">${esc(sub)}</div>` : ''}
      ${c.b ? `<div class="card-builder">By ${esc(c.b)}</div>` : ''}
      ${badges ? `<div class="card-badges" style="margin-bottom:10px">${badges}</div>` : ''}
      ${inc}${stats}${types}${amen}
      ${desc ? `<p class="card-desc">${esc(desc)}</p>` : ''}
    </div>
    <div class="card-footer">
      <a href="${esc(view || homes)}" class="btn btn-primary" target="_blank" rel="noopener">View Community</a>
    </div>
  </article>`;
}

function renderMore(reset) {
  if (reset) { els.grid.innerHTML = ''; shown = 0; }
  const next = filtered.slice(shown, shown + PAGE);
  els.grid.insertAdjacentHTML('beforeend', next.map(card).join(''));
  shown += next.length;
  els.more.hidden = shown >= filtered.length;
  els.more.textContent = `Show more (${(filtered.length - shown).toLocaleString()} remaining)`;
  els.empty.hidden = filtered.length > 0;
  els.count.textContent = filtered.length.toLocaleString();
}

function filter() {
  const q = els.q.value.toLowerCase().trim();
  const county = els.county.value, pr = els.price.value, sortBy = els.sort.value;
  const city = els.city.value.trim().toLowerCase();
  const needInc = els.inc.classList.contains('active'), needLst = els.lst.classList.contains('active');
  const needVid = els.vid.classList.contains('active'), needCur = els.cur.classList.contains('active');
  const bits = lifestyleOn(), types = typesOn();
  let [min, max] = pr ? pr.split('-').map(Number) : [0, 0];
  filtered = DATA.filter(c => {
    if (needCur && c.t !== 1) return false;
    if (bits.length && !bits.every(b => hasBit(c.af, b))) return false;
    if (types.length && !types.some(t => (c.ty || '').toLowerCase().includes(t))) return false;
    if (county && c.c !== county) return false;
    if (city && !(c.y || '').toLowerCase().startsWith(city)) return false;
    if (q && !c._s.includes(q)) return false;
    if (pr) { const p = c.p || 0; if (!p) return false; if (min && p < min) return false; if (max && p > max) return false; }
    if (needInc && !c.i) return false;
    if (needLst && !(c.l > 0)) return false;
    if (needVid && !c.v) return false;
    return true;
  });
  const byName = (a, b) => a.n.localeCompare(b.n);
  filtered.sort((a, b) => {
    switch (sortBy) {
      case 'price-low':  return (a.p || 9e12) - (b.p || 9e12) || byName(a, b);
      case 'price-high': return (b.p || 0) - (a.p || 0) || byName(a, b);
      case 'listings':   return (b.l || 0) - (a.l || 0) || byName(a, b);
      case 'name':       return byName(a, b);
      default:           return (b.t - a.t) || (b.l || 0) - (a.l || 0) || byName(a, b);
    }
  });
  renderMore(true);
}

FINDER_META.county_list.forEach(c => { const o = document.createElement('option'); o.value = c; o.textContent = c + ' County'; els.county.appendChild(o); });

// city suggestions follow the county selection, so the list stays short and relevant
function fillCities() {
  const county = els.county.value;
  const seen = new Set();
  for (const c of DATA) if (c.y && (!county || c.c === county)) seen.add(c.y);
  $('citylist').innerHTML = [...seen].sort().map(c => `<option value="${c.replace(/"/g, '&quot;')}">`).join('');
}
fillCities();

let t; els.q.addEventListener('input', () => { clearTimeout(t); t = setTimeout(filter, 150); });
els.city.addEventListener('input', () => { clearTimeout(t); t = setTimeout(filter, 150); });
document.querySelector('.search-btn').addEventListener('click', filter);
els.county.addEventListener('change', () => { fillCities(); filter(); });
['price', 'sort'].forEach(k => els[k].addEventListener('change', filter));
$('clearall').addEventListener('click', () => {
  els.q.value = ''; els.city.value = ''; els.county.value = ''; els.price.value = ''; els.sort.value = 'featured';
  ['incentive', 'listings', 'video', 'curated'].forEach(id => $(id).classList.remove('active'));
  document.querySelectorAll('.pill').forEach(p => p.classList.remove('active'));
  fillCities(); filter(); window.scrollTo({top: 0, behavior: 'smooth'});
});
$('sbtoggle').addEventListener('click', e => {
  const sb = e.currentTarget.closest('.sidebar'); const open = sb.classList.toggle('open');
  e.currentTarget.setAttribute('aria-expanded', open);
});
['inc', 'lst', 'vid', 'cur'].forEach(k => els[k].addEventListener('click', () => { els[k].classList.toggle('active'); filter(); }));
document.querySelectorAll('.pill').forEach(p => p.addEventListener('click', () => { p.classList.toggle('active'); filter(); }));
els.more.addEventListener('click', () => renderMore(false));

// in-page video player (YouTube privacy-enhanced embed; nothing loads until a tour is opened)
const vm = $('vmodal'), vf = $('vframe'), vt = $('vtitle');
function openVideo(id, title) {
  vt.textContent = title || '';
  vf.src = `https://www.youtube-nocookie.com/embed/${encodeURIComponent(id)}?autoplay=1&rel=0&modestbranding=1`;
  vm.hidden = false; document.body.style.overflow = 'hidden'; $('vclose').focus();
}
function closeVideo() { vf.src = ''; vm.hidden = true; document.body.style.overflow = ''; }
document.addEventListener('click', e => {
  const b = e.target.closest('.card-video-badge'); if (b) { e.preventDefault(); openVideo(b.dataset.video, b.dataset.title); return; }
  if (e.target.closest('#vclose') || e.target === vm) { closeVideo(); return; }
  // whole card is clickable (image, title, stats, anywhere) — inner links/buttons keep their own targets
  if (e.target.closest('a, button')) return;
  const card = e.target.closest('.card[data-href]');
  if (card) window.open(card.dataset.href, '_blank', 'noopener');
});
document.addEventListener('keydown', e => {
  if (e.key === 'Enter' && document.activeElement?.matches('.card[data-href]')) window.open(document.activeElement.dataset.href, '_blank', 'noopener');
});
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !vm.hidden) closeVideo(); });
new IntersectionObserver(e => { if (e[0].isIntersecting && !els.more.hidden) renderMore(false); }, {rootMargin: '600px'}).observe(els.more);
filter();
"""

def video_objects(records):
    out = []
    for r in records:
        if not r.get("v"): continue
        vid = r["v"].rsplit("/", 1)[-1]
        out.append({
            "@context": "https://schema.org", "@type": "VideoObject",
            "name": f"{r['n']} community tour — {r.get('vs','official builder video')}",
            "description": f"Official {r.get('vs','builder')} video for {r['n']}, a new construction community in {r.get('y','')}, {r['c']} County, Florida." + (f" Homes from ${r['p']:,}." if r.get('p') else ""),
            "thumbnailUrl": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", "uploadDate": "2026-09-05",
            "contentUrl": f"https://www.youtube.com/watch?v={vid}", "embedUrl": f"https://www.youtube.com/embed/{vid}",
            "sourceOrganization": {"@type": "Organization", "name": r.get("vs", "")},
            "publisher": {"@type": "RealEstateAgent", "name": "Paradise Realty FLA", "telephone": "+1-772-247-7110", "url": "https://www.paradiserealtyfla.com"},
            "about": {"@type": "Place", "name": r["n"], "address": {"@type": "PostalAddress", "addressLocality": r.get("y", ""), "addressRegion": "FL"}}})
    return out

def main():
    s = open(SRC).read()
    js = open(DATA_JS).read()
    meta = json.loads(re.search(r"const FINDER_META=(\{.*?\});\n", js).group(1))
    records = json.loads(re.search(r"const DATA=(\[.*\]);\n", js, re.S).group(1))
    total, counties, curated = meta["total"], meta["counties"], meta["curated"]

    # head: noindex (test), title/description, ItemList numbers, regenerate VideoObject blocks
    s = s.replace('<title>Florida New Construction Communities</title>', '<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>Florida Community Finder</title>', 1)
    s = re.sub(r'<meta name="description" content="[^"]*">',
               f'<link rel="canonical" href="https://paradise-finder-3vuuwnsvua-ue.a.run.app/">\n<meta name="description" content="Search {total:,} Florida communities across {counties} counties — every MLS subdivision plus {curated} curated new construction communities with builders, prices, incentives, official video tours and entrance photos. Paradise Realty FLA.">', s, 1)
    s = s.replace('"name": "Florida New Construction Communities",\n  "description": "Comprehensive directory of 988 new construction communities across 37 Florida counties, with pricing, amenities, builder information, and active listings data.",',
                  f'"name": "Florida Community Finder",\n  "description": "Directory of {total:,} Florida residential communities across {counties} counties: every MLS subdivision plus {curated} curated new construction communities with builder, pricing, incentive, video and listing data.",', 1)
    s = s.replace('"numberOfItems": 988,', f'"numberOfItems": {total},', 1)
    s = re.sub(r'<script type="application/ld\+json">\s*\{\s*"@context": "https://schema.org",\s*"@type": "VideoObject".*?</script>\s*', "", s, flags=re.S)
    vo = "".join(f'<script type="application/ld+json">{json.dumps(o, separators=(",", ":"), ensure_ascii=False)}</script>\n' for o in video_objects(records))
    s = s.replace("<style>\n:root {", vo + "<style>\n:root {", 1)

    # Header link to the public incentives page — only while offers actually exist. When the last one
    # expires the link disappears on the next rebuild, so there is never a nav item leading to an empty page.
    live_inc = sum(1 for r in records if r.get("i"))
    if live_inc:
        s = s.replace('<a href="tel:7722477110" class="header-phone">',
                      f'<a href="/incentives" class="header-inc">Builder incentives <b>{live_inc}</b></a>\n      '
                      '<a href="tel:7722477110" class="header-phone">', 1)
        s = s.replace("</style>",
                      ".header-inc{display:inline-flex;align-items:center;gap:7px;color:#fff;text-decoration:none;font-weight:600;font-size:13px;"
                      "border:1px solid rgba(201,168,76,.55);border-radius:999px;padding:6px 13px;white-space:nowrap}\n"
                      ".header-inc b{background:var(--gold);color:var(--navy);font-size:11px;font-weight:800;border-radius:999px;padding:1px 7px}\n"
                      ".header-inc:hover{border-color:var(--gold);background:rgba(201,168,76,.14)}\n"
                      "@media (max-width:700px){.header-inc{display:none}}\n</style>", 1)

    # body copy + controls
    s = s.replace('<h1>Florida New Construction Communities</h1>', '<h1>Florida Community Finder</h1>', 1)
    s = s.replace('<p>Search 988 communities across 37 counties. Compare builders, prices, and amenities.</p>',
                  f'<p>Search {total:,} communities across {counties} Florida counties — every MLS subdivision plus {curated} curated new construction communities with builders, incentives, video tours and entrance signs.</p>', 1)
    s = s.replace('<button id="listings" class="filter-toggle">Active Listings</button>',
                  '<button id="listings" class="filter-toggle">Active Listings</button>\n    <button id="video" class="filter-toggle">Has Video Tour</button>\n    <button id="curated" class="filter-toggle">New Construction Only</button>', 1)
    s = s.replace('<option value="name">Sort by Name</option>', '<option value="featured">Featured</option>\n    <option value="name">Sort by Name</option>', 1)
    life = [(3, "Waterfront"), (4, "Boating"), (2, "Golf"), (10, "Active 55+"), (17, "Resort-Style"), (1, "Gated"), (8, "Pickleball"), (0, "Pool"), (6, "Fitness Center"), (9, "Dining")]
    types = ["Single Family", "Condo", "Townhouse", "Villa", "Multi-Family", "Mobile", "Land"]
    pills = ('  <div class="pill-row"><span class="pill-label">Lifestyle</span>' + "".join(f'<button type="button" class="pill life" data-bit="{b}">{l}</button>' for b, l in life) + '</div>\n'
             '  <div class="pill-row"><span class="pill-label">Home type</span>' + "".join(f'<button type="button" class="pill type" data-type="{t}">{t}</button>' for t in types) + '</div>\n')
    s = s.replace('<button id="curated" class="filter-toggle">New Construction Only</button>\n  </div>',
                  '<button id="curated" class="filter-toggle">New Construction Only</button>\n  </div>\n' + pills, 1)
    s = s.replace("</style>",
                  ".pill-row{max-width:1280px;margin:0 auto;padding:6px 24px 0;display:flex;flex-wrap:wrap;gap:8px;align-items:center}\n"
                  ".pill-label{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--text-light);margin-right:4px;min-width:74px}\n"
                  ".pill{border:1px solid var(--border);background:var(--white);color:var(--text);border-radius:999px;padding:6px 12px;font:600 12px Inter,inherit;cursor:pointer}\n"
                  ".pill.active{background:var(--teal);border-color:var(--teal);color:#fff}\n.filters{padding-bottom:12px}\n"
                  ".badge-tier{background:var(--teal-light);color:var(--teal-dark)}\n"
                  ".inc{border:1px solid var(--gold);background:var(--gold-light);border-radius:8px;padding:10px 12px;margin:0 0 10px}\n"
                  ".inc-head{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#8a6d1f;margin-bottom:5px}\n"
                  ".inc-thru{font-weight:600;letter-spacing:.02em;text-transform:none;color:var(--text-mid)}\n"
                  ".inc-text{font-size:13px;line-height:1.5;color:var(--text);margin:0 0 6px}\n"
                  ".inc-note{font-size:10.5px;line-height:1.45;color:var(--text-mid);margin:0}\n"
                  ".card-price{font-size:21px}\n.card-price small{font-size:13px;font-weight:500;color:var(--text-light);margin:0 4px}\n"
                  ".card-sub{font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--text-light);margin:-4px 0 10px}\n"
                  ".stats{display:grid;grid-template-columns:repeat(4,1fr);border:1px solid var(--border);border-radius:8px;overflow:hidden;margin:4px 0 10px}\n"
                  ".stats div{padding:8px 4px;text-align:center;border-left:1px solid var(--border)}\n.stats div:first-child{border-left:0}\n"
                  ".stats b{display:block;font-size:15px;color:var(--text)}\n.stats span{display:block;font-size:10px;letter-spacing:.05em;text-transform:uppercase;color:var(--text-light)}\n"
                  ".types{font-size:12px;color:var(--text-mid);margin-bottom:8px}\n"
                  ".amen{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}\n.amen span{font-size:11px;padding:3px 8px;border:1px solid var(--border);border-radius:4px;color:var(--text-mid);background:var(--cream)}\n.amen .more{color:var(--teal-dark)}\n</style>", 1)
    s = s.replace('<div id="grid" class="grid"></div>',
                  '<div id="grid" class="grid"></div>\n  <button id="more" class="btn btn-outline load-more" hidden>Show more</button>', 1)

    # ---- two-column layout: sticky "Search criteria" sidebar on the left, scrolling results on the right ----
    price_opts = re.search(r'<select id="price"[^>]*>(.*?)</select>', s, re.S).group(1).strip()
    filters_block = re.search(r'<div class="filters">.*?(?=<div class="stats-bar">)', s, re.S).group(0)
    stats_block = re.search(r'<div class="stats-bar">.*?(?=<main class="main">)', s, re.S).group(0)
    main_block = re.search(r'<main class="main">.*?</main>', s, re.S).group(0)
    life_pills = "".join(f'<button type="button" class="pill life" data-bit="{b}">{l}</button>' for b, l in life)
    type_pills = "".join(f'<button type="button" class="pill type" data-type="{t}">{t}</button>' for t in types)
    sidebar = f'''<div class="layout">
  <aside class="sidebar" aria-label="Search criteria">
    <button type="button" class="sb-toggle" id="sbtoggle" aria-expanded="false" aria-controls="sbbody">Search criteria</button>
    <div class="sb-body" id="sbbody">
      <div class="sb-group"><label class="sb-label" for="county">County</label>
        <select id="county" class="filter-select"><option value="">All counties</option></select></div>
      <div class="sb-group"><label class="sb-label" for="city">City</label>
        <input id="city" class="filter-select" type="search" list="citylist" placeholder="Any city" autocomplete="off">
        <datalist id="citylist"></datalist></div>
      <div class="sb-group"><label class="sb-label" for="price">Price range</label>
        <select id="price" class="filter-select">{price_opts}</select></div>
      <div class="sb-group"><span class="sb-label">Show only</span>
        <button id="curated" class="filter-toggle">New construction</button>
        <button id="listings" class="filter-toggle">Active listings</button>
        <button id="video" class="filter-toggle">Has video tour</button>
        <button id="incentive" class="filter-toggle gold">Has incentive</button></div>
      <div class="sb-group"><span class="sb-label">Lifestyle</span><div class="pillwrap">{life_pills}</div></div>
      <div class="sb-group"><span class="sb-label">Home type</span><div class="pillwrap">{type_pills}</div></div>
      <button type="button" class="sb-clear" id="clearall">Clear all filters</button>
    </div>
  </aside>
  <div class="results">
    {stats_block}
    {main_block}
  </div>
</div>
'''
    s = s.replace(filters_block + stats_block + main_block, sidebar, 1)
    s = s.replace("</style>",
                  ".layout{max-width:1440px;margin:0 auto;padding:20px 24px 0;display:flex;gap:24px;align-items:flex-start}\n"
                  ".sidebar{position:sticky;top:78px;width:262px;flex:0 0 262px;max-height:calc(100vh - 96px);overflow-y:auto;overscroll-behavior:contain;"
                  "background:var(--white);border:1px solid var(--border);border-radius:10px;box-shadow:var(--shadow);padding:16px}\n"
                  ".sidebar::-webkit-scrollbar{width:8px}.sidebar::-webkit-scrollbar-thumb{background:var(--border);border-radius:8px}\n"
                  ".results{flex:1;min-width:0}\n.results .main{padding:0}\n.results .stats-bar{padding:0 0 14px;border:0;background:transparent}\n"
                  ".sb-toggle{display:none;width:100%;border:1px solid var(--border);background:var(--white);color:var(--text);border-radius:8px;padding:11px 14px;font:700 13px Inter,inherit;cursor:pointer}\n"
                  ".sb-group{padding:0 0 14px;margin-bottom:14px;border-bottom:1px solid var(--border)}\n.sb-group:last-of-type{border-bottom:0;margin-bottom:8px}\n"
                  ".sb-label{display:block;font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--text-light);margin-bottom:8px}\n"
                  ".sidebar .filter-select{width:100%;margin-bottom:0}\n"
                  ".sidebar .filter-toggle{display:block;width:100%;text-align:left;margin-bottom:6px}\n"
                  ".pillwrap{display:flex;flex-wrap:wrap;gap:6px}\n"
                  ".sb-clear{width:100%;border:1px solid var(--border);background:var(--cream);color:var(--text-mid);border-radius:8px;padding:9px;font:600 12px Inter,inherit;cursor:pointer}\n"
                  ".sb-clear:hover{border-color:var(--teal);color:var(--teal-dark)}\n"
                  "@media (max-width:900px){.layout{display:block;padding:14px 16px 0}.sidebar{position:static;width:auto;max-height:none;flex:none;margin-bottom:16px;padding:12px}\n"
                  ".sb-toggle{display:block}.sb-body{display:none;padding-top:14px}.sidebar.open .sb-body{display:block}}\n</style>", 1)
    s = s.replace('<div class="footer-bottom">',
                  '<div class="footer-note"><strong>About builder incentives:</strong> incentives shown are provided by the builder, '
                  'are current as of the date displayed, and may change or end without notice. Terms, eligibility, and availability vary by '
                  'community and by home, and may require the use of a preferred lender or title company. Nothing here is an offer, a guarantee '
                  'of savings, or a commitment to lend. Confirm current details with Paradise Realty FLA before relying on them. '
                  'Register with us before visiting a builder sales office to preserve your right to representation.</div>\n'
                  '  <div class="footer-bottom">', 1)
    s = s.replace("</style>", ".footer-note{max-width:1280px;margin:28px auto 0;padding:16px 24px 0;border-top:1px solid rgba(255,255,255,.14);"
                              "font-size:12px;line-height:1.6;color:rgba(255,255,255,.72)}\n.footer-note strong{color:rgba(255,255,255,.9)}\n</style>", 1)
    s = s.replace('<footer class="footer">',
                  '<div id="vmodal" class="vmodal" hidden role="dialog" aria-modal="true" aria-labelledby="vtitle">\n'
                  '  <div class="vbox">\n    <div class="vbar"><span id="vtitle"></span><button id="vclose" type="button" aria-label="Close video">&times;</button></div>\n'
                  '    <div class="vwrap"><iframe id="vframe" title="Community video tour" allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe></div>\n'
                  '  </div>\n</div>\n<footer class="footer">', 1)
    s = s.replace("</style>",
                  ".card-video-badge{border:0;cursor:pointer;font-family:inherit}\n"
                  ".card[data-href]{cursor:pointer}\n.card[data-href]:hover .card-name{text-decoration:underline;text-underline-offset:3px}\n"
                  ".card[data-href]:focus-visible{outline:3px solid var(--teal);outline-offset:2px}\n"
                  ".card-image::after{content:'';position:absolute;inset:0;background:rgba(13,148,136,0);transition:background .2s}\n.card:hover .card-image::after{background:rgba(13,148,136,.08)}\n"
                  ".badge-muted{background:var(--navy-light);color:var(--text-mid)}\n"
                  ".card-body .badge-ghost{background:var(--navy-light);color:var(--navy)}\n"
                  ".vmodal{position:fixed;inset:0;z-index:1000;background:rgba(10,37,64,.82);display:flex;align-items:center;justify-content:center;padding:16px}\n"
                  ".vmodal[hidden],[hidden]{display:none!important}\n"
                  ".vbox{width:min(960px,100%);background:#000;border-radius:10px;overflow:hidden;box-shadow:0 24px 80px rgba(0,0,0,.5)}\n"
                  ".vbar{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 14px;background:var(--navy);color:#fff;font-size:14px;font-weight:600}\n"
                  ".vbar button{background:transparent;border:0;color:#fff;font-size:26px;line-height:1;cursor:pointer;padding:0 4px}\n"
                  ".vwrap{position:relative;padding-top:56.25%}\n.vwrap iframe{position:absolute;inset:0;width:100%;height:100%;border:0}\n</style>", 1)
    s = s.replace("</style>", ".load-more{display:block;margin:28px auto 0;max-width:360px;flex:none}\n"
                  ".card-idx-caption{position:absolute;top:12px;left:12px;max-width:68%;background:rgba(10,37,64,.85);color:#fff;font-size:11px;font-weight:600;padding:4px 10px;border-radius:4px;text-decoration:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;z-index:1}\n"
                  ".card-idx-caption:hover{background:var(--teal)}\n</style>", 1)
    s = s.replace('tracks over 988 active new construction communities across 37 Florida counties',
                  f'tracks {total:,} Florida communities across {counties} counties, including {curated} curated new construction communities', 1)

    # script: external data + finder logic
    import hashlib
    ver = hashlib.md5(open(DATA_JS, "rb").read()).hexdigest()[:10]  # cache-bust: new data -> new URL
    inject = f'<script src="/communities_all.js?v={ver}"></script>\n<script>' + FINDER_JS + '</script>'
    s = re.sub(r"<script>\nconst DATA = \{.*?\n</script>", lambda m: inject, s, flags=re.S)  # function form: JS backslashes are not re-interpreted
    open(OUT, "w").write(s)
    print(f"index.html: {os.path.getsize(OUT)//1024} KB | {total:,} communities | {len(video_objects(records))} VideoObject blocks")

if __name__ == "__main__":
    main()
