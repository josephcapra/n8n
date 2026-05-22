"""jazzysphotos-site agent — registry, action dispatch, gating, helpers."""

from __future__ import annotations

from pathlib import Path

import agent.local_agent as la
from agentmgr.approval_gate import (
    ApprovalDecision,
    ApprovalDenied,
    ApprovalNotProvisioned,
)
from agentmgr.assistant import ModelStep, ScriptedDriver
from agentmgr.config import Config
from agentmgr.registry import AgentRegistry
from agentmgr.schemas import TaskSpec, TaskStatus

SITE_TS = """export const site = {
  name: 'Jasmine Capra',
  brand: 'jazz.ysphotos',
  tagline: 'Natural-light portraiture · Stuart, FL',
  heroSubtext:
    'Warm, natural-light portraiture for seniors and prom.',
};
"""

PHOTO_MD = """---
image: ./images/red-gown.jpg
alt: "A senior in a red gown"
title: "Red Gown"
category: "Seniors"
featured: true
order: 5
---
"""


# --- a stub approval gate (records calls; can approve / deny / fail-closed) ---

class _Gate:
    def __init__(self, *, deny: bool = False, unprovisioned: bool = False) -> None:
        self.deny = deny
        self.unprovisioned = unprovisioned
        self.calls: list[tuple] = []

    def request_approval(self, action, details, *, sensitive_reason="explicit"):
        self.calls.append((action, details))
        if self.unprovisioned:
            raise ApprovalNotProvisioned("no verifier")
        if self.deny:
            raise ApprovalDenied("operator denied")
        return ApprovalDecision(approved=True, request_id="appr_test")


def _repo(tmp_path: Path) -> str:
    """A minimal jazzysphotos working tree: site.ts + one portfolio photo."""
    settings = tmp_path / "src" / "content" / "settings"
    portfolio = tmp_path / "src" / "content" / "portfolio" / "images"
    settings.mkdir(parents=True)
    portfolio.mkdir(parents=True)
    (settings / "site.ts").write_text(SITE_TS)
    (portfolio.parent / "red-gown.md").write_text(PHOTO_MD)
    (portfolio / "red-gown.jpg").write_bytes(b"\xff\xd8\xff\xe0jpegbytes")
    return str(tmp_path)


def _task(action: str, **payload) -> TaskSpec:
    return TaskSpec(
        agent="jazzysphotos-site", kind="site",
        payload={"action": action, **payload},
        conversation_id="conv_site", correlation_id="cmd_site",
    )


def _run(store, task, cfg, *, gate=None, session_mgr=None, driver=None):
    la.process_jazzysphotos_task(
        task, store, session_mgr, gate or _Gate(), cfg, driver=driver)
    return store.get_task_result(task.id)


# --- registry -------------------------------------------------------------

def test_registry_has_site_agent():
    spec = AgentRegistry.load().get("jazzysphotos-site")
    assert spec.runtime == "local-agent"
    assert spec.kind == "site"
    assert spec.sensitive_default is False  # per-action gating, not agent-wide


# --- pure helpers ---------------------------------------------------------

def test_set_copy_field_replaces_and_validates():
    out = la._jazzy_set_copy_field(SITE_TS, "tagline", "New · tagline's here")
    assert "New · tagline\\'s here" in out
    assert "Natural-light portraiture" not in out
    # also handles the value on the next line (heroSubtext)
    out2 = la._jazzy_set_copy_field(SITE_TS, "heroSubtext", "Short subtext")
    assert "Short subtext" in out2


def test_set_copy_field_missing_raises():
    import pytest
    with pytest.raises(ValueError):
        la._jazzy_set_copy_field(SITE_TS, "nope", "x")


def test_frontmatter_shape():
    fm = la._jazzy_frontmatter("a.jpg", "alt txt", "Title", "Prom", False, 7)
    assert 'image: ./images/a.jpg' in fm
    assert 'category: "Prom"' in fm
    assert "featured: false" in fm
    assert "order: 7" in fm


# --- benign actions (no approval) -----------------------------------------

def test_status_runs_in_repo(store, tmp_path, monkeypatch):
    seen = {}

    def fake(command, cwd, timeout):
        seen["command"], seen["cwd"] = command, cwd
        return {"command": command, "exit_code": 0, "stdout": "main\n", "stderr": ""}

    monkeypatch.setattr(la, "_run_command", fake)
    res = _run(store, _task("status"), Config(jazzysphotos_dir=_repo(tmp_path)))
    assert res.status == TaskStatus.COMPLETED
    assert seen["cwd"] == str(tmp_path)
    assert "git status" in seen["command"]


