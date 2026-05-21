"""Approve or deny pending SENSITIVE operations — the operator's terminal tool.

Run this in YOUR terminal:

    python -m tools.approve

It lists every pending approval request, lets you pick one, and asks you to
type ``approve`` or ``deny``. Your choice is signed with the approval PRIVATE
key on this machine and written back to the state store; the blocked agent
then unblocks. Nothing else can produce that signature.

This is the only interactive control point in the system — by design.
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentmgr.approval_gate import canonical_message
from agentmgr.config import load_config
from agentmgr.schemas import ApprovalResponse
from agentmgr.state_store import make_state_store

DEFAULT_KEY_PATH = Path.home() / ".agentmgr" / "approval_ed25519"


def _load_private_key() -> Ed25519PrivateKey:
    key_path = Path(os.environ.get("AGENTMGR_APPROVAL_PRIVKEY_FILE", DEFAULT_KEY_PATH))
    if not key_path.exists():
        raise SystemExit(
            f"no approval private key at {key_path}; run `python -m tools.gen_keys`"
        )
    raw = base64.b64decode(key_path.read_bytes())
    return Ed25519PrivateKey.from_private_bytes(raw)


def main() -> int:
    private_key = _load_private_key()
    store = make_state_store(load_config())

    pending = store.list_pending_approvals()
    if not pending:
        print("no pending approval requests.")
        return 0

    print(f"{len(pending)} pending approval request(s):\n")
    for idx, req in enumerate(pending):
        print(f"  [{idx}] {req.id}")
        print(f"        action: {req.action}")
        print(f"        reason: {req.details.get('_sensitive_reason', 'explicit')}")
        print(f"        details: {req.details}")
        print()

    raw = input("select request number to act on (or 'q' to quit): ").strip()
    if raw.lower() in ("q", "quit", ""):
        return 0
    try:
        request = pending[int(raw)]
    except (ValueError, IndexError):
        print("invalid selection.", file=sys.stderr)
        return 1

    decision = input("type 'approve' or 'deny': ").strip().lower()
    if decision not in ("approve", "deny"):
        print("not 'approve' or 'deny' — aborting, nothing signed.", file=sys.stderr)
        return 1
    note = input("optional note: ").strip()

    signature = private_key.sign(
        canonical_message(request.id, request.nonce, decision)
    )
    store.submit_approval_response(
        request.id,
        ApprovalResponse(
            decision=decision,
            signature_b64=base64.b64encode(signature).decode(),
            approver_note=note,
        ),
    )
    print(f"signed '{decision}' for {request.id} — the blocked agent will unblock.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
