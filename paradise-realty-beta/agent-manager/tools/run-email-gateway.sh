#!/bin/bash
# Runs the agent-manager email command gateway once: poll Gmail for authorized
# "AM:" commands from the operator, run them through the master, reply via
# SendGrid. Scheduled by ~/Library/LaunchAgents/com.paradise.emailgateway.plist.
# Kill-switch:  touch ~/.agentmgr-email-gateway.disabled   (rm to re-enable)
set -eo pipefail
cd /Users/User/paradise-realty-beta/agent-manager

# Full PATH so headless Claude Code + its tools (node, git, gcloud) resolve.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export AGENTMGR_MASTER_URL="http://localhost:8080"
export AGENTMGR_API_TOKEN="local-dev-token"        # local master token (run_local.py)
export AGENTMGR_OPERATOR_EMAIL="joe@josephcapra.com"
export AGENTMGR_GMAIL_TOKEN="$HOME/paradise-realty/api/gmail_token.json"
# Routes commands to the agent-manager (runs agents, reports, lookups, the
# sandboxed jazzysphotos bot, etc.). Safe to run autonomously on a schedule.
export AGENTMGR_GATEWAY_TARGET="master"
# --- FULL-POWER OPT-IN (operator-only) -------------------------------------
# To route email straight to headless Claude Code with NO approval gate (full
# Mac access), the operator must flip these two lines ON themselves:
#   export AGENTMGR_GATEWAY_TARGET="claude"
#   export AGENTMGR_CLAUDE_PERMISSION_MODE="bypassPermissions"
# WARNING: this makes any command emailed from your account run immediately on
# your Mac. If your Google account is phished, that's full remote control.
# SENDGRID_API_KEY (for replies) is loaded from .env by the gateway itself.

exec .venv/bin/python -m tools.email_gateway "$@"