def test_build_ok_and_fail(store, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(la, "_run_command", lambda *a, **k: {
        "command": "npm run build", "exit_code": 0, "stdout": "built", "stderr": ""})
    assert _run(store, _task("build"), Config(jazzysphotos_dir=repo)).status \
        == TaskStatus.COMPLETED

    monkeypatch.setattr(la, "_run_command", lambda *a, **k: {
        "command": "npm run build", "exit_code": 1, "stdout": "", "stderr": "boom"})
    bad = _run(store, _task("build"), Config(jazzysphotos_dir=repo))
    assert bad.status == TaskStatus.FAILED and "build failed" in bad.error


def test_unknown_action_fails(store, tmp_path):
    res = _run(store, _task("frobnicate"), Config(jazzysphotos_dir=_repo(tmp_path)))
    assert res.status == TaskStatus.FAILED
    assert "unknown site action" in res.error


# --- update_copy: gated publish -------------------------------------------

def test_update_copy_approved_publishes(store, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    cmds = []

    def fake(command, cwd, timeout):
        cmds.append(command)
        return {"command": command, "exit_code": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(la, "_run_command", fake)
    gate = _Gate()
    res = _run(store, _task("update_copy", field="tagline", value="Fresh tagline"),
               Config(jazzysphotos_dir=repo), gate=gate)

    assert res.status == TaskStatus.COMPLETED and res.output["published"] is True
    assert gate.calls and gate.calls[0][1]["field"] == "tagline"
    # the file was actually edited, and we built + pushed
    assert "Fresh tagline" in (Path(repo) / "src/content/settings/site.ts").read_text()
    assert any("npm run build" in c for c in cmds)
    assert any("git push" in c for c in cmds)


def test_update_copy_denied_does_not_touch_repo(store, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    ran = {"v": False}

    def fake(*a, **k):
        ran["v"] = True
        return {"command": "", "exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(la, "_run_command", fake)
    before = (Path(repo) / "src/content/settings/site.ts").read_text()
    res = _run(store, _task("update_copy", field="tagline", value="nope"),
               Config(jazzysphotos_dir=repo), gate=_Gate(deny=True))

    assert res.status == TaskStatus.FAILED and "denied" in res.error
    assert ran["v"] is False  # never built or pushed
    assert (Path(repo) / "src/content/settings/site.ts").read_text() == before


def test_update_copy_bad_field_fails_before_gate(store, tmp_path):
    gate = _Gate()
    res = _run(store, _task("update_copy", field="services", value="x"),
               Config(jazzysphotos_dir=_repo(tmp_path)), gate=gate)
    assert res.status == TaskStatus.FAILED
    assert "not an editable copy field" in res.error
    assert gate.calls == []  # never asked the operator


def test_build_failure_reverts_and_blocks_push(store, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    cmds = []

    def fake(command, cwd, timeout):
        cmds.append(command)
        code = 1 if "npm run build" in command else 0
        return {"command": command, "exit_code": code, "stdout": "", "stderr": "err"}

    monkeypatch.setattr(la, "_run_command", fake)
    res = _run(store, _task("update_copy", field="tagline", value="x"),
               Config(jazzysphotos_dir=repo), gate=_Gate())
    assert res.status == TaskStatus.FAILED and "build failed" in res.error
    assert any("git checkout" in c for c in cmds)   # reverted
    assert not any("git push" in c for c in cmds)   # never published


# --- remove_photo / publish ----------------------------------------------

def test_remove_photo_missing_fails(store, tmp_path):
    gate = _Gate()
    res = _run(store, _task("remove_photo", slug="does-not-exist"),
               Config(jazzysphotos_dir=_repo(tmp_path)), gate=gate)
    assert res.status == TaskStatus.FAILED and "no photo named" in res.error
    assert gate.calls == []


def test_publish_nothing_to_publish(store, tmp_path, monkeypatch):
    monkeypatch.setattr(la, "_run_command", lambda *a, **k: {
        "command": "git status --porcelain", "exit_code": 0, "stdout": "", "stderr": ""})
    gate = _Gate()
    res = _run(store, _task("publish"),
               Config(jazzysphotos_dir=_repo(tmp_path)), gate=gate)
    assert res.status == TaskStatus.COMPLETED
    assert res.output["published"] is False
    assert gate.calls == []  # nothing to approve


def test_publish_pending_commits_and_pushes(store, tmp_path, monkeypatch):
    cmds = []

    def fake(command, cwd, timeout):
        cmds.append(command)
        stdout = " M src/content/settings/site.ts" if "status" in command else "ok"
        return {"command": command, "exit_code": 0, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(la, "_run_command", fake)
    gate = _Gate()
    res = _run(store, _task("publish", message="Ship it"),
               Config(jazzysphotos_dir=_repo(tmp_path)), gate=gate)
    assert res.status == TaskStatus.COMPLETED and res.output["published"] is True
    assert gate.calls and any("git push" in c for c in cmds)


# --- goal: agentic escape hatch -------------------------------------------

def test_goal_runs_scoped_loop(store, tmp_path):
    driver = ScriptedDriver([ModelStep(text="Updated the tagline and published it.", done=True)])
    res = _run(store, _task("goal", goal="change the tagline"),
               Config(jazzysphotos_dir=_repo(tmp_path)), driver=driver)
    assert res.status == TaskStatus.COMPLETED
    assert res.output["action"] == "goal"
    assert "published" in res.output["answer"]
