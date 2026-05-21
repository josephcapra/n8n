#!/usr/bin/env bash
# deploy-master.sh — deploy the Master as a NEW, PRIVATE Cloud Run Service.
# Creates `agentmgr-master` — does not touch any existing job/service.
# Review before running.
#
# PRIVACY: deployed with --no-allow-unauthenticated. Only callers holding
# roles/run.invoker on this service can reach it (IAM layer). The app also
# enforces a bearer token (defence in depth). Verify BOTH before trusting it.
#
# Usage:
#   bash deploy/deploy-master.sh --dry-run
#   bash deploy/deploy-master.sh

set -euo pipefail

PROJECT="paradise-automation"
REGION="us-east1"
ARTIFACT_REPO="agent-manager"
MASTER_SERVICE="agentmgr-master"
WORKER_JOB="agentmgr-worker-echo"
MASTER_SA="agentmgr-master@${PROJECT}.iam.gserviceaccount.com"
API_TOKEN_SECRET="agentmgr-api-token"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${ARTIFACT_REPO}/agentmgr:latest"

: "${AGENTMGR_APPROVAL_PUBKEY:?set AGENTMGR_APPROVAL_PUBKEY (from tools/gen_keys.py)}"

# WebAuthn passkeys need the Master's real HTTPS host. On the FIRST deploy the
# URL is unknown — deploy once, read the URL, then set these and redeploy:
#   export AGENTMGR_RP_ID=agentmgr-master-xxxx.us-east1.run.app
#   export AGENTMGR_ORIGIN=https://agentmgr-master-xxxx.us-east1.run.app
RP_ID="${AGENTMGR_RP_ID:-localhost}"
ORIGIN="${AGENTMGR_ORIGIN:-http://localhost:8080}"
[[ "$RP_ID" == "localhost" ]] && echo \
  "WARNING: AGENTMGR_RP_ID unset — passkeys will not work until you set it to" \
  "the deployed host and redeploy."

# LLM router (Phase 3). Defaults keep the deterministic 'mock' provider so the
# service runs with no keys. To enable a real provider:
#   export AGENTMGR_PLANNER=llm AGENTMGR_LLM_PROVIDER=anthropic
# and attach the API key from Secret Manager by adding to the deploy command:
#   --update-secrets=ANTHROPIC_API_KEY=agentmgr-anthropic-key:latest
PLANNER="${AGENTMGR_PLANNER:-rule}"
LLM_PROVIDER="${AGENTMGR_LLM_PROVIDER:-mock}"

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true
run() { echo "+ $*"; [[ "$DRY_RUN" == false ]] && "$@"; return 0; }

echo "=== Deploy Master Service: $MASTER_SERVICE ($([ "$DRY_RUN" = true ] && echo DRY RUN || echo LIVE)) ==="
echo "  --no-allow-unauthenticated  (private; IAM-restricted ingress)"
echo

run gcloud run deploy "$MASTER_SERVICE" \
  --project="$PROJECT" --region="$REGION" \
  --image="$IMAGE" \
  --service-account="$MASTER_SA" \
  --no-allow-unauthenticated \
  --set-env-vars="AGENTMGR_STATE_BACKEND=firestore,AGENTMGR_JOB_RUNNER=cloudrun,AGENTMGR_PROJECT_ID=${PROJECT},AGENTMGR_REGION=${REGION},AGENTMGR_WORKER_JOB=${WORKER_JOB},AGENTMGR_APPROVAL_CHANNEL=store,AGENTMGR_APPROVAL_PUBKEY=${AGENTMGR_APPROVAL_PUBKEY},AGENTMGR_RP_ID=${RP_ID},AGENTMGR_ORIGIN=${ORIGIN},AGENTMGR_PLANNER=${PLANNER},AGENTMGR_LLM_PROVIDER=${LLM_PROVIDER}" \
  --set-secrets="AGENTMGR_API_TOKEN=${API_TOKEN_SECRET}:latest"

echo
echo "Deployed. VERIFY before trusting it:"
echo "  1. gcloud run services get-iam-policy $MASTER_SERVICE --region=$REGION"
echo "     -> confirm NO allUsers / allAuthenticatedUsers binding."
echo "  2. curl the URL with no auth -> must return 403 (IAM) or 401 (token)."
echo "  3. Only then share the URL / token with anyone."
