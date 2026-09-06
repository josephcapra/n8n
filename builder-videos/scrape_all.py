#!/usr/bin/env python3
"""
Scrape official videos from all builder community pages.
"""

import os
import re
import json
import subprocess
from datetime import datetime
from playwright.sync_api import sync_playwright

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(OUTPUT_DIR, "downloads")
METADATA_FILE = os.path.join(OUTPUT_DIR, "video_metadata.json")

os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# GL Homes communities
GL_HOMES = [
    ("Apex at Avenir", "https://www.glhomes.com/apex-at-avenir/"),
    ("Lotus Edge", "https://www.glhomes.com/lotus-edge/"),
    ("The Estates @ NOMAR", "https://www.glhomes.com/the-estates-at-nomar/"),
    ("Valencia Grand", "https://www.glhomes.com/valencia-grand/"),
    ("Valencia Del Mar", "https://www.glhomes.com/valencia-del-mar/"),
    ("Valencia Harbor", "https://www.glhomes.com/valencia-harbor/"),
    ("Valencia Ridge", "https://www.glhomes.com/valencia-ridge/"),
    ("Valencia Sky", "https://www.glhomes.com/valencia-sky/"),
    ("Valencia Vista at Riverland", "https://www.glhomes.com/valencia-vista-at-riverland/"),
    ("Valencia Parc at Riverland", "https://www.glhomes.com/valencia-parc-at-riverland/"),
]

# Kolter communities (from existing scraper)
KOLTER = [
    ("Alton", "https://www.kolterhomes.com/find-your-home/southeast-florida/alton/"),
    ("Artistry", "https://www.kolterhomes.com/find-your-home/southeast-florida/artistry-palm-beach/"),
    ("APEX at Avenir", "https://www.kolterhomes.com/find-your-home/southeast-florida/apex-at-avenir/"),
]

def extract_youtube_ids(page):
    """Extract all YouTube video IDs from a page."""
    videos = []
    
    # Method 1: Look for YouTube iframes
    iframes = page.query_selector_all('iframe[src*="youtube"]')
    for iframe in iframes:
        src = iframe.get_attribute('src') or ''
        match = re.search(r'youtube\.com/embed/([a-zA-Z0-9_-]{11})', src)
        if match:
            videos.append(match.group(1))
    
    # Method 2: Look for youtu.be links
    links = page.query_selector_all('a[href*="youtu"]')
    for link in links:
        href = link.get_attribute('href') or ''
        match = re.search(r'youtu\.be/([a-zA-Z0-9_-]{11})', href)
        if match:
            videos.append(match.group(1))
        match = re.search(r'youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})', href)
        if match:
            videos.append(match.group(1))
    
    # Method 3: Look for data attributes
    video_els = page.query_selector_all('[data-video-id], [data-youtube-id]')
    for el in video_els:
        vid = el.get_attribute('data-video-id') or el.get_attribute('data-youtube-id')
        if vid and len(vid) == 11:
            videos.append(vid)
    
    return list(set(videos))  # Dedupe


def scrape_builder(browser, builder_name, communities):
    """Scrape all communities for a builder."""
    results = []
    page = browser.new_page()
    
    for community_name, url in communities:
        print(f"  Scraping {community_name}...")
        
        try:
            page.goto(url, timeout=60000)
            page.wait_for_load_state('domcontentloaded', timeout=30000)
            page.wait_for_timeout(2000)
            
            # Check main page
            video_ids = extract_youtube_ids(page)
            
            # Also check lifestyle/amenities page if it exists
            for sub in ['/lifestyle/', '/amenities/', '/videos/']:
                try:
                    sub_url = url.rstrip('/') + sub
                    page.goto(sub_url, timeout=30000)
                    page.wait_for_load_state('domcontentloaded', timeout=15000)
                    page.wait_for_timeout(1500)
                    video_ids.extend(extract_youtube_ids(page))
                except:
                    pass
            
            video_ids = list(set(video_ids))
            
            if video_ids:
                print(f"    Found {len(video_ids)} video(s)")
                for vid in video_ids:
                    results.append({
                        'community': community_name,
                        'builder': builder_name,
                        'platform': 'youtube',
                        'video_id': vid,
                        'youtube_url': f'https://youtu.be/{vid}',
                        'source_url': url,
                        'scraped_at': datetime.now().isoformat()
                    })
            else:
                print(f"    No videos found")
                
        except Exception as e:
            print(f"    Error: {e}")
    
    page.close()
    return results


