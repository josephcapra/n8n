"""Agent registry — loading and lookups."""

from __future__ import annotations

import json

import pytest

from agentmgr.registry import (
    AgentRegistry,
    append_agent_to_file,
    update_agent_in_file,
)


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


# --- update_agent_in_file (the PATCH /agents/{name} backing helper) ----------

def _seed_registry(tmp_path):
    """Write a small throwaway registry file and return its path."""
    p = tmp_path / "agents.json"
    p.write_text(json.dumps({
        "agents": [
            {
                "name": "demo",
                "kind": "demo",
                "job_name": "",
                "region": "local",
                "runtime": "local-agent",
                "description": "first description",
                "capabilities": ["x"],
                "sensitive_default": False,
            },
        ]
    }))
    return p


def test_update_agent_renames_title(tmp_path):
    p = _seed_registry(tmp_path)
    entry = update_agent_in_file("demo", {"title": "Demo · friendly"}, path=p)
    assert entry["title"] == "Demo · friendly"
    # change is persisted, name (identity) is untouched
    data = json.loads(p.read_text())
    assert data["agents"][0]["title"] == "Demo · friendly"
    assert data["agents"][0]["name"] == "demo"


def test_update_agent_can_change_description(tmp_path):
    p = _seed_registry(tmp_path)
    update_agent_in_file("demo", {"description": "new copy"}, path=p)
    assert json.loads(p.read_text())["agents"][0]["description"] == "new copy"


def test_update_agent_ignores_immutable_fields(tmp_path):
    """A stale frontend can't rewrite an agent's identity through this path."""
    p = _seed_registry(tmp_path)
    # `title` is honoured; `name`/`kind`/`runtime` are dropped.
    update_agent_in_file(
        "demo",
        {"title": "ok", "name": "evil", "kind": "evil", "runtime": "evil"},
        path=p,
    )
    e = json.loads(p.read_text())["agents"][0]
    assert e["title"] == "ok"
    assert e["name"] == "demo"
    assert e["kind"] == "demo"
    assert e["runtime"] == "local-agent"


def test_update_agent_merges_details_preserving_recent(tmp_path):
    """Editing one detail field keeps the others (esp. Scout's `recent`)."""
    p = _seed_registry(tmp_path)
    update_agent_in_file("demo", {"details": {"recent": ["r1"], "summary": "old"}}, path=p)
    update_agent_in_file("demo", {"details": {"summary": "new", "tasks": ["t"]}}, path=p)
    d = json.loads(p.read_text())["agents"][0]["details"]
    assert d["summary"] == "new"
    assert d["tasks"] == ["t"]
    assert d["recent"] == ["r1"]   # merge did not wipe the untouched key


def test_update_agent_unknown_name_raises(tmp_path):
    p = _seed_registry(tmp_path)
    with pytest.raises(KeyError):
        update_agent_in_file("nope", {"title": "x"}, path=p)


def test_update_agent_no_editable_fields_raises(tmp_path):
    p = _seed_registry(tmp_path)
    with pytest.raises(ValueError):
        update_agent_in_file("demo", {"kind": "x"}, path=p)


def test_update_then_load_returns_new_title(tmp_path):
    p = _seed_registry(tmp_path)
    update_agent_in_file("demo", {"title": "Demo!"}, path=p)
    reg = AgentRegistry.load(path=p)
    assert reg.get("demo").title == "Demo!"
