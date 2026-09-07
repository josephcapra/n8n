#!/usr/bin/env python3
"""Submit the Community Finder sitemaps to Google Search Console. ADDS ONLY - deletes nothing.

Joe's rule: add the new sitemaps, do not remove the old ones. Nothing here calls sitemaps().delete(),
and the 54 sitemaps already on the www property are listed before and after so any change is visible.

Property choice: these go to sc-domain:paradiserealtyfla.com, the verified DOMAIN property, because it
covers every subdomain. That lets the files be served from search.paradiserealtyfla.com - a host we
control - while still listing www.paradiserealtyfla.com community URLs inside them. The alternative
(the www URL-prefix property) would require the sitemap itself to sit on www, which is what the
RealGeeks /sitemapN/ redirect trick exists to achieve.

Auth: the stored refresh token of a verified GSC owner (~/.sitemap-sync-gsc-token.json), the same
credential the sitemap-sync agent uses. ADC is not used because it lacks the webmasters scope.
"""
import json, os, sys
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

TOKEN = os.path.expanduser("~/.sitemap-sync-gsc-token.json")
SCOPE = "https://www.googleapis.com/auth/webmasters"
PROPERTY = "sc-domain:paradiserealtyfla.com"
FINDER = "https://search.paradiserealtyfla.com"

SITEMAPS = [
    f"{FINDER}/finder-sitemap-index.xml",             # index -> the two community sitemaps
    f"{FINDER}/finder-sitemap-new-construction.xml",  # 946 curated new construction communities
    f"{FINDER}/finder-sitemap-resale-1.xml",          # 28,045 resale neighbourhoods
    f"{FINDER}/sitemap.xml",                          # the finder + incentives pages themselves
]

def service():
    info = json.load(open(TOKEN))
    creds = Credentials(token=None, refresh_token=info["refresh_token"],
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=info["client_id"], client_secret=info["client_secret"],
                        scopes=[SCOPE])
    return build("searchconsole", "v1", credentials=creds)

def listing(svc, prop):
    try:
        return {s["path"]: s for s in svc.sitemaps().list(siteUrl=prop).execute().get("sitemap", [])}
    except HttpError as e:
        print(f"  (could not list {prop}: {e.resp.status})"); return {}

def main() -> int:
    svc = service()
    before = listing(svc, PROPERTY)
    www_before = listing(svc, "https://www.paradiserealtyfla.com/")
    print(f"BEFORE  {PROPERTY}: {len(before)} sitemaps | www property: {len(www_before)} sitemaps\n")

    ok = fail = 0
    for fp in SITEMAPS:
        try:
            svc.sitemaps().submit(siteUrl=PROPERTY, feedpath=fp).execute()
            print(f"  submitted  {fp}"); ok += 1
        except HttpError as e:
            print(f"  FAILED     {fp}  HTTP {e.resp.status}: {str(e)[:160]}"); fail += 1
        except Exception as e:
            print(f"  FAILED     {fp}  {type(e).__name__}: {str(e)[:160]}"); fail += 1

    after = listing(svc, PROPERTY)
    www_after = listing(svc, "https://www.paradiserealtyfla.com/")
    print(f"\nAFTER   {PROPERTY}: {len(after)} sitemaps | www property: {len(www_after)} sitemaps")
    lost = (set(before) - set(after)) | (set(www_before) - set(www_after))
    print("nothing removed" if not lost else f"WARNING - these disappeared: {sorted(lost)}")
    print(f"\nResult: {ok} submitted, {fail} failed, 0 deleted")
    return 0 if fail == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
