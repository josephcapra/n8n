"""Run the whole stack locally — Master + PWA + Mac agent — in one process.

The fastest way to actually use the agentic assistant on your own machine. It
wires the Master, the PWA, and the Mac agent against one shared in-memory
store, so a chat goal flows end to end:

    PWA  ->  Master  ->  assistant task  ->  Mac agent's agentic loop
         ->  Claude tool use  ->  commands on THIS Mac  ->  reply

Usage:

    export ANTHROPIC_API_KEY=sk-ant-...        # your key — stays in your shell
    python -m tools.run_local

Then open http://localhost:8080, register a passkey with the admin token shown
below, and chat. Read-only commands auto-run; anything that modifies your Mac
pops an approval in the app for your Touch ID / Face ID.

In-memory + local: nothing is deployed, nothing leaves your machine except the
LLM calls. The store is in-memory (fast, responsive under load); an operator
session is auto-opened on every startup so the terminal works with no re-login.
Stop with Ctrl-C.
"""

from __future__ import annotations

import os
import threading
import time

import uvicorn

from agent.local_agent import (
    process_assistant_task,
    process_backup_task,
    process_cfo_task,
    process_jazzysphotos_task,
    process_lead_task,
    process_security_task,
    process_task,
)
from agentmgr.approval_gate import ApprovalGate
from agentmgr.config import Config
from agentmgr.logging_utils import get_logger
from agentmgr.session import SessionManager, new_grant, sign_grant_with_token
from master.main import build_app

log = get_logger("agentmgr.run_local")

_ADMIN_TOKEN = "local-dev-token"


def main() -> int:
    # Load the consolidated connector credentials (.env) so the assistant's
    # shell commands inherit every authenticated service. A real env var
    # already set in the shell wins over the file.
    from agentmgr.connectors import load_env_file, status as connector_status

    loaded = load_env_file()
    if loaded:
        ready = [c["name"] for c in connector_status() if c["configured"]]
        log.info("loaded connector credentials",
                 extra={"vars": loaded, "connectors": ready})

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "WARNING: ANTHROPIC_API_KEY is not set — the assistant cannot "
            "think without it. Add it to agent-manager/.env or export it.\n"
        )

    # Reports go to the shared GCS bucket so cloud-job reports show up here too.
    os.environ.setdefault("AGENTMGR_REPORTS_BACKEND", "gcs")
    os.environ.setdefault("AGENTMGR_REPORTS_BUCKET", "paradise-realty-backups")
    os.environ.setdefault("AGENTMGR_REPORTS_PREFIX", "agentmgr-reports")

    cfg = Config(
        # In-memory store: instant, no per-op network round-trip — keeps the
        # Master responsive under load (Firestore made /agents etc. hang). State
        # is lost on restart, but the auto-opened operator session below is
        # recreated on every startup, so the terminal "just works" with no
        # re-login regardless. (Chat history resets on restart — acceptable; the
        # server normally runs continuously.)
        state_backend="memory",
        job_runner="local",
        api_token=_ADMIN_TOKEN,
        rp_id="localhost",
        origin="http://localhost:8080",
        approval_public_key=None,           # approvals come via the PWA passkey
        poll_interval_s=0.2,
        task_timeout_s=900.0,               # agentic loops can run a while
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        # Other LLMs for the cost-saving fast-path / tiering. Gemini Flash
        # answers trivial conversational queries off the Anthropic bill.
        google_api_key=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"),
        google_model=os.environ.get("AGENTMGR_GOOGLE_MODEL", "gemini-2.5-flash"),
        openai_api_key=os.environ.get("OPENAI_API_KEY"),
        openai_model=os.environ.get("AGENTMGR_OPENAI_MODEL", "gpt-4o-mini"),
    )
    app = build_app(cfg)
    store = app.state.store

    # The Mac agent runs in a background thread sharing the Master's store.
    session_mgr = SessionManager(
        store, cfg.approval_public_key, cfg.always_confirm_patterns,
        rp_id=cfg.rp_id, origin=cfg.origin, api_token=cfg.api_token,
    )
    gate = ApprovalGate(
        store, public_key_b64=cfg.approval_public_key,
        rp_id=cfg.rp_id, origin=cfg.origin, poll_interval_s=cfg.poll_interval_s,
    )

    # Auto-open a durable operator session so the terminal + chat "just work"
    # on this localhost (127.0.0.1) single-operator run — no manual sign-in and
    # no "approval gate not provisioned" dead-end. Persists in Firestore so it
    # survives restarts; long TTL so it never expires mid-use. Catastrophic
    # commands (always-confirm denylist) still require fresh approval.
    if session_mgr.active_grant("shell") is None:
        grant = sign_grant_with_token(
            new_grant("all", 365 * 24 * 3600.0), cfg.api_token
        )
        store.put_session(grant)
        log.info("auto-opened operator session", extra={"session_id": grant.id})

    def _process_one(task) -> None:
        if task.kind == "assistant":
            process_assistant_task(task, store, session_mgr, gate, cfg)
        elif task.kind == "security":
            process_security_task(task, store, cfg)
        elif task.kind == "lead":
            process_lead_task(task, store, cfg)
        elif task.kind == "site":
            process_jazzysphotos_task(task, store, session_mgr, gate, cfg)
        elif task.kind == "cfo":
            process_cfo_task(task, store, cfg)
        elif task.kind == "backup":
            process_backup_task(task, store, gate, cfg)
        else:
            process_task(
                task, store, session_mgr, gate,
                timeout_s=cfg.shell_command_timeout_s,
            )

    def _loop(agent_names: tuple[str, ...], label: str) -> None:
        log.info("%s worker thread started", label)
        while True:
            try:
                for agent_name in agent_names:
                    for task in store.get_pending_tasks(agent_name):
                        _process_one(task)
            except Exception:  # noqa: BLE001 - keep the thread alive
                log.exception("%s loop error; continuing", label)
            time.sleep(0.3)

    # Terminal (mac-shell) gets a DEDICATED thread so fast shell commands are
    # never blocked behind a long agentic chat/assistant task — the two ran on
    # one thread before, which made the terminal hang ("Failed to fetch") while
    # a chat was thinking. assistant + security share a second thread.
    threading.Thread(target=_loop, args=(("mac-shell",), "mac-shell"),
                     daemon=True, name="mac-shell").start()
    threading.Thread(
        target=_loop,
        args=(("assistant", "security-health", "lead-response",
               "jazzysphotos-site", "backup"), "assistant"),
        daemon=True, name="assistant").start()
    # Finance Agent (cfo) gets its own thread — its QBO + Claude runs take a minute+
    # (digest/recurring/alerts), and shouldn't block chat or the terminal.
    threading.Thread(target=_loop, args=(("cfo",), "cfo"),
                     daemon=True, name="cfo").start()

    bar = "=" * 60
    print(f"\n{bar}")
    print("  Agent-Manager running:   http://localhost:8080")
    print(f"  First-time setup token:  {_ADMIN_TOKEN}")
    print("  The chat IS the agentic assistant — it runs commands on")
    print("  THIS Mac. Read-only auto-runs; the rest asks for approval.")
    print("  Ctrl-C to stop.")
    print(f"{bar}\n")
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
