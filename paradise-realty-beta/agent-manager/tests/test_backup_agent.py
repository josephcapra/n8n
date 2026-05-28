"""Backup agent — registry entry, safe vs gated actions, restore approval."""

from __future__ import annotations

import agent.local_agent as la
from agentmgr.approval_gate import (
    ApprovalDecision,
    ApprovalDenied,
    ApprovalNotProvisioned,
)
from agentmgr.config import Config
from agentmgr.registry import AgentRegistry
from agentmgr.schemas import TaskSpec, TaskStatus


# --- a stub approval gate (records calls; can approve / deny / fail-closed) ---
class _Gate:
    def __init__(self, *, deny: bool = False, unprovisioned: bool = False) -> None:
        self.deny = deny
        self.unprovisioned = unprovisioned
        self.calls: list[tuple] = []

    def request_approval(self, action, details, *, sensitive_reason="explicit"):
        self.calls.append((action, details))
        if self.unprovisioned:
            raise ApprovalNotProvisioned("no verifier")
        if self.deny:
            raise ApprovalDenied("operator denied")
        return ApprovalDecision(approved=True, request_id="appr_test")


def _bk_task(action: str | None = None, **payload) -> TaskSpec:
    if action is not None:
        payload["action"] = action
    return TaskSpec(
        agent="backup",
        kind="backup",
        payload=payload,
        conversation_id="conv_bk",
        correlation_id="cmd_bk",
    )


def _cfg() -> Config:
    return Config(backup_dir="/tmp/pr", backup_timeout_s=1800.0)


def test_registry_has_backup_agent():
    reg = AgentRegistry.load()
    a = reg.get("backup")
    assert a.runtime == "local-agent"
    assert a.kind == "backup"
    # Agent overall isn't sensitive; the restore_* actions gate themselves.
    assert a.sensitive_default is False


def test_default_action_lists_backups(store, monkeypatch):
    seen = {}

    def fake_run(command, cwd, timeout):
        seen["command"], seen["cwd"], seen["timeout"] = command, cwd, timeout
        return {"command": command, "exit_code": 0, "stdout": "2026-05-27", "stderr": ""}

    monkeypatch.setattr(la, "_run_command", fake_run)
    gate = _Gate()
    task = _bk_task()  # no action -> default status
    store.put_task(task)
    la.process_backup_task(task, store, gate, _cfg())

    res = store.get_task_result(task.id)
    assert res.status == TaskStatus.COMPLETED
    assert res.worker == "backup"
    assert seen["command"] == "bash restore-agents.sh list"
    assert seen["cwd"] == "/tmp/pr"
    assert gate.calls == []  # read-only — never asks for approval


def test_backup_now_is_safe_and_unGated(store, monkeypatch):
    seen = {}

    def fake_run(command, cwd, timeout):
        seen["command"] = command
        return {"command": command, "exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(la, "_run_command", fake_run)
    gate = _Gate()
    task = _bk_task("backup_now")
    store.put_task(task)
    la.process_backup_task(task, store, gate, _cfg())

    res = store.get_task_result(task.id)
    assert res.status == TaskStatus.COMPLETED
    assert seen["command"] == "bash backup-agents.sh"
    assert gate.calls == []           # backup is non-destructive -> no approval
    assert res.output.get("note")     # a human summary for the GUI bubble


def test_restore_latest_gates_then_runs_noninteractively(store, monkeypatch):
    seen = {}

    def fake_run(command, cwd, timeout):
        seen["command"] = command
        return {"command": command, "exit_code": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(la, "_run_command", fake_run)
    gate = _Gate()
    task = _bk_task("restore_latest")
    store.put_task(task)
    la.process_backup_task(task, store, gate, _cfg())

    res = store.get_task_result(task.id)
    assert res.status == TaskStatus.COMPLETED
    assert gate.calls and gate.calls[0][0] == "backup: restore latest"
    # Runs only AFTER approval, and skips the script's own typed-YES prompt.
    assert seen["command"] == "RESTORE_CONFIRM=YES bash restore-agents.sh latest"


def test_restore_denied_changes_nothing(store, monkeypatch):
    ran = {"v": False}

    def fake_run(*a, **k):
        ran["v"] = True
        return {"command": "", "exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(la, "_run_command", fake_run)
    gate = _Gate(deny=True)
    task = _bk_task("restore_latest")
    store.put_task(task)
    la.process_backup_task(task, store, gate, _cfg())

    res = store.get_task_result(task.id)
    assert res.status == TaskStatus.FAILED
    assert "denied" in res.error
    assert ran["v"] is False  # never shelled out — nothing overwritten


def test_restore_date_requires_a_date_before_gate(store, monkeypatch):
    ran = {"v": False}
    monkeypatch.setattr(la, "_run_command",
                        lambda *a, **k: ran.update(v=True) or {"exit_code": 0})
    gate = _Gate()
    task = _bk_task("restore_date")  # no 'date'
    store.put_task(task)
    la.process_backup_task(task, store, gate, _cfg())

    res = store.get_task_result(task.id)
    assert res.status == TaskStatus.FAILED
    assert "date" in res.error
    assert gate.calls == []     # validated its input before bothering the operator
    assert ran["v"] is False


def test_restore_date_shells_with_the_date(store, monkeypatch):
    seen = {}
    monkeypatch.setattr(la, "_run_command", lambda command, cwd, timeout: seen.update(
        command=command) or {"command": command, "exit_code": 0, "stdout": "", "stderr": ""})
    gate = _Gate()
    task = _bk_task("restore_date", date="2026-05-27")
    store.put_task(task)
    la.process_backup_task(task, store, gate, _cfg())

    res = store.get_task_result(task.id)
    assert res.status == TaskStatus.COMPLETED
    assert seen["command"] == "RESTORE_CONFIRM=YES bash restore-agents.sh 2026-05-27"


def test_unknown_action_fails_without_shelling(store, monkeypatch):
    ran = {"v": False}
    monkeypatch.setattr(la, "_run_command",
                        lambda *a, **k: ran.update(v=True) or {"exit_code": 0})
    gate = _Gate()
    task = _bk_task("bogus")
    store.put_task(task)
    la.process_backup_task(task, store, gate, _cfg())

    res = store.get_task_result(task.id)
    assert res.status == TaskStatus.FAILED
    assert "unknown backup action" in res.error
    assert ran["v"] is False


def test_failed_exit_recorded(store, monkeypatch):
    monkeypatch.setattr(la, "_run_command", lambda *a, **k: {
        "command": "bash backup-agents.sh", "exit_code": 1,
        "stdout": "", "stderr": "boom"})
    gate = _Gate()
    task = _bk_task("backup_now")
    store.put_task(task)
    la.process_backup_task(task, store, gate, _cfg())

    res = store.get_task_result(task.id)
    assert res.status == TaskStatus.FAILED
    assert "exit 1" in res.error
