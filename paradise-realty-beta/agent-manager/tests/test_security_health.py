"""Security-health worker — scan, grade, render, and SendGrid email."""

from __future__ import annotations

from agentmgr.planner import RuleBasedPlanner
from agentmgr.registry import AgentRegistry
from tools import security_health as sh


def test_scan_returns_expected_findings():
    findings = sh.scan()
    assert findings
    ids = {f.id for f in findings}
    assert {"filevault", "firewall", "ports", "remote_access", "env_perms"} <= ids
    assert all(f.severity in sh._RANK for f in findings)


def test_grade_one_high_is_C():
    findings = [sh.Finding("a", "A", "ok", "fine"),
                sh.Finding("b", "B", "high", "bad", "fix it")]
    grade, counts = sh._grade(findings)
    assert grade == "C"
    assert counts["high"] == 1


def test_grade_clean_is_A():
    grade, _ = sh._grade([sh.Finding("a", "A", "ok", "fine")])
    assert grade == "A"


def test_render_includes_fix_and_html():
    findings = [sh.Finding("b", "Firewall", "high", "off", "turn it on")]
    subject, text, html = sh.render(findings)
    assert "Security health" in subject
    assert "turn it on" in text
    assert "<table" in html


def test_send_email_without_key_fails_gracefully(monkeypatch):
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    res = sh.send_email("s", "t", "h", "joe@josephcapra.com")
    assert res["sent"] is False


def test_send_email_builds_correct_payload(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 202
        text = ""

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, json=json)
        return _Resp()

    monkeypatch.setenv("SENDGRID_API_KEY", "SG.test-key")
    monkeypatch.setattr(sh.httpx, "post", _fake_post)
    res = sh.send_email("subj", "body", "<b>body</b>", "joe@josephcapra.com")
    assert res["sent"] is True
    assert captured["url"].endswith("/v3/mail/send")
    assert captured["headers"]["Authorization"] == "Bearer SG.test-key"
    assert captured["json"]["personalizations"][0]["to"][0]["email"] == "joe@josephcapra.com"
    assert captured["json"]["subject"] == "subj"


def test_run_dry_does_not_email(monkeypatch):
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    out = sh.run(send=False)
    assert "grade" in out and "findings" in out
    assert "email" not in out


def test_planner_routes_security_health():
    plan = RuleBasedPlanner().plan("send me a security health email",
                                   AgentRegistry.load())
    assert plan.steps[0].agent == "security-health"
    assert plan.steps[0].kind == "security"
    assert plan.steps[0].payload.get("send") is True
