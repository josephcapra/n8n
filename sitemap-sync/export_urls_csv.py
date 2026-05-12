#!/usr/bin/env python3
"""Export all DynamicPage URLs from the admin to a CSV file."""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

import admin_scraper

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

start = time.time()

urls = admin_scraper.collect_urls_from_admin(
    admin_url=os.environ["ADMIN_URL"],
    site_url=os.environ["SITE_URL"],
    login_url=os.environ["REALGEEKS_LOGIN_URL"],
    username=os.environ["REALGEEKS_USER"],
    password=os.environ["REALGEEKS_PASS"],
)

out = Path(__file__).parent / "dynamic_page_urls.csv"
admin_scraper.write_csv(urls, out)

elapsed = time.time() - start
print(f"\nDone: {len(urls):,} URLs → {out}  ({elapsed:.0f}s)")
