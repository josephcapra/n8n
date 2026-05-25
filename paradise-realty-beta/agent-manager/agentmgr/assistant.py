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

import base64
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .config import Config
from .logging_utils import get_logger

log = get_logger("agentmgr.assistant")

_IMAGE_MEDIA_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def build_user_content(goal: str, attachments: list[dict] | None = None) -> list[dict]:
    """Build the first user-turn content for Claude.

    Image attachments become base64 image blocks so Claude can actually *see*
    screenshots; every attachment (image or not) is also listed by on-disk path
    so the assistant can read/process it with shell commands. Returns a content
    block list (text-only when there are no attachments)."""
    blocks: list[dict] = []
    notes: list[str] = []
    for att in attachments or []:
        path = att.get("path") or ""
        name = att.get("filename") or (os.path.basename(path) if path else "file")
        media_type = att.get("media_type") or ""
        if att.get("kind") == "image" and media_type in _IMAGE_MEDIA_TYPES and os.path.exists(path):
            try:
                data = base64.standard_b64encode(open(path, "rb").read()).decode()
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": data},
                })
                notes.append(f"- {name} — image shown above (also saved at {path})")
                continue
            except OSError:
                pass
        notes.append(f"- {name} ({media_type or 'file'}) saved at {path}")

    text = f"Goal: {goal}"
    if notes:
        text += (
            "\n\nThe operator attached these files. Images are included above for "
            "you to view directly; read any non-image files from disk with shell "
            "commands when relevant:\n" + "\n".join(notes)
        )
    blocks.append({"type": "text", "text": text})
    return blocks


def _text_goal(goal: str, attachments: list[dict] | None = None) -> str:
    """Plain-text goal (with attachment path notes) for providers we drive in
    text-only mode (OpenAI, Gemini). Images aren't shown to these models in this
    build, but their on-disk paths are listed so the loop can read them."""
    blocks = build_user_content(goal, attachments)
    for b in blocks:
        if b.get("type") == "text":
            return b["text"]
    return f"Goal: {goal}"

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


def system_prompt(
    memory: str = "", connectors: str = "", history: str = "", agent_focus: str = ""
) -> str:
    """The assistant's system prompt, optionally with the operator's persistent
    memory, available connectors, and the recent conversation so it 'remembers'
    both across conversations (memory) and within this one (history). When
    ``agent_focus`` is set the assistant answers scoped to one specific agent
    (the left-rail "Message" chat) — placed first so it frames the whole reply."""
    parts = [_SYSTEM]
    if agent_focus:
        parts.append(agent_focus)
    if connectors:
        parts.append(connectors)
    if memory:
        parts.append(memory)
    if history:
        parts.append(history)
    return "\n\n".join(parts)


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
    def start(self, goal: str, system: str, attachments: list[dict] | None = None) -> None: ...

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
    attachments: list[dict] | None = None,
) -> AgenticResult:
    """Drive the agent loop.

    ``shell_runner(command) -> ShellResult`` gates and executes a command and
    must NOT raise — a denied command comes back as a ``ShellResult`` with
    ``gate='denied'``. The loop is bounded by ``max_steps``. ``attachments`` are
    files the operator shared (images shown to the model, all readable on disk).
    """
    driver.start(goal, system, attachments)
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

    def __init__(self, config: Config, model: str | None = None) -> None:
        self._model = model or config.anthropic_model
        self._api_key = config.anthropic_api_key
        self._max_tokens = max(config.llm_max_tokens, 4096)
        self._client = None
        self._system = _SYSTEM
        self._messages: list[dict] = []

    def start(self, goal: str, system: str, attachments: list[dict] | None = None) -> None:
        import anthropic  # lazy

        self._client = (
            anthropic.Anthropic(api_key=self._api_key)
            if self._api_key
            else anthropic.Anthropic()
        )
        self._system = system
        self._messages = [{"role": "user", "content": build_user_content(goal, attachments)}]

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


