"""Session-window approval (Phase 1.5).

You chose the *session window* model: one approval authorizes commands in a
scope for a bounded window, instead of approving every command.

A session is a :class:`SessionGrant` — and like an approval, it is a
**signed** artifact, not a flag. It is signed by the operator's approval key
(or, from Increment 2, a passkey assertion). The Master and the Mac agent hold
only the public key, so a session:

  * cannot be forged or created by the agents themselves,
  * cannot be extended (the signed ``expires_at`` is the hard limit),
  * can be revoked instantly (``revoked`` flips and verification fails).

Catastrophic commands still require fresh approval even inside a window — see
``requires_fresh_approval`` and ``Config.always_confirm_patterns``.

This module only *verifies* sessions. Signing lives in ``tools/start_session``
(operator-side), so no deployed process can mint a session.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .logging_utils import get_logger
from .schemas import SessionGrant
from .state_store import StateStore

log = get_logger("agentmgr.session")

_CANONICAL_PREFIX = "agentmgr-session:v1"
_VALID_SCOPES = ("shell", "cloudrun", "all")


def canonical_session_message(grant: SessionGrant) -> bytes:
    """The exact bytes the operator signs to mint a session grant."""
    return (
        f"{_CANONICAL_PREFIX}:{grant.id}:{grant.scope}:"
        f"{grant.expires_at}:{grant.nonce}"
    ).encode("utf-8")


def new_grant(scope: str, ttl_s: float) -> SessionGrant:
    """Build an UNSIGNED grant. The caller (operator tool) signs it."""
    if scope not in _VALID_SCOPES:
        raise ValueError(f"scope must be one of {_VALID_SCOPES}")
    expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_s)
    return SessionGrant(
        scope=scope, expires_at=expires.isoformat().replace("+00:00", "Z")
    )


def _token_signature(grant: SessionGrant, api_token: str) -> str:
    """HMAC-SHA256 of the grant's canonical message under the api_token."""
    return hmac.new(
        api_token.encode(), canonical_session_message(grant), hashlib.sha256
    ).hexdigest()


def sign_grant_with_token(grant: SessionGrant, api_token: str) -> SessionGrant:
    """Sign ``grant`` with the shared api_token (HMAC) for desktop password
    login. Unlike Ed25519/passkey grants — which no deployed process can mint —
    this CAN be minted by the Master, because the Master holds the token. That
    is the deliberate trade for skipping biometrics on the operator's laptop."""
    grant.proof_type = "token"
    grant.signature_b64 = _token_signature(grant, api_token)
    return grant


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class SessionManager:
    """Verifies session grants and decides if a command may run un-prompted."""

    def __init__(
        self,
        store: StateStore,
        public_key_b64: str | None,
        always_confirm_patterns: tuple[str, ...],
        rp_id: str = "localhost",
        origin: str = "http://localhost:8080",
        api_token: str | None = None,
    ) -> None:
        self._store = store
        self._public_key: Ed25519PublicKey | None = (
            Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
            if public_key_b64
            else None
        )
        self._always_confirm = tuple(p.lower() for p in always_confirm_patterns)
        self._rp_id = rp_id          # for verifying passkey (webauthn) grants
        self._origin = origin
        self._api_token = api_token  # for verifying 'token' (password) grants

    def verify_grant(self, grant: SessionGrant) -> bool:
        """True iff the grant is unrevoked, not past its signed expiry, and
        carries a valid proof — an Ed25519 signature OR a passkey assertion."""
        if grant.revoked:
            return False
        try:
            if datetime.now(timezone.utc) >= _parse_iso(grant.expires_at):
                return False
        except ValueError:
            return False

        if grant.proof_type == "webauthn":
            if not grant.webauthn_assertion:
                return False
            from .passkey import challenge_for, verify_assertion

            expected = challenge_for(canonical_session_message(grant))
            return verify_assertion(
                self._store, grant.webauthn_assertion, expected,
                self._rp_id, self._origin,
            )

        if grant.proof_type == "token":
            if not self._api_token or not grant.signature_b64:
                return False
            return hmac.compare_digest(
                grant.signature_b64, _token_signature(grant, self._api_token)
            )

        # default: Ed25519 signature
        if self._public_key is None or not grant.signature_b64:
            return False
        try:
            signature = base64.b64decode(grant.signature_b64)
        except Exception:  # noqa: BLE001
            return False
        try:
            self._public_key.verify(signature, canonical_session_message(grant))
            return True
        except InvalidSignature:
            return False

    def active_grant(self, scope: str) -> SessionGrant | None:
        """Return a valid grant covering ``scope`` (or scope 'all'), else None."""
        for grant in self._store.list_sessions():
            if grant.scope in (scope, "all") and self.verify_grant(grant):
                return grant
        return None

    def requires_fresh_approval(self, command: str) -> bool:
        """True if ``command`` matches the always-confirm denylist — these
        demand a fresh human approval even when a session window is open."""
        low = command.lower()
        return any(pattern in low for pattern in self._always_confirm)
