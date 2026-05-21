"""Small cross-cutting helpers: ids, timestamps, bounded retry with timeout."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Callable, TypeVar

T = TypeVar("T")


def now_iso() -> str:
    """UTC timestamp, ISO-8601 with trailing Z."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class TimeoutExceeded(RuntimeError):
    """Raised when a bounded wait runs past its deadline."""


def retry(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    """Call ``fn`` with bounded retries and exponential backoff.

    Re-raises the last exception once attempts are exhausted. Used on every
    Cloud Run Job trigger and remote state read (cross-cutting requirement).
    """
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:  # noqa: BLE001 - intentional bounded retry
            last_exc = exc
            if attempt == attempts:
                break
            time.sleep(base_delay * (2 ** (attempt - 1)))
    assert last_exc is not None
    raise last_exc


def poll_until(
    predicate: Callable[[], T | None],
    *,
    timeout_s: float,
    interval_s: float = 1.0,
) -> T:
    """Poll ``predicate`` until it returns a truthy value or the deadline passes.

    Raises :class:`TimeoutExceeded` on deadline. Every state read in the
    Master's report loop goes through here so nothing can hang forever.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise TimeoutExceeded(f"poll exceeded {timeout_s}s")
        time.sleep(interval_s)
