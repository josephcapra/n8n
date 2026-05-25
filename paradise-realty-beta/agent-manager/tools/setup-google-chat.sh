#!/bin/bash
# One-command setup for the Google Chat -> agent-manager relay.
# You run this yourself (it changes YOUR Google Cloud project, on your authority):
#     bash tools/setup-google-chat.sh
# It enables APIs, creates the Pub/Sub topic + subscription, a service account +
# key, grants IAM, and installs the launchd relay. Then it prints the ~3 console
# clicks only you can do. Safe to re-run (idempotent).
set -uo pipefail

PROJECT="paradise-automation"
TOPIC="agentmgr-chat"
SUB="agentmgr-chat-sub"
SA="agentmgr-chat"
SA_EMAIL="${SA}@${PROJECT}.iam.gserviceaccount.com"
CHAT_PUSH="chat-api-push@system.gserviceaccount.com"
KEY_PATH="$HOME/paradise-realty/api/agentmgr-chat-sa.json"
PLIST="$HOME/Library/LaunchAgents/com.paradise.chatrelay.plist"
RUNNER="/Users/User/paradise-realty-beta/agent-manager/tools/run-chat-relay.sh"

say() { printf "\n\033[1m== %s\033[0m\n" "$1"; }

say "1/8 Enabling Chat + Pub/Sub APIs"
gcloud services enable chat.googleapis.com pubsub.googleapis.com --project "$PROJECT"

say "2/8 Creating Pub/Sub topic ($TOPIC)"
gcloud pubsub topics create "$TOPIC" --project "$PROJECT" 2>/dev/null \
  && echo "created" || echo "already exists — ok"

say "3/8 Creating pull subscription ($SUB)"
gcloud pubsub subscriptions create "$SUB" --topic "$TOPIC" --project "$PROJECT" \
  --ack-deadline=60 2>/dev/null && echo "created" || echo "already exists — ok"

say "4/8 Letting Google Chat publish to the topic"
gcloud pubsub topics add-iam-policy-binding "$TOPIC" --project "$PROJECT" \
  --member="serviceAccount:${CHAT_PUSH}" --role="roles/pubsub.publisher" >/dev/null \
  && echo "granted"

say "5/8 Creating relay service account ($SA_EMAIL)"
gcloud iam service-accounts create "$SA" --project "$PROJECT" \
  --display-name="Agent-Manager Chat relay" 2>/dev/null \
  && echo "created" || echo "already exists — ok"

say "6/8 Granting the relay SA permission to pull the subscription"
gcloud pubsub subscriptions add-iam-policy-binding "$SUB" --project "$PROJECT" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/pubsub.subscriber" >/dev/null \
  && echo "granted"

say "7/8 Creating the relay SA key"
if [ -f "$KEY_PATH" ]; then
  echo "key already present at $KEY_PATH — keeping it"
else
  mkdir -p "$(dirname "$KEY_PATH")"
  gcloud iam service-accounts keys create "$KEY_PATH" \
    --iam-account "$SA_EMAIL" --project "$PROJECT"
  chmod 600 "$KEY_PATH"
  echo "saved $KEY_PATH"
fi

say "8/8 Installing the launchd relay (com.paradise.chatrelay)"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.paradise.chatrelay</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>${RUNNER}</string></array>
  <key>WorkingDirectory</key><string>/Users/User/paradise-realty-beta/agent-manager</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>/tmp/agentmgr-chat-relay.log</string>
  <key>StandardErrorPath</key><string>/tmp/agentmgr-chat-relay.log</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
PLISTEOF
launchctl bootout "gui/$(id -u)/com.paradise.chatrelay" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" && echo "relay loaded (logs: /tmp/agentmgr-chat-relay.log)"

cat <<DONE

============================================================
 CLOUD SIDE DONE. Now the 3 console clicks only you can do:
============================================================
 1) Open the Chat API config page:
    https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat?project=${PROJECT}

 2) Fill it in:
      App name:     Agent Manager
      Description:  Personal command channel
      Avatar URL:   https://www.gstatic.com/images/branding/product/2x/chat_48dp.png
      [x] Enable interactive features
      Functionality: [x] Receive 1:1 messages
      Connection settings: (o) Cloud Pub/Sub
          Topic name:  projects/${PROJECT}/topics/${TOPIC}
      Visibility: make available to  joe@josephcapra.com
      App status: LIVE
    -> SAVE

 3) Open Google Chat (https://chat.google.com) -> New chat ->
    search "Agent Manager" -> send it:  hello

 Then tell Claude Code "done" and it will confirm the round-trip.
 Kill-switch:  touch ~/.agentmgr-chat-relay.disabled
============================================================
DONE
