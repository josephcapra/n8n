"""Connector registry — env loading, status, and prompt injection.

Security-critical: status and prompt_block must never expose secret values.
"""

from __future__ import annotations

import os

from agentmgr.connectors import (
    CONNECTORS,
    load_env_file,
    prompt_block,
    status,
)


def test_load_env_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text('FOO_TEST_X=bar\n# a comment\nQUOTED="baz"\n\nNOEQ\n')
    try:
        loaded = load_env_file(p)
        assert loaded == 2
        assert os.environ["FOO_TEST_X"] == "bar"
        assert os.environ["QUOTED"] == "baz"
    finally:
        os.environ.pop("FOO_TEST_X", None)
        os.environ.pop("QUOTED", None)


def test_load_env_file_does_not_override_by_default(tmp_path):
    p = tmp_path / ".env"
    p.write_text("FOO_TEST_Y=fromfile\n")
    os.environ["FOO_TEST_Y"] = "preset"
    try:
        load_env_file(p)
        assert os.environ["FOO_TEST_Y"] == "preset"        # shell wins
        load_env_file(p, override=True)
        assert os.environ["FOO_TEST_Y"] == "fromfile"
    finally:
        os.environ.pop("FOO_TEST_Y", None)


def test_load_env_file_missing_is_ok(tmp_path):
    assert load_env_file(tmp_path / "nope.env") == 0


def test_status_reflects_configured(monkeypatch):
    for c in CONNECTORS:
        for k in c.required_env:
            monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("GOOGLE_DRIVE_ENABLED", raising=False)

    assert all(not s["configured"] for s in status())

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    by_id = {s["id"]: s["configured"] for s in status()}
    assert by_id["anthropic"] is True
    assert by_id["openai"] is False


def test_drive_configured_by_flag(monkeypatch):
    monkeypatch.setenv("GOOGLE_DRIVE_ENABLED", "1")
    by_id = {s["id"]: s["configured"] for s in status()}
    assert by_id["google_drive"] is True


def test_prompt_block_lists_configured_without_secrets(monkeypatch):
    for c in CONNECTORS:
        for k in c.required_env:
            monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("GOOGLE_DRIVE_ENABLED", raising=False)
    assert prompt_block() == ""

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRETVALUE")
    block = prompt_block()
    assert "Anthropic" in block
    assert "$ANTHROPIC_API_KEY" in block      # references the var name
    assert "SECRETVALUE" not in block          # never the value
