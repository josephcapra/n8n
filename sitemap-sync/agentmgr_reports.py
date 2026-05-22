"""Vendored report writer for Cloud Run jobs — CANONICAL COPY.

Copied verbatim into each job repo that emails the operator a report. It saves
the SAME HTML to a shared GCS bucket so the agent-manager command center can
list and open it. Standalone: only depends on ``google-cloud-storage``.

Layout (must match agentmgr.reports' GCS backend so the Master can read it):
  gs://<bucket>/<prefix>/<id>.html   — the report HTML
  gs://<bucket>/<prefix>/<id>.json   — {id, title, source, updated_at}

Bucket/prefix come from env, with defaults so jobs need no extra config:
  AGENTMGR_REPORTS_BUCKET  (default: paradise-realty-backups)
  AGENTMGR_REPORTS_PREFIX  (default: agentmgr-reports)
"""

from __future__ import annotations

import json
import os
import re
import time

_BUCKET = os.environ.get("AGENTMGR_REPORTS_BUCKET", "paradise-realty-backups")
_PREFIX = os.environ.get("AGENTMGR_REPORTS_PREFIX", "agentmgr-reports").strip("/")


def _safe_id(report_id: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "-", (report_id or "report").lower())[:64] or "report"


def save_report(report_id: str, title: str, html: str, source: str = "") -> bool:
    """Best-effort: write the report HTML + metadata to GCS. Never raises —
    a failure here must not break the job's real work or its email."""
    try:
        from google.cloud import storage  # lazy

        rid = _safe_id(report_id)
        meta = {
            "id": rid,
            "title": title or rid,
            "source": source,
            "updated_at": time.strftime("%Y-%m-%d %H:%M"),
        }
        bucket = storage.Client().bucket(_BUCKET)
        bucket.blob(f"{_PREFIX}/{rid}.html").upload_from_string(
            html, content_type="text/html"
        )
        bucket.blob(f"{_PREFIX}/{rid}.json").upload_from_string(
            json.dumps(meta), content_type="application/json"
        )
        return True
    except Exception:  # noqa: BLE001 - report capture is best-effort
        return False
