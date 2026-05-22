"""security-probe worker — external risk checks (mocked network)."""

from __future__ import annotations

from worker import security_probe as sp


class _Resp:
    def __init__(self, status=200, headers=None, text=""):
        self.status_code = status
        self.headers = headers or {}
        self.text = text


class _Client:
    """Fake httpx client: homepage returns given headers; other paths 404."""
    def __init__(self, homepage_headers):
        self._h = homepage_headers

    def get(self, url):
        if url.rstrip("/").endswith(".com"):          # homepage
            return _Resp(200, self._h, "<html></html>")
        return _Resp(404, {}, "")                     # sensitive paths

    def close(self):
        pass


def _patch_net(monkeypatch, days=120, redirect=True):
    monkeypatch.setattr(sp, "_cert_days_left", lambda host: days)
    loc = "https://x/" if redirect else ""
    monkeypatch.setattr(sp.httpx, "get",
                        lambda *a, **k: _Resp(301 if redirect else 200,
                                              {"location": loc}, ""))


def test_missing_headers_flagged(monkeypatch):
    _patch_net(monkeypatch)
    findings = sp.probe_target("example.com", client=_Client({}))
    titles = [f.title for f in findings]
    assert any("HSTS" in t for t in titles)
    assert any("Content-Security-Policy" in t for t in titles)
    assert any(f.title == "TLS certificate" and f.severity == "ok" for f in findings)


def test_present_headers_not_flagged(monkeypatch):
    _patch_net(monkeypatch)
    good = {
        "strict-transport-security": "max-age=63072000",
        "content-security-policy": "default-src 'self'",
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "referrer-policy": "no-referrer",
    }
    findings = sp.probe_target("example.com", client=_Client(good))
    assert not [f for f in findings if f.title.startswith("Missing header")]


def test_expired_cert_is_critical(monkeypatch):
    _patch_net(monkeypatch, days=-3)
    findings = sp.probe_target("example.com", client=_Client({}))
    tls = [f for f in findings if f.title == "TLS certificate"][0]
    assert tls.severity == "critical"


def test_http_not_redirected_flagged(monkeypatch):
    _patch_net(monkeypatch, redirect=False)
    findings = sp.probe_target("example.com", client=_Client({}))
    assert any("HTTP not redirected" in f.title for f in findings)


def test_grade_and_render():
    g, counts = sp.grade([sp.Finding("h", "TLS certificate", "critical", "expired")])
    assert g == "F"
    subject, text, html = sp.render([sp.Finding("h", "X", "high", "bad", "fix")])
    assert "External risk probe" in subject and "<table" in html


def test_targets_env_override(monkeypatch):
    monkeypatch.setenv("PROBE_TARGETS", "a.com, b.com")
    assert sp._targets() == ["a.com", "b.com"]