class OpenAIAgenticDriver(AgenticDriver):
    """OpenAI agentic driver — same run_shell tool loop via the openai SDK."""

    def __init__(self, config: Config, model: str | None = None) -> None:
        self._model = model or config.openai_model
        self._api_key = config.openai_api_key
        self._max_tokens = max(config.llm_max_tokens, 4096)
        self._client = None
        self._messages: list[dict] = []
        self._tools = [{
            "type": "function",
            "function": {
                "name": "run_shell",
                "description": _SHELL_TOOL["description"],
                "parameters": _SHELL_TOOL["input_schema"],
            },
        }]

    def start(self, goal: str, system: str, attachments: list[dict] | None = None) -> None:
        import openai  # lazy

        self._client = (
            openai.OpenAI(api_key=self._api_key) if self._api_key else openai.OpenAI()
        )
        self._messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": _text_goal(goal, attachments)},
        ]

    def next_step(self) -> ModelStep:
        import json
        resp = self._client.chat.completions.create(
            model=self._model, messages=self._messages,
            tools=self._tools, max_completion_tokens=self._max_tokens,
        )
        msg = resp.choices[0].message
        self._messages.append(msg.model_dump(exclude_none=True))
        calls = []
        for tc in (msg.tool_calls or []):
            if tc.function.name == "run_shell":
                try:
                    cmd = json.loads(tc.function.arguments or "{}").get("command", "")
                except ValueError:
                    cmd = ""
                calls.append(ShellCall(id=tc.id, command=str(cmd)))
        return ModelStep(text=msg.content or "", calls=calls, done=not calls)

    def add_results(self, results: list[ShellResult]) -> None:
        for r in results:
            self._messages.append(
                {"role": "tool", "tool_call_id": r.call_id, "content": r.feedback}
            )


