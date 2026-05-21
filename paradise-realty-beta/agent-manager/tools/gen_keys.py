"""Generate the approval Ed25519 key pair.

Run this ONCE, on your own machine:

    python -m tools.gen_keys

It writes the PRIVATE key to ``~/.agentmgr/approval_ed25519`` (mode 0600) and
prints the PUBLIC key. Set the public key as ``AGENTMGR_APPROVAL_PUBKEY`` on
the Master and worker.

SECURITY: the private key must never be committed, never be uploaded to Secret
Manager, and never be present in any deployed environment. It is the only
thing that can approve a SENSITIVE operation — keep it on your machine only.
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DEFAULT_KEY_PATH = Path.home() / ".agentmgr" / "approval_ed25519"


def main() -> int:
    key_path = Path(os.environ.get("AGENTMGR_APPROVAL_PRIVKEY_FILE", DEFAULT_KEY_PATH))
    if key_path.exists():
        print(f"refusing to overwrite existing key at {key_path}", file=sys.stderr)
        print("delete it yourself if you really mean to rotate keys.", file=sys.stderr)
        return 1

    private_key = Ed25519PrivateKey.generate()
    raw_private = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    raw_public = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    key_path.parent.mkdir(parents=True, exist_ok=True)
    # Create with 0600 from the start — never world-readable, even briefly.
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(base64.b64encode(raw_private))

    print(f"private key written to {key_path} (mode 0600 — keep it there)")
    print()
    print("set this on the Master and worker as AGENTMGR_APPROVAL_PUBKEY:")
    print()
    print(f"  AGENTMGR_APPROVAL_PUBKEY={base64.b64encode(raw_public).decode()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
