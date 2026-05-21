"""Command planner — decomposition + routing (Phase 2)."""

from __future__ import annotations

from agentmgr.config import Config
from agentmgr.planner import RuleBasedPlanner, make_planner
from agentmgr.registry import AgentRegistry

_REG = AgentRegistry.load()
_P = RuleBasedPlanner()


def test_plain_text_routes_to_the_assistant():
    plan = _P.plan("summarize my repo", _REG)
    assert len(plan.steps) == 1
    assert plan.steps[0].agent == "assistant"
    assert plan.steps[0].payload["goal"] == "summarize my repo"
    assert plan.steps[0].consume_previous is False


def test_shell_routes_to_mac_agent():
    step = _P.plan("$ git status", _REG).steps[0]
    assert step.agent == "mac-shell"
    assert step.payload["command"] == "git status"
    assert step.sensitive is True


def test_cloud_run_routes_to_admin():
    step = _P.plan("cloud run, list jobs", _REG).steps[0]
    assert step.agent == "cloudrun-admin"
    assert step.payload["op"] == "list_jobs"


def test_transform_segment_parsed():
    step = _P.plan("transform: upper hello there", _REG).steps[0]
    assert step.agent == "transform-worker"
    assert step.payload["op"] == "upper"
    assert step.payload["text"] == "hello there"


def test_transform_relay_suffix():
    step = _P.plan("transform: upper hi > echo-worker", _REG).steps[0]
    assert step.payload["relay_to"] == "echo-worker"


def test_pipeline_decomposes_and_marks_consume_previous():
    plan = _P.plan("transform: upper hi | transform: reverse | wrap it up", _REG)
    assert [s.agent for s in plan.steps] == [
        "transform-worker", "transform-worker", "assistant",
    ]
    assert plan.steps[0].consume_previous is False
    assert plan.steps[1].consume_previous is True
    assert plan.steps[2].consume_previous is True


def test_force_sensitive_prefix():
    assert _P.plan("!echo secret", _REG).steps[0].sensitive is True
    assert _P.plan("echo secret", _REG).steps[0].sensitive is False


def test_make_planner_factory():
    assert isinstance(make_planner(Config(planner="rule")), RuleBasedPlanner)
