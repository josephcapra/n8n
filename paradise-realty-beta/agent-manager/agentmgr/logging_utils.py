"""Structured JSON logging with a per-command correlation ID.

Every top-level command issued to the Master gets one correlation ID. It is
stored in a contextvar and stamped onto every log line — Master, routing, and
(once they read it back from the task spec) workers — so a single command can
be traced through every agent.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys

_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agentmgr_correlation_id", default="-"
)

# Reserved LogRecord attributes we never want to duplicate into "extra".
_RESERVED = set(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime"}


def set_correlation_id(value: str) -> None:
    _correlation_id.set(value)


def get_correlation_id() -> str:
    return _correlation_id.get()


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "severity": record.levelname,
            "logger": record.name,
            "correlation_id": _correlation_id.get(),
            "message": record.getMessage(),
        }
        # Anything passed via logger.info(..., extra={...}) is merged in.
        for key, val in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = val
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def get_logger(name: str) -> logging.Logger:
    """Return a logger that emits single-line JSON to stdout (Cloud Run friendly)."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
