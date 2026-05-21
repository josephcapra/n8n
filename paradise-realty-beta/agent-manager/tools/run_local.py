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
LLM calls to Anthropic. Stop with Ctrl-C.
"""

from __future__ import annotations

import os
import threading
import time

import uvicorn

from agent.local_agent import process_assistant_task, process_task
from agentmgr.approval_gate import ApprovalGate
from agentmgr.config import Config
from agentmgr.logging_utils import get_logger
from agentmgr.session import SessionManager
from master.main import build_app

log = get_logger("agentmgr.run_local")

_ADMIN_TOKEN = "local-dev-token"


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "WARNING: ANTHROPIC_API_KEY is not set — the assistant cannot "
            "think without it. Set it and restart:\n"
            "  export ANTHROPIC_API_KEY=sk-ant-...\n"
        )

    cfg = Config(
        state_backend="memory",
        job_runner="local",
        api_token=_ADMIN_TOKEN,
        rp_id="localhost",
        origin="http://localhost:8080",
        approval_public_key=None,           # approvals come via the PWA passkey
        poll_interval_s=0.2,
        task_timeout_s=900.0,               # agentic loops can run a while
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
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

    def mac_agent_loop() -> None:
        log.info("local Mac agent thread started")
        while True:
            try:
                for agent_name in ("mac-shell", "assistant"):
                    for task in store.get_pending_tasks(agent_name):
                        if task.kind == "assistant":
                            process_assistant_task(task, store, session_mgr, gate, cfg)
                        else:
                            process_task(
                                task, store, session_mgr, gate,
                                timeout_s=cfg.shell_command_timeout_s,
                            )
            except Exception:  # noqa: BLE001 - keep the thread alive
                log.exception("mac agent loop error; continuing")
            time.sleep(0.3)

    threading.Thread(target=mac_agent_loop, daemon=True, name="mac-agent").start()

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
