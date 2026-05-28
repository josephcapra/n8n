"""Brokermint pipeline agent — registry, pull→cfo relay (pending + active listings),
preview, and the cfo forecast action."""

from __future__ import annotations

import json

import agent.local_agent as la
from agentmgr.config import Config
from agentmgr.registry import AgentRegistry
from agentmgr.schemas import TaskSpec, TaskStatus

_PENDING = [
    {"address": "123 Palm St", "sale_price": 500000, "commission": 3000, "close_date": "2026-06-15", "status": "pending"},
    {"address": "9 Ocean Ave", "sale_price": 750000, "commission": 4500, "close_date": "2026-07-20", "status": "pending"},
]
_ACTIVE_BM = [{"address": "55 Listing Rd", "sale_price": 400000, "commission": 2000, "close_date": None, "status": "active"}]
_BM_ALL = _PENDING + _ACTIVE_BM
_MLS = [{"listing_id": "R1", "address": "55 Listing Rd, Stuart, FL", "list_price": 400000,
         "status": "Active", "days_on_market": 12, "list_agent": "Jane"}]


def _pipe_task(action: str | None = None) -> TaskSpec:
    payload = {"action": action} if action is not None else {}
    return TaskSpec(agent="brokermint-pipeline", kind="brokermint_pipeline",
                    payload=payload, conversation_id="conv_bp", correlation_id="cmd_bp")


def _fake_runs(brokermint=_BM_ALL, mls=_MLS, bm_ok=True):
    """A _run_command stub that answers BOTH shell-outs the handler makes:
    `node bm_pipeline.js` (Brokermint) and `python3 office_active_json.py` (MLS)."""
    def fake(command, cwd, timeout):
        if "office_active_json" in command:
            return {"command": command, "exit_code": 0,
                    "stdout": json.dumps({"ok": True, "as_of": "2026-05-25", "listings": mls}), "stderr": ""}
        body = {"ok": bm_ok, "pipeline": brokermint} if bm_ok else {"ok": False, "error": "needs_login"}
        return {"command": command, "exit_code": 0 if bm_ok else 1,
                "stdout": json.dumps(body), "stderr": ""}
    return fake


def test_registry_has_pipeline_agent():
    a = AgentRegistry.load().get("brokermint-pipeline")
    assert a.runtime == "local-agent"
    assert a.kind == "brokermint_pipeline"
    assert a.sensitive_default is False
    assert "cfo" in (a.details.get("relays") or [])


def test_pull_relays_pending_and_listings_to_cfo(store, monkeypatch):
    monkeypatch.setattr(la, "_run_command", _fake_runs())
    task = _pipe_task()  # default = pull
    store.put_task(task)
    la.process_brokermint_pipeline_task(task, store, Config())

    res = store.get_task_result(task.id)
    assert res.status == TaskStatus.COMPLETED
    assert res.output["relayed_to"] == "cfo"
    assert res.output["pending_count"] == 2
    assert res.output["pending_net_total"] == 7500.0
    assert res.output["active_listing_count"] == 1

    cfo = store.get_pending_tasks("cfo")
    assert len(cfo) == 1
    p = cfo[0].payload
    assert p["action"] == "forecast"
    assert p["pending"] == _PENDING                 # under-contract deals
    assert p["active_listings"] == _MLS             # Beaches MLS inventory
    assert p["brokermint_active"]["count"] == 1     # the 'active' Brokermint deal
    assert p["mls_as_of"] == "2026-05-25"


def test_preview_does_not_relay(store, monkeypatch):
    monkeypatch.setattr(la, "_run_command", _fake_runs())
    task = _pipe_task("preview")
    store.put_task(task)
    la.process_brokermint_pipeline_task(task, store, Config())

    res = store.get_task_result(task.id)
    assert res.status == TaskStatus.COMPLETED
    assert res.output["pending"] == _PENDING
    assert res.output["active_listings"] == _MLS
    assert "relayed_to" not in res.output
    assert store.get_pending_tasks("cfo") == []


def test_needs_login_fails_helpfully(store, monkeypatch):
    monkeypatch.setattr(la, "_run_command", _fake_runs(bm_ok=False))
    task = _pipe_task("pull")
    store.put_task(task)
    la.process_brokermint_pipeline_task(task, store, Config())

    res = store.get_task_result(task.id)
    assert res.status == TaskStatus.FAILED
    assert "Brokermint" in res.error
    assert store.get_pending_tasks("cfo") == []


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
                    payload={"action": "forecast", "pending": _PENDING, "active_listings": _MLS,
                             "brokermint_active": {"count": 1, "net_total": 2000}, "source": "brokermint-pipeline"},
                    conversation_id="c", correlation_id="c")
    store.put_task(task)
    la.process_cfo_task(task, store, Config(cfo_dir="/tmp/cfo"))

    res = store.get_task_result(task.id)
    assert res.status == TaskStatus.COMPLETED
    assert seen["command"].startswith("python3 run_cfo.py forecast --pipeline-file ")
    assert seen["cwd"] == "/tmp/cfo"
    assert res.output["action"] == "forecast"
