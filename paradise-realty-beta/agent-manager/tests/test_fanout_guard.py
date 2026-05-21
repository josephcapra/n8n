"""Fan-out / recursion guard — both task fan-out and relay depth are bounded."""

from __future__ import annotations

import pytest

from agentmgr.config import Config
from agentmgr.fanout_guard import FanoutGuard, FanoutLimitExceeded
from agentmgr.schemas import Message


def _msg(relay_depth: int) -> Message:
    return Message(
        correlation_id="cmd_1",
        from_agent="a",
        to_agent="master",
        relay_depth=relay_depth,
    )


def test_task_within_limits_ok(store):
    guard = FanoutGuard(Config(max_fanout_depth=3, max_jobs_per_command=10), store)
    guard.check_task("cmd_1", depth=0)
    guard.check_task("cmd_1", depth=3)


def test_task_depth_exceeded_raises(store):
    guard = FanoutGuard(Config(max_fanout_depth=2), store)
    with pytest.raises(FanoutLimitExceeded):
        guard.check_task("cmd_1", depth=3)


def test_task_job_count_exceeded_raises(store):
    guard = FanoutGuard(Config(max_fanout_depth=99, max_jobs_per_command=3), store)
    for _ in range(3):
        guard.check_task("cmd_1", depth=0)
    with pytest.raises(FanoutLimitExceeded):
        guard.check_task("cmd_1", depth=0)


def test_relay_depth_bounded(store):
    """A->Master->B->Master->A cycles are bounded by relay depth (Phase 2 path)."""
    guard = FanoutGuard(Config(max_fanout_depth=2), store)
    guard.check_relay(_msg(relay_depth=2))
    with pytest.raises(FanoutLimitExceeded):
        guard.check_relay(_msg(relay_depth=3))
