#!/usr/bin/env bash
# deploy-worker.sh — deploy the placeholder worker as a NEW Cloud Run Job.
# Creates `agentmgr-worker-echo` — does not touch any existing job/service.
# Review before running.
#
# Usage:
#   bash deploy/deploy-worker.sh --dry-run
#   bash deploy/deploy-worker.sh

set -euo pipefail

PROJECT="paradise-automation"
REGION="us-east1"
ARTIFACT_REPO="agent-manager"
WORKER_JOB="agentmgr-worker-echo"
WORKER_SA="agentmgr-worker@${PROJECT}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${ARTIFACT_REPO}/agentmgr:latest"

: "${AGENTMGR_APPROVAL_PUBKEY:?set AGENTMGR_APPROVAL_PUBKEY (from tools/gen_keys.py)}"

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true
run() { echo "+ $*"; [[ "$DRY_RUN" == false ]] && "$@"; return 0; }

echo "=== Deploy worker Job: $WORKER_JOB ($([ "$DRY_RUN" = true ] && echo DRY RUN || echo LIVE)) ==="
echo

# The worker shares the single image but OVERRIDES the container command:
#   command = python   args = -m worker.run
run gcloud run jobs deploy "$WORKER_JOB" \
  --project="$PROJECT" --region="$REGION" \
  --image="$IMAGE" \
  --service-account="$WORKER_SA" \
  --command="python" \
  --args="-m,worker.run" \
  --max-retries=1 \
  --task-timeout=600 \
  --set-env-vars="AGENTMGR_STATE_BACKEND=firestore,AGENTMGR_PROJECT_ID=${PROJECT},AGENTMGR_APPROVAL_CHANNEL=store,AGENTMGR_APPROVAL_PUBKEY=${AGENTMGR_APPROVAL_PUBKEY}"

echo
echo "Deployed job $WORKER_JOB. It runs only when the Master triggers it."
