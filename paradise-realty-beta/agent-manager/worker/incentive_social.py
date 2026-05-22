"""incentive-social worker — a subordinate of the Master.

Scans the builder-incentives Google Sheet for fresh, customer-facing incentives
(no mortgage rates/financing, no agent/realtor bonuses), drafts Facebook +
Instagram posts linked to the matching SE-Florida area page, and emails them to
the operator for approval. Posting to Meta happens only after approval.

The incentive logic lives in its own self-contained project
(``~/incentive-social-agent``); this module is the thin adapter that the Master
triggers (LocalJobRunner in dev, a Cloud Run Job in prod).
"""
from __future__ import annotations

import os
import sys

_PKG = os.path.expanduser("~/incentive-social-agent")
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)

WORKER_NAME = "incentive-social"


def run_task(task_id, store):
    from agentmgr.schemas import TaskResult, TaskStatus
    try:
        import pipeline  # from ~/incentive-social-agent
        payload = {}
        task = store.get_task(task_id)
        if task is not None:
            payload = task.payload or {}
            store.update_task_status(task_id, TaskStatus.RUNNING)
        out = pipeline.run(send=bool(payload.get("send", True)),
                           today=payload.get("today"))
        result = TaskResult(task_id=task_id, status=TaskStatus.COMPLETED,
                            output=out, worker=WORKER_NAME)
    except Exception as exc:  # noqa: BLE001 - failure recorded as a result
        result = TaskResult(task_id=task_id, status=TaskStatus.FAILED,
                            error=f"{type(exc).__name__}: {exc}", worker=WORKER_NAME)
    store.put_task_result(result)
    return result
