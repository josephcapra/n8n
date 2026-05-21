"""Placeholder worker (echo-worker) — a Cloud Run Job entrypoint.

Phase 1 worker. It:
  1. reads the task id from ``AGENTMGR_TASK_ID``,
  2. loads the typed :class:`TaskSpec` from the shared state store,
  3. runs placeholder logic (echoes the payload),
  4. writes a typed :class:`TaskResult` back to the store,
  5. exits (0 on success, 1 on failure) — run-to-completion semantics.
"""

from __future__ import annotations

import os
import sys

from agentmgr.config import load_config
from agentmgr.logging_utils import get_logger, set_correlation_id
from agentmgr.schemas import TaskResult, TaskSpec, TaskStatus
from agentmgr.state_store import StateStore, make_state_store

log = get_logger("agentmgr.worker.echo")

WORKER_NAME = "echo-worker"


def _execute(task: TaskSpec) -> dict:
    """Placeholder task logic. Real workers replace this with actual work."""
    if task.kind in ("echo", "ping"):
        text = task.payload.get("text", "") or task.payload.get("result", "")
        return {
            "result": text,            # standard key consumed by pipelines
            "echo": text,
            "received_payload": task.payload,
        }
    raise ValueError(f"{WORKER_NAME} cannot handle task kind {task.kind!r}")


def run_task(task_id: str, store: StateStore) -> TaskResult:
    """Run one task end to end and persist its typed result."""
    task = store.get_task(task_id)
    if task is None:
        raise KeyError(f"task {task_id} not found in state store")

    set_correlation_id(task.correlation_id)
    log.info("worker started", extra={"task_id": task_id, "kind": task.kind})
    store.update_task_status(task_id, TaskStatus.RUNNING)

    try:
        output = _execute(task)
        result = TaskResult(
            task_id=task_id,
            status=TaskStatus.COMPLETED,
            output=output,
            worker=WORKER_NAME,
        )
        log.info("worker completed", extra={"task_id": task_id})
    except Exception as exc:  # noqa: BLE001 - failure is recorded as a result
        result = TaskResult(
            task_id=task_id,
            status=TaskStatus.FAILED,
            error=f"{type(exc).__name__}: {exc}",
            worker=WORKER_NAME,
        )
        log.exception("worker failed", extra={"task_id": task_id})

    store.put_task_result(result)
    return result


def main() -> int:
    task_id = os.environ.get("AGENTMGR_TASK_ID")
    if not task_id:
        log.error("AGENTMGR_TASK_ID not set; nothing to do")
        return 2

    store = make_state_store(load_config())
    result = run_task(task_id, store)
    return 0 if result.status == TaskStatus.COMPLETED else 1


if __name__ == "__main__":
    sys.exit(main())
