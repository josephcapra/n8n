# sitemap-sync

Idempotent pipeline: fetch paradiserealtyfla.com sitemap → shard → upload to GCS → sync RealGeeks redirects → submit to Google Search Console → submit to Bing Webmaster Tools.

Safe to run repeatedly. Each step diffs current state against this run and deletes stale entries.

---

## Prerequisites

- Python 3.11+
- `gcloud` CLI ([install](https://cloud.google.com/sdk/docs/install))
- Playwright Chromium

---

## One-time setup

### 1. Clone / enter the directory

```bash
cd sitemap-sync
```

### 2. Create a virtual environment and install deps

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 3. Create your `.env` file

```bash
cp .env.example .env
```

Then fill in the values — see **Credentials** below.

### 4. Authenticate Google

```bash
gcloud auth application-default login \
  --scopes=https://www.googleapis.com/auth/webmasters,https://www.googleapis.com/auth/cloud-platform
```

This writes ADC credentials to `~/.config/gcloud/application_default_credentials.json`.  
No service account key file is needed.

---

## Credentials

| Variable | How to get it |
|---|---|
| `GCS_PROJECT` | `paradise-automation` (already set in `.env.example`) |
| `GCS_BUCKET` | `site-map-dynamic-page3` (already set) |
| `SITE_URL` | `https://www.paradiserealtyfla.com/` (already set) |
| `BING_API_KEY` | Already set in `.env.example` |
| `REALGEEKS_LOGIN_URL` | URL of your RealGeeks admin login page — see below |
| `REALGEEKS_REDIRECTS_URL` | URL of the redirects admin list — see below |
| `REALGEEKS_USER` | Your RealGeeks admin username / email |
| `REALGEEKS_PASS` | Your RealGeeks admin password |

### RealGeeks — persistent login setup

RealGeeks uses OAuth via `login.realgeeks.com`. The script uses a **persistent Chromium profile** (`BROWSER_DATA_DIR`, default `~/.sitemap-sync-browser`) so you only authenticate once.

**First-time setup (if 2FA is enabled on your account):**

```bash
python3 -c "
from dotenv import load_dotenv; import os; load_dotenv()
from realgeeks_client import pre_login
pre_login(os.environ['REALGEEKS_LOGIN_URL'], os.environ['REALGEEKS_USER'], os.environ['REALGEEKS_PASS'])
"
```

This opens a visible browser window. Complete 2FA there if prompted, then press Enter. The session is saved and all future runs use it headlessly.

**Redirects admin URL** — set as `REALGEEKS_REDIRECTS_URL` in `.env`:
```
https://www.paradiserealtyfla.com/admin/redirects/redirect/
```
Confirm this after your first login by navigating to the admin and finding the Redirects section.

### GSC — Google Search Console

The site `https://www.paradiserealtyfla.com/` must be **verified** in your Search Console account. ADC handles auth — no extra steps if you ran the `gcloud auth` command above.

---

## Running

```bash
# Activate virtualenv first
source .venv/bin/activate

# Full run
python sync-sitemaps.py

# Dry run — logs everything without writing
python sync-sitemaps.py --dry-run

# Skip specific steps
python sync-sitemaps.py --skip-redirects --skip-gsc
python sync-sitemaps.py --skip-gcs --skip-bing

# Read sitemap from local file instead of fetching live
python sync-sitemaps.py --source-file /path/to/sitemap.xml

# Verbose logging
python sync-sitemaps.py --verbose
```

Exit code `0` = full success. Non-zero = one or more steps failed (check the log).

---

## Output

Each run writes a timestamped log to `logs/run-YYYY-MM-DD-HHMMSS.log`.  
RealGeeks redirects page screenshot saved to `logs/redirects-YYYY-MM-DD-HHMMSS.png`.

End-of-run summary example:

```
══════════════════════════════════════════════════════════════
  Sitemap Sync Summary
══════════════════════════════════════════════════════════════
  URLs collected:        12,450
  Shards generated:      1
  GCS uploaded:          2 (deleted: 0) ✅
  Redirects upserted:    2 (deleted: 0) ✅
  GSC submitted:         2 (deleted: 0) ✅
  Bing submitted:        2 (deleted: 0) ✅
  Total runtime:         38.4s
  Log file:              logs/run-2026-05-12-143022.log
══════════════════════════════════════════════════════════════
✅  All steps completed successfully
```

---

## How it works (step by step)

| Step | What happens |
|---|---|
| 1 | Fetch `SOURCE_SITEMAP_URL`. If sitemapindex, recursively fetch child sitemaps. Deduplicate URLs. |
| 2 | Shard into files of ≤40,000 URLs each (`sitemap-1.xml`, …). Build `sitemap-index.xml`. Validate XML + size (<50 MB). |
| 3 | Upload shards + index to `gs://site-map-dynamic-page3/sitemaps/`. Set public-read ACL. Delete stale shards. |
| 4 | Log in to RealGeeks admin. Upsert 301 redirects: `/sitemapN/` → GCS URL. Delete stale redirects. Screenshot. |
| 5 | Submit each `/sitemapN/` feedpath + `/sitemap-index/` to GSC. Confirm acceptance. Delete stale GSC sitemaps. |
| 6 | POST each feedpath to Bing SubmitFeed. Confirm via GetFeeds. Remove stale feeds via RemoveFeed. |

---

## Common errors and fixes

### `Missing required env var: REALGEEKS_LOGIN_URL`
Fill in the missing variable in `.env`. Copy `.env.example` if you haven't.

### `Login failed — credentials rejected`
Check `REALGEEKS_USER` and `REALGEEKS_PASS`. Make sure you're not using an SSO/OAuth login that requires a browser flow.

### `Could not find 'Add redirect' button`
The RealGeeks admin UI may use different HTML than expected. Open the `REALGEEKS_REDIRECTS_URL` in your browser, inspect the "Add" button element, and update `SEL_ADD_BTN` in `realgeeks_client.py`.

### `GCS auth failed` / `403 Forbidden`
Run: `gcloud auth application-default login --scopes=...` (see setup step 4).  
Also confirm the `paradise-automation` project and your account have `Storage Object Admin` on the bucket.

### `⚠️  Cannot set per-object ACL`
The bucket uses Uniform Bucket-Level Access. Run:
```bash
gcloud storage buckets add-iam-policy-binding gs://site-map-dynamic-page3 \
  --member=allUsers --role=roles/storage.objectViewer
```

### `GSC auth error (403)`
Confirm `https://www.paradiserealtyfla.com/` is verified in Search Console under the account whose ADC credentials you used.

### `Bing API 401`
Check `BING_API_KEY` in `.env`. Also confirm the site is registered in Bing Webmaster Tools at <https://www.bing.com/webmasters/>.
