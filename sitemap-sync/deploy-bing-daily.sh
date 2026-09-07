#!/bin/bash
# Deploy the autonomous Bing daily-management job to Cloud Run.
#
# Reuses the artifact, service account, and SendGrid secret from the
# existing sitemap-sync deploy. Creates:
#   - new image tag pushed to the existing Artifact Registry repo
#   - new Cloud Run Job: bing-daily
#   - new Cloud Scheduler entry: bing-daily-morning (06:00 ET daily)
#
# Idempotent: re-running updates the job + scheduler in place.
set -euo pipefail

PROJECT="paradise-automation"
REGION="us-east1"
REPO="sitemap-sync"
IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/sitemap-sync:latest"
JOB="bing-daily"
SA="sitemap-sync-runner"
SA_EMAIL="$SA@$PROJECT.iam.gserviceaccount.com"
SCHEDULER_JOB="bing-daily-morning"

echo "=== Building & pushing image (shared with sitemap-sync) ==="
gcloud builds submit /Users/User/sitemap-sync \
  --tag=$IMAGE \
  --project=$PROJECT

echo "=== Creating/updating Cloud Run Job: $JOB ==="
gcloud run jobs create $JOB \
  --image=$IMAGE \
  --region=$REGION \
  --project=$PROJECT \
  --service-account=$SA_EMAIL \
  --task-timeout=1800 \
  --max-retries=1 \
  --command="python3" --args="bing_manager.py" \
  --set-env-vars="\
GCS_PROJECT=$PROJECT,\
SITE_URL=https://www.paradiserealtyfla.com/,\
BING_API_KEY=d49f8705515a4259b7d6f4fb256f04dd,\
DAILY_SUBMIT_GCS_CSV=gs://site-map-dynamic-page3/sitemaps/communities_combined.csv,\
DAILY_SUBMIT_LIMIT=500,\
REPORT_EMAIL_TO=joe@josephcapra.com,\
GSC_SITE_URL=https://www.paradiserealtyfla.com/" \
  --set-secrets="SENDGRID_API_KEY=SENDGRID_API_KEY:latest,GSC_OAUTH_CREDS=GSC_OAUTH_CREDS:latest" \
  2>/dev/null || \
gcloud run jobs update $JOB \
  --image=$IMAGE \
  --region=$REGION \
  --project=$PROJECT \
  --command="python3" --args="bing_manager.py" \
  --set-env-vars="\
GCS_PROJECT=$PROJECT,\
SITE_URL=https://www.paradiserealtyfla.com/,\
BING_API_KEY=d49f8705515a4259b7d6f4fb256f04dd,\
DAILY_SUBMIT_GCS_CSV=gs://site-map-dynamic-page3/sitemaps/communities_combined.csv,\
DAILY_SUBMIT_LIMIT=500,\
REPORT_EMAIL_TO=joe@josephcapra.com,\
GSC_SITE_URL=https://www.paradiserealtyfla.com/" \
  --set-secrets="SENDGRID_API_KEY=SENDGRID_API_KEY:latest,GSC_OAUTH_CREDS=GSC_OAUTH_CREDS:latest"

echo "=== Creating/updating Cloud Scheduler: $SCHEDULER_JOB (daily 6 AM ET) ==="
gcloud scheduler jobs create http $SCHEDULER_JOB \
  --location=$REGION \
  --schedule="0 6 * * *" \
  --time-zone="America/New_York" \
  --uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/$JOB:run" \
  --http-method=POST \
  --oauth-service-account-email=$SA_EMAIL \
  --project=$PROJECT 2>/dev/null || \
gcloud scheduler jobs update http $SCHEDULER_JOB \
  --location=$REGION \
  --schedule="0 6 * * *" \
  --time-zone="America/New_York" \
  --project=$PROJECT

echo ""
echo "✅  Done."
echo "    Cloud Run Job:  $JOB  ($REGION)"
echo "    Scheduler:      $SCHEDULER_JOB  (daily 6:00 AM ET)"
echo ""
echo "Run manually:"
echo "  gcloud run jobs execute $JOB --region=$REGION --project=$PROJECT"
echo ""
echo "Tail logs:"
echo "  gcloud beta run jobs executions list --job=$JOB --region=$REGION --project=$PROJECT --limit=1"
