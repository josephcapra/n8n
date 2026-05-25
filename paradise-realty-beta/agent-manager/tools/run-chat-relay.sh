#!/bin/bash
# Runs the Google Chat → agent-manager relay (continuous Pub/Sub pull).
# Installed/loaded by setup-google-chat.sh as launchd com.paradise.chatrelay.
# Kill-switch:  touch ~/.agentmgr-chat-relay.disabled   (rm to re-enable)
set -eo pipefail
cd /Users/User/paradise-realty-beta/agent-manager

# Full PATH so headless Claude Code + its tools resolve (if you opt into claude).
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export AGENTMGR_GCP_PROJECT="paradise-automation"
export AGENTMGR_CHAT_SA_KEY="$HOME/paradise-realty/api/agentmgr-chat-sa.json"
export AGENTMGR_OPERATOR_EMAIL="joe@josephcapra.com"

# Where commands go (same model as email):
#   claude  → headless Claude Code on this Mac (this is what we want).
#   master  → the agent-manager (runs agents/reports/lookups).
export AGENTMGR_MASTER_URL="http://localhost:8080"   # only used if TARGET=master
export AGENTMGR_API_TOKEN="local-dev-token"
export AGENTMGR_GATEWAY_TARGET="claude"
# --- FULL POWER (operator-only) --------------------------------------------
# Chat goes straight to headless Claude Code with NO approval gate.
# WARNING: anything you send in that chat runs immediately on your Mac, as you.
# Anyone who can post in that Chat space runs commands as you — keep it 1:1.
# To dial back: set AGENTMGR_CLAUDE_PERMISSION_MODE="acceptEdits" (edits auto-run,
# risky shell/deploys declined) or AGENTMGR_GATEWAY_TARGET="master".
export AGENTMGR_CLAUDE_PERMISSION_MODE="bypassPermissions"

exec .venv/bin/python -m tools.chat_gateway
