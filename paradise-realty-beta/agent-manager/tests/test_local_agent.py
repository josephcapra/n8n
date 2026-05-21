"""Mac local agent — gating + execution of shell tasks."""

from __future__ import annotations

import base64
import threading
import time

from agentmgr.approval_gate import ApprovalGate, canonical_message
from agentmgr.config import Config
from agentmgr.schemas import ApprovalResponse, TaskSpec, TaskStatus
from agentmgr.session import (
    SessionManager,
    canonical_session_message,
    new_grant,
    sign_grant_with_token,
)
from agent.local_agent import _assistant_shell_runner, process_task

_ALWAYS = Config().always_confirm_patterns


def _shell_task(command: str) -> TaskSpec:
    return TaskSpec(
        agent="mac-shell",
        kind="shell",
        payload={"command": command},
        conversation_id="conv_1",
        correlation_id="cmd_1",
    )


def _open_session(store, private_key, scope="shell"):
    grant = new_grant(scope, 900)
    grant.signature_b64 = base64.b64encode(
        private_key.sign(canonical_session_message(grant))
    ).decode()
    store.put_session(grant)


def _mgr_gate(store, pub):
    return (
        SessionManager(store, pub, _ALWAYS),
        ApprovalGate(store, public_key_b64=pub, poll_interval_s=0.02),
    )


def test_runs_under_active_session(store, keypair):
    private_key, pub = keypair
    _open_session(store, private_key)
    mgr, gate = _mgr_gate(store, pub)
    task = _shell_task("echo phase15-ok")
    store.put_task(task)
    result = process_task(task, store, mgr, gate, timeout_s=10)
    assert result.status == TaskStatus.COMPLETED
    assert "phase15-ok" in result.output["stdout"]
    assert result.output["exit_code"] == 0


def test_failed_command_recorded(store, keypair):
    private_key, pub = keypair
    _open_session(store, private_key)
    mgr, gate = _mgr_gate(store, pub)
    task = _shell_task("exit 3")
    store.put_task(task)
    result = process_task(task, store, mgr, gate, timeout_s=10)
    assert result.status == TaskStatus.FAILED
    assert result.output["exit_code"] == 3


def test_empty_command_fails_fast(store, keypair):
    _, pub = keypair
    mgr, gate = _mgr_gate(store, pub)
    task = _shell_task("   ")
    store.put_task(task)
    result = process_task(task, store, mgr, gate, timeout_s=10)
    assert result.status == TaskStatus.FAILED


def test_no_session_blocks_until_approved(store, keypair):
    """With no session window, the command blocks on per-command approval."""
    private_key, pub = keypair
    mgr, gate = _mgr_gate(store, pub)
    task = _shell_task("echo approved-run")
    store.put_task(task)

    box: dict = {}
    thread = threading.Thread(
        target=lambda: box.update(
            result=process_task(task, store, mgr, gate, timeout_s=10)
        ),
        daemon=True,
    )
    thread.start()

    appr = None
    for _ in range(500):
        pending = store.list_pending_approvals()
        if pending:
            appr = pending[0]
            break
        time.sleep(0.01)
    assert appr is not None, "command should block on a pending approval"
    assert "result" not in box

    signature = private_key.sign(canonical_message(appr.id, appr.nonce, "approve"))
    store.submit_approval_response(
        appr.id,
        ApprovalResponse(
            decision="approve",
            signature_b64=base64.b64encode(signature).decode(),
        ),
    )
    thread.join(timeout=5)
    assert box["result"].status == TaskStatus.COMPLETED
    assert "approved-run" in box["result"].output["stdout"]


# --- assistant honors session windows (desktop password login) -----------

def _token_session(store, token, scope="all"):
    store.put_session(sign_grant_with_token(new_grant(scope, 900), token))


def test_assistant_runs_write_under_token_session(store):
    """A non-read-only command runs WITHOUT approval when a token-signed
    session window is open — the desktop password-login experience."""
    cfg = Config(api_token="pw", shell_command_timeout_s=10)
    _token_session(store, "pw")
    mgr = SessionManager(store, None, _ALWAYS, api_token="pw")
    gate = ApprovalGate(store, public_key_b64=None, poll_interval_s=0.02)
    run = _assistant_shell_runner(mgr, gate, cfg)
    res = run("echo session-write > /tmp/agentmgr_test_write")  # '>' => not auto
    assert res.gate == "session"
    assert res.exit_code == 0


def test_assistant_read_only_still_auto(store):
    cfg = Config(api_token="pw", shell_command_timeout_s=10)
    _token_session(store, "pw")
    mgr = SessionManager(store, None, _ALWAYS, api_token="pw")
    gate = ApprovalGate(store, public_key_b64=None, poll_interval_s=0.02)
    run = _assistant_shell_runner(mgr, gate, cfg)
    assert run("ls").gate == "auto"
