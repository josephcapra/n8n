#!/usr/bin/env python3
"""
Submit /sitemap1/ ... /sitemap16/ to Google Search Console for
https://www.paradiserealtyfla.com/.

Uses ADC. Requires the webmasters scope:
  gcloud auth application-default login \
    --scopes=https://www.googleapis.com/auth/webmasters,https://www.googleapis.com/auth/cloud-platform
"""
import sys
import google.auth
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SITE_URL = "https://www.paradiserealtyfla.com/"
BASE = SITE_URL.rstrip("/")
N_SHARDS = 16
SCOPE = "https://www.googleapis.com/auth/webmasters"


def main() -> int:
    creds, _ = google.auth.default(scopes=[SCOPE])
    service = build("searchconsole", "v1", credentials=creds)

    feedpaths = [f"{BASE}/sitemap{n}/" for n in range(1, N_SHARDS + 1)]

    ok = 0
    fail = 0
    for fp in feedpaths:
        try:
            service.sitemaps().submit(siteUrl=SITE_URL, feedpath=fp).execute()
            try:
                info = service.sitemaps().get(siteUrl=SITE_URL, feedpath=fp).execute()
                print(
                    f"✅  {fp}  isPending={info.get('isPending')}  "
                    f"lastSubmitted={info.get('lastSubmitted')}  "
                    f"warnings={info.get('warnings', 0)}  errors={info.get('errors', 0)}"
                )
            except Exception:
                print(f"✅  {fp}  (status fetch failed)")
            ok += 1
        except HttpError as e:
            print(f"❌  {fp}  HTTP {e.resp.status}: {str(e)[:200]}")
            fail += 1
        except Exception as e:
            print(f"❌  {fp}  {type(e).__name__}: {str(e)[:200]}")
            fail += 1

    print(f"\nResult: {ok} submitted, {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
