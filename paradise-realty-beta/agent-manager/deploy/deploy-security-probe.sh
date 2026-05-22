#!/usr/bin/env bash
# deploy-security-probe.sh — build + deploy the security-probe Cloud Run Job.
# Probes the operator's own public web assets for risk and emails a report.
# Subordinate to the Master: registered in agents.json (runtime cloudrun-job),
# so the Master can trigger it like any other job; also safe to schedule.
#
# Review before running. Run from the agent-manager/ directory.
#   bash deploy/deploy-security-probe.sh --dry-run
#   bash deploy/deploy-security-probe.sh

set -euo pipefail

PROJECT="paradise-automation"
REGION="us-east1"
JOB="agentmgr-security-probe"
# Reuses the existing Secret Manager secret the other jobs use for email.
SENDGRID_SECRET="SENDGRID_API_KEY"
REPORT_TO="joe@josephcapra.com"
REPORT_FROM="noreply@paradiserealtyfla.com"      # a verified SendGrid sender domain
# The job runs as the default compute SA; grant it read on the SendGrid secret
# once (see the grant command in the chat) so it can send the report.

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true
run() { echo "+ $*"; [[ "$DRY_RUN" == false ]] && "$@"; return 0; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."   # agent-manager/ — the build context

echo "=== Deploy $JOB ($([ "$DRY_RUN" = true ] && echo DRY RUN || echo LIVE)) ==="

run gcloud run jobs deploy "$JOB" \
  --source . \
  --region "$REGION" \
  --project "$PROJECT" \
  --command python \
  --args=-m,worker.security_probe \
  --set-secrets="SENDGRID_API_KEY=${SENDGRID_SECRET}:latest" \
  --set-env-vars="REPORT_EMAIL_TO=${REPORT_TO},SECURITY_REPORT_FROM=${REPORT_FROM}" \
  --max-retries=0 --task-timeout=300

echo
echo "Run it now:        gcloud run jobs execute $JOB --region $REGION"
echo "Schedule it daily: see README — Cloud Scheduler -> run job."
echo "Trigger via Master: the security-probe shows in /agents and the Cloud Run"
echo "  job picker, or POST /chat 'run a security probe'."
