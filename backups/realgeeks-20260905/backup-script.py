#!/usr/bin/env python3
"""
Full backup of Paradise Realty FLA RealGeeks content.
Backs up: area pages, content pages, footer code, homepage.
"""

import os
import json
import requests
from datetime import datetime

BACKUP_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_URL = "https://www.paradiserealtyfla.com"

# Known area pages to backup (SE Florida counties + community pages)
AREA_PAGES = [
    "/martin-county/",
    "/palm-beach-county/",
    "/st-lucie-county/",
    "/indian-river-county/",
    "/broward-county/",
    "/miami-dade-county/",
    "/collier-county/",
    "/lee-county/",
    "/charlotte-county/",
    "/sarasota-county/",
    "/manatee-county/",
    "/hillsborough-county/",
    "/pinellas-county/",
    "/pasco-county/",
    "/hernando-county/",
    "/lake-county/",
    "/orange-county/",
    "/osceola-county/",
    "/seminole-county/",
    "/volusia-county/",
    "/flagler-county/",
    "/brevard-county/",
    "/communities/",
    "/new-construction/",
    "/about/",
    "/contact/",
    "/blog/",
]

def backup_page(path, subdir="pages"):
    """Download and save a page."""
    url = BASE_URL + path
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        
        # Create safe filename
        filename = path.strip("/").replace("/", "_") or "homepage"
        filename += ".html"
        
        outdir = os.path.join(BACKUP_DIR, subdir)
        os.makedirs(outdir, exist_ok=True)
        
        filepath = os.path.join(outdir, filename)
        with open(filepath, "w") as f:
            f.write(resp.text)
        
        print(f"  ✓ {path} -> {filename}")
        return True
    except Exception as e:
        print(f"  ✗ {path}: {e}")
        return False

def main():
    print(f"Paradise Realty FLA Full Backup")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Directory: {BACKUP_DIR}")
    print("-" * 50)
    
    # Backup main pages
    print("\nBacking up area/content pages...")
    success = 0
    for page in AREA_PAGES:
        if backup_page(page):
            success += 1
    
    # Backup homepage
    print("\nBacking up homepage...")
    backup_page("/", "pages")
    
    print(f"\n✓ Backed up {success}/{len(AREA_PAGES)} pages")
    print(f"Backup location: {BACKUP_DIR}")

if __name__ == "__main__":
    main()
