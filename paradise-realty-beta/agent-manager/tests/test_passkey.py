"""Passkey / WebAuthn layer + PWA endpoints.

The live Face ID ceremony cannot be exercised headlessly — generating a real
assertion needs an authenticator. These tests cover everything *around* it:
options generation, challenge lifecycle, the PWA token, endpoint wiring, and
that every un-verifiable path fails closed. The live ceremony is verified
on-device (see README 'Verifying the passkey flow').
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from agentmgr.approval_gate import ApprovalGate
from agentmgr.config import Config
from agentmgr.passkey import (
    PasskeyService,
    b64u,
    b64u_decode,
    challenge_for,
    verify_assertion,
)
from agentmgr.schemas import ApprovalRequest, ApprovalResponse, PasskeyCredential
from agentmgr.session import SessionManager, new_grant
from master.main import build_app, check_pwa_token, mint_pwa_token

_RP = dict(rp_id="localhost", rp_name="Agent-Manager", origin="http://localhost:8080")


def _svc(store) -> PasskeyService:
    return PasskeyService(Config(**_RP), store)


def _app():
    return build_app(Config(state_backend="memory", job_runner="local", api_token="adm"))


# --- helpers -------------------------------------------------------------

def test_challenge_for_is_deterministic_and_bound():
    a = challenge_for(b"agentmgr-approval:v1:x:y:approve")
    assert a == challenge_for(b"agentmgr-approval:v1:x:y:approve")
    assert a != challenge_for(b"agentmgr-approval:v1:x:y:deny")
    assert len(a) == 32


def test_b64u_roundtrip():
    for raw in (b"", b"\x00\x01\x02", b"hello", bytes(range(40))):
        assert b64u_decode(b64u(raw)) == raw


# --- options generation --------------------------------------------------

def test_registration_options_stores_challenge(store):
    options = json.loads(_svc(store).registration_options())
    assert options["rp"]["id"] == "localhost"
    assert "challenge" in options
    assert store.pop_challenge("register") is not None


def test_login_options_generates_and_stores(store):
    options = json.loads(_svc(store).login_options())
    assert "challenge" in options
    assert store.pop_challenge("login") is not None


def test_assertion_options_use_the_given_challenge(store):
    challenge = challenge_for(b"some-request-message")
    options = json.loads(_svc(store).assertion_options(challenge))
    assert b64u_decode(options["challenge"]) == challenge


# --- verification fails closed -------------------------------------------

def test_verify_assertion_rejects_garbage(store):
    rp, origin = "localhost", "http://localhost:8080"
    assert verify_assertion(store, '{"id":"unknown"}', b"c", rp, origin) is False
    assert verify_assertion(store, "not-json", b"c", rp, origin) is False


def test_gate_rejects_webauthn_proof_without_assertion(store, keypair):
    _, pub = keypair
    gate = ApprovalGate(store, public_key_b64=pub)
    request = ApprovalRequest(action="x")
    response = ApprovalResponse(
        decision="approve", proof_type="webauthn", webauthn_assertion=None
    )
    assert gate._verify_response(request, response) is False


def test_session_rejects_webauthn_grant_without_assertion(store, keypair):
    _, pub = keypair
    mgr = SessionManager(store, pub, Config().always_confirm_patterns)
    grant = new_grant("shell", 900)
    grant.proof_type = "webauthn"
    assert mgr.verify_grant(grant) is False


# --- passkey + challenge storage -----------------------------------------

def test_passkey_store_roundtrip(store):
    store.put_passkey(PasskeyCredential(credential_id="cid1", public_key_b64="cGs=",
                                        label="iPhone"))
    assert store.get_passkey("cid1").label == "iPhone"
    assert [c.credential_id for c in store.list_passkeys()] == ["cid1"]
    assert store.get_passkey("missing") is None


def test_challenge_is_one_time(store):
    store.put_challenge("k", "v")
    assert store.pop_challenge("k") == "v"
    assert store.pop_challenge("k") is None


# --- PWA bearer token ----------------------------------------------------

def test_pwa_token_roundtrip():
    token = mint_pwa_token("secret", 3600)
    assert check_pwa_token("secret", token) is True
    assert check_pwa_token("wrong", token) is False
    assert check_pwa_token("secret", "garbage") is False


def test_pwa_token_expires():
    assert check_pwa_token("secret", mint_pwa_token("secret", -1)) is False


# --- endpoint wiring -----------------------------------------------------

def test_register_begin_requires_admin_token():
    client = TestClient(_app())
    assert client.post("/passkey/register/begin").status_code == 403
    ok = client.post("/passkey/register/begin", headers={"Authorization": "Bearer adm"})
    assert ok.status_code == 200 and "options" in ok.json()


def test_login_begin_409_when_no_passkey_registered():
    assert TestClient(_app()).post("/passkey/login/begin").status_code == 409


def test_pwa_token_grants_api_access():
    client = TestClient(_app())
    pwa = mint_pwa_token("adm", 3600)
    assert client.get("/agents", headers={"Authorization": f"Bearer {pwa}"}).status_code == 200


def test_pwa_serves_shell():
    client = TestClient(_app())
    assert client.get("/").status_code == 200
    assert client.get("/manifest.json").status_code == 200
    assert "agentmgr" in client.get("/sw.js").text.lower()
