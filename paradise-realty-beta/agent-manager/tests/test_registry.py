"""Agent registry — loading and lookups."""

from __future__ import annotations

import pytest

from agentmgr.registry import AgentRegistry


def test_load_default_registry():
    registry = AgentRegistry.load()
    echo = registry.get("echo-worker")
    assert echo.job_name == "agentmgr-worker-echo"
    assert echo.region == "us-east1"
    assert "echo" in echo.capabilities
    assert echo.sensitive_default is False


def test_unknown_agent_raises():
    registry = AgentRegistry.load()
    with pytest.raises(KeyError):
        registry.get("does-not-exist")


def test_find_by_capability():
    registry = AgentRegistry.load()
    assert [a.name for a in registry.find_by_capability("echo")] == ["echo-worker"]
    assert registry.find_by_capability("nonexistent") == []
