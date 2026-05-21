#!/usr/bin/env bash
# run-mac-agent.sh — run the Mac local agent on THIS machine.
# Review before running.
#
# The agent is OUTBOUND-ONLY: it opens no port. It polls the shared state
# store for shell tasks, gates each one (session window / approval), runs it
# as your user, and writes results back. Stop it (Ctrl-C) and nothing runs.
#
# Prerequisites:
#   * gcloud application-default credentials on this Mac (Firestore access)
#   * AGENTMGR_APPROVAL_PUBKEY set (printed by tools/gen_keys.py)
#   * deps installed: .venv/bin/pip install -r requirements-dev.txt

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."   # agent-manager/

: "${AGENTMGR_APPROVAL_PUBKEY:?set AGENTMGR_APPROVAL_PUBKEY (from tools/gen_keys.py)}"

export AGENTMGR_STATE_BACKEND="${AGENTMGR_STATE_BACKEND:-firestore}"
export AGENTMGR_PROJECT_ID="${AGENTMGR_PROJECT_ID:-paradise-automation}"
export AGENTMGR_APPROVAL_CHANNEL="${AGENTMGR_APPROVAL_CHANNEL:-store}"

PY="./.venv/bin/python"
[[ -x "$PY" ]] || PY="python3"

echo "=== Mac local agent ==="
echo "  runs commands as user: $(whoami)"
echo "  state backend:         $AGENTMGR_STATE_BACKEND"
echo "  inbound ports opened:  none"
echo "  Ctrl-C to stop."
echo

exec "$PY" -m agent.local_agent
