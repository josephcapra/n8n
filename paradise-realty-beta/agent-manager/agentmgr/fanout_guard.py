"""Fan-out / recursion guard.

In the hub design the Master can trigger Jobs and (from Phase 2) relay
messages between workers. That creates two ways to loop forever:

  1. **Fan-out depth** — task spawns task spawns task ...
  2. **Relay depth** — A -> Master -> B -> Master -> A ...

This guard bounds both. ``check_task`` is enforced before every Job trigger
(used in Phase 1). ``check_relay`` is enforced before every message relay
(wired up in Phase 2) — it is implemented now so the bound exists from the
start rather than being bolted on later.
"""

from __future__ import annotations

from .config import Config
from .schemas import Message
from .state_store import StateStore


class FanoutLimitExceeded(RuntimeError):
    """Raised when a command exceeds its depth or job-count budget."""


class FanoutGuard:
    def __init__(self, config: Config, store: StateStore) -> None:
        self._max_depth = config.max_fanout_depth
        self._max_jobs = config.max_jobs_per_command
        self._store = store

    def check_task(self, correlation_id: str, depth: int) -> None:
        """Enforce depth and per-command job-count budgets before a trigger."""
        if depth > self._max_depth:
            raise FanoutLimitExceeded(
                f"fan-out depth {depth} exceeds max {self._max_depth}"
            )
        count = self._store.incr_job_count(correlation_id)
        if count > self._max_jobs:
            raise FanoutLimitExceeded(
                f"command {correlation_id} exceeded {self._max_jobs} jobs"
            )

    def check_relay(self, message: Message) -> None:
        """Enforce relay-hop depth before the Master forwards a message.

        Bounds A->Master->B->Master->A style cycles. Wired into the relay path
        in Phase 2; the bound is defined here so it cannot be forgotten.
        """
        if message.relay_depth > self._max_depth:
            raise FanoutLimitExceeded(
                f"relay depth {message.relay_depth} exceeds max {self._max_depth}"
            )
