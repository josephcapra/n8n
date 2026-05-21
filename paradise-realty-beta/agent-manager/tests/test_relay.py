"""Phase 2 — the inter-agent message hub, and BOTH guard bounds.

The relay-depth guard is the one the original brief flagged: once workers can
pass messages back through the Master, A->Master->B->Master->A cycles become
possible. These tests prove relay depth is bounded, *separately* from the job
fan-out bound.
"""

from __future__ import annotations

import base64
import threading
import time

from fastapi.testclient import TestClient

from agentmgr.approval_gate import canonical_message
from agentmgr.config import Config
from agentmgr.schemas import ApprovalResponse
from master.main import build_app

_TOKEN = "tok"


def _cfg(**overrides) -> Config:
    base = dict(
        state_backend="memory", job_runner="local", api_token=_TOKEN,
        poll_interval_s=0.05, task_timeout_s=10.0,
    )
    base.update(overrides)
    return Config(**base)


def _post(app, message):
    return TestClient(app).post(
        "/chat", json={"message": message},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )


def test_pipeline_relays_output_between_workers():
    """`a | b` — the Master relays step a's output into step b."""
    resp = _post(build_app(_cfg()), "transform: upper hello | transform: reverse")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["task_ids"]) == 2
    assert "OLLEH" in body["reply"]          # upper("hello") -> reverse -> OLLEH


def test_worker_emitted_message_relayed_to_another_agent():
    """A worker emits a message; the Master relays it to a second agent."""
    body = _post(build_app(_cfg()), "transform: upper hi > echo-worker").json()
    assert "[transform-worker] completed" in body["reply"]
    assert "[echo-worker] completed" in body["reply"]
    assert len(body["task_ids"]) == 2


def test_relay_depth_is_bounded_on_a_cycle():
    """transform-worker relays to itself forever — check_relay must stop it."""
    app = build_app(_cfg(max_fanout_depth=3, max_jobs_per_command=50))
    body = _post(app, "transform: upper x > transform-worker").json()
    assert "relay bounded" in body["reply"]
    # bounded => a small finite number of jobs ran, not an infinite loop
    assert 0 < len(body["task_ids"]) <= 6


def test_job_fanout_is_bounded_independently_of_relay_depth():
    """A tight job-count cap trips check_task even with relay depth wide open."""
    app = build_app(_cfg(max_fanout_depth=99, max_jobs_per_command=3))
    body = _post(app, "transform: upper x > transform-worker").json()
    assert "fan-out bounded" in body["reply"]
    assert len(body["task_ids"]) <= 3


def test_sensitive_cloudrun_subtask_gated_before_dispatch(keypair):
    """A SENSITIVE cloudrun-job subtask blocks on approval BEFORE it runs."""
    private_key, pub = keypair
    app = build_app(_cfg(approval_public_key=pub))
    store = app.state.store

    box: dict = {}
    thread = threading.Thread(
        target=lambda: box.update(resp=_post(app, "!transform: upper secret-task")),
        daemon=True,
    )
    thread.start()

    appr = None
    for _ in range(500):
        pending = store.list_pending_approvals()
        if pending:
            appr = pending[0]
            break
        time.sleep(0.01)
    assert appr is not None, "sensitive subtask must block before dispatch"
    assert appr.action == "task:transform"

    signature = private_key.sign(canonical_message(appr.id, appr.nonce, "approve"))
    store.submit_approval_response(
        appr.id,
        ApprovalResponse(
            decision="approve",
            signature_b64=base64.b64encode(signature).decode(),
        ),
    )
    thread.join(timeout=5)
    assert box["resp"].status_code == 200
    assert "[transform-worker] completed" in box["resp"].json()["reply"]
