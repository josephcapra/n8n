#!/bin/bash
# Deploy sitemap-sync as a Cloud Run Job + Cloud Scheduler weekly trigger.
# Run once: bash sitemap-sync/deploy.sh
set -euo pipefail

PROJECT="paradise-automation"
REGION="us-east1"
REPO="sitemap-sync"
IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/sitemap-sync:latest"
JOB="sitemap-sync-weekly"
SA="sitemap-sync-runner"
SA_EMAIL="$SA@$PROJECT.iam.gserviceaccount.com"

echo "=== Enabling APIs ==="
gcloud services enable \
  cloudresourcemanager.googleapis.com \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  --project=$PROJECT

echo "=== Creating Artifact Registry repo ==="
gcloud artifacts repositories create $REPO \
  --repository-format=docker \
  --location=$REGION \
  --project=$PROJECT 2>/dev/null || echo "  (already exists)"

echo "=== Creating service account ==="
gcloud iam service-accounts create $SA \
  --display-name="Sitemap Sync Runner" \
  --project=$PROJECT 2>/dev/null || echo "  (already exists)"

echo "=== Granting IAM roles ==="
for ROLE in \
  roles/storage.admin \
  roles/run.invoker \
  roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding $PROJECT \
    --member="serviceAccount:$SA_EMAIL" \
    --role="$ROLE" --quiet
done

echo "=== Building & pushing Docker image via Cloud Build ==="
gcloud services enable cloudbuild.googleapis.com --project=$PROJECT
gcloud builds submit /Users/User/sitemap-sync \
  --tag=$IMAGE \
  --project=$PROJECT

echo "=== Storing SendGrid key in Secret Manager ==="
echo -n "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" | \
  gcloud secrets create SENDGRID_API_KEY \
    --data-file=- \
    --project=$PROJECT 2>/dev/null || \
echo -n "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" | \
  gcloud secrets versions add SENDGRID_API_KEY \
    --data-file=- \
    --project=$PROJECT

gcloud secrets add-iam-policy-binding SENDGRID_API_KEY \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/secretmanager.secretAccessor" \
  --project=$PROJECT --quiet

echo "=== Creating Cloud Run Job ==="
gcloud run jobs create $JOB \
  --image=$IMAGE \
  --region=$REGION \
  --project=$PROJECT \
  --service-account=$SA_EMAIL \
  --task-timeout=3600 \
  --max-retries=1 \
  --set-env-vars="\
GCS_PROJECT=$PROJECT,\
GCS_BUCKET=site-map-dynamic-page3,\
GCS_PREFIX=sitemaps/,\
SITE_URL=https://www.paradiserealtyfla.com/,\
REDIRECT_BASE_URL=https://www.paradiserealtyfla.com/,\
BING_API_KEY=d49f8705515a4259b7d6f4fb256f04dd,\
REPORT_EMAIL_TO=joe@josephcapra.com" \
  --set-secrets="\
SENDGRID_API_KEY=SENDGRID_API_KEY:latest" \
  --args="--source-gcs-csv,gs://site-map-dynamic-page3/sitemaps/communities_combined.csv,--skip-redirects,--email-to,joe@josephcapra.com" \
  2>/dev/null || \
gcloud run jobs update $JOB \
  --image=$IMAGE \
  --region=$REGION \
  --project=$PROJECT \
  --set-env-vars="\
GCS_PROJECT=$PROJECT,\
GCS_BUCKET=site-map-dynamic-page3,\
GCS_PREFIX=sitemaps/,\
SITE_URL=https://www.paradiserealtyfla.com/,\
REDIRECT_BASE_URL=https://www.paradiserealtyfla.com/,\
BING_API_KEY=d49f8705515a4259b7d6f4fb256f04dd,\
REPORT_EMAIL_TO=joe@josephcapra.com" \
  --args="--source-gcs-csv,gs://site-map-dynamic-page3/sitemaps/communities_combined.csv,--skip-redirects,--email-to,joe@josephcapra.com"

echo "=== Storing SendGrid key in Secret Manager ==="
echo -n "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" | \
  gcloud secrets create SENDGRID_API_KEY \
    --data-file=- \
    --project=$PROJECT 2>/dev/null || \
echo -n "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" | \
  gcloud secrets versions add SENDGRID_API_KEY \
    --data-file=- \
    --project=$PROJECT

# Grant SA access to the secret
gcloud secrets add-iam-policy-binding SENDGRID_API_KEY \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/secretmanager.secretAccessor" \
  --project=$PROJECT --quiet

echo "=== Creating Cloud Scheduler job (Mon 5 AM ET) ==="
gcloud scheduler jobs create http sitemap-sync-monday \
  --location=$REGION \
  --schedule="0 5 * * 1" \
  --time-zone="America/New_York" \
  --uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/$JOB:run" \
  --http-method=POST \
  --oauth-service-account-email=$SA_EMAIL \
  --project=$PROJECT 2>/dev/null || \
gcloud scheduler jobs update http sitemap-sync-monday \
  --location=$REGION \
  --schedule="0 5 * * 1" \
  --time-zone="America/New_York" \
  --project=$PROJECT

echo ""
echo "✅  Done. Weekly schedule: every Monday at 5:00 AM ET"
echo "    Cloud Run Job:  $JOB  ($REGION)"
echo "    Scheduler:      sitemap-sync-monday"
echo ""
echo "To run manually:"
echo "  gcloud run jobs execute $JOB --region=$REGION --project=$PROJECT"
