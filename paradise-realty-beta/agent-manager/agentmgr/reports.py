"""Report store for the command center — file or GCS backend.

Any component that emails the operator a report also saves its HTML here, so
the UI can list the reports and open the same content in a new window. Backend
is chosen by env so the local Master and remote Cloud Run jobs can share one
store:

  AGENTMGR_REPORTS_BACKEND  file | gcs        (default: file)
  AGENTMGR_REPORTS_BUCKET   <gcs bucket>      (required for gcs)
  AGENTMGR_REPORTS_PREFIX   <key prefix>      (default: agentmgr-reports)

There is ONE entry per ``report_id`` — saving the same id overwrites it, so the
roster shows the latest of each report type. Each report is stored as two
objects, ``<id>.html`` and ``<id>.json`` (metadata), so distributed writers
never race on a shared index.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

_FILE_DIR = Path.home() / ".agentmgr-reports"


def _backend() -> str:
    return os.environ.get("AGENTMGR_REPORTS_BACKEND", "file")


def _bucket() -> str:
    return os.environ.get("AGENTMGR_REPORTS_BUCKET", "")


def _prefix() -> str:
    return os.environ.get("AGENTMGR_REPORTS_PREFIX", "agentmgr-reports").strip("/")


def _use_gcs() -> bool:
    return _backend() == "gcs" and bool(_bucket())


def _safe_id(report_id: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "-", (report_id or "report").lower())[:64] or "report"


# --- GCS backend ---------------------------------------------------------

def _gcs_bucket():
    from google.cloud import storage  # lazy
    return storage.Client().bucket(_bucket())


def _gcs_save(rid: str, meta: dict, html: str) -> None:
    b = _gcs_bucket()
    p = _prefix()
    b.blob(f"{p}/{rid}.html").upload_from_string(html, content_type="text/html")
    b.blob(f"{p}/{rid}.json").upload_from_string(
        json.dumps(meta), content_type="application/json"
    )


def _gcs_list() -> list[dict]:
    b = _gcs_bucket()
    out = []
    for blob in b.list_blobs(prefix=f"{_prefix()}/"):
        if blob.name.endswith(".json") and not blob.name.endswith("index.json"):
            try:
                meta = json.loads(blob.download_as_text())
            except (ValueError, OSError):
                continue
            if isinstance(meta, dict) and meta.get("id"):
                out.append(meta)
    return out


def _gcs_get(rid: str) -> str | None:
    blob = _gcs_bucket().blob(f"{_prefix()}/{rid}.html")
    return blob.download_as_text() if blob.exists() else None


# --- file backend --------------------------------------------------------

def _file_save(rid: str, meta: dict, html: str) -> None:
    _FILE_DIR.mkdir(parents=True, exist_ok=True)
    (_FILE_DIR / f"{rid}.html").write_text(html)
    (_FILE_DIR / f"{rid}.json").write_text(json.dumps(meta))


def _file_list() -> list[dict]:
    if not _FILE_DIR.exists():
        return []
    out = []
    for j in _FILE_DIR.glob("*.json"):
        if j.name == "index.json":  # legacy format — ignore
            continue
        try:
            meta = json.loads(j.read_text())
        except (ValueError, OSError):
            continue
        if isinstance(meta, dict) and meta.get("id"):
            out.append(meta)
    return out


def _file_get(rid: str) -> str | None:
    p = _FILE_DIR / f"{rid}.html"
    return p.read_text() if p.exists() else None


# --- public API ----------------------------------------------------------

def save_report(report_id: str, title: str, html: str, source: str = "") -> dict:
    """Persist a report's HTML + metadata. Overwrites any existing same id."""
    rid = _safe_id(report_id)
    meta = {
        "id": rid,
        "title": title or rid,
        "source": source,
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
    }
    if _use_gcs():
        _gcs_save(rid, meta, html)
    else:
        _file_save(rid, meta, html)
    return meta


def list_reports() -> list[dict]:
    """Saved reports, newest first."""
    items = _gcs_list() if _use_gcs() else _file_list()
    return sorted(items, key=lambda r: r.get("updated_at", ""), reverse=True)


def get_report_html(report_id: str) -> str | None:
    rid = _safe_id(report_id)
    return _gcs_get(rid) if _use_gcs() else _file_get(rid)
