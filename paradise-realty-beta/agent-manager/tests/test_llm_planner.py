"""LLM planner — JSON plan parsing and graceful fallback to the rule planner."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agentmgr.approval_gate import ApprovalGate
from agentmgr.config import Config
from agentmgr.llm import LLMRouter, MockLLMProvider
from agentmgr.planner import LLMPlanner, RuleBasedPlanner, make_planner
from agentmgr.registry import AgentRegistry
from master.main import build_app

_REG = AgentRegistry.load()


def _router(store, reply: str) -> LLMRouter:
    gate = ApprovalGate(store, public_key_b64=None)
    return LLMRouter(MockLLMProvider(reply=reply), store, gate, budget_usd=100.0)


def test_llm_planner_parses_a_json_plan(store):
    plan_json = json.dumps({
        "summary": "echo it back",
        "steps": [{
            "agent": "echo-worker", "kind": "echo",
            "payload": {"text": "hi"}, "sensitive": False,
            "consume_previous": False,
        }],
    })
    plan = LLMPlanner(_router(store, plan_json)).plan("anything", _REG, "cmd_1")
    assert len(plan.steps) == 1
    assert plan.steps[0].agent == "echo-worker"
    assert "[LLM:mock]" in plan.summary


def test_llm_planner_handles_code_fenced_json(store):
    fenced = "```json\n" + json.dumps({
        "summary": "shell it",
        "steps": [{"agent": "mac-shell", "kind": "shell",
                   "payload": {"command": "ls"}, "sensitive": True}],
    }) + "\n```"
    plan = LLMPlanner(_router(store, fenced)).plan("list files", _REG, "cmd_2")
    assert plan.steps[0].agent == "mac-shell"
    assert plan.steps[0].sensitive is True


def test_llm_planner_falls_back_on_unparseable_output(store):
    plan = LLMPlanner(_router(store, "sorry, no JSON here")).plan(
        "transform: upper hi", _REG, "cmd_3"
    )
    # the rule-based fallback handled it
    assert plan.steps[0].agent == "transform-worker"


def test_llm_planner_falls_back_on_unknown_agent(store):
    bad = json.dumps({
        "summary": "x",
        "steps": [{"agent": "no-such-agent", "kind": "x", "payload": {}}],
    })
    plan = LLMPlanner(_router(store, bad)).plan("hello there", _REG, "cmd_4")
    assert plan.steps[0].agent == "assistant"  # fell back to the rule planner


def test_make_planner_llm_requires_a_router():
    with pytest.raises(ValueError):
        make_planner(Config(planner="llm"), router=None)


def test_make_planner_defaults_to_rule_based():
    assert isinstance(make_planner(Config()), RuleBasedPlanner)


def test_master_with_llm_planner_degrades_gracefully():
    """planner=llm + mock provider: the mock reply isn't JSON, so the Master
    falls back to rule-based planning and the command still completes."""
    cfg = Config(
        state_backend="memory", job_runner="local", api_token="t",
        planner="llm", llm_provider="mock", poll_interval_s=0.05,
    )
    resp = TestClient(build_app(cfg)).post(
        "/chat", json={"message": "transform: upper hello world"},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 200
    assert "[transform-worker] completed" in resp.json()["reply"]
