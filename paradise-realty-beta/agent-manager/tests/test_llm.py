"""LLM router — cost computation, cost logging, and the budget ceiling."""

from __future__ import annotations

import base64
import threading
import time

from fastapi.testclient import TestClient

from agentmgr.approval_gate import ApprovalGate, canonical_message
from agentmgr.config import Config
from agentmgr.llm import (
    LLMRequest,
    LLMRouter,
    MockLLMProvider,
    compute_cost,
    make_provider,
)
from agentmgr.schemas import ApprovalResponse
from master.main import build_app


# --- pricing -------------------------------------------------------------

def test_compute_cost_opus_4_7():
    # 1M input + 1M output on claude-opus-4-7 = $5 + $25
    assert compute_cost("claude-opus-4-7", 1_000_000, 1_000_000) == 30.0


def test_compute_cost_cache_read_discount():
    # cached reads bill ~0.1x the input price
    assert round(compute_cost("claude-opus-4-7", 0, 0, cache_read_tokens=1_000_000), 4) == 0.5


def test_compute_cost_unknown_model_uses_override():
    assert compute_cost("some-model", 1_000_000, 0, price_override=(2.0, 8.0)) == 2.0


# --- providers -----------------------------------------------------------

def test_mock_provider_is_deterministic():
    provider = MockLLMProvider(reply="hi", input_tokens=100, output_tokens=50)
    response = provider.complete(LLMRequest(prompt="x"))
    assert response.text == "hi"
    assert response.provider == "mock"
    assert response.cost_usd > 0


def test_make_provider_mock():
    assert isinstance(make_provider(Config(llm_provider="mock")), MockLLMProvider)


# --- router: cost logging ------------------------------------------------

def test_router_records_cost_per_command_and_total(store, keypair):
    _, pub = keypair
    gate = ApprovalGate(store, public_key_b64=pub)
    router = LLMRouter(MockLLMProvider(cost_usd=0.01), store, gate, budget_usd=10.0)

    router.complete(LLMRequest(prompt="a"), correlation_id="cmd_1")
    router.complete(LLMRequest(prompt="b"), correlation_id="cmd_1")
    router.complete(LLMRequest(prompt="c"), correlation_id="cmd_2")

    assert round(store.get_llm_cost("cmd_1"), 4) == 0.02
    assert round(store.get_llm_cost("cmd_2"), 4) == 0.01
    assert round(store.get_total_llm_cost(), 4) == 0.03


# --- router: budget ceiling trips the approval gate ----------------------

def test_router_budget_ceiling_trips_the_gate(store, keypair):
    private_key, pub = keypair
    gate = ApprovalGate(store, public_key_b64=pub, poll_interval_s=0.02)
    router = LLMRouter(MockLLMProvider(cost_usd=5.0), store, gate, budget_usd=1.0)

    # First call: cumulative spend is 0, under the $1 budget — proceeds.
    router.complete(LLMRequest(prompt="a"), correlation_id="cmd_x")
    assert store.get_llm_cost("cmd_x") == 5.0

    # Second call: cumulative $5 >= $1 budget — BLOCKS on the approval gate.
    box: dict = {}
    thread = threading.Thread(
        target=lambda: box.update(
            resp=router.complete(LLMRequest(prompt="b"), correlation_id="cmd_x")
        ),
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
    assert appr is not None, "over-budget LLM call must block on the gate"
    assert appr.action == "llm-spend-over-budget"

    signature = private_key.sign(canonical_message(appr.id, appr.nonce, "approve"))
    store.submit_approval_response(
        appr.id,
        ApprovalResponse(
            decision="approve",
            signature_b64=base64.b64encode(signature).decode(),
        ),
    )
    thread.join(timeout=5)
    assert box.get("resp") is not None


# --- /cost endpoint ------------------------------------------------------

def test_cost_endpoint_reports_provider_and_budget():
    app = build_app(Config(state_backend="memory", job_runner="local", api_token="t"))
    resp = TestClient(app).get("/cost", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["llm_provider"] == "mock"
    assert body["total_llm_cost_usd"] == 0.0
