"""incentive-social converted from a (never-deployed) Cloud Run job to a
local-agent that runs the pipeline on the Mac."""

from __future__ import annotations

import agent.local_agent as la
import worker.incentive_social as wis
from agentmgr.registry import AgentRegistry
from agentmgr.schemas import TaskSpec, TaskStatus


def test_registry_incentive_social_is_local():
    a = AgentRegistry.load().get("incentive-social")
    assert a.runtime == "local-agent"
    assert a.kind == "incentive_social"
    assert a.job_name == ""          # no Cloud Run job behind it anymore
    assert a.region == "local"


def test_handler_delegates_to_worker_adapter(store, monkeypatch):
    seen = {}

    def fake_run(task_id, st):
        seen["task_id"], seen["store"] = task_id, st
        st.put_task_result.__self__  # ensure a real store was passed
    monkeypatch.setattr(wis, "run_task", fake_run)

    task = TaskSpec(
        agent="incentive-social", kind="incentive_social",
        payload={"send": False}, conversation_id="c1", correlation_id="r1")
    store.put_task(task)
    la.process_incentive_social_task(task, store)
    assert seen["task_id"] == task.id
    assert seen["store"] is store


def test_handler_records_failure_on_adapter_crash(store, monkeypatch):
    def boom(task_id, st):
        raise RuntimeError("pipeline import failed")
    monkeypatch.setattr(wis, "run_task", boom)

    task = TaskSpec(
        agent="incentive-social", kind="incentive_social",
        payload={}, conversation_id="c1", correlation_id="r1")
    store.put_task(task)
    la.process_incentive_social_task(task, store)
    res = store.get_task_result(task.id)
    assert res.status == TaskStatus.FAILED
    assert "pipeline import failed" in res.error
    assert res.worker == "incentive-social"
