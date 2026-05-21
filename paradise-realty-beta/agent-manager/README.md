# Agent-Manager — Private Agent-Manager Chatbot over Cloud Run Jobs

A single private chatbot — the **Master Agent / manager** — interprets your
natural-language commands, routes work to **worker agents**, brokers all
inter-agent communication, and reports back in the chat.

This directory is fully self-contained. It does not import from, or modify,
any other project in the repo, and every Cloud Run resource it creates is
newly and distinctly named (`agentmgr-*`).

> **Build status: complete through Phase 4 — 104 tests passing.** The framework
> (routing, the inter-agent hub, the approval gate, session windows), the PWA +
> passkey auth, the multi-LLM router with cost controls, and the **agentic
> assistant** — a Claude tool-use loop that runs your Mac's terminal — are all
> built and tested. Deploying to a phone-reachable HTTPS URL is the one
> remaining manual step — see **[DEPLOY.md](DEPLOY.md)**. The live Face ID
> ceremony needs on-device verification.

---

## Architecture

```
        you ──chat──▶  ┌─────────────────────────┐
                       │   Master Agent          │   Cloud Run SERVICE
                       │   (manager + hub)       │   private, authenticated
                       │  interpret → route →    │
                       │  trigger → poll → report│
                       └───────┬────────▲────────┘
                  triggers Job │        │ reads results / messages
                               ▼        │
                       ┌──────────────────────────┐
                       │  Worker agents           │  Cloud Run JOBS
                       │  (run-to-completion)     │  echo-worker, ...
                       └───────┬────────▲─────────┘
                               │        │
                               ▼        │
                       ┌──────────────────────────┐
                       │  Shared state store      │  Firestore (swappable)
                       │  conversations · tasks · │
                       │  message bus · approvals │
                       └──────────────────────────┘
```

**Hub model.** Workers never call each other. A worker writes its result (or
an intermediate message) to the shared store; the Master reads it, decides
routing, and relays. One auditable control point — one place to log, gate, and
bound everything.

| Component | Runtime | Code | Notes |
|-----------|---------|------|-------|
| Master Agent | Cloud Run **Service** (`agentmgr-master`) | `master/main.py` | Always-on, private, authenticated chat endpoint + hub |
| **PWA** | static, served by the Master | `master/static/` | Installable mobile chat app; passkey (Face ID) login |
| Echo worker | Cloud Run **Job** (`agentmgr-worker-echo`) | `worker/run.py` | Phase 1 placeholder; runs to completion, exits |
| Transform worker | Cloud Run **Job** (`agentmgr-worker-transform`) | `worker/transform.py` | Phase 2 second worker; text transforms + can relay results |
| **Mac agent** | **local daemon** on your Mac | `agent/local_agent.py` | Runs shell commands on your Mac; outbound-only poller |
| **Cloud Run admin** | **master-inline** | `agentmgr/cloudrun_admin.py` | Reads/manages Cloud Run via the Master's service account |
| Shared library | — | `agentmgr/` | State store, registry, approval gate, session, guards |
| Operator tools | your machine | `tools/` | Key generation, terminal approval, session start |

Agents declare a `runtime` in `agents.json` — `cloudrun-job` (Master triggers
it), `local-agent` (a daemon polls for its own tasks), or `master-inline` (the
Master runs it directly). The Master dispatches by runtime; adding a new agent
of any runtime is still data-only.

One Docker image is built and deployed twice: the Service runs `uvicorn`, the
Job overrides the container command to `python -m worker.run`. The shared
`agentmgr` library is therefore byte-identical across Master and workers.

### Swappable backends

Two interfaces have multiple implementations; the concrete one is chosen by a
single env var, and nothing else in the codebase imports a concrete class:

| Interface | Env var | Production | Local / tests |
|-----------|---------|------------|---------------|
| `StateStore` (`state_store.py`) | `AGENTMGR_STATE_BACKEND` | `firestore` | `memory` |
| `JobRunner` (`job_runner.py`)   | `AGENTMGR_JOB_RUNNER`   | `cloudrun` | `local` (in-process worker) |

---

## Security model

This is the part to read carefully. A chat endpoint that can trigger jobs and
(from Phase 3) route across LLMs is a high-value target.

### 1. The Master is private

Deployed with `--no-allow-unauthenticated`, so **two** layers protect it:

1. **Cloud Run IAM** — only principals with `roles/run.invoker` on the service
   can reach it at all. `deploy/setup-iam.sh` grants this to the operator only.
