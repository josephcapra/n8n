"""State-store behaviour (exercised against the in-memory backend)."""

from __future__ import annotations

from agentmgr.config import Config
from agentmgr.schemas import (
    ApprovalRequest,
    ApprovalResponse,
    ApprovalStatus,
    ConversationTurn,
    Message,
    TaskResult,
    TaskSpec,
    TaskStatus,
)
from agentmgr.state_store import InMemoryStateStore, make_state_store


def _task() -> TaskSpec:
    return TaskSpec(
        agent="echo-worker",
        kind="echo",
        payload={"text": "x"},
        conversation_id="conv_1",
        correlation_id="cmd_1",
    )


def test_conversation_roundtrip(store):
    cid = store.create_conversation()
    store.append_turn(cid, ConversationTurn(role="user", text="hi"))
    store.append_turn(cid, ConversationTurn(role="master", text="hello"))
    assert [t.role for t in store.get_conversation(cid)] == ["user", "master"]


def test_task_lifecycle(store):
    task = _task()
    store.put_task(task)
    assert store.get_task(task.id).status == TaskStatus.PENDING

    store.update_task_status(task.id, TaskStatus.RUNNING)
    assert store.get_task(task.id).status == TaskStatus.RUNNING

    store.put_task_result(
        TaskResult(task_id=task.id, status=TaskStatus.COMPLETED, output={"echo": "x"})
    )
    assert store.get_task(task.id).status == TaskStatus.COMPLETED
    assert store.get_task_result(task.id).output["echo"] == "x"


def test_message_bus_filters_by_correlation(store):
    store.put_message(
        Message(correlation_id="cmd_1", from_agent="echo-worker", to_agent="master")
    )
    store.put_message(
        Message(correlation_id="other", from_agent="x", to_agent="master")
    )
    got = store.get_messages("cmd_1")
    assert len(got) == 1
    assert got[0].from_agent == "echo-worker"


def test_approval_roundtrip(store):
    appr = ApprovalRequest(action="sensitive-op")
    store.put_approval(appr)
    assert len(store.list_pending_approvals()) == 1

    store.submit_approval_response(
        appr.id, ApprovalResponse(decision="approve", signature_b64="sig")
    )
    assert store.get_approval(appr.id).response.decision == "approve"

    store.set_approval_status(appr.id, ApprovalStatus.APPROVED)
    assert store.list_pending_approvals() == []


def test_job_count_increments_per_command(store):
    assert store.incr_job_count("cmd_1") == 1
    assert store.incr_job_count("cmd_1") == 2
    assert store.incr_job_count("cmd_2") == 1


def test_factory_returns_memory_backend():
    assert isinstance(make_state_store(Config(state_backend="memory")), InMemoryStateStore)


def test_stored_task_is_isolated_copy(store):
    """Mutating a returned object must not corrupt the store."""
    task = _task()
    store.put_task(task)
    fetched = store.get_task(task.id)
    fetched.payload["text"] = "tampered"
    assert store.get_task(task.id).payload["text"] == "x"
