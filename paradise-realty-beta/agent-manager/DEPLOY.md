# Deploy — getting Agent-Manager onto your iPhone

This is the runbook to take the app from "runs on localhost" to "installed on
your iPhone." It is the **one remaining step** — the code is built and tested
(104 tests). Budget ~15 minutes.

> **First, set expectations.** A PWA is not an App Store download. You deploy
> the Master to a public HTTPS URL, open that URL in **Safari on your iPhone**,
> and tap **Share → Add to Home Screen**. It then behaves like a native app —
> own icon, full-screen, Face ID. There is nothing to "download" from a store.

> **What deploying exposes.** Once the Master is on Cloud Run, it is an
> internet-reachable endpoint (behind IAM + passkey auth) that can run commands
> on your Mac. That is the whole point — but it is real exposure. Step 7 is the
> verification that it is actually private. Do not skip it.

---

## What runs where (read this once)

- **Master + PWA** → Cloud Run service `agentmgr-master` (public HTTPS URL).
- **echo / transform workers** → Cloud Run jobs.
- **The agentic assistant** → runs inside the **Mac agent on your Mac**, not in
  the cloud. The phone queues a goal; your Mac executes it. So the assistant
  only works while the Mac agent is running on your Mac.
- **Shared state** → Firestore.
- **Your Anthropic key** lives in **two** independent places, both yours to set:
  - on your **Mac** (`export ANTHROPIC_API_KEY=…`) — powers the assistant's brain;
  - optionally as a Cloud Run **secret** — only if you also want LLM *planning*
    in the Master.

---

## Step 0 — Prerequisites (one-time, only you can do these)

```bash
cd agent-manager

# 0a. Enable Firestore in NATIVE mode (permanent project decision).
gcloud firestore databases create --location=nam5 --project=paradise-automation
#     ^ this database does not exist yet — deployment needs it.

# 0b. Generate the approval key pair on THIS Mac (private key stays here).
python -m tools.gen_keys
#     copy the printed line:  AGENTMGR_APPROVAL_PUBKEY=...
export AGENTMGR_APPROVAL_PUBKEY='...the value it printed...'

# 0c. App-level API token secret (a long random string).
printf '%s' "$(openssl rand -hex 32)" | \
  gcloud secrets create agentmgr-api-token --data-file=- --project=paradise-automation
```

---

## Step 1–5 — Build and deploy

```bash
# 1. IAM: service accounts + Artifact Registry. Review first, then run live.
bash deploy/setup-iam.sh --dry-run
bash deploy/setup-iam.sh

# 2. Build the container image.
bash deploy/build.sh

# 3. Deploy the worker Jobs.
bash deploy/deploy-worker.sh
bash deploy/deploy-worker-transform.sh

# 4. Deploy the private Master (first pass — RP_ID not known yet).
bash deploy/deploy-master.sh

# 5. Grab the URL, then redeploy so passkeys bind to the real host.
URL=$(gcloud run services describe agentmgr-master --region=us-east1 \
        --format='value(status.url)')
echo "Master URL: $URL"
export AGENTMGR_RP_ID="${URL#https://}"
export AGENTMGR_ORIGIN="$URL"
bash deploy/deploy-master.sh          # second pass — passkeys now work
```

---

## Step 6 — Re-grant the scoped IAM bindings

`setup-iam.sh` notes this: the job/service IAM bindings need the resources to
exist, so run them now that they do:

```bash
bash deploy/setup-iam.sh    # the add-iam-policy-binding steps now succeed
```

---

## Step 7 — Verify it is private (do NOT skip)

```bash
# No public access binding:
gcloud run services get-iam-policy agentmgr-master --region=us-east1
#   -> must NOT contain allUsers or allAuthenticatedUsers

# Unauthenticated request is refused:
curl -s -o /dev/null -w '%{http_code}\n' "$URL/chat"
#   -> 403 (IAM) — good
```

Only after both checks pass should you open the URL anywhere.

---

## Step 8 — Install on your iPhone

1. Open **`$URL`** in **Safari** on your iPhone.
2. **Share → Add to Home Screen.** Now it's an app icon.
3. Open it. Under **First-time setup**, paste the admin token
   (`gcloud secrets versions access latest --secret=agentmgr-api-token`) and
   **Register this device's passkey** → Face ID.
4. Sign in with **Face ID**. Done.

---

## Step 9 — Run the Mac agent (so the assistant can act)

The chat is the agentic assistant, and it runs commands on your Mac — so your
Mac must be running the agent. On your Mac, in a terminal:

```bash
cd agent-manager
export ANTHROPIC_API_KEY='sk-ant-...your key...'     # stays in your shell
export AGENTMGR_APPROVAL_PUBKEY='...from step 0b...'
export AGENTMGR_PROJECT_ID=paradise-automation
bash deploy/run-mac-agent.sh
```

While that's running, your phone can drive your Mac. Stop it (Ctrl-C) and the
door is closed — the phone can still chat, but nothing runs on the Mac.

Read-only commands run automatically; anything that modifies the Mac pops an
approval in the app for your Face ID. Catastrophic commands always re-prompt.

---

## Honest caveats

- **The live Face ID / WebAuthn ceremony** can only be verified on the device —
  if registration fails, `AGENTMGR_RP_ID` / `AGENTMGR_ORIGIN` don't match the
  real host (step 5).
- **Cost:** Cloud Run + Cloud Build + Firestore + Anthropic API usage all bill
  to your account. The per-command LLM budget ceiling (`AGENTMGR_LLM_BUDGET_USD`)
  caps runaway spend by tripping the approval gate.
- **The OpenAI/Google providers** are best-effort and untested-by-build; the
  assistant's brain is Claude.
- This is a remote shell into your Mac. Run the Mac agent only when you want
  that, and keep the verification in step 7 honest.