2. **App-level bearer token** — `master/main.py` checks an `Authorization:
   Bearer` token (from Secret Manager) on `/chat` and `/agents`. This is
   defence-in-depth: a misconfigured ingress still does not expose the service.
   It **fails closed** — if no token is provisioned, `/chat` returns `503`.

`/health` is the only unauthenticated route and reveals nothing sensitive.

> **Verify, don't assume.** "Documented" is not "secured." Before sharing the
> Master URL with anyone, run the three checks printed by `deploy-master.sh`:
> confirm no `allUsers` binding, and confirm an unauthenticated `curl` is
> rejected.

### 2. The approval gate cannot be self-approved

`agentmgr/approval_gate.py` is one standalone module. Any **SENSITIVE**
operation calls `request_approval(...)`, which **blocks** until a human
approves it — no timeout, no auto-approve, no bulk-approve.

SENSITIVE (per spec) = filesystem access outside the job's own working
directory · access to credentials / tokens / 2FA-MFA material · any Google
account API call · any spend above `AGENTMGR_SPEND_THRESHOLD_USD`.

Approval is a **public-key signature** problem:

- A human runs `tools/gen_keys.py` once, producing an Ed25519 key pair. The
  **private** key stays on the human's machine (`~/.agentmgr/approval_ed25519`,
  mode `0600`). It is **never** committed, **never** uploaded to Secret
  Manager, **never** present in any deployed environment.
- The Master and every worker hold only the **public** key. The `ApprovalGate`
  object has no field, method, or import that can produce a signature — it can
  only *verify* one.
- To approve request *X* you must sign request *X*'s unique nonce. Only the
  private-key holder can. **A fully compromised Master or worker still cannot
  forge an approval**, because the private key is not reachable from any GCP
  identity it holds.

