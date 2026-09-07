#!/usr/bin/env python3
"""
One-shot: OAuth grant for webmasters scope (browser approval), then submit
/sitemap1/ ... /sitemap16/ to Google Search Console.

Saves the refresh token to ~/.sitemap-sync-gsc-token.json so subsequent runs
don't need a fresh consent.
"""
import json
import os
import sys
from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

CLIENT_SECRET = "/Users/User/paradise-realty/api/client_secret_383923649216-61v5iee0o30omc41f6ba5io4ss0617k2.apps.googleusercontent.com.json"
TOKEN_PATH = Path.home() / ".sitemap-sync-gsc-token.json"
SCOPES = ["https://www.googleapis.com/auth/webmasters"]

SITE_URL = "https://www.paradiserealtyfla.com/"
BASE = SITE_URL.rstrip("/")
N_SHARDS = 16


def get_creds() -> Credentials:
    if TOKEN_PATH.exists():
        try:
            data = json.loads(TOKEN_PATH.read_text())
            creds = Credentials(
                token=None,
                refresh_token=data["refresh_token"],
                token_uri="https://oauth2.googleapis.com/token",
                client_id=data["client_id"],
                client_secret=data["client_secret"],
                scopes=SCOPES,
            )
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            print(f"✅  Reused cached token from {TOKEN_PATH}")
            return creds
        except Exception as e:
            print(f"⚠️   Cached token failed: {e}. Re-running consent flow.")

    print("→  Opening browser for one-time OAuth consent (webmasters scope)...")
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)

    TOKEN_PATH.write_text(json.dumps({
        "refresh_token": creds.refresh_token,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
    }))
    os.chmod(TOKEN_PATH, 0o600)
    print(f"→  Saved refresh token to {TOKEN_PATH}")
    return creds


def main() -> int:
    creds = get_creds()
    service = build("searchconsole", "v1", credentials=creds)

    print(f"\n→  Submitting {N_SHARDS} sitemaps to GSC for {SITE_URL}\n")
    ok = fail = 0
    for n in range(1, N_SHARDS + 1):
        fp = f"{BASE}/sitemap{n}/"
        try:
            service.sitemaps().submit(siteUrl=SITE_URL, feedpath=fp).execute()
            info = service.sitemaps().get(siteUrl=SITE_URL, feedpath=fp).execute()
            warnings = info.get("warnings", 0)
            errors = info.get("errors", 0)
            print(f"  ✅  {fp}  pending={info.get('isPending')}  warnings={warnings}  errors={errors}")
            ok += 1
        except HttpError as e:
            print(f"  ❌  {fp}  HTTP {e.resp.status}: {str(e)[:200]}")
            fail += 1

    print(f"\nResult: {ok} submitted, {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
