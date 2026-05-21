"""Agentic assistant — a Claude tool-use loop that operates your Mac terminal.

Give it a natural-language goal; it runs an agentic loop — Claude proposes one
shell command, the command is gated and executed, the output is fed back, and
Claude decides the next step — until the goal is met or a step limit is hit.

Gating follows the operator's posture (minimal permissions, approval before
major decisions): read-only commands auto-run, everything else blocks on the
approval gate — see ``command_policy``.

``run_agentic_loop`` is provider-agnostic; it drives an :class:`AgenticDriver`.
:class:`AnthropicAgenticDriver` is the production driver (Claude, official SDK,
tool use). :class:`ScriptedDriver` backs the tests.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .config import Config
from .logging_utils import get_logger

log = get_logger("agentmgr.assistant")

# The single tool Claude is given: run one shell command on the Mac.
_SHELL_TOOL = {
    "name": "run_shell",
    "description": (
        "Run ONE shell command on the operator's Mac and return its stdout, "
        "stderr, and exit code. Run a single command at a time — do not chain "
        "with ; && || or pipes. Read-only commands run immediately; commands "
        "that modify the system require the operator's approval and may be "
        "denied."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "the shell command"},
        },
        "required": ["command"],
    },
}

_SYSTEM = (
    "You are a helpful assistant that can operate the operator's Mac terminal "
    "to get things done. Work in small steps: run one command, read its "
    "output, then decide the next. Prefer read-only commands — they run "
    "without interruption. Commands that change the system need the operator's "
    "approval and can be denied; if one is denied, adapt or stop.\n\n"
    "When you're done, reply the way a knowledgeable colleague would in a chat: "
    "a few plain, friendly sentences. Lead with the answer or what you did. "
    "Do not narrate each command you ran, do not paste raw terminal output or "
    "file dumps unless the operator asked to see them, and never repeat the "
    "same point twice. Skip markdown headings, tables, and code fences — just "
    "talk normally. Keep it short; if there's nothing else to say, a single "
    "sentence is perfect."
)


def system_prompt(memory: str = "") -> str:
    """The assistant's system prompt, optionally with the operator's persistent
    memory appended so it 'remembers' across conversations."""
    return f"{_SYSTEM}\n\n{memory}" if memory else _SYSTEM


@dataclass
class ShellCall:
    id: str
    command: str


@dataclass
class ModelStep:
    text: str = ""
    calls: list[ShellCall] = field(default_factory=list)
    done: bool = False


@dataclass
class ShellResult:
    command: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    gate: str = "auto"          # "auto" | "approved" | "denied"
    call_id: str = ""

    @property
    def feedback(self) -> str:
        """What the model is told happened."""
        if self.gate == "denied":
            return f"DENIED by the operator ({self.stderr}). Do not retry — adapt or stop."
        return (
            f"exit_code={self.exit_code}\n"
            f"stdout:\n{self.stdout[-6000:]}\n"
            f"stderr:\n{self.stderr[-2000:]}"
        )


@dataclass
class AgenticResult:
    answer: str
    transcript: list[dict] = field(default_factory=list)
    steps: int = 0
    hit_limit: bool = False


class AgenticDriver(ABC):
    """The model side of the loop — provider-specific."""

    @abstractmethod
    def start(self, goal: str, system: str) -> None: ...

    @abstractmethod
    def next_step(self) -> ModelStep: ...

    @abstractmethod
    def add_results(self, results: list[ShellResult]) -> None: ...


def run_agentic_loop(
    driver: AgenticDriver,
    goal: str,
    *,
    shell_runner,
    max_steps: int,
    system: str = _SYSTEM,
) -> AgenticResult:
    """Drive the agent loop.

    ``shell_runner(command) -> ShellResult`` gates and executes a command and
    must NOT raise — a denied command comes back as a ``ShellResult`` with
    ``gate='denied'``. The loop is bounded by ``max_steps``.
    """
    driver.start(goal, system)
    transcript: list[dict] = []
    for step in range(1, max_steps + 1):
        model_step = driver.next_step()
        if model_step.text:
            transcript.append({"type": "thought", "text": model_step.text})
        if model_step.done or not model_step.calls:
            return AgenticResult(
                answer=model_step.text or "(no summary)",
                transcript=transcript, steps=step,
            )

        results: list[ShellResult] = []
        for call in model_step.calls:
            result = shell_runner(call.command)
            result.call_id = call.id
            results.append(result)
            transcript.append({
                "type": "command",
                "command": call.command,
                "gate": result.gate,
                "exit_code": result.exit_code,
                "stdout": result.stdout[-2000:],
                "stderr": result.stderr[-2000:],
            })
            log.info(
                "assistant ran a command",
                extra={"command": call.command, "gate": result.gate,
                       "exit_code": result.exit_code},
            )
        driver.add_results(results)

    return AgenticResult(
        answer="(stopped: reached the step limit)",
        transcript=transcript, steps=max_steps, hit_limit=True,
    )


class AnthropicAgenticDriver(AgenticDriver):
    """Production driver — Claude tool use via the official ``anthropic`` SDK."""

    def __init__(self, config: Config) -> None:
        self._model = config.anthropic_model
        self._api_key = config.anthropic_api_key
        self._max_tokens = max(config.llm_max_tokens, 4096)
        self._client = None
        self._system = _SYSTEM
        self._messages: list[dict] = []

    def start(self, goal: str, system: str) -> None:
        import anthropic  # lazy

        self._client = (
            anthropic.Anthropic(api_key=self._api_key)
            if self._api_key
            else anthropic.Anthropic()
        )
        self._system = system
        self._messages = [{"role": "user", "content": f"Goal: {goal}"}]

    def next_step(self) -> ModelStep:
        message = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=self._system,
            tools=[_SHELL_TOOL],
            messages=self._messages,
        )
        # Append the full content (preserves tool_use blocks for the next turn).
        self._messages.append({"role": "assistant", "content": message.content})
        text = "".join(b.text for b in message.content if b.type == "text")
        calls = [
            ShellCall(id=b.id, command=str(b.input.get("command", "")))
            for b in message.content
            if b.type == "tool_use" and b.name == "run_shell"
        ]
        return ModelStep(text=text, calls=calls, done=not calls)

    def add_results(self, results: list[ShellResult]) -> None:
        self._messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": r.call_id,
                    "content": r.feedback,
                    "is_error": r.gate == "denied" or r.exit_code != 0,
                }
                for r in results
            ],
        })


class ScriptedDriver(AgenticDriver):
    """Deterministic driver for tests — replays a fixed list of model steps."""

    def __init__(self, steps: list[ModelStep]) -> None:
        self._steps = list(steps)
        self.results_log: list[list[ShellResult]] = []
        self.goal = ""

    def start(self, goal: str, system: str) -> None:
        self.goal = goal

    def next_step(self) -> ModelStep:
        return self._steps.pop(0) if self._steps else ModelStep(
            text="done", done=True
        )

    def add_results(self, results: list[ShellResult]) -> None:
        self.results_log.append(results)


def make_driver(config: Config) -> AgenticDriver:
    """Factory — the production driver. (Provider-pluggable; Claude for now.)"""
    return AnthropicAgenticDriver(config)
