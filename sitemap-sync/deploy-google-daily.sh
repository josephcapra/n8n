#!/bin/bash
# Deploy the autonomous Google daily-management job (google-daily) to Cloud Run.
#
# The Google-side sibling of deploy-bing-daily.sh. Reuses the shared
# sitemap-sync image, service account, and SendGrid/GSC secrets. Creates:
#   - rebuilt image (now includes gsc_manager.py + anthropic for SEO research)
#   - new Cloud Run Job: google-daily  (runs gsc_manager.py)
#   - new Cloud Scheduler entry: google-daily-morning (06:30 ET daily)
#
# Idempotent: re-running updates the job + scheduler in place.
#
# NOTE: the Google Indexing API step degrades gracefully until you (a) enable
# indexing.googleapis.com, (b) add the runner SA as a Search Console OWNER, and
# (c) provide a SA key with the auth/indexing scope via a GOOGLE_INDEXING_CREDS
# secret. Until then google-daily reports "Indexing API not configured" and the
# other four jobs (performance, sitemaps, on-page/AI scan, research) run fully.
set -euo pipefail

PROJECT="paradise-automation"
REGION="us-east1"
REPO="sitemap-sync"
IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/sitemap-sync:latest"
JOB="google-daily"
SA="sitemap-sync-runner"
SA_EMAIL="$SA@$PROJECT.iam.gserviceaccount.com"
SCHEDULER_JOB="google-daily-morning"

# GA4_PROPERTY_ID = the numeric GA4 property id (NOT the G-XXXX measurement id).
# Find it in GA4 Admin -> Property Settings -> Property ID. Pass it at deploy
# time:  GA4_PROPERTY_ID=123456789 bash deploy-google-daily.sh
# Until it's set (and the runner SA is a GA4 Viewer), the GA4 step degrades
# gracefully and the report shows "GA4 behavior data not wired yet".
ENV_VARS="\
GCS_PROJECT=$PROJECT,\
SITE_URL=https://www.paradiserealtyfla.com/,\
GSC_SITE_URL=https://www.paradiserealtyfla.com/,\
DAILY_SUBMIT_GCS_CSV=gs://site-map-dynamic-page3/sitemaps/communities_combined.csv,\
GSC_INDEXING_LIMIT=100,\
GA4_PROPERTY_ID=${GA4_PROPERTY_ID:-},\
GA4_SA_EMAIL=${GA4_SA_EMAIL:-$SA_EMAIL},\
GA4_IMPERSONATE_SUBJECT=${GA4_IMPERSONATE_SUBJECT:-joe@josephcapra.com},\
REPORT_EMAIL_TO=joe@josephcapra.com"

# anthropic-api-key (lowercase-dashed) is the existing secret; map it to the
# ANTHROPIC_API_KEY env var gsc_manager reads for the SEO-research loop.
SECRETS="SENDGRID_API_KEY=SENDGRID_API_KEY:latest,\
GSC_OAUTH_CREDS=GSC_OAUTH_CREDS:latest,\
ANTHROPIC_API_KEY=anthropic-api-key:latest"

echo "=== Enabling Analytics Data API (for the GA4 behavior pull) ==="
gcloud services enable analyticsdata.googleapis.com --project=$PROJECT \
  >/dev/null 2>&1 && echo "enabled" || echo "enable skipped/failed (continuing)"

# GA4 access uses KEYLESS DOMAIN-WIDE DELEGATION: the runner SA impersonates a
# real GA4 user (GA4_IMPERSONATE_SUBJECT) instead of being added as a GA4 user
# (the GA4 UI refuses service-account emails). Two one-time setup steps the
# script can't do for you:
#   1) Workspace Admin console (admin.google.com) > Security > Access and data
#      control > API controls > Manage Domain Wide Delegation > Add new:
#        Client ID = the runner SA's numeric uniqueId (gcloud iam ... describe)
#        Scope     = https://www.googleapis.com/auth/analytics.readonly
#   2) The runner SA must be able to sign its own impersonation JWT (granted
#      below). Needs iamcredentials.googleapis.com (enabled above).
echo "=== Enabling iamcredentials + self token-creator (keyless GA4 impersonation) ==="
gcloud services enable iamcredentials.googleapis.com --project=$PROJECT >/dev/null 2>&1 || true
gcloud iam service-accounts add-iam-policy-binding $SA_EMAIL \
  --project=$PROJECT \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/iam.serviceAccountTokenCreator" >/dev/null && echo "granted" || echo "grant skipped/failed"

echo "=== Building & pushing image (shared with sitemap-sync) ==="
gcloud builds submit /Users/User/sitemap-sync \
  --tag=$IMAGE \
  --project=$PROJECT

# The runner SA already reads SENDGRID/GSC secrets, but the SEO-research loop
# needs the (JoeGPT-owned) anthropic-api-key secret. Grant accessor so the
# ANTHROPIC_API_KEY mount below works; without it the job deploys but research
# is skipped. (Run by you = you authorize this IAM grant.)
echo "=== Granting runner SA accessor on anthropic-api-key (for SEO research) ==="
gcloud secrets add-iam-policy-binding anthropic-api-key \
  --project=$PROJECT \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/secretmanager.secretAccessor" >/dev/null && echo "granted" || echo "grant skipped/failed — research stays disabled"

echo "=== Creating/updating Cloud Run Job: $JOB ==="
gcloud run jobs create $JOB \
  --image=$IMAGE \
  --region=$REGION \
  --project=$PROJECT \
  --service-account=$SA_EMAIL \
  --task-timeout=1800 \
  --max-retries=1 \
  --command="python3" --args="gsc_manager.py" \
  --set-env-vars="$ENV_VARS" \
  --set-secrets="$SECRETS" \
  2>/dev/null || \
gcloud run jobs update $JOB \
  --image=$IMAGE \
  --region=$REGION \
  --project=$PROJECT \
  --command="python3" --args="gsc_manager.py" \
  --set-env-vars="$ENV_VARS" \
  --set-secrets="$SECRETS"

echo "=== Creating/updating Cloud Scheduler: $SCHEDULER_JOB (daily 6:30 AM ET) ==="
gcloud scheduler jobs create http $SCHEDULER_JOB \
  --location=$REGION \
  --schedule="30 6 * * *" \
  --time-zone="America/New_York" \
  --uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/$JOB:run" \
  --http-method=POST \
  --oauth-service-account-email=$SA_EMAIL \
  --project=$PROJECT 2>/dev/null || \
gcloud scheduler jobs update http $SCHEDULER_JOB \
  --location=$REGION \
  --schedule="30 6 * * *" \
  --time-zone="America/New_York" \
  --project=$PROJECT

echo ""
echo "✅  Done."
echo "    Cloud Run Job:  $JOB  ($REGION)"
echo "    Scheduler:      $SCHEDULER_JOB  (daily 6:30 AM ET)"
echo ""
echo "Run manually:"
echo "  gcloud run jobs execute $JOB --region=$REGION --project=$PROJECT"
echo ""
echo "Tail logs:"
echo "  gcloud beta run jobs executions list --job=$JOB --region=$REGION --project=$PROJECT --limit=1"
