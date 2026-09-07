#!/bin/bash
# Deploy the daily Website Report agent to Cloud Run.
#
# Replaces the old Bing-only 6 AM email. Creates:
#   - new image tag (shared sitemap-sync repo, now includes area_changes.py)
#   - Cloud Run Job:    website-report   (runs bing_manager.py = full report)
#   - Cloud Scheduler:  website-report-weekday  (5:00 AM ET, Mon–Fri)
# And PAUSES the old bing-daily-morning scheduler so the 6 AM email stops.
#
# Report contents: Google Search Console + Bing performance + live site-health
# checks + area-page change detection (diff vs. yesterday's GCS snapshot).
#
# Idempotent: re-running updates the job + scheduler in place.
set -euo pipefail

PROJECT="paradise-automation"
REGION="us-east1"
REPO="sitemap-sync"
IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/sitemap-sync:latest"
JOB="website-report"
SA="sitemap-sync-runner"
SA_EMAIL="$SA@$PROJECT.iam.gserviceaccount.com"
SCHEDULER_JOB="website-report-weekday"
OLD_SCHEDULER="bing-daily-morning"

ENV_VARS="\
GCS_PROJECT=$PROJECT,\
SITE_URL=https://www.paradiserealtyfla.com/,\
GSC_SITE_URL=https://www.paradiserealtyfla.com/,\
BING_API_KEY=d49f8705515a4259b7d6f4fb256f04dd,\
DAILY_SUBMIT_GCS_CSV=gs://site-map-dynamic-page3/sitemaps/communities_combined.csv,\
DAILY_SUBMIT_LIMIT=500,\
REPORT_EMAIL_TO=joe@josephcapra.com,\
AREA_TRACKED_URLS_GCS=gs://site-map-dynamic-page3/area-report/area-tracked-urls.txt,\
AREA_SNAPSHOT_GCS=gs://site-map-dynamic-page3/area-report/area-snapshot-latest.json,\
AREA_FETCH_WORKERS=25"

SECRETS="SENDGRID_API_KEY=SENDGRID_API_KEY:latest,GSC_OAUTH_CREDS=GSC_OAUTH_CREDS:latest"

echo "=== Building & pushing image (includes area_changes.py) ==="
gcloud builds submit /Users/User/sitemap-sync --tag="$IMAGE" --project="$PROJECT"

echo "=== Creating/updating Cloud Run Job: $JOB ==="
gcloud run jobs create "$JOB" \
  --image="$IMAGE" \
  --region="$REGION" \
  --project="$PROJECT" \
  --service-account="$SA_EMAIL" \
  --task-timeout=1800 \
  --max-retries=1 \
  --memory=1Gi \
  --command="python3" --args="bing_manager.py" \
  --set-env-vars="$ENV_VARS" \
  --set-secrets="$SECRETS" \
  2>/dev/null || \
gcloud run jobs update "$JOB" \
  --image="$IMAGE" \
  --region="$REGION" \
  --project="$PROJECT" \
  --task-timeout=1800 \
  --memory=1Gi \
  --command="python3" --args="bing_manager.py" \
  --set-env-vars="$ENV_VARS" \
  --set-secrets="$SECRETS"

echo "=== Creating/updating Cloud Scheduler: $SCHEDULER_JOB (5 AM ET, Mon–Fri) ==="
gcloud scheduler jobs create http "$SCHEDULER_JOB" \
  --location="$REGION" \
  --schedule="0 5 * * 1-5" \
  --time-zone="America/New_York" \
  --uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/$JOB:run" \
  --http-method=POST \
  --oauth-service-account-email="$SA_EMAIL" \
  --project="$PROJECT" 2>/dev/null || \
gcloud scheduler jobs update http "$SCHEDULER_JOB" \
  --location="$REGION" \
  --schedule="0 5 * * 1-5" \
  --time-zone="America/New_York" \
  --uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/$JOB:run" \
  --http-method=POST \
  --oauth-service-account-email="$SA_EMAIL" \
  --project="$PROJECT"

echo "=== Pausing old Bing-only 6 AM scheduler: $OLD_SCHEDULER ==="
gcloud scheduler jobs pause "$OLD_SCHEDULER" --location="$REGION" --project="$PROJECT" \
  && echo "  paused $OLD_SCHEDULER (the 6 AM Bing email will no longer fire)" \
  || echo "  (note: $OLD_SCHEDULER not found or already paused — skipping)"

echo ""
echo "✅  Done."
echo "    Cloud Run Job:  $JOB  ($REGION)"
echo "    Scheduler:      $SCHEDULER_JOB  (5:00 AM ET, Mon–Fri)"
echo "    Old Bing email: $OLD_SCHEDULER  (paused)"
echo ""
echo "Run manually now:"
echo "  gcloud run jobs execute $JOB --region=$REGION --project=$PROJECT --wait"
echo ""
echo "Tail logs:"
echo "  gcloud logging read 'resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"$JOB\"' \\"
echo "    --project=$PROJECT --limit=200 --format='value(textPayload)' --order=asc"
