"""Office-Lead CRM agent — registry entries, action dispatch, safety routing."""

from __future__ import annotations

import agent.local_agent as la
from agentmgr.config import Config
from agentmgr.registry import AgentRegistry
from agentmgr.schemas import TaskSpec, TaskStatus


def _crm_task(action: str, kind: str = "crm") -> TaskSpec:
    return TaskSpec(
        agent="crm-office-leads" if kind == "crm" else "crm-task-cleanup",
        kind=kind,
        payload={"action": action},
        conversation_id="conv_crm",
        correlation_id="cmd_crm",
    )


def test_registry_has_crm_agents():
    reg = AgentRegistry.load()
    reports = reg.get("crm-office-leads")
    assert reports.runtime == "local-agent"
    assert reports.kind == "crm"
    assert reports.sensitive_default is False
    cleanup = reg.get("crm-task-cleanup")
    assert cleanup.runtime == "local-agent"
    assert cleanup.kind == "crm_cleanup"
    assert cleanup.sensitive_default is True  # destructive -> must be gated


def test_benign_action_runs(store, monkeypatch):
    seen = {}

    def fake_run(command, cwd, timeout):
        seen["command"], seen["cwd"], seen["timeout"] = command, cwd, timeout
        return {"command": command, "exit_code": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(la, "_run_command", fake_run)
    task = _crm_task("daily_report")
    store.put_task(task)
    la.process_crm_task(task, store, Config(crm_project_dir="/tmp/crm"))

    res = store.get_task_result(task.id)
    assert res.status == TaskStatus.COMPLETED
    assert res.worker == "crm-office-leads"
    assert "office-leads/daily-report.js" in seen["command"]
    assert seen["cwd"] == "/tmp/crm"
    assert res.output["action"] == "daily_report"


def test_unknown_action_fails(store, monkeypatch):
    monkeypatch.setattr(la, "_run_command", lambda *a, **k: {
        "command": "", "exit_code": 0, "stdout": "", "stderr": ""})
    task = _crm_task("bogus")
    store.put_task(task)
    la.process_crm_task(task, store, Config())
    res = store.get_task_result(task.id)
    assert res.status == TaskStatus.FAILED
    assert "unknown CRM action" in res.error


def test_destructive_blocked_on_benign_kind(store, monkeypatch):
    ran = {"v": False}

    def fake_run(*a, **k):
        ran["v"] = True
        return {"command": "", "exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(la, "_run_command", fake_run)
    # clear_phantom_tasks is destructive; sending it as kind 'crm' must be refused.
    task = _crm_task("clear_phantom_tasks", kind="crm")
    store.put_task(task)
    la.process_crm_task(task, store, Config())
    res = store.get_task_result(task.id)
    assert res.status == TaskStatus.FAILED
    assert "destructive" in res.error
    assert ran["v"] is False  # never shelled out


def test_cleanup_kind_allows_destructive(store, monkeypatch):
    monkeypatch.setattr(la, "_run_command", lambda *a, **k: {
        "command": "node office-leads/actions/bulk-clear-2052.js",
        "exit_code": 0, "stdout": "cleared", "stderr": ""})
    task = _crm_task("clear_phantom_tasks", kind="crm_cleanup")
    store.put_task(task)
    la.process_crm_task(task, store, Config())
    res = store.get_task_result(task.id)
    assert res.status == TaskStatus.COMPLETED
    assert res.worker == "crm-task-cleanup"


def test_failed_node_exit_recorded(store, monkeypatch):
    monkeypatch.setattr(la, "_run_command", lambda *a, **k: {
        "command": "node ...", "exit_code": 2, "stdout": "", "stderr": "boom"})
    task = _crm_task("verify_phantom_tasks")
    store.put_task(task)
    la.process_crm_task(task, store, Config())
    res = store.get_task_result(task.id)
    assert res.status == TaskStatus.FAILED
    assert "exit 2" in res.error