class GoogleAgenticDriver(AgenticDriver):
    """Gemini agentic driver — same run_shell tool loop via the google-genai SDK."""

    def __init__(self, config: Config, model: str | None = None) -> None:
        self._model = model or config.google_model
        self._api_key = config.google_api_key
        self._max_tokens = max(config.llm_max_tokens, 4096)
        self._client = None
        self._contents: list = []
        self._config = None
        self._types = None

    def start(self, goal: str, system: str, attachments: list[dict] | None = None) -> None:
        from google import genai
        from google.genai import types

        self._types = types
        self._client = genai.Client(api_key=self._api_key)
        tool = types.Tool(function_declarations=[types.FunctionDeclaration(
            name="run_shell",
            description=_SHELL_TOOL["description"],
            parameters=_SHELL_TOOL["input_schema"],
        )])
        self._config = types.GenerateContentConfig(
            system_instruction=system, tools=[tool],
            max_output_tokens=self._max_tokens,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        self._contents = [types.Content(
            role="user", parts=[types.Part(text=_text_goal(goal, attachments))])]

    def next_step(self) -> ModelStep:
        resp = self._client.models.generate_content(
            model=self._model, contents=self._contents, config=self._config)
        cand = resp.candidates[0]
        self._contents.append(cand.content)
        parts = cand.content.parts or []
        text = "".join(p.text for p in parts if getattr(p, "text", None))
        calls = [
            ShellCall(id="run_shell", command=str(dict(p.function_call.args or {}).get("command", "")))
            for p in parts
            if getattr(p, "function_call", None) and p.function_call.name == "run_shell"
        ]
        return ModelStep(text=text, calls=calls, done=not calls)

    def add_results(self, results: list[ShellResult]) -> None:
        parts = [
            self._types.Part.from_function_response(
                name="run_shell", response={"output": r.feedback})
            for r in results
        ]
        self._contents.append(self._types.Content(role="user", parts=parts))


class ScriptedDriver(AgenticDriver):
    """Deterministic driver for tests — replays a fixed list of model steps."""

    def __init__(self, steps: list[ModelStep]) -> None:
        self._steps = list(steps)
        self.results_log: list[list[ShellResult]] = []
        self.goal = ""

    def start(self, goal: str, system: str, attachments: list[dict] | None = None) -> None:
        self.goal = goal
        self.attachments = attachments or []

    def next_step(self) -> ModelStep:
        return self._steps.pop(0) if self._steps else ModelStep(
            text="done", done=True
        )

    def add_results(self, results: list[ShellResult]) -> None:
        self.results_log.append(results)


def provider_for_model(model: str | None) -> str:
    """Map a model id to its provider. Defaults to anthropic."""
    m = (model or "").lower()
    if m.startswith("gpt") or m.startswith("o1") or m.startswith("o3") or m.startswith("o4") or m.startswith("chatgpt"):
        return "openai"
    if m.startswith("gemini"):
        return "google"
    return "anthropic"


def make_driver(config: Config, model: str | None = None) -> AgenticDriver:
    """Factory — pick the agentic driver by ``model``. A Claude model (or None)
    uses the Anthropic driver; gpt-*/o-* use OpenAI; gemini-* use Gemini. All
    drive the same run_shell tool loop."""
    provider = provider_for_model(model)
    if provider == "openai":
        return OpenAIAgenticDriver(config, model=model)
    if provider == "google":
        return GoogleAgenticDriver(config, model=model)
    return AnthropicAgenticDriver(config, model=model)


# --- cost-aware routing --------------------------------------------------
# Keep everyday work off the priciest model. The agentic shell loop stays on
# Claude (its tool-use protocol), but the *tier* scales with the task; the
# cheapest, no-tool conversational/recall queries can skip Claude entirely
# (see the Gemini fast-path in agent/local_agent.process_assistant_task).
_OPUS = "claude-opus-4-7"      # $5/$25 — complex, multi-step work only
_SONNET = "claude-sonnet-4-6"  # $3/$15 — capable default for real tasks
_HAIKU = "claude-haiku-4-5"    # $1/$5  — light/simple tasks

_COMPLEX_HINTS = (
    "deploy", "build", "refactor", "audit", "migrate", "investigate",
    "across all", "every ", "all the", "pipeline", "end to end", "end-to-end",
    "set up", "configure", "provision", "orchestrate", "step by step",
    "rewrite", "redesign", "analyze", "diagnose",
)
_TRIVIAL_HINTS = (
    "hi", "hello", "hey", "thanks", "thank you", "yo ", "good morning",
    "what did i", "what's my", "what is my", "what do you remember",
    "who are you", "what can you do", "what were we", "remind me what",
)
# An action on the Mac/cloud → must use the tool-using loop, never a one-shot.
_ACTION_HINTS = (
    "run ", " ls", "ls ", "cat ", "open ", "deploy", "build", "git ", "gcloud",
    "list ", "show me", "check ", "find ", "search ", "read ", "write ", "edit ",
    "create ", "delete", "remove", "install", "fix ", "update ", "restart",
    "kill ", "curl", "file", "folder", "directory", "log", "report", "job",
    "scrape", "email", "lead", "crm", "website", "push", "pull", "commit",
    "deploy", "screenshot", "download", "upload",
)


def select_model(goal: str) -> str:
    """Pick the cheapest capable Claude tier for an agentic goal."""
    g = goal.strip().lower()
    if len(goal) > 400 or any(h in g for h in _COMPLEX_HINTS):
        return _OPUS
    if len(goal) < 36 or any(h in g for h in _TRIVIAL_HINTS):
        return _HAIKU
    return _SONNET


def classify_query(goal: str) -> str:
    """'chat' = answerable from memory/history/general knowledge with no Mac
    tools (eligible for the cheap one-shot fast-path); 'task' = needs the
    agentic shell loop. Conservative — anything action-like stays a 'task'."""
    g = goal.strip().lower()
    if any(h in g for h in _ACTION_HINTS):
        return "task"
    if len(goal) <= 200 and any(h in g for h in _TRIVIAL_HINTS):
        return "chat"
    return "task"