def download_videos(videos):
    """Download all videos."""
    downloaded = 0
    
    for v in videos:
        if v.get('downloaded'):
            continue
            
        safe_name = re.sub(r'[^\w\-]', '_', f"{v['builder']}_{v['community']}_{v['video_id'][:6]}")
        output_path = os.path.join(DOWNLOADS_DIR, f"{safe_name}.mp4")
        
        if os.path.exists(output_path):
            print(f"  Already have: {safe_name}")
            v['local_path'] = output_path
            v['downloaded'] = True
            downloaded += 1
            continue
        
        try:
            print(f"  Downloading: {v['community']} ({v['video_id']})")
            cmd = [
                'yt-dlp',
                '-f', 'bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/mp4/best[height<=1080]',
                '-o', output_path,
                '--no-playlist',
                '--quiet',
                v['youtube_url']
            ]
            subprocess.run(cmd, check=True, timeout=300)
            v['local_path'] = output_path
            v['downloaded'] = True
            downloaded += 1
            print(f"    ✓ Downloaded")
        except Exception as e:
            print(f"    ✗ Failed: {e}")
    
    return downloaded


def main():
    print("=" * 60)
    print("Official Builder Video Scraper")
    print("=" * 60)
    
    all_videos = []
    
    # Load existing
    if os.path.exists(METADATA_FILE):
        with open(METADATA_FILE) as f:
            all_videos = json.load(f)
        print(f"Loaded {len(all_videos)} existing videos")
    
    existing_ids = {v['video_id'] for v in all_videos}
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        
        # GL Homes
        print("\n[GL Homes]")
        gl_videos = scrape_builder(browser, "GL Homes", GL_HOMES)
        for v in gl_videos:
            if v['video_id'] not in existing_ids:
                all_videos.append(v)
                existing_ids.add(v['video_id'])
        
        # Kolter
        print("\n[Kolter Homes]")
        kolter_videos = scrape_builder(browser, "Kolter Homes", KOLTER)
        for v in kolter_videos:
            if v['video_id'] not in existing_ids:
                all_videos.append(v)
                existing_ids.add(v['video_id'])
        
        browser.close()
    
    # Save metadata
    with open(METADATA_FILE, 'w') as f:
        json.dump(all_videos, f, indent=2)
    
    print(f"\nTotal videos found: {len(all_videos)}")
    
    # Download
    print("\nDownloading videos...")
    downloaded = download_videos(all_videos)
    print(f"Downloaded {downloaded} new videos")
    
    # Save updated metadata
    with open(METADATA_FILE, 'w') as f:
        json.dump(all_videos, f, indent=2)
    
    # Summary
    print("\n" + "=" * 60)
    print("Summary:")
    for v in all_videos:
        status = "✓" if v.get('downloaded') else "○"
        print(f"  {status} {v['builder']} - {v['community']}")
    
    return all_videos


if __name__ == "__main__":
    main()

# Additional builder communities
LENNAR = [
    ("Mirada", "https://www.lennar.com/new-homes/florida/tampa-bay-area/tampa/mirada"),
    ("Meridian at Mayfair", "https://www.lennar.com/new-homes/florida/space-coast-melbourne/melbourne/meridian-at-mayfair"),
    ("Angeline", "https://www.lennar.com/new-homes/florida/tampa-bay-area/land-o-lakes/angeline"),
    ("Amelia Groves", "https://www.lennar.com/new-homes/florida/orlando/st-cloud/amelia-groves"),
    ("Aurora at Lakewood Ranch", "https://www.lennar.com/new-homes/florida/sarasota/bradenton/aurora-at-lakewood-ranch"),
]

DR_HORTON = [
    ("Riverwalk of Cocoa", "https://www.drhorton.com/florida/east-florida/riverwalk-of-cocoa"),
    ("Crossmolina", "https://www.drhorton.com/florida/east-florida/crossmolina"),
]