Python code can always be monkeypatched in-process — so the guarantee is
deliberately *not* "Python is tamper-proof." It is a **key-possession
boundary** enforced by IAM and process isolation. `tests/test_approval_gate.py`
asserts the real properties: the gate holds no signing capability, forged and
wrong-key signatures never satisfy it, no env var bypasses it, and it fails
closed with no key. (Hard security requirement #3.)

Because a deployed Cloud Run Service/Job has no interactive terminal, you
approve from **your own** terminal: `python -m tools.approve` lists pending
requests and signs your decision. That is the "type an approval code in the
terminal" step — it is the single interactive control point in the system.

### 3. Google account integration is OUT OF SCOPE

`agentmgr/integrations/google.py` stubs Gmail / Drive / Calendar as interfaces
that raise `NotImplementedError`. Real OAuth wiring is deferred to a later,
manually reviewed phase so credential handling gets dedicated review.

### 4. Least-privilege IAM

Created by `deploy/setup-iam.sh`:

| Identity | Roles | Scope |
|----------|-------|-------|
| `agentmgr-master@…` (Master SA) | `roles/datastore.user` | project (Firestore) |
| | `roles/run.invoker` | the `agentmgr-worker-echo` **job only** |
| | `roles/secretmanager.secretAccessor` | the `agentmgr-api-token` **secret only** |
| `agentmgr-worker@…` (Worker SA) | `roles/datastore.user` | project (Firestore) |
| operator (`user:…`) | `roles/run.invoker` | the `agentmgr-master` **service only** |

Neither service account can read the approval private key — it is not in GCP
at all. The Master cannot read any secret other than its own API token.

### 5. Cross-cutting safety

- **Loop / fan-out guard** (`fanout_guard.py`) — bounds both task fan-out
  *depth* and per-command *job count* (`check_task`, enforced in Phase 1) and
  message **relay depth** (`check_relay`, the bound that stops
  A→Master→B→Master→A cycles; implemented now, wired into the relay path in
  Phase 2).
- **Timeouts + bounded retries** (`util.py`) — every Job trigger is retried
  with backoff; every result poll is bounded by `AGENTMGR_TASK_TIMEOUT_S`.
- **Structured JSON logs** (`logging_utils.py`) — one correlation ID per
  top-level command, stamped on every log line across Master and workers, so a
  command is traceable end to end.

---

## The Mac agent & session windows (Phase 1.5)

The `mac-shell` agent runs shell commands on **your Mac**. Be clear-eyed about
what that is: a remote shell into your laptop, reachable (behind auth) from the
chat endpoint. The design keeps it safe:

- **Outbound-only.** `agent/local_agent.py` opens *no* inbound port. It polls
  the shared state store for tasks addressed to `mac-shell`, runs them as your
  user, writes results back. If the agent is not running, nothing runs.
- **Session windows.** You approve a *window*, not every command. A
  `SessionGrant` is **signed** by your approval key with a hard, signed expiry
  (`AGENTMGR_SESSION_TTL_S`, default 15 min). It cannot be forged, cannot be
  extended, and is revocable instantly. While a window is open, commands in its
  scope run without re-prompting.
- **Always-confirm denylist.** Catastrophic commands (`rm -rf /`,
  `diskutil erase`, …) require a fresh approval **even mid-session**
  (`AGENTMGR_ALWAYS_CONFIRM`).
- **Every command is logged** with its correlation ID.

```bash
# 1. open a 15-minute shell session window (signs a grant with your key)
python -m tools.start_session shell

# 2. run the Mac agent (in its own terminal; Ctrl-C stops it)
bash deploy/run-mac-agent.sh

# 3. from chat, route a command to the Mac:
#      "$ git status"        or   "run: ls -la"     or   "gcloud run services list"
# revoke a window early:  POST /sessions/<id>/revoke  on the Master
```

**Cloud Run control** works two ways (you chose *both*): path A — `gcloud run …`
is just a shell command the Mac agent runs; path B — `cloudrun-admin` is a
`master-inline` agent that reads/manages Cloud Run via the Master's own service
account, so `"cloud run, list jobs"` works even when your Mac is off. Read ops
are benign; mutating ops (running a Job) route through the approval gate.

---

## The phone app — PWA + passkey (Phase 1.5 Increment 2)

The Master serves an installable Progressive Web App (`master/static/`). On
your iPhone, open the Master URL in Safari and **Share → Add to Home Screen**;
it then behaves like a native app (its own icon, standalone window).

**Passkey login.** First-time setup: in the app's *First-time setup* panel,
enter the admin token (`AGENTMGR_API_TOKEN`) once and register a passkey — iOS
stores it in the Secure Enclave. After that, every sign-in is just **Face ID**.
A successful login mints a short-lived PWA bearer token (`AGENTMGR_PWA_TOKEN_TTL_S`).

**Face ID approves.** When the Master blocks on a SENSITIVE operation, the app
shows an approval banner. Tapping *Approve* triggers a fresh WebAuthn assertion
(Face ID) whose challenge is bound to that exact request. The same is true for
opening a session window. The assertion is verified by the Master **and
independently re-verified by the gate** in the Mac agent — a compromised Master
cannot fake one, because producing an assertion needs the Secure Enclave key on
your phone.

The Ed25519 laptop key (`tools/approve.py`, `tools/start_session.py`) still
works in parallel — a fallback approver when you don't have your phone.

### Verifying the passkey flow

The WebAuthn ceremony involves your iPhone's authenticator, so it cannot be
exercised by automated tests (the 60-test suite covers everything around it —
options, challenge lifecycle, token, endpoint wiring, fail-closed paths). After
deploying, verify on-device:

1. `AGENTMGR_RP_ID` / `AGENTMGR_ORIGIN` **must** match the Master's real
   HTTPS host (e.g. `agentmgr-master-xxxx.us-east1.run.app`) — WebAuthn checks
   them exactly. Passkeys do not work over plain HTTP except on `localhost`.
2. Register a passkey, sign out, sign back in with Face ID.
3. From chat, run `$ echo hi` with no session open — confirm the approval
   banner appears and Face ID approval lets it run.

---

## Routing & the message hub (Phase 2)

A command is decomposed by a swappable **planner** (`agentmgr/planner.py`) into
one or more subtasks, each routed to an agent by the registry. The Phase 2
`RuleBasedPlanner` understands this grammar (Phase 3 swaps in an LLM planner
behind the same interface):

| Syntax | Routed to |
|--------|-----------|
| `a \| b \| c` | a **pipeline** — each stage consumes the previous stage's output |
| `$ cmd` · `run: cmd` · `gcloud …` | the Mac shell agent |
| `cloud run …` | the Cloud Run admin (Master-inline) |
| `transform: <op> <text> [> <agent>]` | the transform worker; `> <agent>` relays the result on |
| `!<segment>` | forces the subtask SENSITIVE |
| anything else | the echo worker |

**The hub.** Workers never call each other. A worker writes a `Message` to the
shared bus; the Master picks it up and relays it as a follow-up task to the
target agent. Pipelines work the same way — the Master relays stage *n*'s
output into stage *n+1*.

**Two independent bounds — both enforced on every hop.** This is the part the
brief flagged: once messages relay through the Master, `A→Master→B→Master→A`
cycles are possible.

- `check_task` bounds **job fan-out** — total jobs per command
  (`AGENTMGR_MAX_JOBS_PER_COMMAND`).
- `check_relay` bounds **relay depth** — how many Master hops a chain may take
  (`AGENTMGR_MAX_FANOUT_DEPTH`). A self-relaying worker is stopped here, e.g.
  `transform: upper x > transform-worker` runs 4 jobs then halts with
  *"relay bounded at depth 4"*.

SENSITIVE Cloud Run subtasks block on the approval gate **before** the Job is
triggered. (Mac-agent and Master-inline subtasks self-gate, so they are not
double-prompted.)

---

## Multi-LLM routing & cost controls (Phase 3)

`agentmgr/llm.py` is a provider-agnostic LLM router. The Master can send
reasoning — currently command planning — to **Anthropic, OpenAI, or Google**,
chosen by `AGENTMGR_LLM_PROVIDER`.

- **`LLMPlanner`** — set `AGENTMGR_PLANNER=llm` and the planner decomposes
  commands with the LLM instead of the rule grammar. It **falls back to the
  rule-based planner on any failure** (missing key, network error, budget
  denial, unparseable output) — planning never hard-fails.
- **Anthropic provider** — built with the official `anthropic` SDK to the
  `claude-api` skill's spec: model `claude-opus-4-7`, adaptive thinking, and
  prompt caching on the stable system prefix.
- **OpenAI / Google providers** — official SDKs, lazy-imported. They are
  best-effort: the call shape cannot be exercised here without live keys, so
  verify it against your chosen model before relying on them. Install with
  `pip install openai google-genai`.
- **Keys** come only from the environment (`ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`, `GOOGLE_API_KEY`) — injected from Secret Manager at deploy,
  never in code.

**Cost controls.** Every call is cost-logged — per request and cumulatively per
command (`GET /cost` shows the running total). A per-command **budget ceiling**
(`AGENTMGR_LLM_BUDGET_USD`) trips the **approval gate**: once a command's
cumulative LLM spend reaches the ceiling, the next call BLOCKS until a human
signs off — the same gate, the same signatures, as every other SENSITIVE
operation.

The default provider is `mock` (deterministic, no network, no keys), so the
system runs end to end with nothing configured; the LLM path is opt-in.

---

## The agentic assistant (Phase 4)

Plain chat is the **agentic assistant** (`agentmgr/assistant.py`) — a Claude
tool-use loop. You give it a natural-language goal; it runs one shell command,
reads the output, decides the next command, and loops until done — your own
Claude Code, drivable from your phone.

The loop runs inside the **Mac agent** (it has the shell), with Claude as the
brain via the official `anthropic` SDK. It is bounded by
`AGENTMGR_ASSISTANT_MAX_STEPS` and every command is audit-logged.

**Gating — minimal permissions, approval before major decisions**
(`agentmgr/command_policy.py`):

- **Read-only commands auto-run** — `ls`, `cat`, `git status`, `grep`,
  `gcloud … list`, etc. No friction.
- **Everything else blocks on the approval gate** — writes, deletes, installs,
  `sudo`, chained/redirected commands, anything not provably safe. You approve
  with Face ID in the app.
- Catastrophic patterns always re-prompt, even mid-session.

A denied command isn't fatal — the assistant is told and adapts. The loop
(`run_agentic_loop`) is provider-agnostic behind an `AgenticDriver`; Claude is
the production driver, `ScriptedDriver` backs the tests.

`tools/run_local.py` runs the Master, PWA, and Mac agent together in one
process for local use — `export ANTHROPIC_API_KEY=…` then
`python -m tools.run_local`.

---

## Testing

### Local — no GCP access required

```bash
cd agent-manager
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

.venv/bin/python -m pytest                 # 104 tests: gate, store, registry, guard,
                                            # session, Mac agent, passkey, planner,
                                            # relay/hub, LLM router, command policy,
                                            # agentic assistant, e2e

# Use the whole stack locally — Master + PWA + Mac agent in one process:
export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/python -m tools.run_local         # then open http://localhost:8080
.venv/bin/python -m tools.local_demo "kick off the brevard launch"
```

`local_demo` builds the Master on the in-memory store with the in-process job
runner and runs one full command: authenticated chat → interpretation → task
dispatch → worker → typed result → reported reply. It also shows an
unauthenticated call being rejected with `401`.

### Deployed — end-to-end on Cloud Run

After [deploying](#deploying) the worker and Master:

```bash
URL=$(gcloud run services describe agentmgr-master --region=us-east1 \
        --format='value(status.url)')

# IAM layer: identity token. App layer: the bearer token from Secret Manager.
TOKEN=$(gcloud secrets versions access latest --secret=agentmgr-api-token)

curl -s -X POST "$URL/chat" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -H "Content-Type: application/json" \
  -d '{"message":"ping the echo worker"}'
```

Note: `/chat` authenticates with the **app token**, while Cloud Run IAM
authenticates with the **identity token**. For a deployed test, send the
identity token to pass IAM, and configure the client to also send the app
token. (Phase 1 deployments are expected to be exercised mainly via the local
harness; the app token is the value `/chat` checks.)

Watch a command flow through every agent:

```bash
gcloud logging read \
  'resource.type=cloud_run_revision AND jsonPayload.correlation_id="cmd_…"' \
  --project=paradise-automation --freshness=1h
```

---

## Deploying

> Review every script before running. They mirror the repo's cautious deploy
> style — each supports `--dry-run`. **Do not run them automatically.**

```bash
cd agent-manager

# 0. one-time: generate the approval key pair on YOUR machine
python -m tools.gen_keys
export AGENTMGR_APPROVAL_PUBKEY='…printed by gen_keys…'

# 1. one-time: create the app-token secret (value = a long random string)
printf '%s' "$(openssl rand -hex 32)" | \
  gcloud secrets create agentmgr-api-token --data-file=- --project=paradise-automation

# 2. provision IAM + Artifact Registry  (re-run job/service bindings after step 4–5)
bash deploy/setup-iam.sh --dry-run     # then without --dry-run

# 3. build the single image
bash deploy/build.sh

# 4. deploy the worker Job
bash deploy/deploy-worker.sh

# 5. deploy the private Master Service
bash deploy/deploy-master.sh
```

### Manual steps you must perform

1. **Enable a Firestore database** in native mode in `paradise-automation`
   (region `us-east1`) if one does not already exist. The state store uses
   collections prefixed `agentmgr_`, so it will not collide with other data.
2. **Generate the approval key pair** (`tools/gen_keys.py`) and keep the
   private key on your machine only.
3. **Create the `agentmgr-api-token` secret** with a strong random value.
4. **Run `setup-iam.sh`**, then re-run its two `add-iam-policy-binding` steps
   after the job and service exist.
5. **Verify the Master is private** using the checks `deploy-master.sh` prints
   before you share its URL or token.
6. Ensure `gcloud builds` / Cloud Build is enabled for the project.
7. To use the `mac-shell` agent: run `deploy/run-mac-agent.sh` on your Mac
   (it needs gcloud application-default credentials for Firestore access).
   Keep it running only while you want remote shell access; stopping it
   closes the door completely.

---

## Extending the registry

Adding a worker agent is **data-only** — no Master code changes:

1. Append an entry to `agentmgr/agents.json` (`name`, `kind`, `job_name`,
   `region`, `description`, `capabilities`, `sensitive_default`).
2. Add the worker's code (a module like `worker/run.py`) and deploy a Cloud
   Run Job whose name matches `job_name`.
3. The Master's `AgentRegistry` picks it up on next start. Phase 2's
   interpreter routes to it by capability.

---

## Configuration

All configuration is environment variables (`agentmgr/config.py`). Nothing
secret is hardcoded.

| Variable | Default | Purpose |
|----------|---------|---------|
| `AGENTMGR_PROJECT_ID` | `paradise-automation` | GCP project |
| `AGENTMGR_REGION` | `us-east1` | Cloud Run region |
| `AGENTMGR_STATE_BACKEND` | `firestore` | `firestore` \| `memory` |
| `AGENTMGR_JOB_RUNNER` | `cloudrun` | `cloudrun` \| `local` |
| `AGENTMGR_API_TOKEN` | — | app-level bearer token (from Secret Manager) |
| `AGENTMGR_APPROVAL_PUBKEY` | — | base64 Ed25519 **public** key (not secret) |
| `AGENTMGR_APPROVAL_CHANNEL` | `store` | `store` \| `stdin` |
| `AGENTMGR_SPEND_THRESHOLD_USD` | `5` | spend above this is SENSITIVE |
| `AGENTMGR_MAX_FANOUT_DEPTH` | `3` | task fan-out / relay depth bound |
| `AGENTMGR_MAX_JOBS_PER_COMMAND` | `10` | per-command job-count bound |
| `AGENTMGR_TASK_TIMEOUT_S` | `120` | bound on the Master's result poll |
| `AGENTMGR_SESSION_TTL_S` | `900` | default session-window length (seconds) |
| `AGENTMGR_ALWAYS_CONFIRM` | built-in list | comma-separated catastrophic-command patterns that always re-prompt |
| `AGENTMGR_SHELL_TIMEOUT_S` | `300` | per-command hard timeout on the Mac agent |
| `AGENTMGR_LOCAL_AGENT_POLL_S` | `2` | how often the Mac agent polls for tasks |
| `AGENTMGR_RP_ID` | `localhost` | WebAuthn Relying Party ID = the Master's hostname |
| `AGENTMGR_ORIGIN` | `http://localhost:8080` | exact PWA origin (scheme + host) |
| `AGENTMGR_RP_NAME` | `Agent-Manager` | display name shown during passkey registration |
| `AGENTMGR_PWA_TOKEN_TTL_S` | `3600` | lifetime of the PWA bearer token from a passkey login |
| `AGENTMGR_PLANNER` | `rule` | command planner — `rule` or `llm` |
| `AGENTMGR_LLM_PROVIDER` | `mock` | `anthropic` \| `openai` \| `google` \| `mock` |
| `AGENTMGR_ANTHROPIC_MODEL` | `claude-opus-4-7` | Anthropic model id |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_API_KEY` | — | provider keys (from Secret Manager — never in code) |
| `AGENTMGR_LLM_BUDGET_USD` | `1.0` | per-command LLM spend ceiling; exceeding it trips the approval gate |
| `AGENTMGR_ASSISTANT_MAX_STEPS` | `25` | max LLM-driven command steps per goal |
| `AGENTMGR_ASSISTANT_GATE` | `allowlist` | how the assistant's commands are gated |

The deploy image runs on `python:3.11-slim` (matching the repo). Dependency
versions in `requirements.txt` are pinned to releases that ship wheels for
both 3.11 and 3.13, so local testing works on a 3.13 interpreter too.

---

## Roadmap

- **Phase 1 — done.** Master shell + one worker Job, authenticated chat,
  interpretation echo, single-worker dispatch, typed task/result, poll-and-
  report, swappable state store + job runner, standalone approval gate with
  bypass-resistance tests, fan-out guard, structured logging, deploy scripts.
- **Phase 1.5 Increment 1 — done.** The Mac shell agent (outbound-only),
  signed session windows with always-confirm denylist, runtime-aware dispatch
  (`cloudrun-job` / `local-agent` / `master-inline`), Cloud Run control via
  both paths, `/sessions` endpoints, `tools/start_session`, `run-mac-agent.sh`.
- **Phase 1.5 Increment 2 — done.** Installable PWA chat app served by the
  Master; WebAuthn passkey registration + login (Face ID); passkey-signed
  approvals and session windows, bound per-request and re-verified by the gate;
  PWA bearer tokens; service worker + manifest.
- **Phase 2 — done.** Swappable command planner with pipeline + capability
  routing; a second worker (`transform-worker`); the inter-agent message hub
  (workers relay through the Master, never directly); SENSITIVE Cloud Run
  subtasks gated before the Job triggers; job fan-out AND relay-depth bounds
  enforced on every hop.
- **Phase 3 — done.** Provider-agnostic LLM router (Anthropic / OpenAI /
  Google by config, keys from Secret Manager); `LLMPlanner` with graceful
  fallback to the rule planner; per-request and cumulative cost logging
  (`GET /cost`); per-command budget ceiling that trips the approval gate.
- **Phase 4 — done.** The agentic assistant — a Claude tool-use loop
  (`assistant.py`) that takes a natural-language goal and runs your Mac's
  terminal to accomplish it, deciding each command from the last one's output.
  Command-safety policy (`command_policy.py`): read-only commands auto-run,
  anything that mutates / chains / escalates blocks on the approval gate.
  Plain chat now routes to the assistant; `tools/run_local.py` runs the whole
  stack locally in one process.

Possible next steps: streaming the assistant's progress to the PWA live
(instead of one final reply), and provider-native agentic loops for OpenAI and
Gemini (the assistant's brain is Claude today).
