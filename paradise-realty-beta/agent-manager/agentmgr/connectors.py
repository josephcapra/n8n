"""Connector registry — the external services the master agent can use.

Two jobs:
  1. Load the consolidated `.env` so every connector's credentials land in the
     process environment (and therefore in the assistant's shell environment).
  2. Describe each connector so the assistant *knows* what it can reach and how
     — this manifest is injected into its system prompt.

Security: this module never logs or returns secret *values*. The status view
only reports whether each connector is configured; the prompt block references
environment-variable names, not the secrets themselves.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .logging_utils import get_logger

log = get_logger("agentmgr.connectors")

_DEFAULT_ENV = Path(__file__).resolve().parent.parent / ".env"


def load_env_file(path: str | os.PathLike | None = None, *, override: bool = False) -> int:
    """Load KEY=VALUE pairs from a .env file into os.environ. Existing values
    are kept unless ``override`` is set. Returns the count loaded. Never raises
    on a missing file."""
    p = Path(path or _DEFAULT_ENV).expanduser()
    try:
        lines = p.read_text().splitlines()
    except FileNotFoundError:
        return 0
    except Exception:  # noqa: BLE001
        log.warning("could not read env file", extra={"path": str(p)})
        return 0
    loaded = 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


@dataclass(frozen=True)
class Connector:
    id: str
    name: str
    required_env: tuple[str, ...]          # all must be set to be "configured"
    summary: str                            # what it's for
    usage: str                              # how the assistant should use it
    optional_env: tuple[str, ...] = field(default_factory=tuple)

    def configured(self) -> bool:
        if self.id == "google_drive":
            # Drive uses gcloud ADC, not an env key.
            return os.environ.get("GOOGLE_DRIVE_ENABLED", "") not in ("", "0", "false")
        return all(os.environ.get(k) for k in self.required_env)


CONNECTORS: tuple[Connector, ...] = (
    Connector(
        id="anthropic", name="Anthropic (Claude)",
        required_env=("ANTHROPIC_API_KEY",),
        summary="Claude models for reasoning and text.",
        usage="Key in $ANTHROPIC_API_KEY. POST https://api.anthropic.com/v1/messages "
              "with header 'x-api-key: $ANTHROPIC_API_KEY' and 'anthropic-version: 2023-06-01'.",
    ),
    Connector(
        id="openai", name="OpenAI",
        required_env=("OPENAI_API_KEY",),
        summary="GPT models and OpenAI APIs.",
        usage="Key in $OPENAI_API_KEY. POST https://api.openai.com/v1/chat/completions "
              "with 'Authorization: Bearer $OPENAI_API_KEY'.",
    ),
    Connector(
        id="google_gemini", name="Google (Gemini)",
        required_env=("GEMINI_API_KEY",),
        summary="Google Gemini models.",
        usage="Key in $GEMINI_API_KEY. Use the Generative Language API "
              "(generativelanguage.googleapis.com) with ?key=$GEMINI_API_KEY.",
    ),
    Connector(
        id="realgeeks", name="RealGeeks (CRM + Leads)",
        required_env=("REALGEEKS_USER", "REALGEEKS_PASS"),
        optional_env=("RG_API_BASE", "RG_SITE_UUID", "RG_GRANT_URL", "RG_PASSWORD"),
        summary="Paradise Realty's RealGeeks CRM and Leads API.",
        usage="Site login in $REALGEEKS_USER / $REALGEEKS_PASS. Leads API at "
              "$RG_API_BASE (basic auth, site UUID $RG_SITE_UUID). The CRM site is "
              "$REALGEEKS_URL.",
    ),
    Connector(
        id="beaches_mls", name="Beaches MLS (Spark)",
        required_env=("SPARK_ACCESS_TOKEN",),
        optional_env=("SPARK_BASE_URL",),
        summary="BeachesMLS listing data via the Spark Platform RESO OData feed.",
        usage="Token in $SPARK_ACCESS_TOKEN. RESO OData at $SPARK_BASE_URL "
              "(default https://replication.sparkapi.com) — e.g. GET "
              "/Reso/OData/Property with 'Authorization: Bearer $SPARK_ACCESS_TOKEN'. "
              "A ready client + analytics live in ~/paradise-realty/spark "
              "(pull_listings.py for filtered pulls, analytics.py for market/CMA).",
    ),
    Connector(
        id="google_drive", name="Google Drive",
        required_env=(),
        summary="Search and read the operator's Google Drive to find knowledge.",
        usage="Run `python -m tools.drive search \"<query>\"` to find files and "
              "`python -m tools.drive read <file_id>` to read one. Uses gcloud ADC; "
              "read-only.",
    ),
    Connector(
        id="bing", name="Bing Webmaster",
        required_env=("BING_API_KEY",),
        summary="Bing Webmaster Tools (indexing, search stats).",
        usage="Key in $BING_API_KEY for the Bing Webmaster API.",
    ),
    Connector(
        id="sendgrid", name="SendGrid",
        required_env=("SENDGRID_API_KEY",),
        summary="Transactional email.",
        usage="Key in $SENDGRID_API_KEY. POST https://api.sendgrid.com/v3/mail/send "
              "with 'Authorization: Bearer $SENDGRID_API_KEY'. (Sends real email — "
              "confirm with the operator first.)",
    ),
    Connector(
        id="gcs", name="Google Cloud Storage",
        required_env=("GCS_PROJECT", "GCS_BUCKET"),
        summary="Project GCS bucket (uses gcloud ADC).",
        usage="Bucket $GCS_BUCKET in project $GCS_PROJECT. Use `gcloud storage` / `gsutil`.",
    ),
)


def status() -> list[dict]:
    """Per-connector configured/not — no secret values."""
    return [
        {"id": c.id, "name": c.name, "configured": c.configured(),
         "summary": c.summary}
        for c in CONNECTORS
    ]


def prompt_block() -> str:
    """Configured connectors rendered for the assistant's system prompt."""
    ready = [c for c in CONNECTORS if c.configured()]
    if not ready:
        return ""
    lines = "\n".join(f"- {c.name}: {c.summary} {c.usage}" for c in ready)
    return (
        "You are authenticated to these connectors. Their credentials are "
        "already in your shell environment (the variable names below) — use "
        "them directly in commands; never print a secret's value. Prefer these "
        "over asking the operator:\n" + lines
    )
