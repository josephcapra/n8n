#!/usr/bin/env bash
# build.sh — build and push the single Agent-Manager image.
# Review before running. Run from the agent-manager/ directory.
#
# Usage:
#   bash deploy/build.sh --dry-run
#   bash deploy/build.sh

set -euo pipefail

PROJECT="paradise-automation"
REGION="us-east1"
ARTIFACT_REPO="agent-manager"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${ARTIFACT_REPO}/agentmgr:latest"

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true
run() { echo "+ $*"; [[ "$DRY_RUN" == false ]] && "$@"; return 0; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."   # agent-manager/  — the Docker build context

echo "=== Build Agent-Manager image ($([ "$DRY_RUN" = true ] && echo DRY RUN || echo LIVE)) ==="
echo "Image: $IMAGE"
echo

run gcloud builds submit --project="$PROJECT" --tag "$IMAGE" .

echo
echo "Built: $IMAGE"
echo "Next: deploy/deploy-worker.sh then deploy/deploy-master.sh"
