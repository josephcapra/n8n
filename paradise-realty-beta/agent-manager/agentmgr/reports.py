"""File-backed report store for the command center.

Any component that emails the operator a report also saves its HTML here, so
the UI can list the reports and open the exact same content in a new window.

By default there is ONE entry per ``report_id`` — saving the same id overwrites
it, so the roster shows the latest of each report type (e.g. the most recent
security scan). Pass a unique id (with a timestamp) to keep history instead.

Layout:  ~/.agentmgr-reports/<id>.html  +  index.json  (metadata, newest first)
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

_DIR = Path.home() / ".agentmgr-reports"
_INDEX = _DIR / "index.json"


def _safe_id(report_id: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "-", (report_id or "report").lower())[:64] or "report"


def _read_index() -> list[dict]:
    if not _INDEX.exists():
        return []
    try:
        return json.loads(_INDEX.read_text())
    except (OSError, ValueError):
        return []


def save_report(report_id: str, title: str, html: str, source: str = "") -> dict:
    """Persist a report's HTML + metadata. Overwrites any existing same id."""
    _DIR.mkdir(parents=True, exist_ok=True)
    rid = _safe_id(report_id)
    (_DIR / f"{rid}.html").write_text(html)
    entry = {
        "id": rid,
        "title": title or rid,
        "source": source,
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
    }
    items = [r for r in _read_index() if r.get("id") != rid]
    items.insert(0, entry)
    _INDEX.write_text(json.dumps(items, indent=2))
    return entry


def list_reports() -> list[dict]:
    """Saved reports, newest first; drops any whose HTML file went missing."""
    return [r for r in _read_index() if (_DIR / f"{r.get('id')}.html").exists()]


def get_report_html(report_id: str) -> str | None:
    p = _DIR / f"{_safe_id(report_id)}.html"
    return p.read_text() if p.exists() else None
