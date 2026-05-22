"""Vendored Agent-Manager worker shim — CANONICAL COPY.

This file is copied verbatim into each external Cloud Run Job repo that wants
to be a Level-B agent-manager worker (communities-scraper, newhome-source-
updater, sitemap-sync). It deliberately has **no dependency on the agentmgr
package** — only ``google-cloud-firestore`` — so heavyweight ETL/scraper
images don't have to vendor FastAPI/webauthn/etc.

Contract (mirrors agentmgr.state_store.FirestoreStateStore exactly):
  * The Master triggers the job with env override ``AGENTMGR_TASK_ID``.
  * The task doc lives at ``{prefix}tasks/{task_id}`` (we read ``payload``).
  * We write a typed result to ``{prefix}task_results/{task_id}`` with the
    same shape as schemas.TaskResult: task_id, status, output, error, worker,
    finished_at — and flip the task's ``status`` field to match.

Usage in a job entrypoint (dual-mode — keeps the existing cron path intact)::

    import agentmgr_worker
    if agentmgr_worker.task_id():
        sys.exit(agentmgr_worker.run_as_worker("communities-scraper", _work))
    else:
        main()                       # legacy scheduled behavior

where ``_work(payload, task) -> dict`` runs the real job and returns the
``output`` dict the Master will see.
"""

from __future__ import annotations

import datetime
import os
import traceback
from typing import Any, Callable

PROJECT = os.environ.get("AGENTMGR_PROJECT_ID", "paradise-automation")
PREFIX = os.environ.get("AGENTMGR_COLLECTION_PREFIX", "agentmgr_")
_TASKS = f"{PREFIX}tasks"
_RESULTS = f"{PREFIX}task_results"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def task_id() -> str | None:
    """The task id the Master assigned, or None when running the cron path."""
    return os.environ.get("AGENTMGR_TASK_ID") or None


def _write_result(db, tid: str, status: str, output: dict, error: str | None,
                  worker: str) -> None:
    db.collection(_RESULTS).document(tid).set(
        {
            "task_id": tid,
            "status": status,
            "output": output,
            "error": error,
            "worker": worker,
            "finished_at": _now_iso(),
        }
    )
    db.collection(_TASKS).document(tid).set({"status": status}, merge=True)


def run_as_worker(worker_name: str, work: Callable[[dict, dict], Any]) -> int:
    """Run ``work(payload, task)`` for AGENTMGR_TASK_ID and persist a result.

    ``work`` returns the output dict (anything non-dict is wrapped under a
    ``result`` key). Returns a process exit code: 0 on COMPLETED, 1 on FAILED.
    """
    from google.cloud import firestore  # lazy import

    tid = task_id()
    if not tid:
        raise RuntimeError("run_as_worker() called without AGENTMGR_TASK_ID")

    db = firestore.Client(project=PROJECT)
    snap = db.collection(_TASKS).document(tid).get()
    if not snap.exists:
        _write_result(db, tid, "FAILED", {}, f"task {tid} not found", worker_name)
        return 1

    task = snap.to_dict() or {}
    payload = task.get("payload", {}) or {}
    db.collection(_TASKS).document(tid).set({"status": "RUNNING"}, merge=True)

    try:
        output = work(payload, task)
        if not isinstance(output, dict):
            output = {"result": "completed" if output is None else str(output)}
        _write_result(db, tid, "COMPLETED", output, None, worker_name)
        return 0
    except Exception as exc:  # noqa: BLE001 — failure is recorded as a result
        traceback.print_exc()
        _write_result(db, tid, "FAILED", {}, f"{type(exc).__name__}: {exc}", worker_name)
        return 1
