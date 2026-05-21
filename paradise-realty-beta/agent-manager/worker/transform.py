"""transform-worker — Phase 2 second worker (a Cloud Run Job).

Applies a text transform and, if the task asks, relays its result through the
Master's message bus to another agent. That relay path is exactly what makes
A -> Master -> B -> Master -> A cycles possible — and what the relay-depth
guard (``FanoutGuard.check_relay``) bounds.
"""

from __future__ import annotations

import os
import sys

from agentmgr.config import load_config
from agentmgr.logging_utils import get_logger, set_correlation_id
from agentmgr.schemas import Message, TaskResult, TaskStatus
from agentmgr.state_store import StateStore, make_state_store

log = get_logger("agentmgr.worker.transform")

WORKER_NAME = "transform-worker"


def _transform(op: str, text: str) -> str:
    op = (op or "upper").lower()
    if op == "upper":
        return text.upper()
    if op == "lower":
        return text.lower()
    if op == "reverse":
        return text[::-1]
    if op == "count":
        return str(len(text))
    raise ValueError(f"unknown transform op {op!r}")


def _input_text(payload: dict) -> str:
    """Text to transform — a relayed/pipelined input wins over a literal one."""
    relayed = payload.get("relayed_input") or {}
    return (
        relayed.get("result")
        or payload.get("result")
        or payload.get("text", "")
    )


def run_task(task_id: str, store: StateStore) -> TaskResult:
    task = store.get_task(task_id)
    if task is None:
        raise KeyError(f"task {task_id} not found in state store")

    set_correlation_id(task.correlation_id)
    store.update_task_status(task_id, TaskStatus.RUNNING)
    log.info("transform worker started", extra={"task_id": task_id})

    try:
        op = task.payload.get("op", "upper")
        text = _input_text(task.payload)
        result_text = _transform(op, text)
        output = {"result": result_text, "op": op, "input": text}

        # Optional relay: emit a message THROUGH the Master to another agent.
        # Written BEFORE the result so the Master sees it when it picks the
        # result up. relay_depth climbs one per hop -> bounded by the guard.
        relay_to = task.payload.get("relay_to")
        if relay_to:
            store.put_message(
                Message(
                    correlation_id=task.correlation_id,
                    from_agent=WORKER_NAME,
                    to_agent=relay_to,
                    payload={"op": op, "text": result_text, "relay_to": relay_to},
                    relay_depth=task.depth + 1,
                )
            )
            output["relayed_to"] = relay_to

        result = TaskResult(
            task_id=task_id, status=TaskStatus.COMPLETED,
            output=output, worker=WORKER_NAME,
        )
        log.info("transform worker completed", extra={"task_id": task_id})
    except Exception as exc:  # noqa: BLE001 - failure recorded as a result
        result = TaskResult(
            task_id=task_id, status=TaskStatus.FAILED,
            error=f"{type(exc).__name__}: {exc}", worker=WORKER_NAME,
        )
        log.exception("transform worker failed", extra={"task_id": task_id})

    store.put_task_result(result)
    return result


def main() -> int:
    task_id = os.environ.get("AGENTMGR_TASK_ID")
    if not task_id:
        log.error("AGENTMGR_TASK_ID not set; nothing to do")
        return 2
    result = run_task(task_id, make_state_store(load_config()))
    return 0 if result.status == TaskStatus.COMPLETED else 1


if __name__ == "__main__":
    sys.exit(main())
