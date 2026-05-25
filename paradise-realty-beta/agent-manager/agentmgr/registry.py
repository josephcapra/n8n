"""Agent / task registry.

The Master consults the registry to learn what worker agents exist and what
each can do. Adding a worker later is data-only: append an entry to
``agents.json`` and deploy a Cloud Run Job with the matching ``job_name``.
No rearchitecting, no code change in the Master.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_REGISTRY = Path(__file__).with_name("agents.json")


@dataclass(frozen=True)
class AgentSpec:
    name: str
    kind: str
    job_name: str
    region: str
    description: str
    capabilities: tuple[str, ...]
    sensitive_default: bool
    # Optional friendly label for the GUI roster (falls back to ``name``).
    title: str = ""
    # How the agent runs:
    #   "cloudrun-job"  — a Cloud Run Job the Master triggers
    #   "local-agent"   — a long-lived local daemon that polls for its tasks
    #   "master-inline" — executed by the Master itself (no separate process)
    runtime: str = "cloudrun-job"
    # For cloudrun-job agents: the Python module exposing run_task() — used by
    # the local job runner, and as the Job's `python -m <module>` entrypoint.
    worker_module: str = ""
    # Rich, structured info for the GUI info sheet (the "ⓘ" button). Optional,
    # data-only. Keys: summary, tasks[], purpose, automation, talks_to[],
    # security, recent[]. ``recent`` is auto-refreshed by Scout from git history.
    details: dict = field(default_factory=dict)


class AgentRegistry:
    def __init__(self, agents: list[AgentSpec]) -> None:
        self._by_name = {a.name: a for a in agents}
        if len(self._by_name) != len(agents):
            raise ValueError("duplicate agent name in registry")

    @classmethod
    def load(cls, path: Path | None = None) -> "AgentRegistry":
        data = json.loads((path or _DEFAULT_REGISTRY).read_text())
        agents = [
            AgentSpec(
                name=a["name"],
                kind=a["kind"],
                job_name=a["job_name"],
                region=a["region"],
                description=a["description"],
                capabilities=tuple(a.get("capabilities", [])),
                sensitive_default=bool(a.get("sensitive_default", False)),
                title=a.get("title", ""),
                runtime=a.get("runtime", "cloudrun-job"),
                worker_module=a.get("worker_module", ""),
                details=a.get("details", {}) or {},
            )
            for a in data["agents"]
        ]
        return cls(agents)

    def get(self, name: str) -> AgentSpec:
        if name not in self._by_name:
            raise KeyError(f"no registered agent named {name!r}")
        return self._by_name[name]

    def all(self) -> list[AgentSpec]:
        return list(self._by_name.values())

    def find_by_capability(self, capability: str) -> list[AgentSpec]:
        return [a for a in self.all() if capability in a.capabilities]


def registry_path() -> Path:
    """Path to the JSON registry file the Master loads."""
    return _DEFAULT_REGISTRY


def append_agent_to_file(entry: dict, path: Path | None = None) -> None:
    """Append one agent entry to ``agents.json`` (data-only registration).

    Validates the new name is unique within the file, then writes it back so
    the addition survives restarts. Callers should reload the registry after.
    """
    p = path or _DEFAULT_REGISTRY
    data = json.loads(p.read_text())
    existing = {a["name"] for a in data.get("agents", [])}
    if entry["name"] in existing:
        raise ValueError(f"agent {entry['name']!r} already in registry")
    data.setdefault("agents", []).append(entry)
    p.write_text(json.dumps(data, indent=2) + "\n")


# Fields the PATCH /agents/{name} endpoint is allowed to change. Identity-bearing
# fields (name, kind, runtime, job_name, region, worker_module) stay immutable —
# changing them would silently break callers + the worker contract. ``details``
# is the structured info-sheet content; it's merged key-by-key (see below).
_EDITABLE_AGENT_FIELDS = ("title", "description", "details")


def update_agent_in_file(
    name: str, updates: dict, path: Path | None = None
) -> dict:
    """Patch an existing agent entry in ``agents.json`` (rename / re-describe).

    Only the safe display fields in ``_EDITABLE_AGENT_FIELDS`` are honoured —
    other keys are silently ignored so a stale frontend can't rewrite an
    agent's identity. Returns the updated entry. Callers should reload the
    registry after so live requests see the change.
    """
    p = path or _DEFAULT_REGISTRY
    data = json.loads(p.read_text())
    agents = data.get("agents", [])
    for entry in agents:
        if entry.get("name") == name:
            applied = False
            for k in _EDITABLE_AGENT_FIELDS:
                if k not in updates:
                    continue
                if k == "details" and isinstance(updates[k], dict):
                    # Merge so editing one section keeps the others (and the
                    # Scout-maintained ``recent``) intact.
                    merged = dict(entry.get("details") or {})
                    merged.update(updates[k])
                    entry["details"] = merged
                else:
                    entry[k] = updates[k]
                applied = True
            if not applied:
                raise ValueError("no editable fields provided")
            p.write_text(json.dumps(data, indent=2) + "\n")
            return entry
    raise KeyError(f"no registered agent named {name!r}")
