"""Approval-gate tests.

These satisfy hard security requirement #3: the Master and workers must not be
able to bypass, disable, or auto-satisfy the gate, and there is a test that
asserts it.

Threat model note: in-process Python can always be monkeypatched, so these
tests do not pretend otherwise. They assert the *real* guarantee — the gate
holds no signing capability, so a process that has only the gate (every
deployed agent) cannot manufacture an approval. The protection boundary is
key possession, enforced by IAM (see README 'Security model').
"""

from __future__ import annotations

import base64
import threading
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentmgr.approval_gate import (
    ApprovalDenied,
    ApprovalGate,
    ApprovalNotProvisioned,
    SensitiveCategory,
    canonical_message,
    load_public_key,
    verify_signature,
)
from agentmgr.schemas import ApprovalResponse, ApprovalStatus, ApprovalRequest


def _gate(store, pub_b64):
    return ApprovalGate(store, public_key_b64=pub_b64, poll_interval_s=0.02)


def _wait_pending(store) -> ApprovalRequest:
    for _ in range(500):
        pending = store.list_pending_approvals()
        if pending:
            return pending[0]
        time.sleep(0.01)
    raise AssertionError("no pending approval request appeared")


def _submit(store, private_key, request, decision):
    signature = private_key.sign(
        canonical_message(request.id, request.nonce, decision)
    )
    store.submit_approval_response(
        request.id,
        ApprovalResponse(
            decision=decision,
            signature_b64=base64.b64encode(signature).decode(),
        ),
    )


def _run_request(gate, box):
    try:
        box["result"] = gate.request_approval("sensitive-op", {"k": "v"})
    except Exception as exc:  # noqa: BLE001 - captured for the test to inspect
        box["exc"] = exc


# --- happy path ----------------------------------------------------------

def test_blocks_until_signed_then_approves(store, keypair):
    private_key, pub = keypair
    gate = _gate(store, pub)
    box: dict = {}
    thread = threading.Thread(target=_run_request, args=(gate, box), daemon=True)
    thread.start()

    request = _wait_pending(store)
    time.sleep(0.15)
    assert thread.is_alive(), "gate must block until a signature arrives"
    assert "result" not in box

    _submit(store, private_key, request, "approve")
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert box["result"].approved is True
    assert store.get_approval(request.id).status == ApprovalStatus.APPROVED


def test_signed_denial_raises(store, keypair):
    private_key, pub = keypair
    gate = _gate(store, pub)
    box: dict = {}
    thread = threading.Thread(target=_run_request, args=(gate, box), daemon=True)
    thread.start()
    request = _wait_pending(store)
    _submit(store, private_key, request, "deny")
    thread.join(timeout=3)
    assert isinstance(box.get("exc"), ApprovalDenied)


# --- bypass attempts MUST fail ------------------------------------------

def test_invalid_signature_keeps_blocking(store, keypair):
    private_key, pub = keypair
    gate = _gate(store, pub)
    box: dict = {}
    thread = threading.Thread(target=_run_request, args=(gate, box), daemon=True)
    thread.start()
    request = _wait_pending(store)

    # A forged 64-byte "signature" — what a compromised agent could fabricate.
    store.submit_approval_response(
        request.id,
        ApprovalResponse(
            decision="approve",
            signature_b64=base64.b64encode(b"\x00" * 64).decode(),
        ),
    )
    time.sleep(0.2)
    assert thread.is_alive(), "a forged signature must NOT satisfy the gate"

    _submit(store, private_key, request, "approve")  # clean up the thread
    thread.join(timeout=3)


def test_wrong_key_cannot_approve(store, keypair):
    _, pub = keypair
    gate = _gate(store, pub)
    box: dict = {}
    thread = threading.Thread(target=_run_request, args=(gate, box), daemon=True)
    thread.start()
    request = _wait_pending(store)

    attacker_key = Ed25519PrivateKey.generate()  # not the configured key
    _submit(store, attacker_key, request, "approve")
    time.sleep(0.2)
    assert thread.is_alive(), "only the configured key may approve"

    real_key, _ = keypair
    _submit(store, real_key, request, "approve")
    thread.join(timeout=3)


def test_no_env_var_auto_approves(store, keypair, monkeypatch):
    _, pub = keypair
    for name in (
        "AGENTMGR_AUTO_APPROVE", "APPROVE_ALL", "AGENTMGR_BYPASS_APPROVAL",
        "SKIP_APPROVAL", "AGENTMGR_APPROVAL_DISABLE",
    ):
        monkeypatch.setenv(name, "1")
    gate = _gate(store, pub)
    box: dict = {}
    thread = threading.Thread(target=_run_request, args=(gate, box), daemon=True)
    thread.start()
    request = _wait_pending(store)
    time.sleep(0.2)
    assert thread.is_alive(), "no env var may auto-approve the gate"

    real_key, _ = keypair
    _submit(store, real_key, request, "approve")
    thread.join(timeout=3)


def test_fail_closed_without_public_key(store):
    """With no public key the gate cannot verify anything -> must refuse."""
    gate = ApprovalGate(store, public_key_b64=None)
    with pytest.raises(ApprovalNotProvisioned):
        gate.request_approval("sensitive-op", {})


def test_gate_holds_no_signing_capability(store, keypair):
    """The gate object cannot produce an approval signature, by construction."""
    _, pub = keypair
    gate = _gate(store, pub)

    # No attribute anywhere on the gate is a private key.
    for value in vars(gate).values():
        assert not isinstance(value, Ed25519PrivateKey)

    # The only key it holds is a public key, which has no .sign().
    assert not hasattr(gate._public_key, "sign")

    # No public method looks like a bypass / auto-approve.
    public_api = {m for m in dir(gate) if not m.startswith("_")}
    for forbidden in ("approve", "bypass", "disable", "auto_approve", "force"):
        assert forbidden not in public_api
    assert {"guard", "request_approval", "classify"} <= public_api


def test_agent_with_only_public_key_cannot_forge(keypair):
    """A compromised agent has the public key + request fields and still loses."""
    _, pub = keypair
    public_key = load_public_key(pub)
    request = ApprovalRequest(action="exfiltrate")

    # Anything the agent can fabricate without the private key fails to verify.
    for forged in (b"\x00" * 64, b"\xff" * 64, request.nonce.encode().ljust(64)):
        forged_b64 = base64.b64encode(forged).decode()
        assert not verify_signature(
            public_key, request.id, request.nonce, "approve", forged_b64
        )


# --- classification ------------------------------------------------------

def test_classify_sensitive_categories(store, keypair, tmp_path):
    _, pub = keypair
    gate = ApprovalGate(
        store, public_key_b64=pub, workdir=str(tmp_path), spend_threshold_usd=5.0
    )
    assert gate.classify("read the gmail inbox", {}) == SensitiveCategory.GOOGLE_API
    assert gate.classify("fetch", {"api_key": "x"}) == SensitiveCategory.CREDENTIAL_ACCESS
    assert gate.classify("buy ads", {"spend_usd": 50}) == SensitiveCategory.SPEND_OVER_THRESHOLD
    assert gate.classify("write", {"path": "/etc/hosts"}) == SensitiveCategory.FILESYSTEM_OUTSIDE_WORKDIR
    # benign operations need no approval
    assert gate.classify("buy ads", {"spend_usd": 1}) is None
    assert gate.classify("echo", {"text": "hello"}) is None
    assert gate.classify("write", {"path": str(tmp_path / "out.txt")}) is None
