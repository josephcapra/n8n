FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-cloud.txt .
RUN pip install --no-cache-dir -r requirements-cloud.txt

COPY admin_scraper.py area_changes.py bing_client.py bing_manager.py \
     bing_seo_report.py bing_seo_scan.py email_report.py export_urls_csv.py gcs_client.py \
     ga4_analytics.py gsc_analytics.py gsc_client.py gsc_manager.py realgeeks_client.py \
     sitemap_builder.py sync-sitemaps.py agentmgr_worker.py agentmgr_reports.py ./

# Default entrypoint is the weekly sitemap sync. The bing-daily Cloud Run
# job overrides command to `python3 bing_manager.py`.
ENTRYPOINT ["python3", "sync-sitemaps.py"]
