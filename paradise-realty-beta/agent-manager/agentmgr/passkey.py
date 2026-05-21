"""WebAuthn passkey support (Phase 1.5 Increment 2).

A passkey is a key pair whose private half lives in your iPhone's Secure
Enclave and is unlocked by Face ID. It is used for two things here:

  * **Login** to the PWA — phishing-resistant authentication to the Master.
  * **Approvals & session windows** — to approve a SENSITIVE operation the PWA
    performs a fresh assertion whose challenge is bound to that exact request
    (``challenge_for`` over the request's canonical message). Face ID literally
    signs off on the operation.

Why a compromised Master still cannot self-approve: producing a valid assertion
needs the Secure Enclave private key, which exists only on your phone. The
``ApprovalGate`` (running in the Mac agent / worker) re-verifies the assertion
itself via :func:`verify_assertion` — it does not take the Master's word for it.
"""

from __future__ import annotations

import base64
import hashlib
import json

import webauthn
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .config import Config
from .logging_utils import get_logger
from .schemas import PasskeyCredential
from .state_store import StateStore

log = get_logger("agentmgr.passkey")

# Single-operator system: one stable WebAuthn user handle.
_OPERATOR_USER_ID = b"agentmgr-operator"
_OPERATOR_USER_NAME = "operator"


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def b64u_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def challenge_for(message: bytes) -> bytes:
    """A deterministic 32-byte WebAuthn challenge bound to ``message``.

    Binding the challenge to the canonical approval/session message means an
    assertion can only ever satisfy the one request it was produced for.
    """
    return hashlib.sha256(message).digest()


def verify_assertion(
    store: StateStore,
    assertion_json: str,
    expected_challenge: bytes,
    rp_id: str,
    origin: str,
) -> bool:
    """True iff ``assertion_json`` is a valid passkey assertion for a registered
    credential over ``expected_challenge``. Never raises.

    Sign-count replay protection is intentionally not enforced (Apple passkeys
    report a count of 0); replay protection comes from the unique, request-bound
    challenge and from each request being consumed once.
    """
    try:
        data = json.loads(assertion_json)
        credential_id = data.get("id")
        if not credential_id:
            return False
        stored = store.get_passkey(credential_id)
        if stored is None:
            return False
        webauthn.verify_authentication_response(
            credential=assertion_json,
            expected_challenge=expected_challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=base64.b64decode(stored.public_key_b64),
            credential_current_sign_count=0,
            require_user_verification=True,
        )
        return True
    except Exception:  # noqa: BLE001 - any failure means "not verified"
        return False


class PasskeyService:
    """WebAuthn relying-party operations for the Master."""

    def __init__(self, config: Config, store: StateStore) -> None:
        self._rp_id = config.rp_id
        self._rp_name = config.rp_name
        self._origin = config.origin
        self._store = store

    # --- registration ----------------------------------------------------
    def registration_options(self) -> str:
        options = webauthn.generate_registration_options(
            rp_id=self._rp_id,
            rp_name=self._rp_name,
            user_id=_OPERATOR_USER_ID,
            user_name=_OPERATOR_USER_NAME,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        self._store.put_challenge("register", b64u(options.challenge))
        return webauthn.options_to_json(options)

    def verify_registration(
        self, credential_json: str, label: str = "passkey"
    ) -> PasskeyCredential:
        challenge = self._store.pop_challenge("register")
        if challenge is None:
            raise ValueError("no pending registration challenge")
        verified = webauthn.verify_registration_response(
            credential=credential_json,
            expected_challenge=b64u_decode(challenge),
            expected_rp_id=self._rp_id,
            expected_origin=self._origin,
            require_user_verification=True,
        )
        credential = PasskeyCredential(
            credential_id=b64u(verified.credential_id),
            public_key_b64=base64.b64encode(verified.credential_public_key).decode(),
            sign_count=verified.sign_count,
            label=label,
        )
        self._store.put_passkey(credential)
        log.info("passkey registered", extra={"credential_id": credential.credential_id})
        return credential

    # --- login (random challenge) ---------------------------------------
    def login_options(self) -> str:
        options = webauthn.generate_authentication_options(
            rp_id=self._rp_id,
            allow_credentials=self._allow_credentials(),
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        self._store.put_challenge("login", b64u(options.challenge))
        return webauthn.options_to_json(options)

    def verify_login(self, assertion_json: str) -> PasskeyCredential:
        challenge = self._store.pop_challenge("login")
        if challenge is None:
            raise ValueError("no pending login challenge")
        if not verify_assertion(
            self._store, assertion_json, b64u_decode(challenge),
            self._rp_id, self._origin,
        ):
            raise ValueError("passkey login assertion failed verification")
        return self._store.get_passkey(json.loads(assertion_json)["id"])

    # --- request-bound assertion options (approvals / sessions) ---------
    def assertion_options(self, challenge: bytes) -> str:
        """Options for an assertion bound to a specific request. The challenge
        is deterministic (``challenge_for``), so it need not be stored — the
        verifier re-derives it."""
        options = webauthn.generate_authentication_options(
            rp_id=self._rp_id,
            challenge=challenge,
            allow_credentials=self._allow_credentials(),
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return webauthn.options_to_json(options)

    def has_credentials(self) -> bool:
        return bool(self._store.list_passkeys())

    def _allow_credentials(self) -> list[PublicKeyCredentialDescriptor]:
        return [
            PublicKeyCredentialDescriptor(id=b64u_decode(c.credential_id))
            for c in self._store.list_passkeys()
        ]
