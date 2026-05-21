#!/usr/bin/env bash
# setup-iam.sh — one-time provisioning for the Agent-Manager system.
# Review every line before running. DO NOT run this automatically.
#
# Creates: 2 least-privilege service accounts, an Artifact Registry repo,
# and grants the exact IAM roles documented in the README. Idempotent-ish:
# re-running is safe (account/repo creation is tolerated if they exist).
#
# Usage:
#   bash deploy/setup-iam.sh --dry-run     # print every command, change nothing
#   bash deploy/setup-iam.sh               # execute

set -euo pipefail

PROJECT="paradise-automation"
REGION="us-east1"
ARTIFACT_REPO="agent-manager"
MASTER_SA="agentmgr-master"
WORKER_SA="agentmgr-worker"
WORKER_JOB="agentmgr-worker-echo"
MASTER_SERVICE="agentmgr-master"
OPERATOR="joe@josephcapra.com"           # who may invoke the private Master
API_TOKEN_SECRET="agentmgr-api-token"

MASTER_SA_EMAIL="${MASTER_SA}@${PROJECT}.iam.gserviceaccount.com"
WORKER_SA_EMAIL="${WORKER_SA}@${PROJECT}.iam.gserviceaccount.com"

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true
# Never abort the script on a single failed step — the job/service IAM
# bindings are expected to fail on the first run (their targets don't exist
# yet) and succeed when this script is re-run after deployment.
run() {
  echo "+ $*"
  if [[ "$DRY_RUN" == false ]]; then
    "$@" || echo "  (step failed — continuing; re-run this script after deploy)"
  fi
  return 0
}

echo "=== Agent-Manager IAM setup ($([ "$DRY_RUN" = true ] && echo DRY RUN || echo LIVE)) ==="
echo

# --- service accounts (least privilege) ---------------------------------
run gcloud iam service-accounts create "$MASTER_SA" --project="$PROJECT" \
  --display-name="Agent-Manager Master Service" || true
run gcloud iam service-accounts create "$WORKER_SA" --project="$PROJECT" \
  --display-name="Agent-Manager Worker Jobs" || true

# --- artifact registry ---------------------------------------------------
run gcloud artifacts repositories create "$ARTIFACT_REPO" --project="$PROJECT" \
  --repository-format=docker --location="$REGION" \
  --description="Agent-Manager container images" || true

# --- Firestore access (both need it for the shared state store) ----------
run gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${MASTER_SA_EMAIL}" --role="roles/datastore.user" \
  --condition=None
run gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${WORKER_SA_EMAIL}" --role="roles/datastore.user" \
  --condition=None

# --- Master may RUN the worker Job (scoped to that one job) --------------
run gcloud run jobs add-iam-policy-binding "$WORKER_JOB" --project="$PROJECT" \
  --region="$REGION" \
  --member="serviceAccount:${MASTER_SA_EMAIL}" --role="roles/run.invoker"

# --- Master may read ONLY the app-token secret ---------------------------
# Create the secret first (value supplied by you — see README).
run gcloud secrets add-iam-policy-binding "$API_TOKEN_SECRET" --project="$PROJECT" \
  --member="serviceAccount:${MASTER_SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor"

# --- Operator may invoke the private Master ------------------------------
run gcloud run services add-iam-policy-binding "$MASTER_SERVICE" --project="$PROJECT" \
  --region="$REGION" \
  --member="user:${OPERATOR}" --role="roles/run.invoker"

echo
echo "Done. NOT granted (by design): the approval PRIVATE key is never in GCP;"
echo "neither service account can read it. See README 'Security model'."
echo
echo "Note: run jobs/services IAM bindings require the job/service to exist —"
echo "if this is a first run, deploy the worker + master first, then re-run"
echo "the two add-iam-policy-binding steps above."
