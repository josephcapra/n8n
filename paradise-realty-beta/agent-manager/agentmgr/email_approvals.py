"""One-time-token approval store for the email reply-to-approve channel.

An agent that needs the operator's OK creates a pending approval (gets back a
short token to put in its approval email's subject as ``AM-OK:<token>``). When
the operator replies "approve"/"deny", the email gateway resolves the token
here. The agent polls ``get()`` / ``await_decision()`` to see the outcome.

Scope: **non-sensitive** approvals only (e.g. "send this drip batch", "post
this incentive"). Sensitive master operations still require the Ed25519/passkey
gate — email never resolves those.

Firestore-backed (collection ``agentmgr_email_approvals``); tokens are
single-use and expire.
"""

from __future__ import annotations

import os
import secrets as _secrets
import time

COLLECTION = "agentmgr_email_approvals"
_APPROVE = {"approve", "approved", "yes", "ok", "okay", "y", "go", "send", "do it"}
_DENY = {"deny", "denied", "no", "n", "cancel", "stop", "reject", "rejected"}


def _db():
    from google.cloud import firestore
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("AGENTMGR_PROJECT")
    return firestore.Client(project=project) if project else firestore.Client()


def normalize_decision(text: str) -> str | None:
    """Map free-text reply to 'approved' / 'denied' / None (unclear)."""
    first = (text or "").strip().lower().splitlines()[0] if (text or "").strip() else ""
    word = first.strip(" .!*>-").split()[0] if first.split() else ""
    if word in _APPROVE or first in _APPROVE:
        return "approved"
    if word in _DENY or first in _DENY:
        return "denied"
    return None


def create_pending(kind: str, summary: str, payload: dict | None = None,
                    ttl_s: int = 7 * 86400) -> str:
    """Create a pending approval; return the one-time token to email."""
    token = _secrets.token_urlsafe(9)
    _db().collection(COLLECTION).document(token).set({
        "token": token, "kind": kind, "summary": summary, "payload": payload or {},
        "status": "pending", "created_at": time.time(),
        "expires_at": time.time() + ttl_s, "decided_by": None, "decided_at": None,
    })
    return token


def resolve(token: str, decision: str, by: str = "email") -> dict:
    """Resolve a pending token. ``decision`` is free text or approved/denied.

    Single-use: a token that is not currently 'pending' is rejected.
    """
    norm = decision if decision in ("approved", "denied") else normalize_decision(decision)
    if norm not in ("approved", "denied"):
        return {"ok": False, "reason": "unclear decision"}
    ref = _db().collection(COLLECTION).document(token)
    snap = ref.get()
    if not getattr(snap, "exists", False):
        return {"ok": False, "reason": "unknown token"}
    d = snap.to_dict() or {}
    if d.get("status") != "pending":
        return {"ok": False, "reason": f"already {d.get('status')}"}
    if d.get("expires_at", 0) < time.time():
        ref.set({"status": "expired"}, merge=True)
        return {"ok": False, "reason": "expired"}
    ref.set({"status": norm, "decided_by": by, "decided_at": time.time()}, merge=True)
    return {"ok": True, "status": norm, "kind": d.get("kind"), "summary": d.get("summary")}


def get(token: str) -> dict | None:
    snap = _db().collection(COLLECTION).document(token).get()
    return snap.to_dict() if getattr(snap, "exists", False) else None


def await_decision(token: str, timeout_s: float = 0, poll_s: float = 15) -> str:
    """Return the current/eventual status ('approved'/'denied'/'pending'/...).

    timeout_s=0 returns the current status immediately (non-blocking).
    """
    deadline = time.time() + timeout_s
    while True:
        d = get(token) or {}
        status = d.get("status", "unknown")
        if status != "pending" or timeout_s <= 0 or time.time() >= deadline:
            return status
        time.sleep(poll_s)
