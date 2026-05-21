"""Run the whole Phase 1 flow locally — no GCP access required.

    python -m tools.local_demo "summarize the brevard launch"

Builds the Master on the in-memory state store with the local (in-process)
job runner, sends one chat command through the authenticated endpoint, and
prints the Master's reply. This is the fast end-to-end smoke test.
"""

from __future__ import annotations

import sys

from fastapi.testclient import TestClient

from agentmgr.config import Config
from master.main import build_app

_TOKEN = "local-demo-token"


def main() -> int:
    message = " ".join(sys.argv[1:]) or "transform: upper hello from the local demo"

    cfg = Config(
        state_backend="memory",
        job_runner="local",
        api_token=_TOKEN,
    )
    app = build_app(cfg)
    client = TestClient(app)

    health = client.get("/health").json()
    print(f"health: {health}\n")

    # Unauthenticated call must be rejected.
    unauth = client.post("/chat", json={"message": message})
    print(f"unauthenticated /chat -> {unauth.status_code} (expected 401)\n")

    resp = client.post(
        "/chat",
        json={"message": message},
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    if resp.status_code != 200:
        print(f"/chat failed: {resp.status_code} {resp.text}", file=sys.stderr)
        return 1

    body = resp.json()
    print(f"correlation_id: {body['correlation_id']}")
    print(f"interpretation: {body['interpretation']}")
    print(f"task_ids:       {body['task_ids']}")
    print(f"\nreply:\n{body['reply']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
