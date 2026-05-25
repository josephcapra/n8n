"""Multi-provider LLM router (Phase 3).

The Master can send reasoning/subtasks to Anthropic, OpenAI, or Google, chosen
by config (``AGENTMGR_LLM_PROVIDER``). Every call is cost-logged — per request
and cumulatively per command — and a configurable per-command budget ceiling
trips the approval gate when exceeded.

API keys come from the environment (injected from Secret Manager at deploy
time) — never hardcoded.

Provider notes:
  * **Anthropic** — built to spec with the official ``anthropic`` SDK: model
    ``claude-opus-4-7``, adaptive thinking, and prompt caching on the stable
    system prefix.
  * **OpenAI / Google** — implemented with their official SDKs (lazy-imported).
    Because they cannot be exercised without live keys, verify the call shape
    against your chosen model before relying on them in production.
  * **mock** — deterministic, no network; the default, used for tests/local.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .config import Config
from .logging_utils import get_logger
from .state_store import StateStore
from .util import retry

log = get_logger("agentmgr.llm")


class LLMProviderError(RuntimeError):
    """A provider could not be used (missing SDK, missing model, etc.)."""


# --- request / response --------------------------------------------------

@dataclass
class LLMRequest:
    prompt: str
    system: str = ""
    max_tokens: int = 4096
    thinking: bool = True            # adaptive thinking (Anthropic)
    effort: str = "medium"           # low | medium | high (Anthropic)


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0


# --- pricing -------------------------------------------------------------

# USD per 1M tokens: (input, output). Anthropic prices are current as of the
# claude-api skill snapshot. Models not listed fall back to the configurable
# AGENTMGR_LLM_PRICE_IN / _OUT override (0 => cost logged as 0 with a warning).
_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    # Cheap non-Anthropic tiers used for the conversational fast-path.
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.0-flash": (0.10, 0.40),
    "gpt-4o-mini": (0.15, 0.60),
}


def compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    price_override: tuple[float, float] = (0.0, 0.0),
) -> float:
    """Cost in USD. Cached reads bill ~0.1x input, cache writes ~1.25x input."""
    in_price, out_price = _PRICING.get(model, price_override)
    if (in_price, out_price) == (0.0, 0.0):
        log.warning("no pricing for model — cost logged as 0", extra={"model": model})
    return (
        input_tokens * in_price
        + output_tokens * out_price
        + cache_read_tokens * in_price * 0.1
        + cache_write_tokens * in_price * 1.25
    ) / 1_000_000


# --- providers -----------------------------------------------------------

class LLMProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse: ...


class AnthropicProvider(LLMProvider):
    """Anthropic Messages API via the official SDK (claude-api skill compliant)."""

    name = "anthropic"

    def __init__(self, config: Config) -> None:
        self._model = config.anthropic_model
        self._api_key = config.anthropic_api_key
        self._price_override = (
            config.llm_price_in_per_mtok, config.llm_price_out_per_mtok
        )

    def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            import anthropic  # lazy — keeps the module importable without the SDK
        except ImportError as exc:  # pragma: no cover
            raise LLMProviderError("anthropic SDK not installed") from exc

        client = (
            anthropic.Anthropic(api_key=self._api_key)
            if self._api_key
            else anthropic.Anthropic()
        )
        kwargs: dict = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "messages": [{"role": "user", "content": request.prompt}],
        }
        if request.system:
            # Prompt caching: cache the stable system prefix. (For prefixes
            # below the model's minimum cacheable size this is a no-op, but it
            # is correct as the system prompt grows.)
            kwargs["system"] = [{
                "type": "text",
                "text": request.system,
                "cache_control": {"type": "ephemeral"},
            }]
        if request.thinking:
            # Adaptive thinking is the recommended mode for Opus 4.7.
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": request.effort}

        message = retry(lambda: client.messages.create(**kwargs), attempts=3)
        text = "".join(b.text for b in message.content if b.type == "text")
        usage = message.usage
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        return LLMResponse(
            text=text,
            provider=self.name,
            model=self._model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cost_usd=compute_cost(
                self._model, usage.input_tokens, usage.output_tokens,
                cache_read, cache_write, self._price_override,
            ),
        )


class OpenAIProvider(LLMProvider):
    """OpenAI Chat Completions via the official SDK (lazy-imported).

    Best-effort — verify the call shape against your chosen model, since this
    path cannot be exercised here without a live key.
    """

    name = "openai"

    def __init__(self, config: Config) -> None:
        self._model = config.openai_model
        self._api_key = config.openai_api_key
        self._price_override = (
            config.llm_price_in_per_mtok, config.llm_price_out_per_mtok
        )

    def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            import openai
        except ImportError as exc:
            raise LLMProviderError(
                "openai provider requires `pip install openai`"
            ) from exc
        if not self._model:
            raise LLMProviderError("set AGENTMGR_OPENAI_MODEL")

        client = (
            openai.OpenAI(api_key=self._api_key)
            if self._api_key
            else openai.OpenAI()
        )
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})
        resp = retry(
            lambda: client.chat.completions.create(
                model=self._model,
                messages=messages,
                max_completion_tokens=request.max_tokens,
            ),
            attempts=3,
        )
        text = resp.choices[0].message.content or ""
        usage = resp.usage
        return LLMResponse(
            text=text, provider=self.name, model=self._model,
            input_tokens=usage.prompt_tokens, output_tokens=usage.completion_tokens,
            cost_usd=compute_cost(
                self._model, usage.prompt_tokens, usage.completion_tokens,
                price_override=self._price_override,
            ),
        )


class GoogleProvider(LLMProvider):
    """Google Gemini via the official ``google-genai`` SDK (lazy-imported).

    Best-effort — verify the call shape against your chosen model.
    """

    name = "google"

    def __init__(self, config: Config) -> None:
        self._model = config.google_model
        self._api_key = config.google_api_key
        self._price_override = (
            config.llm_price_in_per_mtok, config.llm_price_out_per_mtok
        )

    def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            from google import genai
        except ImportError as exc:
            raise LLMProviderError(
                "google provider requires `pip install google-genai`"
            ) from exc
        if not self._model:
            raise LLMProviderError("set AGENTMGR_GOOGLE_MODEL")

        client = genai.Client(api_key=self._api_key)
        prompt = (
            f"{request.system}\n\n{request.prompt}" if request.system
            else request.prompt
        )
        resp = retry(
            lambda: client.models.generate_content(
                model=self._model, contents=prompt
            ),
            attempts=3,
        )
        usage = getattr(resp, "usage_metadata", None)
        in_tok = getattr(usage, "prompt_token_count", 0) or 0
        out_tok = getattr(usage, "candidates_token_count", 0) or 0
        return LLMResponse(
            text=resp.text or "", provider=self.name, model=self._model,
            input_tokens=in_tok, output_tokens=out_tok,
            cost_usd=compute_cost(
                self._model, in_tok, out_tok, price_override=self._price_override
            ),
        )


class MockLLMProvider(LLMProvider):
    """Deterministic provider for local dev and tests — no network, no keys."""

    name = "mock"

    def __init__(
        self,
        reply: str = "mock-llm-response",
        input_tokens: int = 1000,
        output_tokens: int = 200,
        cost_usd: float | None = None,
    ) -> None:
        self._reply = reply
        self._in = input_tokens
        self._out = output_tokens
        self._cost = cost_usd

    def complete(self, request: LLMRequest) -> LLMResponse:
        cost = (
            self._cost if self._cost is not None
            else compute_cost("claude-opus-4-7", self._in, self._out)
        )
        return LLMResponse(
            text=self._reply, provider=self.name, model="mock-1",
            input_tokens=self._in, output_tokens=self._out, cost_usd=cost,
        )


def make_provider(config: Config) -> LLMProvider:
    """Factory — the single place an LLM provider is chosen."""
    providers = {
        "anthropic": AnthropicProvider,
        "openai": OpenAIProvider,
        "google": GoogleProvider,
        "mock": lambda _cfg: MockLLMProvider(),
    }
    if config.llm_provider not in providers:
        raise ValueError(f"unknown llm provider: {config.llm_provider!r}")
    return providers[config.llm_provider](config)


# --- router (cost logging + budget ceiling) ------------------------------

class LLMRouter:
    """Routes a request to the configured provider, logs cost, enforces budget.

    Before each call, if a command's cumulative LLM spend has already reached
    the budget ceiling, the call BLOCKS on the approval gate — a human must
    sign off before more spend is incurred.
    """

    def __init__(
        self,
        provider: LLMProvider,
        store: StateStore,
        gate,
        budget_usd: float,
    ) -> None:
        self._provider = provider
        self._store = store
        self._gate = gate
        self._budget_usd = budget_usd

    @property
    def provider_name(self) -> str:
        return self._provider.name

    def complete(self, request: LLMRequest, *, correlation_id: str) -> LLMResponse:
        spent = self._store.get_llm_cost(correlation_id)
        if spent >= self._budget_usd:
            log.warning(
                "LLM budget ceiling reached — routing through approval gate",
                extra={"spent_usd": round(spent, 6), "budget_usd": self._budget_usd},
            )
            self._gate.request_approval(
                "llm-spend-over-budget",
                {
                    "spent_usd": round(spent, 6),
                    "budget_usd": self._budget_usd,
                    "spend_usd": round(spent, 6),  # classified SENSITIVE too
                },
            )

        response = self._provider.complete(request)
        command_total = self._store.record_llm_cost(correlation_id, response.cost_usd)
        log.info(
            "llm call",
            extra={
                "provider": response.provider,
                "model": response.model,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "cache_read_tokens": response.cache_read_tokens,
                "request_cost_usd": round(response.cost_usd, 6),
                "command_total_usd": round(command_total, 6),
            },
        )
        return response


def make_llm_router(config: Config, store: StateStore, gate) -> LLMRouter:
    """Factory — the single place the router is assembled."""
    return LLMRouter(make_provider(config), store, gate, config.llm_budget_usd)
