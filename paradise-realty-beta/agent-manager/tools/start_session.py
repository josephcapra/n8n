"""Start a session window — operator terminal tool.

    python -m tools.start_session shell          # 15-min shell session
    python -m tools.start_session cloudrun 600   # 10-min cloudrun session
    python -m tools.start_session all 300        # 5-min everything

Signs a SessionGrant with your approval PRIVATE key and stores it. While the
window is open, commands in that scope run without per-command approval —
except catastrophic commands (the always-confirm denylist), which always
re-prompt. Revoke early from the app or with `tools.approve`-style access.

The signing key never leaves this machine; the deployed system can only
*verify* the grant, never mint one.
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentmgr.config import load_config
from agentmgr.session import canonical_session_message, new_grant
from agentmgr.state_store import make_state_store

DEFAULT_KEY_PATH = Path.home() / ".agentmgr" / "approval_ed25519"


def main() -> int:
    scope = sys.argv[1] if len(sys.argv) > 1 else "shell"
    cfg = load_config()
    ttl = float(sys.argv[2]) if len(sys.argv) > 2 else cfg.session_ttl_s
    if scope not in ("shell", "cloudrun", "all"):
        print("scope must be one of: shell | cloudrun | all", file=sys.stderr)
        return 1

    key_path = Path(os.environ.get("AGENTMGR_APPROVAL_PRIVKEY_FILE", DEFAULT_KEY_PATH))
    if not key_path.exists():
        print(f"no approval key at {key_path}; run `python -m tools.gen_keys`",
              file=sys.stderr)
        return 1
    private_key = Ed25519PrivateKey.from_private_bytes(
        base64.b64decode(key_path.read_bytes())
    )

    grant = new_grant(scope, ttl)
    grant.signature_b64 = base64.b64encode(
        private_key.sign(canonical_session_message(grant))
    ).decode()

    store = make_state_store(cfg)
    store.put_session(grant)
    print(f"session {grant.id} started — scope={scope}, expires {grant.expires_at}")
    print(f"revoke early:  POST /sessions/{grant.id}/revoke  on the Master")
    return 0


if __name__ == "__main__":
    sys.exit(main())
