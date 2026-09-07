#!/usr/bin/env python3
"""
Paradise Finder - Test Version
Simple Flask server to serve the finder HTML
"""

import os, time, threading, urllib.request
from flask import Flask, send_file, Response, request

app = Flask(__name__)

# The nightly refresh job publishes to GCS; we serve from there with a short cache so no redeploy is needed.
GCS = "https://storage.googleapis.com/paradise-realty-images/finder/"
TTL = 300
_cache, _lock = {}, threading.Lock()

def gcs_bytes(name, local_fallback):
    now = time.time()
    with _lock:
        hit = _cache.get(name)
        if hit and now - hit[0] < TTL:
            return hit[1]
    try:
        # ask for gzip so GCS hands back the stored bytes untouched (urllib does not auto-decompress)
        req = urllib.request.Request(GCS + name + f"?t={int(now)}", headers={"Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
        if data:
            with _lock: _cache[name] = (now, data)
            return data
    except Exception:
        pass
    if hit: return hit[1]
    return open(local_fallback, "rb").read()

@app.route("/")
def index():
    html = gcs_bytes("index.html", "index.html")
    # the published page is indexable; only the test host gets noindex
    if request.host.startswith("paradise-finder-test"):
        html = html.replace(b'<meta name="description"', b'<meta name="robots" content="noindex, nofollow">\n<meta name="description"', 1)
    return Response(html, mimetype="text/html", headers={"Cache-Control": "public, max-age=300"})

@app.route("/incentives")
def incentives():
    """Public builder-incentive page. Rebuilt by the refresh job; when no offer is running it serves its
    own empty state rather than 404ing, so a shared or indexed link never dead-ends."""
    html = gcs_bytes("incentives.html", "incentives.html")
    if request.host.startswith("paradise-finder-test"):
        html = html.replace(b'<meta name="description"', b'<meta name="robots" content="noindex, nofollow">\n<meta name="description"', 1)
    return Response(html, mimetype="text/html", headers={"Cache-Control": "public, max-age=300"})


@app.route("/communities_all.js")
def data():
    gz = gcs_bytes("communities_all.js.gz", "communities_all.js.gz")  # stored gzipped; GCS returns it raw with identity encoding
    return Response(gz, mimetype="application/javascript",
                    headers={"Content-Encoding": "gzip", "Cache-Control": "public, max-age=3600, immutable", "Vary": "Accept-Encoding"})

@app.route("/stats.json")
def stats():
    return Response(gcs_bytes("stats.json", "communities_all.stats.json"), mimetype="application/json", headers={"Cache-Control": "no-cache"})

SITE = "https://search.paradiserealtyfla.com"

@app.route("/robots.txt")
def robots():
    """The test host stays out of the index entirely; the public host advertises its sitemap.
    Community area pages live on www and are covered by the sitemaps Joe submits for that host."""
    if request.host.startswith("paradise-finder-test"):
        body = "User-agent: *\nDisallow: /\n"
    else:
        body = f"User-agent: *\nAllow: /\n\nSitemap: {SITE}/sitemap.xml\n"
    return Response(body, mimetype="text/plain", headers={"Cache-Control": "public, max-age=3600"})


@app.route("/sitemap.xml")
def sitemap():
    """Only the two pages this host actually serves. The 41k community URLs are www URLs and belong
    in the www sitemaps — listing them here would be a cross-host sitemap for pages we do not own."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    urls = [(f"{SITE}/", "daily", "1.0"), (f"{SITE}/incentives", "daily", "0.9")]
    body = "".join(f"  <url><loc>{u}</loc><lastmod>{today}</lastmod>"
                   f"<changefreq>{c}</changefreq><priority>{p}</priority></url>\n" for u, c, p in urls)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + body + "</urlset>\n")
    return Response(xml, mimetype="application/xml", headers={"Cache-Control": "public, max-age=3600"})


@app.route("/llms.txt")
def llms():
    """Served from GCS so the twice-daily job can keep its counts current without a redeploy."""
    return Response(gcs_bytes("llms.txt", "llms.txt"), mimetype="text/plain; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.route("/health")
def health():
    return {"status": "ok", "service": "paradise-finder-test"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
