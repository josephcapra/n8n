"""Session-window tests — grants are signed artifacts, not flags."""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentmgr.config import Config
from agentmgr.session import (
    SessionManager,
    canonical_session_message,
    new_grant,
    sign_grant_with_token,
)

_ALWAYS = Config().always_confirm_patterns


def _sign(grant, private_key):
    grant.signature_b64 = base64.b64encode(
        private_key.sign(canonical_session_message(grant))
    ).decode()
    return grant


def test_signed_grant_verifies(store, keypair):
    private_key, pub = keypair
    mgr = SessionManager(store, pub, _ALWAYS)
    assert mgr.verify_grant(_sign(new_grant("shell", 900), private_key)) is True


def test_expired_grant_rejected(store, keypair):
    private_key, pub = keypair
    mgr = SessionManager(store, pub, _ALWAYS)
    assert mgr.verify_grant(_sign(new_grant("shell", -10), private_key)) is False


def test_revoked_grant_rejected(store, keypair):
    private_key, pub = keypair
    mgr = SessionManager(store, pub, _ALWAYS)
    grant = _sign(new_grant("shell", 900), private_key)
    grant.revoked = True
    assert mgr.verify_grant(grant) is False


def test_unsigned_grant_rejected(store, keypair):
    _, pub = keypair
    mgr = SessionManager(store, pub, _ALWAYS)
    assert mgr.verify_grant(new_grant("shell", 900)) is False


def test_wrong_key_grant_rejected(store, keypair):
    _, pub = keypair
    mgr = SessionManager(store, pub, _ALWAYS)
    grant = _sign(new_grant("shell", 900), Ed25519PrivateKey.generate())
    assert mgr.verify_grant(grant) is False


def test_tampered_grant_rejected(store, keypair):
    """Editing a signed grant (e.g. widening scope) breaks the signature."""
    private_key, pub = keypair
    mgr = SessionManager(store, pub, _ALWAYS)
    grant = _sign(new_grant("shell", 900), private_key)
    grant.scope = "all"
    assert mgr.verify_grant(grant) is False


def test_active_grant_scope_matching(store, keypair):
    private_key, pub = keypair
    mgr = SessionManager(store, pub, _ALWAYS)
    store.put_session(_sign(new_grant("shell", 900), private_key))
    assert mgr.active_grant("shell") is not None
    assert mgr.active_grant("cloudrun") is None


def test_all_scope_covers_everything(store, keypair):
    private_key, pub = keypair
    mgr = SessionManager(store, pub, _ALWAYS)
    store.put_session(_sign(new_grant("all", 900), private_key))
    assert mgr.active_grant("shell") is not None
    assert mgr.active_grant("cloudrun") is not None


def test_always_confirm_denylist(store):
    mgr = SessionManager(store, None, ("rm -rf /", "diskutil erase"))
    assert mgr.requires_fresh_approval("sudo rm -rf / --no-preserve-root") is True
    assert mgr.requires_fresh_approval("DISKUTIL ERASE disk2") is True
    assert mgr.requires_fresh_approval("ls -la && git status") is False


# --- desktop password login: token-signed grants ----------------------------

def test_token_grant_verifies_with_matching_token(store):
    mgr = SessionManager(store, None, _ALWAYS, api_token="s3cret")
    grant = sign_grant_with_token(new_grant("all", 900), "s3cret")
    assert grant.proof_type == "token"
    assert mgr.verify_grant(grant) is True


def test_token_grant_rejected_with_wrong_token(store):
    grant = sign_grant_with_token(new_grant("all", 900), "s3cret")
    other = SessionManager(store, None, _ALWAYS, api_token="different")
    assert other.verify_grant(grant) is False


def test_token_grant_rejected_when_manager_has_no_token(store):
    grant = sign_grant_with_token(new_grant("all", 900), "s3cret")
    mgr = SessionManager(store, None, _ALWAYS)  # api_token=None
    assert mgr.verify_grant(grant) is False


def test_tampered_token_grant_rejected(store):
    """Widening scope after signing breaks the HMAC."""
    mgr = SessionManager(store, None, _ALWAYS, api_token="s3cret")
    grant = sign_grant_with_token(new_grant("shell", 900), "s3cret")
    grant.scope = "all"
    assert mgr.verify_grant(grant) is False
