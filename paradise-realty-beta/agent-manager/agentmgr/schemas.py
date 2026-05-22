"""Typed models shared across the Master, workers, and the state store.

Timestamps are ISO-8601 strings so every model serializes cleanly into
Firestore or JSON with no custom encoders.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .util import gen_id, now_iso


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"


# --- Conversation --------------------------------------------------------

class ConversationTurn(BaseModel):
    role: str                       # "user" | "master" | "worker"
    text: str
    ts: str = Field(default_factory=now_iso)


# --- Tasks (a unit of work handed to one worker) -------------------------

class TaskSpec(BaseModel):
    id: str = Field(default_factory=lambda: gen_id("task"))
    agent: str                      # registry name of the target worker
    kind: str                       # task type the worker understands
    payload: dict[str, Any] = Field(default_factory=dict)
    conversation_id: str
    correlation_id: str             # top-level command this task belongs to
    depth: int = 0                  # fan-out depth (loop guard)
    sensitive: bool = False         # routed through approval_gate if True
    status: TaskStatus = TaskStatus.PENDING
    created_at: str = Field(default_factory=now_iso)


class TaskResult(BaseModel):
    task_id: str
    status: TaskStatus
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    worker: str | None = None
    finished_at: str = Field(default_factory=now_iso)


# --- Inter-agent message bus (hub model: always relayed via the Master) --

class Message(BaseModel):
    id: str = Field(default_factory=lambda: gen_id("msg"))
    correlation_id: str
    from_agent: str
    to_agent: str                   # "master" for results headed to the hub
    payload: dict[str, Any] = Field(default_factory=dict)
    relay_depth: int = 0            # how many Master hops this message has taken
    created_at: str = Field(default_factory=now_iso)


# --- Approval requests ---------------------------------------------------

class ApprovalResponse(BaseModel):
    decision: str                   # "approve" | "deny"
    proof_type: str = "ed25519"     # "ed25519" (laptop key) | "webauthn" (passkey)
    signature_b64: str = ""         # Ed25519 signature over the canonical message
    webauthn_assertion: str | None = None  # JSON assertion from the browser
    credential_id: str | None = None       # passkey credential id (webauthn proof)
    approver_note: str = ""
    signed_at: str = Field(default_factory=now_iso)


class ApprovalRequest(BaseModel):
    id: str = Field(default_factory=lambda: gen_id("appr"))
    action: str
    details: dict[str, Any] = Field(default_factory=dict)
    nonce: str = Field(default_factory=lambda: gen_id("nonce"))
    status: ApprovalStatus = ApprovalStatus.PENDING
    response: ApprovalResponse | None = None
    created_at: str = Field(default_factory=now_iso)


# --- Session window (Phase 1.5) ------------------------------------------

class SessionGrant(BaseModel):
    """A signed grant that lets commands in a scope run without per-command
    approval until ``expires_at``. Signed by the operator's approval key (or,
    from Increment 2, a passkey assertion). Cannot be forged or extended."""

    id: str = Field(default_factory=lambda: gen_id("sess"))
    scope: str                       # "shell" | "cloudrun" | "all"
    issued_at: str = Field(default_factory=now_iso)
    expires_at: str                  # ISO-8601; hard expiry
    nonce: str = Field(default_factory=lambda: gen_id("snonce"))
    proof_type: str = "ed25519"      # "ed25519" (laptop key) | "webauthn" (passkey) | "token" (password)
    signature_b64: str = ""          # Ed25519 sig, or HMAC hex for proof_type='token'
    webauthn_assertion: str | None = None  # JSON assertion from the browser
    credential_id: str | None = None       # passkey credential id (webauthn proof)
    revoked: bool = False


class PasskeyCredential(BaseModel):
    """A registered WebAuthn passkey (the operator's Face ID credential)."""

    credential_id: str               # base64url, unpadded
    public_key_b64: str              # COSE public key, standard base64
    sign_count: int = 0
    label: str = "passkey"
    created_at: str = Field(default_factory=now_iso)


# --- Chat API ------------------------------------------------------------

class Attachment(BaseModel):
    """A file the operator shared in chat, saved on disk by /upload."""
    id: str = ""
    filename: str = ""
    path: str
    kind: str = "file"               # "image" | "file"
    media_type: str = ""
    size: int = 0


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)


class ChatResponse(BaseModel):
    conversation_id: str
    correlation_id: str
    interpretation: str
    task_ids: list[str] = Field(default_factory=list)
    reply: str
