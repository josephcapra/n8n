"""Command planner — decomposes a natural-language command into subtasks.

Two implementations behind one :class:`Planner` interface:
  * :class:`RuleBasedPlanner` — deterministic, no LLM (Phase 2).
  * :class:`LLMPlanner` — uses the multi-provider LLM router (Phase 3); falls
    back to the rule-based planner on any failure, so the system degrades
    gracefully when no API key / budget / network is available.

Command grammar understood by the rule-based planner (Phase 2):
  * ``a | b | c``         — a pipeline; each stage consumes the previous output.
  * ``$ <cmd>`` / ``run: <cmd>`` / ``gcloud ...``  — shell, to the Mac agent.
  * ``cloud run ...``     — Cloud Run read, to the Master-inline admin.
  * ``transform: <op> <text> [> <agent>]``  — text transform, optional relay.
  * ``!<segment>``        — force the subtask SENSITIVE.
  * anything else         — echo placeholder worker.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .logging_utils import get_logger
from .registry import AgentRegistry

log = get_logger("agentmgr.planner")


@dataclass
class PlanStep:
    agent: str
    kind: str
    payload: dict
    sensitive: bool = False
    consume_previous: bool = False  # pipeline: feed the prior step's output in


@dataclass
class Plan:
    summary: str
    steps: list[PlanStep] = field(default_factory=list)


class Planner(ABC):
    @abstractmethod
    def plan(
        self, message: str, registry: AgentRegistry, correlation_id: str = ""
    ) -> Plan:
        """Decompose ``message`` into an ordered list of subtasks."""


class RuleBasedPlanner(Planner):
    """Deterministic planner — no LLM."""

    def plan(
        self, message: str, registry: AgentRegistry, correlation_id: str = ""
    ) -> Plan:
        segments = [s.strip() for s in message.split(" | ") if s.strip()]
        if not segments:
            segments = [message.strip()]
        steps = [self._interpret(seg) for seg in segments]
        for i, step in enumerate(steps):
            step.consume_previous = i > 0
        summary = f"{len(steps)} step(s): " + " -> ".join(s.agent for s in steps)
        return Plan(summary=summary, steps=steps)

    def _interpret(self, segment: str) -> PlanStep:
        sensitive = segment.startswith("!")
        seg = segment[1:].strip() if sensitive else segment
        low = seg.lower()

        if seg.startswith("$ "):
            return PlanStep("mac-shell", "shell", {"command": seg[2:].strip()}, True)
        if low.startswith("run:"):
            return PlanStep("mac-shell", "shell", {"command": seg[4:].strip()}, True)
        if low.startswith("gcloud "):
            return PlanStep("mac-shell", "shell", {"command": seg}, True)

        if "cloud run" in low:
            op = "list_jobs" if "job" in low else "list_services"
            return PlanStep("cloudrun-admin", "cloudrun", {"op": op}, sensitive)

        if low.startswith("transform:"):
            spec = seg.split(":", 1)[1].strip()
            relay_to = None
            if " > " in spec:
                spec, _, relay_to = spec.partition(" > ")
                spec, relay_to = spec.strip(), relay_to.strip()
            op, _, text = spec.partition(" ")
            payload: dict = {"op": op or "upper", "text": text}
            if relay_to:
                payload["relay_to"] = relay_to
            return PlanStep("transform-worker", "transform", payload, sensitive)

        # Plain natural language -> the agentic assistant (the default route).
        return PlanStep("assistant", "assistant", {"goal": seg}, sensitive)


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of an LLM response (handles code fences)."""
    t = text.strip()
    if t.startswith("```"):
        parts = t.split("```")
        if len(parts) >= 2:
            t = parts[1]
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found in LLM response")
    return json.loads(t[start : end + 1])


class LLMPlanner(Planner):
    """Decomposes commands with the LLM router. Falls back to the rule-based
    planner on ANY failure (missing key, network error, budget denial,
    unparseable output) — the system never hard-fails on planning."""

    def __init__(self, router, max_tokens: int = 4096) -> None:
        self._router = router
        self._max_tokens = max_tokens
        self._fallback = RuleBasedPlanner()

    def plan(
        self, message: str, registry: AgentRegistry, correlation_id: str = ""
    ) -> Plan:
        try:
            return self._llm_plan(message, registry, correlation_id)
        except Exception as exc:  # noqa: BLE001 - graceful degradation by design
            log.warning(
                "LLM planning failed; falling back to the rule-based planner",
                extra={"error": f"{type(exc).__name__}: {exc}"},
            )
            return self._fallback.plan(message, registry, correlation_id)

    def _llm_plan(
        self, message: str, registry: AgentRegistry, correlation_id: str
    ) -> Plan:
        from .llm import LLMRequest

        agents_desc = "\n".join(
            f"- {a.name} (kind={a.kind}, capabilities={list(a.capabilities)}): "
            f"{a.description}"
            for a in registry.all()
        )
        system = (
            "You are the routing planner for an agent-manager system. Decompose "
            "the user's command into an ordered list of subtasks, each routed to "
            "one of the available agents. Respond with ONLY a JSON object — no "
            "prose, no code fence — matching this shape:\n"
            '{"summary": "<short>", "steps": [{"agent": "<name>", '
            '"kind": "<kind>", "payload": {}, "sensitive": false, '
            '"consume_previous": false}]}\n'
            "Rules: 'agent' must be one of the listed agent names; 'kind' must be "
            "that agent's kind; set 'consume_previous' true when a step should "
            "receive the previous step's output; set 'sensitive' true for "
            "anything touching the shell, credentials, or money.\n\n"
            f"Available agents:\n{agents_desc}"
        )
        request = LLMRequest(
            system=system,
            prompt=f"Command: {message}",
            max_tokens=self._max_tokens,
        )
        response = self._router.complete(request, correlation_id=correlation_id)
        data = _extract_json(response.text)

        steps: list[PlanStep] = []
        for raw in data.get("steps", []):
            agent = registry.get(raw["agent"])  # KeyError on invalid -> caught
            steps.append(
                PlanStep(
                    agent=agent.name,
                    kind=str(raw.get("kind") or agent.kind),
                    payload=dict(raw.get("payload") or {}),
                    sensitive=bool(raw.get("sensitive", agent.sensitive_default)),
                    consume_previous=bool(raw.get("consume_previous", False)),
                )
            )
        if not steps:
            raise ValueError("LLM plan contained no steps")
        summary = str(data.get("summary") or f"{len(steps)} step(s)")
        return Plan(summary=f"[LLM:{self._router.provider_name}] {summary}", steps=steps)


def make_planner(config, router=None) -> Planner:
    """Factory — the single place a planner implementation is chosen."""
    if config.planner == "rule":
        return RuleBasedPlanner()
    if config.planner == "llm":
        if router is None:
            raise ValueError("the 'llm' planner requires an LLM router")
        return LLMPlanner(router, max_tokens=config.llm_max_tokens)
    raise ValueError(f"unknown planner: {config.planner!r}")
