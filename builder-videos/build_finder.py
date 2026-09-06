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
const els = {q: $('search'), county: $('county'), price: $('price'), inc: $('incentive'), lst: $('listings'),
             vid: $('video'), cur: $('curated'), sort: $('sort'), count: $('count'), grid: $('grid'), empty: $('empty'), more: $('more')};
const PAGE = 48;
let filtered = [], shown = 0;

for (const c of DATA) c._s = [c.n, c.y, c.b || '', c.c].join(' ').toLowerCase();

const esc = s => String(s ?? '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const fmt = n => !n ? 'Contact for Price' : n >= 1e6 ? '$' + (n/1e6).toFixed(1).replace('.0','') + 'M' : '$' + Math.round(n/1e3) + 'K';
const grad = p => p >= 1e6 ? 'linear-gradient(135deg,#0A2540 0%,#1a4a6e 50%,#0D9488 100%)'
                : p >= 5e5 ? 'linear-gradient(135deg,#134e4a 0%,#0D9488 50%,#2dd4bf 100%)'
                : p >= 3e5 ? 'linear-gradient(135deg,#1e3a5f 0%,#3b82f6 50%,#60a5fa 100%)'
                :            'linear-gradient(135deg,#065f46 0%,#10b981 50%,#6ee7b7 100%)';

function card(c) {
  const curated = c.t === 1;
  const price = (!curated && c.x && c.x > c.p) ? `${fmt(c.p)} – ${fmt(c.x)}` : fmt(c.p);
  const homes = c.h || c.u;
  const view = c.dl ? (c.f || homes) : c.u;   // page currently 404 -> working fallback; the record's URL itself is never changed
  const desc = c.d ? c.d.slice(0, 120) + (c.d.length > 120 ? '…' : '') : (c.ty ? `${c.ty} subdivision in ${c.y}, ${c.c} County.` : '');
  let badges = '';
  if (c.i) badges += `<span class="badge badge-gold" title="${esc(c.i)}">Incentive</span>`;
  if (curated) badges += '<span class="badge badge-teal">New Construction</span>';
  if (c.l > 0) badges += `<span class="badge badge-ghost">${c.l} Listed</span>`;
  const img = c.g  ? `<img src="${esc(c.g)}" alt="${esc(c.n)} community entrance sign" loading="lazy">`
            : c.ph ? `<img src="${esc(c.ph)}?width=880&height=495&aspect_ratio=880:495" alt="Current listing in ${esc(c.n)}: ${esc(c.pa)}" loading="lazy">
                     <a href="${esc(c.pu)}" target="_blank" rel="noopener" class="card-idx-caption" title="Photo is from an active MLS listing and updates automatically">Current listing · ${esc(c.pa)}${c.pp ? ' · ' + fmt(c.pp) : ''}</a>`
            :        `<div class="card-image-placeholder" style="background:${grad(c.p)}"></div>`;
  return `<article class="card">
    <div class="card-image">${img}
      ${c.v ? `<button type="button" class="card-video-badge" data-video="${esc(c.v.split('/').pop())}" data-title="${esc(c.n)} — ${esc(c.vs || 'official builder video')}" title="Plays here, no redirect">Watch Tour</button>` : ''}
      <div class="card-image-overlay"><h3 class="card-name">${esc(c.n)}</h3><div class="card-location">${esc(c.y)}${c.y ? ', ' : ''}${esc(c.c)} County</div></div>
    </div>
    <div class="card-body">
      <div class="card-price">${price}</div>
      ${c.b ? `<div class="card-builder">By ${esc(c.b)}</div>` : ''}
      ${badges ? `<div class="card-badges" style="margin-bottom:10px">${badges}</div>` : ''}
      ${desc ? `<p class="card-desc">${esc(desc)}</p>` : ''}
    </div>
    <div class="card-footer">
      <a href="${esc(view || homes)}" class="btn btn-primary" target="_blank" rel="noopener">View Community</a>
      ${c.l > 0 ? `<a href="${esc(homes)}" class="btn btn-outline" target="_blank" rel="noopener">See Homes</a>` : ''}
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
  const needInc = els.inc.classList.contains('active'), needLst = els.lst.classList.contains('active');
  const needVid = els.vid.classList.contains('active'), needCur = els.cur.classList.contains('active');
  let [min, max] = pr ? pr.split('-').map(Number) : [0, 0];
  filtered = DATA.filter(c => {
    if (needCur && c.t !== 1) return false;
    if (county && c.c !== county) return false;
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
let t; els.q.addEventListener('input', () => { clearTimeout(t); t = setTimeout(filter, 150); });
document.querySelector('.search-btn').addEventListener('click', filter);
['county', 'price', 'sort'].forEach(k => els[k].addEventListener('change', filter));
['inc', 'lst', 'vid', 'cur'].forEach(k => els[k].addEventListener('click', () => { els[k].classList.toggle('active'); filter(); }));
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
  if (e.target.closest('#vclose') || e.target === vm) closeVideo();
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
               f'<meta name="robots" content="noindex, nofollow">\n<meta name="description" content="Search {total:,} Florida communities across {counties} counties — every MLS subdivision plus {curated} curated new construction communities with builders, prices, incentives, official video tours and entrance photos. Paradise Realty FLA.">', s, 1)
    s = s.replace('"name": "Florida New Construction Communities",\n  "description": "Comprehensive directory of 988 new construction communities across 37 Florida counties, with pricing, amenities, builder information, and active listings data.",',
                  f'"name": "Florida Community Finder",\n  "description": "Directory of {total:,} Florida residential communities across {counties} counties: every MLS subdivision plus {curated} curated new construction communities with builder, pricing, incentive, video and listing data.",', 1)
    s = s.replace('"numberOfItems": 988,', f'"numberOfItems": {total},', 1)
    s = re.sub(r'<script type="application/ld\+json">\s*\{\s*"@context": "https://schema.org",\s*"@type": "VideoObject".*?</script>\s*', "", s, flags=re.S)
    vo = "".join(f'<script type="application/ld+json">{json.dumps(o, separators=(",", ":"), ensure_ascii=False)}</script>\n' for o in video_objects(records))
    s = s.replace("<style>\n:root {", vo + "<style>\n:root {", 1)

    # body copy + controls
    s = s.replace('<h1>Florida New Construction Communities</h1>', '<h1>Florida Community Finder</h1>', 1)
    s = s.replace('<p>Search 988 communities across 37 counties. Compare builders, prices, and amenities.</p>',
                  f'<p>Search {total:,} communities across {counties} Florida counties — every MLS subdivision plus {curated} curated new construction communities with builders, incentives, video tours and entrance signs.</p>', 1)
    s = s.replace('<button id="listings" class="filter-toggle">Active Listings</button>',
                  '<button id="listings" class="filter-toggle">Active Listings</button>\n    <button id="video" class="filter-toggle">Has Video Tour</button>\n    <button id="curated" class="filter-toggle">New Construction Only</button>', 1)
    s = s.replace('<option value="name">Sort by Name</option>', '<option value="featured">Featured</option>\n    <option value="name">Sort by Name</option>', 1)
    s = s.replace('<div id="grid" class="grid"></div>',
                  '<div id="grid" class="grid"></div>\n  <button id="more" class="btn btn-outline load-more" hidden>Show more</button>', 1)
    s = s.replace('<footer class="footer">',
                  '<div id="vmodal" class="vmodal" hidden role="dialog" aria-modal="true" aria-labelledby="vtitle">\n'
                  '  <div class="vbox">\n    <div class="vbar"><span id="vtitle"></span><button id="vclose" type="button" aria-label="Close video">&times;</button></div>\n'
                  '    <div class="vwrap"><iframe id="vframe" title="Community video tour" allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe></div>\n'
                  '  </div>\n</div>\n<footer class="footer">', 1)
    s = s.replace("</style>",
                  ".card-video-badge{border:0;cursor:pointer;font-family:inherit}\n"
                  ".vmodal{position:fixed;inset:0;z-index:1000;background:rgba(10,37,64,.82);display:flex;align-items:center;justify-content:center;padding:16px}\n"
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
    s = re.sub(r"<script>\nconst DATA = \{.*?\n</script>", f'<script src="/communities_all.js?v={ver}"></script>\n<script>' + FINDER_JS + '</script>', s, flags=re.S)
    open(OUT, "w").write(s)
    print(f"index.html: {os.path.getsize(OUT)//1024} KB | {total:,} communities | {len(video_objects(records))} VideoObject blocks")

if __name__ == "__main__":
    main()
