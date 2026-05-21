#!/usr/bin/env bash
# deploy-worker-transform.sh — deploy the Phase 2 second worker as a NEW
# Cloud Run Job. Creates `agentmgr-worker-transform` — touches nothing else.
# Review before running.
#
# Usage:
#   bash deploy/deploy-worker-transform.sh --dry-run
#   bash deploy/deploy-worker-transform.sh

set -euo pipefail

PROJECT="paradise-automation"
REGION="us-east1"
ARTIFACT_REPO="agent-manager"
WORKER_JOB="agentmgr-worker-transform"
WORKER_SA="agentmgr-worker@${PROJECT}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${ARTIFACT_REPO}/agentmgr:latest"

: "${AGENTMGR_APPROVAL_PUBKEY:?set AGENTMGR_APPROVAL_PUBKEY (from tools/gen_keys.py)}"

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true
run() { echo "+ $*"; [[ "$DRY_RUN" == false ]] && "$@"; return 0; }

echo "=== Deploy worker Job: $WORKER_JOB ($([ "$DRY_RUN" = true ] && echo DRY RUN || echo LIVE)) ==="
echo

# Same single image as the echo worker — only the container command differs.
run gcloud run jobs deploy "$WORKER_JOB" \
  --project="$PROJECT" --region="$REGION" \
  --image="$IMAGE" \
  --service-account="$WORKER_SA" \
  --command="python" \
  --args="-m,worker.transform" \
  --max-retries=1 \
  --task-timeout=600 \
  --set-env-vars="AGENTMGR_STATE_BACKEND=firestore,AGENTMGR_PROJECT_ID=${PROJECT},AGENTMGR_APPROVAL_CHANNEL=store,AGENTMGR_APPROVAL_PUBKEY=${AGENTMGR_APPROVAL_PUBKEY}"

echo
echo "Deployed job $WORKER_JOB. The Master triggers it when a plan routes a"
echo "transform subtask to it. Grant the Master run.invoker on this job too:"
echo "  gcloud run jobs add-iam-policy-binding $WORKER_JOB --region=$REGION \\"
echo "    --member=serviceAccount:agentmgr-master@${PROJECT}.iam.gserviceaccount.com \\"
echo "    --role=roles/run.invoker"
