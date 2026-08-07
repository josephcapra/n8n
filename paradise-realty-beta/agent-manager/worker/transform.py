"""transform-worker — Phase 2 second worker (a Cloud Run Job).

Applies a text transform and, if the task asks, relays its result through the
Master's message bus to another agent. That relay path is exactly what makes
A -> Master -> B -> Master -> A cycles possible — and what the relay-depth
guard (``FanoutGuard.check_relay``) bounds.

Enhanced for Taylor integration: supports data aggregation and formatting ops
that help prepare data for weekly report generation.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any

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


def _aggregate_metrics(data: dict) -> dict:
    """Aggregate raw metrics data for report generation.

    Takes raw data from multiple sources and computes summary statistics
    suitable for Taylor's weekly reports.
    """
    result = {"aggregated_at": datetime.utcnow().isoformat() + "Z"}

    if "leads" in data:
        leads = data["leads"]
        result["lead_summary"] = {
            "total": len(leads),
            "new_this_week": sum(1 for l in leads if l.get("is_new")),
            "awaiting_reply": sum(1 for l in leads if l.get("awaiting_reply")),
            "by_agent": _group_count(leads, "agent"),
        }

    if "activities" in data:
        activities = data["activities"]
        result["activity_summary"] = {
            "total": len(activities),
            "by_type": _group_count(activities, "type"),
            "by_agent": _group_count(activities, "agent"),
        }

    if "incentives" in data:
        result["incentive_count"] = len(data["incentives"])

    return result


def _group_count(items: list, key: str) -> dict:
    """Group items by a key and count occurrences."""
    counts: dict[str, int] = {}
    for item in items:
        k = str(item.get(key, "unknown"))
        counts[k] = counts.get(k, 0) + 1
    return counts


def _format_for_report(data: dict, format_type: str = "summary") -> dict:
    """Format aggregated data for inclusion in Taylor's reports."""
    if format_type == "summary":
        return {
            "formatted": True,
            "format_type": format_type,
            "data": data,
            "ready_for_taylor": True,
        }
    if format_type == "detailed":
        return {
            "formatted": True,
            "format_type": format_type,
            "data": data,
            "sections": list(data.keys()),
            "ready_for_taylor": True,
        }
    return {"formatted": False, "error": f"unknown format_type: {format_type}"}


def _transform_data(payload: dict) -> dict:
    """Handle structured data transformations for Taylor integration.

    Supported operations:
    - aggregate: Compute summary statistics from raw data
    - format: Prepare data for report inclusion
    - merge: Combine multiple data sources
    """
    op = payload.get("data_op", "aggregate")
    data = payload.get("data", {})

    if op == "aggregate":
        return _aggregate_metrics(data)
    if op == "format":
        return _format_for_report(data, payload.get("format_type", "summary"))
    if op == "merge":
        sources = payload.get("sources", [])
        merged = {}
        for src in sources:
            if isinstance(src, dict):
                merged.update(src)
        return {"merged": merged, "source_count": len(sources)}

    return {"error": f"unknown data_op: {op}"}


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
        # Check if this is a data transformation (for Taylor) or text transform
        if task.payload.get("data") or task.payload.get("data_op"):
            # Structured data transformation for Taylor reports
            transformed = _transform_data(task.payload)
            output = {"result": transformed, "mode": "data", "data_op": task.payload.get("data_op", "aggregate")}
        else:
            # Legacy text transform
            op = task.payload.get("op", "upper")
            text = _input_text(task.payload)
            result_text = _transform(op, text)
            output = {"result": result_text, "op": op, "input": text, "mode": "text"}

        # Optional relay: emit a message THROUGH the Master to another agent.
        # Written BEFORE the result so the Master sees it when it picks the
        # result up. relay_depth climbs one per hop -> bounded by the guard.
        relay_to = task.payload.get("relay_to")
        if relay_to:
            # Build the relay payload based on mode
            if output.get("mode") == "data":
                # Structured data transform for Taylor integration
                relay_payload: dict[str, Any] = {"transformed_data": output.get("result")}
                if task.payload.get("relay_action"):
                    relay_payload["action"] = task.payload["relay_action"]
                if task.payload.get("relay_context"):
                    relay_payload.update(task.payload["relay_context"])
            else:
                # Text transform — preserve legacy relay behavior (for cycles/tests)
                relay_payload = {
                    "op": output.get("op", "upper"),
                    "text": output.get("result", ""),
                    "relay_to": relay_to,  # continue the chain
                }

            store.put_message(
                Message(
                    correlation_id=task.correlation_id,
                    from_agent=WORKER_NAME,
                    to_agent=relay_to,
                    payload=relay_payload,
                    relay_depth=task.depth + 1,
                )
            )
            output["relayed_to"] = relay_to

        result = TaskResult(
            task_id=task_id, status=TaskStatus.COMPLETED,
            output=output, worker=WORKER_NAME,
        )
        log.info("transform worker completed", extra={"task_id": task_id, "mode": output.get("mode")})
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
