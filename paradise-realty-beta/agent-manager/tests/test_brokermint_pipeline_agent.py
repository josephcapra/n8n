"""Brokermint pipeline agent — registry, pull→cfo relay, preview, cfo forecast."""

from __future__ import annotations

import json

import agent.local_agent as la
from agentmgr.config import Config
from agentmgr.registry import AgentRegistry
from agentmgr.schemas import TaskSpec, TaskStatus


def _pipe_task(action: str | None = None) -> TaskSpec:
    payload = {"action": action} if action is not None else {}
    return TaskSpec(agent="brokermint-pipeline", kind="brokermint_pipeline",
                    payload=payload, conversation_id="conv_bp", correlation_id="cmd_bp")


_SAMPLE = [
    {"address": "123 Palm St", "sale_price": 500000, "commission": 12000, "close_date": "2026-06-15", "status": "under_contract"},
    {"address": "9 Ocean Ave", "sale_price": 750000, "commission": 18000, "close_date": "2026-07-20", "status": "pending"},
]


def _ok_run(pipeline):
    def fake_run(command, cwd, timeout):
        return {"command": command, "exit_code": 0,
                "stdout": json.dumps({"ok": True, "pipeline": pipeline}), "stderr": ""}
    return fake_run


def test_registry_has_pipeline_agent():
    a = AgentRegistry.load().get("brokermint-pipeline")
    assert a.runtime == "local-agent"
    assert a.kind == "brokermint_pipeline"
    assert a.sensitive_default is False
    assert "cfo" in (a.details.get("relays") or [])  # relays to the Finance Agent


def test_pull_relays_pipeline_to_cfo(store, monkeypatch):
    monkeypatch.setattr(la, "_run_command", _ok_run(_SAMPLE))
    task = _pipe_task()  # default action = pull
    store.put_task(task)
    la.process_brokermint_pipeline_task(task, store, Config())

    res = store.get_task_result(task.id)
    assert res.status == TaskStatus.COMPLETED
    assert res.worker == "brokermint-pipeline"
    assert res.output["relayed_to"] == "cfo"
    assert res.output["pipeline_count"] == 2
    assert res.output["projected_commission_total"] == 30000.0

    # A cfo forecast task was enqueued with the pipeline data (the handoff).
    cfo_pending = store.get_pending_tasks("cfo")
    assert len(cfo_pending) == 1
    cfo = cfo_pending[0]
    assert cfo.kind == "cfo"
    assert cfo.payload["action"] == "forecast"
    assert cfo.payload["pipeline"] == _SAMPLE
    assert cfo.payload["source"] == "brokermint-pipeline"


def test_preview_does_not_relay(store, monkeypatch):
    monkeypatch.setattr(la, "_run_command", _ok_run(_SAMPLE))
    task = _pipe_task("preview")
    store.put_task(task)
    la.process_brokermint_pipeline_task(task, store, Config())

    res = store.get_task_result(task.id)
    assert res.status == TaskStatus.COMPLETED
    assert res.output["pipeline"] == _SAMPLE
    assert "relayed_to" not in res.output
    assert store.get_pending_tasks("cfo") == []  # nothing handed off


def test_needs_login_fails_helpfully(store, monkeypatch):
    def fake_run(command, cwd, timeout):
        return {"command": command, "exit_code": 1,
                "stdout": json.dumps({"ok": False, "error": "needs_login"}), "stderr": ""}
    monkeypatch.setattr(la, "_run_command", fake_run)
    task = _pipe_task("pull")
    store.put_task(task)
    la.process_brokermint_pipeline_task(task, store, Config())

    res = store.get_task_result(task.id)
    assert res.status == TaskStatus.FAILED
    assert "Brokermint" in res.error and "Chrome" in res.error
    assert store.get_pending_tasks("cfo") == []  # no bad handoff on failure


def test_unknown_action_fails_without_shelling(store, monkeypatch):
    ran = {"v": False}
    monkeypatch.setattr(la, "_run_command", lambda *a, **k: ran.update(v=True) or {"exit_code": 0})
    task = _pipe_task("bogus")
    store.put_task(task)
    la.process_brokermint_pipeline_task(task, store, Config())

    res = store.get_task_result(task.id)
    assert res.status == TaskStatus.FAILED
    assert "unknown brokermint-pipeline action" in res.error
    assert ran["v"] is False


def test_cfo_forecast_action_shells_run_cfo(store, monkeypatch):
    seen = {}

    def fake_run(command, cwd, timeout):
        seen["command"], seen["cwd"] = command, cwd
        return {"command": command, "exit_code": 0, "stdout": "forecast ok", "stderr": ""}

    monkeypatch.setattr(la, "_run_command", fake_run)
    task = TaskSpec(agent="cfo", kind="cfo",
                    payload={"action": "forecast", "pipeline": _SAMPLE, "source": "brokermint-pipeline"},
                    conversation_id="c", correlation_id="c")
    store.put_task(task)
    la.process_cfo_task(task, store, Config(cfo_dir="/tmp/cfo"))

    res = store.get_task_result(task.id)
    assert res.status == TaskStatus.COMPLETED
    assert seen["command"].startswith("python3 run_cfo.py forecast --pipeline-file ")
    assert seen["cwd"] == "/tmp/cfo"
    assert res.output["action"] == "forecast"
