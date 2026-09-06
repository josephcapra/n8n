#!/usr/bin/env python3
"""
Official Builder Video Scraper
Downloads videos ONLY from official builder websites.
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

# Builder configurations
BUILDERS = {
    "lennar": {
        "base_url": "https://www.lennar.com",
        "community_pattern": "/new-homes/florida/",
        "video_selectors": [
            'iframe[src*="youtube"]',
            'iframe[src*="vimeo"]',
            'video source',
            '[data-video-id]',
        ]
    },
    "drhorton": {
        "base_url": "https://www.drhorton.com",
        "community_pattern": "/florida/",
        "video_selectors": [
            'iframe[src*="youtube"]',
            'iframe[src*="vimeo"]',
            '.video-container iframe',
        ]
    },
    "toll_brothers": {
        "base_url": "https://www.tollbrothers.com",
        "community_pattern": "/florida/",
        "video_selectors": [
            'iframe[src*="youtube"]',
            '[data-video-url]',
        ]
    },
    "gl_homes": {
        "base_url": "https://www.glhomes.com",
        "community_pattern": "/communities/",
        "video_selectors": [
            'iframe[src*="youtube"]',
            'iframe[src*="vimeo"]',
            '.video-wrapper iframe',
        ]
    },
    "pulte": {
        "base_url": "https://www.pulte.com",
        "community_pattern": "/homes/florida/",
        "video_selectors": [
            'iframe[src*="youtube"]',
            '[data-video-id]',
        ]
    },
    "taylor_morrison": {
        "base_url": "https://www.taylormorrison.com",
        "community_pattern": "/new-homes/florida/",
        "video_selectors": [
            'iframe[src*="youtube"]',
            'iframe[src*="vimeo"]',
        ]
    }
}


def extract_video_id(url):
    """Extract YouTube or Vimeo video ID from URL."""
    # YouTube patterns
    yt_patterns = [
        r'youtube\.com/embed/([a-zA-Z0-9_-]{11})',
        r'youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
        r'youtu\.be/([a-zA-Z0-9_-]{11})',
    ]
    for pattern in yt_patterns:
        match = re.search(pattern, url)
        if match:
            return ('youtube', match.group(1))
    
    # Vimeo patterns
    vimeo_match = re.search(r'vimeo\.com/(?:video/)?(\d+)', url)
    if vimeo_match:
        return ('vimeo', vimeo_match.group(1))
    
    return (None, None)


def download_video(platform, video_id, community_name, builder):
    """Download video using yt-dlp."""
    if platform == 'youtube':
        url = f"https://www.youtube.com/watch?v={video_id}"
    elif platform == 'vimeo':
        url = f"https://vimeo.com/{video_id}"
    else:
        return None
    
    safe_name = re.sub(r'[^\w\-]', '_', f"{builder}_{community_name}")
    output_path = os.path.join(DOWNLOADS_DIR, f"{safe_name}.mp4")
    
    if os.path.exists(output_path):
        print(f"  Already downloaded: {safe_name}")
        return output_path
    
    try:
        cmd = [
            'yt-dlp',
            '-f', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4/best',
            '-o', output_path,
            '--no-playlist',
            url
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        print(f"  ✓ Downloaded: {safe_name}")
        return output_path
    except subprocess.CalledProcessError as e:
        print(f"  ✗ Failed to download {video_id}: {e}")
        return None


def scrape_community_videos(page, community_url, builder_config, builder_name):
    """Scrape videos from a community page."""
    videos = []
    
    try:
        page.goto(community_url, timeout=60000)
        page.wait_for_load_state('domcontentloaded', timeout=30000)
        page.wait_for_timeout(3000)
    except Exception as e:
        print(f"  Failed to load {community_url}: {e}")
        return videos
    
    # Extract community name from page
    try:
        title_el = page.query_selector('h1')
        community_name = title_el.inner_text() if title_el else community_url.split('/')[-2]
    except:
        community_name = community_url.split('/')[-2]
    
    # Find video embeds
    for selector in builder_config['video_selectors']:
        try:
            elements = page.query_selector_all(selector)
            for el in elements:
                src = el.get_attribute('src') or el.get_attribute('data-video-url') or el.get_attribute('data-video-id')
                if src:
                    platform, video_id = extract_video_id(src)
                    if video_id:
                        videos.append({
                            'community': community_name,
                            'builder': builder_name,
                            'platform': platform,
                            'video_id': video_id,
                            'source_url': community_url,
                            'scraped_at': datetime.now().isoformat()
                        })
        except Exception as e:
            continue
    
    return videos


def main(builders_to_scrape=None, community_urls=None):
    """
    Main scraper function.
    
    Args:
        builders_to_scrape: List of builder names to scrape, or None for all
        community_urls: Specific community URLs to scrape (overrides discovery)
    """
    all_videos = []
    
    # Load existing metadata
    if os.path.exists(METADATA_FILE):
        with open(METADATA_FILE) as f:
            all_videos = json.load(f)
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        if community_urls:
            # Scrape specific URLs
            for url in community_urls:
                # Detect builder from URL
                builder_name = None
                builder_config = None
                for name, config in BUILDERS.items():
                    if config['base_url'].replace('https://www.', '') in url:
                        builder_name = name
                        builder_config = config
                        break
                
                if builder_config:
                    print(f"Scraping: {url}")
                    videos = scrape_community_videos(page, url, builder_config, builder_name)
                    all_videos.extend(videos)
        else:
            # Discovery mode - not implemented yet
            print("Discovery mode requires community URLs. Use --urls flag.")
        
        browser.close()
    
    # Save metadata
    with open(METADATA_FILE, 'w') as f:
        json.dump(all_videos, f, indent=2)
    
    print(f"\nFound {len(all_videos)} total videos")
    
    # Download videos
    print("\nDownloading videos...")
    for video in all_videos:
        if video.get('downloaded'):
            continue
        path = download_video(
            video['platform'],
            video['video_id'],
            video['community'],
            video['builder']
        )
        if path:
            video['local_path'] = path
            video['downloaded'] = True
    
    # Save updated metadata
    with open(METADATA_FILE, 'w') as f:
        json.dump(all_videos, f, indent=2)
    
    return all_videos


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == '--urls':
        urls = sys.argv[2:]
        main(community_urls=urls)
    else:
        print("Usage: python scraper.py --urls <url1> <url2> ...")
        print("\nSupported builders:")
        for name in BUILDERS:
            print(f"  - {name}")
