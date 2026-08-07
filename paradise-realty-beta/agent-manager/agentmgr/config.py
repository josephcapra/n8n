"""Central configuration, sourced entirely from environment variables.

Nothing secret is hardcoded. Secrets (the app-level API token) are injected
as env vars at deploy time from Secret Manager. The approval *private* key is
NEVER read here or in any deployed process — see ``approval_gate``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(key: str, default: str | None = None) -> str | None:
    val = os.environ.get(key)
    return val if val not in (None, "") else default


@dataclass(frozen=True)
class Config:
    # --- GCP placement -------------------------------------------------
    project_id: str = "paradise-automation"
    region: str = "us-east1"

    # --- Resource names (NEW resources, distinct from anything existing)
    master_service: str = "agentmgr-master"
    worker_job: str = "agentmgr-worker-echo"
    artifact_repo: str = "agent-manager"

    # --- Backends (swappable) -----------------------------------------
    state_backend: str = "firestore"        # "firestore" | "memory"
    job_runner: str = "cloudrun"            # "cloudrun"   | "local"
    collection_prefix: str = "agentmgr_"    # namespaces our Firestore docs

    # --- Auth ----------------------------------------------------------
    api_token: str | None = None            # app-level bearer token (Secret Manager)

    # --- Approval gate -------------------------------------------------
    approval_public_key: str | None = None  # base64 Ed25519 PUBLIC key (not secret)
    approval_channel: str = "store"         # "store" | "stdin"

    # --- Controls / limits --------------------------------------------
    spend_threshold_usd: float = 5.0
    max_fanout_depth: int = 3
    max_jobs_per_command: int = 10
    poll_interval_s: float = 1.0
    task_timeout_s: float = 120.0
    trigger_retry_attempts: int = 3

    # --- session window (Phase 1.5) -----------------------------------
    session_ttl_s: float = 900.0            # default 15 min hard expiry
    local_agent_name: str = "mac-shell"     # registry name of the Mac agent
    local_agent_poll_s: float = 2.0         # how often the Mac agent polls
    shell_command_timeout_s: float = 300.0  # per-command hard timeout
    # Office-Lead CRM agent: dir holding the Node scripts (office-leads/*).
    crm_project_dir: str = "/Users/User/paradise-crm-audit"
    crm_task_timeout_s: float = 1200.0      # CRM pulls/cleanups can run minutes
    # jazzysphotos-site agent: the local Astro git repo it edits + publishes.
    jazzysphotos_dir: str = "/Users/User/jazzysphotos"
    jazzysphotos_timeout_s: float = 600.0   # npm build + git push can run minutes
    # listing-report agent: the spark/ dir holding the Zillow scraper + report scripts.
    listing_report_dir: str = "/Users/User/paradise-realty/spark"
    listing_report_timeout_s: float = 1800.0  # headed Zillow scrape + gen + email
    # zoom-insights agent: dir holding the Zoom transcript pull + analysis scripts.
    zoom_insights_dir: str = "/Users/User/zoom-insights"
    zoom_insights_timeout_s: float = 1800.0  # transcript pull + Claude analysis + email
    # cfo agent: dir holding the QuickBooks pull + Claude analysis + emailer.
    cfo_dir: str = "/Users/User/quickbooks-cfo"
    cfo_timeout_s: float = 600.0             # QBO report pulls + Claude digest + email
    # backup agent: dir holding backup-agents.sh + restore-agents.sh (tar all
    # agents -> gs://paradise-agents-backup; restore the latest on demand).
    backup_dir: str = "/Users/User/paradise-realty"
    backup_timeout_s: float = 1800.0         # tar + GCS upload (or download + extract) of all agents
    # youtube-upload agent: dir holding youtube_auth.js + upload_video.js and
    # the uploads/<video>/ working dirs (video.json + optional captions.srt).
    youtube_dir: str = "/Users/User/youtube-auth"
    youtube_timeout_s: float = 7200.0        # resumable multi-GB upload + captions
    # brokermint-pipeline agent: dir holding bm_pipeline.js (pulls pending deals
    # -> commission + closing date via the logged-in Brokermint session).
    brokermint_pipeline_dir: str = "/Users/User/paperless-tc"
    brokermint_pipeline_timeout_s: float = 900.0   # headed login + API pull
    # transaction-coordinator agent (umbrella): dir holding run_tc.sh — pulls
    # Paperless Pipeline + Brokermint, reconciles them, emails the top-priority
    # digest, and drafts co-op recruiting thank-you cards.
    transaction_coordinator_dir: str = "/Users/User/paperless-tc"
    transaction_coordinator_timeout_s: float = 1200.0  # 2 web pulls + analyze + email
    # Catastrophic commands that ALWAYS need fresh approval, even mid-session.
    always_confirm_patterns: tuple[str, ...] = (
        "rm -rf /", "rm -rf ~", "rm -rf *", "mkfs", "dd if=", "diskutil erase",
        ":(){", "> /dev/", "shutdown", "reboot", "sudo rm", "chmod -r 777 /",
    )

    # --- passkey / PWA (Phase 1.5 Increment 2) --------------------------
    rp_id: str = "localhost"                # WebAuthn Relying Party ID = host
    rp_name: str = "Agent-Manager"
    origin: str = "http://localhost:8080"   # exact PWA origin (scheme+host[:port])
    pwa_token_ttl_s: float = 3600.0         # PWA bearer token lifetime

    # --- routing (Phase 2) ---------------------------------------------
    planner: str = "rule"                   # "rule" | "llm"

    # --- LLM router (Phase 3) ------------------------------------------
    llm_provider: str = "mock"              # "anthropic" | "openai" | "google" | "mock"
    anthropic_model: str = "claude-sonnet-4-6"
    openai_model: str = "gpt-4o-mini"        # cheap default; override per account
    google_model: str = "gemini-2.5-flash"   # cheap default for the chat fast-path
    anthropic_api_key: str | None = None    # from Secret Manager — never in code
    openai_api_key: str | None = None
    google_api_key: str | None = None
    llm_budget_usd: float = 1.0             # per-command spend ceiling
    llm_max_tokens: int = 4096
    # Optional price override (USD per 1M tokens) for models not in the
    # built-in pricing table — e.g. the OpenAI / Google model you choose.
    llm_price_in_per_mtok: float = 0.0
    llm_price_out_per_mtok: float = 0.0

    # --- agentic assistant (Phase 4) -----------------------------------
    assistant_max_steps: int = 25           # max LLM-driven command steps per goal
    assistant_gate: str = "allowlist"       # "allowlist" | "approve-each" | "session"

    # --- persistent long-term memory -----------------------------------
    # A JSON file of durable facts the assistant remembers across sessions.
    # Lives on disk so it survives restarts regardless of the state backend.
    memory_path: str = "~/.agentmgr-memory/memory.json"
    memory_inject_limit: int = 50    # most-recent memories injected per turn

    # --- desktop password login (no biometric) -------------------------
    # When enabled, signing in with the api_token as a password also opens an
    # 'all'-scope session window signed by the token, so writes/deletes run
    # without per-command Face ID. Trades the phishing-resistant passkey for a
    # shared secret — fine for a single-operator localhost run; on the public
    # Cloud Run deploy, leave it off and keep passkeys. Phones keep Face ID
    # either way (they just use the passkey login instead of this path).
    allow_password_login: bool = True
    password_session_ttl_s: float = 43200.0   # 12h working window after password login


def load_config() -> Config:
    """Build a Config from the current environment (read fresh each call)."""
    return Config(
        project_id=_env("AGENTMGR_PROJECT_ID", "paradise-automation"),
        region=_env("AGENTMGR_REGION", "us-east1"),
        master_service=_env("AGENTMGR_MASTER_SERVICE", "agentmgr-master"),
        worker_job=_env("AGENTMGR_WORKER_JOB", "agentmgr-worker-echo"),
        artifact_repo=_env("AGENTMGR_ARTIFACT_REPO", "agent-manager"),
        state_backend=_env("AGENTMGR_STATE_BACKEND", "firestore"),
        job_runner=_env("AGENTMGR_JOB_RUNNER", "cloudrun"),
        collection_prefix=_env("AGENTMGR_COLLECTION_PREFIX", "agentmgr_"),
        api_token=_env("AGENTMGR_API_TOKEN"),
        approval_public_key=_env("AGENTMGR_APPROVAL_PUBKEY"),
        approval_channel=_env("AGENTMGR_APPROVAL_CHANNEL", "store"),
        spend_threshold_usd=float(_env("AGENTMGR_SPEND_THRESHOLD_USD", "5")),
        max_fanout_depth=int(_env("AGENTMGR_MAX_FANOUT_DEPTH", "3")),
        max_jobs_per_command=int(_env("AGENTMGR_MAX_JOBS_PER_COMMAND", "10")),
        poll_interval_s=float(_env("AGENTMGR_POLL_INTERVAL_S", "1.0")),
        task_timeout_s=float(_env("AGENTMGR_TASK_TIMEOUT_S", "120")),
        trigger_retry_attempts=int(_env("AGENTMGR_TRIGGER_RETRY_ATTEMPTS", "3")),
        session_ttl_s=float(_env("AGENTMGR_SESSION_TTL_S", "900")),
        local_agent_name=_env("AGENTMGR_LOCAL_AGENT_NAME", "mac-shell"),
        local_agent_poll_s=float(_env("AGENTMGR_LOCAL_AGENT_POLL_S", "2")),
        shell_command_timeout_s=float(_env("AGENTMGR_SHELL_TIMEOUT_S", "300")),
        crm_project_dir=_env("AGENTMGR_CRM_DIR", "/Users/User/paradise-crm-audit"),
        crm_task_timeout_s=float(_env("AGENTMGR_CRM_TIMEOUT_S", "1200")),
        jazzysphotos_dir=_env("AGENTMGR_JAZZY_DIR", "/Users/User/jazzysphotos"),
        jazzysphotos_timeout_s=float(_env("AGENTMGR_JAZZY_TIMEOUT_S", "600")),
        listing_report_dir=_env("AGENTMGR_LISTING_REPORT_DIR", "/Users/User/paradise-realty/spark"),
        listing_report_timeout_s=float(_env("AGENTMGR_LISTING_REPORT_TIMEOUT_S", "1800")),
        zoom_insights_dir=_env("AGENTMGR_ZOOM_INSIGHTS_DIR", "/Users/User/zoom-insights"),
        zoom_insights_timeout_s=float(_env("AGENTMGR_ZOOM_INSIGHTS_TIMEOUT_S", "1800")),
        cfo_dir=_env("AGENTMGR_CFO_DIR", "/Users/User/quickbooks-cfo"),
        cfo_timeout_s=float(_env("AGENTMGR_CFO_TIMEOUT_S", "600")),
        backup_dir=_env("AGENTMGR_BACKUP_DIR", "/Users/User/paradise-realty"),
        backup_timeout_s=float(_env("AGENTMGR_BACKUP_TIMEOUT_S", "1800")),
        youtube_dir=_env("AGENTMGR_YOUTUBE_DIR", "/Users/User/youtube-auth"),
        youtube_timeout_s=float(_env("AGENTMGR_YOUTUBE_TIMEOUT_S", "7200")),
        brokermint_pipeline_dir=_env("AGENTMGR_BROKERMINT_PIPELINE_DIR", "/Users/User/paperless-tc"),
        brokermint_pipeline_timeout_s=float(_env("AGENTMGR_BROKERMINT_PIPELINE_TIMEOUT_S", "900")),
        transaction_coordinator_dir=_env("AGENTMGR_TC_DIR", "/Users/User/paperless-tc"),
        transaction_coordinator_timeout_s=float(_env("AGENTMGR_TC_TIMEOUT_S", "1200")),
        always_confirm_patterns=tuple(
            p.strip()
            for p in (_env("AGENTMGR_ALWAYS_CONFIRM") or "").split(",")
            if p.strip()
        )
        or Config.always_confirm_patterns,
        rp_id=_env("AGENTMGR_RP_ID", "localhost"),
        rp_name=_env("AGENTMGR_RP_NAME", "Agent-Manager"),
        origin=_env("AGENTMGR_ORIGIN", "http://localhost:8080"),
        pwa_token_ttl_s=float(_env("AGENTMGR_PWA_TOKEN_TTL_S", "3600")),
        planner=_env("AGENTMGR_PLANNER", "rule"),
        llm_provider=_env("AGENTMGR_LLM_PROVIDER", "mock"),
        anthropic_model=_env("AGENTMGR_ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        openai_model=_env("AGENTMGR_OPENAI_MODEL", "gpt-4o-mini"),
        google_model=_env("AGENTMGR_GOOGLE_MODEL", "gemini-2.5-flash"),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        openai_api_key=_env("OPENAI_API_KEY"),
        google_api_key=_env("GOOGLE_API_KEY") or _env("GEMINI_API_KEY"),
        llm_budget_usd=float(_env("AGENTMGR_LLM_BUDGET_USD", "1.0")),
        llm_max_tokens=int(_env("AGENTMGR_LLM_MAX_TOKENS", "4096")),
        llm_price_in_per_mtok=float(_env("AGENTMGR_LLM_PRICE_IN", "0")),
        llm_price_out_per_mtok=float(_env("AGENTMGR_LLM_PRICE_OUT", "0")),
        assistant_max_steps=int(_env("AGENTMGR_ASSISTANT_MAX_STEPS", "25")),
        assistant_gate=_env("AGENTMGR_ASSISTANT_GATE", "allowlist"),
        allow_password_login=_env("AGENTMGR_PASSWORD_LOGIN", "true").lower()
        not in ("0", "false", "no", ""),
        password_session_ttl_s=float(_env("AGENTMGR_PASSWORD_SESSION_TTL_S", "43200")),
        memory_path=_env("AGENTMGR_MEMORY_PATH", "~/.agentmgr-memory/memory.json"),
        memory_inject_limit=int(_env("AGENTMGR_MEMORY_INJECT_LIMIT", "50")),
    )
