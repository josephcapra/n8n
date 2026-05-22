"""End-to-end Phase 1 flow on the in-memory backend + local job runner.

Exercises: authenticated chat -> command interpretation -> task dispatch ->
worker runs -> typed result written to the store -> Master reports it back.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agentmgr.config import Config
from master.main import build_app

_TOKEN = "test-token"


def _cfg(**overrides) -> Config:
    base = dict(
        state_backend="memory",
        job_runner="local",
        api_token=_TOKEN,
        poll_interval_s=0.05,
        task_timeout_s=10.0,
    )
    base.update(overrides)
    return Config(**base)


def _mem_cfg(tmp_path, **overrides):
    """Config with a throwaway memory file so tests never touch the real one."""
    return _cfg(memory_path=str(tmp_path / "mem.json"), **overrides)


def test_health_is_open():
    client = TestClient(build_app(_cfg()))
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_chat_rejects_unauthenticated():
    client = TestClient(build_app(_cfg()))
    assert client.post("/chat", json={"message": "hi"}).status_code == 401


def test_chat_fails_closed_when_no_token_provisioned():
    client = TestClient(build_app(_cfg(api_token=None)))
    resp = client.post(
        "/chat",
        json={"message": "hi"},
        headers={"Authorization": "Bearer anything"},
    )
    assert resp.status_code == 503


def test_chat_end_to_end():
    client = TestClient(build_app(_cfg()))
    resp = client.post(
        "/chat",
        json={"message": "transform: upper launch the brevard campaign"},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["correlation_id"].startswith("cmd_")
    assert len(body["task_ids"]) == 1
    assert "launch the brevard campaign" in body["interpretation"]
    assert "[transform-worker] completed" in body["reply"]
    assert "LAUNCH THE BREVARD CAMPAIGN" in body["reply"]


def test_chat_keeps_conversation_id():
    client = TestClient(build_app(_cfg()))
    headers = {"Authorization": f"Bearer {_TOKEN}"}
    first = client.post(
        "/chat", json={"message": "transform: upper one"}, headers=headers
    ).json()
    second = client.post(
        "/chat",
        json={"message": "transform: upper two",
              "conversation_id": first["conversation_id"]},
        headers=headers,
    ).json()
    assert second["conversation_id"] == first["conversation_id"]


def test_agents_endpoint_lists_all_agents():
    client = TestClient(build_app(_cfg()))
    resp = client.get("/agents", headers={"Authorization": f"Bearer {_TOKEN}"})
    assert resp.status_code == 200
    names = {a["name"] for a in resp.json()["agents"]}
    assert {"echo-worker", "mac-shell", "cloudrun-admin", "security-health"} <= names


# --- desktop password login ----------------------------------------------

def test_password_login_with_correct_token():
    app = build_app(_cfg())
    client = TestClient(app)
    resp = client.post("/password/login", json={"password": _TOKEN})
    assert resp.status_code == 200
    body = resp.json()
    # The returned bearer token authenticates a real request.
    assert client.get(
        "/agents", headers={"Authorization": f"Bearer {body['token']}"}
    ).status_code == 200
    # An 'all'-scope session window is now open and verifies against the token.
    grant = app.state.store.get_session(body["session"])
    assert grant.scope == "all"
    assert app.state.session_mgr.active_grant("shell") is not None


def test_password_login_rejects_wrong_password():
    client = TestClient(build_app(_cfg()))
    assert client.post(
        "/password/login", json={"password": "nope"}
    ).status_code == 401


def test_password_login_can_be_disabled():
    client = TestClient(build_app(_cfg(allow_password_login=False)))
    assert client.post(
        "/password/login", json={"password": _TOKEN}
    ).status_code == 403


# --- Cloud Run jobs run-selector -----------------------------------------

class _FakeCloudRun:
    def __init__(self):
        self.ran: list[str] = []

    def list_jobs(self):
        return [{"name": "bing-daily"}, {"name": "sitemap-sync-weekly"}]

    def run_job(self, name):
        self.ran.append(name)
        return "exec-123"


_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


def test_cloudrun_jobs_listed():
    app = build_app(_cfg())
    app.state.cloudrun_admin = _FakeCloudRun()
    resp = TestClient(app).get("/cloudrun/jobs", headers=_AUTH)
    assert resp.status_code == 200
    assert {j["name"] for j in resp.json()["jobs"]} == {
        "bing-daily", "sitemap-sync-weekly"
    }


def test_cloudrun_run_requires_active_session():
    app = build_app(_cfg())
    app.state.cloudrun_admin = _FakeCloudRun()
    resp = TestClient(app).post("/cloudrun/jobs/bing-daily/run", headers=_AUTH)
    assert resp.status_code == 409


def test_cloudrun_run_with_password_session():
    app = build_app(_cfg())
    fake = _FakeCloudRun()
    app.state.cloudrun_admin = fake
    client = TestClient(app)
    client.post("/password/login", json={"password": _TOKEN})  # opens 'all' window
    resp = client.post("/cloudrun/jobs/bing-daily/run", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["started"] is True
    assert fake.ran == ["bing-daily"]


def test_cloudrun_run_unknown_job_is_404():
    app = build_app(_cfg())
    app.state.cloudrun_admin = _FakeCloudRun()
    client = TestClient(app)
    client.post("/password/login", json={"password": _TOKEN})
    resp = client.post("/cloudrun/jobs/nope/run", headers=_AUTH)
    assert resp.status_code == 404


# --- persistent memory ----------------------------------------------------

def test_memory_endpoints_add_list_delete(tmp_path):
    client = TestClient(build_app(_mem_cfg(tmp_path)))
    add = client.post("/memory", json={"text": "brokerage is Paradise Realty"},
                      headers=_AUTH)
    assert add.status_code == 200
    mem_id = add.json()["memory"]["id"]
    listed = client.get("/memory", headers=_AUTH).json()["memories"]
    assert [m["text"] for m in listed] == ["brokerage is Paradise Realty"]
    assert client.delete(f"/memory/{mem_id}", headers=_AUTH).status_code == 200
    assert client.get("/memory", headers=_AUTH).json()["memories"] == []


def test_chat_remember_then_recall(tmp_path):
    client = TestClient(build_app(_mem_cfg(tmp_path)))
    r = client.post("/chat", json={"message": "remember that I prefer morning showings"},
                    headers=_AUTH)
    assert r.status_code == 200
    assert "remember" in r.json()["reply"].lower()
    # it should now come back on recall, with no Mac agent / LLM involved
    recall = client.post("/chat", json={"message": "what do you remember"},
                         headers=_AUTH).json()
    assert "morning showings" in recall["reply"]
    assert recall["task_ids"] == []


def test_chat_remember_persists_to_disk(tmp_path):
    cfg_kwargs = dict(memory_path=str(tmp_path / "mem.json"))
    c1 = TestClient(build_app(_cfg(**cfg_kwargs)))
    c1.post("/chat", json={"message": "remember my office is in Stuart"}, headers=_AUTH)
    # a fresh app pointed at the same file still has the memory (survives restart)
    c2 = TestClient(build_app(_cfg(**cfg_kwargs)))
    assert "Stuart" in c2.get("/memory", headers=_AUTH).json()["memories"][0]["text"]


def test_chat_forget(tmp_path):
    client = TestClient(build_app(_mem_cfg(tmp_path)))
    client.post("/chat", json={"message": "remember that I dislike cold calls"}, headers=_AUTH)
    forget = client.post("/chat", json={"message": "forget cold calls"}, headers=_AUTH).json()
    assert "forgotten" in forget["reply"].lower()
    assert client.get("/memory", headers=_AUTH).json()["memories"] == []


# --- connectors -----------------------------------------------------------

def test_connectors_endpoint_reports_status_without_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-SECRET")
    monkeypatch.delenv("BING_API_KEY", raising=False)
    client = TestClient(build_app(_cfg()))
    resp = client.get("/connectors", headers=_AUTH)
    assert resp.status_code == 200
    by_id = {c["id"]: c for c in resp.json()["connectors"]}
    assert by_id["openai"]["configured"] is True
    assert by_id["bing"]["configured"] is False
    assert "SECRET" not in resp.text          # endpoint never leaks values


# --- Phase 1.5: Master -> Mac agent + session endpoints ------------------

def _open_signed_session(store, private_key, scope="shell"):
    import base64

    from agentmgr.session import canonical_session_message, new_grant

    grant = new_grant(scope, 900)
    grant.signature_b64 = base64.b64encode(
        private_key.sign(canonical_session_message(grant))
    ).decode()
    store.put_session(grant)
    return grant


def test_master_routes_shell_command_to_mac_agent(keypair):
    """Full Phase 1.5 path: chat -> mac-shell task queued -> Mac agent runs it
    under an open session window -> result reported back."""
    import threading
    import time

    from agent.local_agent import process_task
    from agentmgr.approval_gate import ApprovalGate
    from agentmgr.session import SessionManager

    private_key, pub = keypair
    cfg = _cfg(approval_public_key=pub)
    app = build_app(cfg)
    store = app.state.store
    _open_signed_session(store, private_key, "shell")

    mgr = SessionManager(store, pub, cfg.always_confirm_patterns)
    gate = ApprovalGate(store, public_key_b64=pub, poll_interval_s=0.05)
    stop = threading.Event()

    def agent_loop():
        while not stop.is_set():
            for task in store.get_pending_tasks("mac-shell"):
                process_task(task, store, mgr, gate, timeout_s=10)
            time.sleep(0.03)

    thread = threading.Thread(target=agent_loop, daemon=True)
    thread.start()
    try:
        resp = TestClient(app).post(
            "/chat",
            json={"message": "$ echo routed-ok"},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    finally:
        stop.set()

    assert resp.status_code == 200
    # Shell replies are clean prose now — just the output, no "[mac-shell]" label.
    reply = resp.json()["reply"]
    assert reply.strip() == "routed-ok"
    assert "[mac-shell]" not in reply


def test_sessions_endpoint_lists_and_revokes(keypair):
    private_key, pub = keypair
    app = build_app(_cfg(approval_public_key=pub))
    store = app.state.store
    grant = _open_signed_session(store, private_key, "shell")
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {_TOKEN}"}

    listed = client.get("/sessions", headers=headers).json()["sessions"]
    assert listed[0]["id"] == grant.id and listed[0]["active"] is True

    assert client.post(f"/sessions/{grant.id}/revoke", headers=headers).status_code == 200

    after = client.get("/sessions", headers=headers).json()["sessions"]
    assert after[0]["active"] is False
