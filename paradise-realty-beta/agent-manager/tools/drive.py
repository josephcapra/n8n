"""Google Drive reader — find knowledge in the operator's Drive (read-only).

The assistant calls this as a shell command:

    python -m tools.drive search "listing agreement"
    python -m tools.drive read <file_id>

Auth: a one-time `rclone` Google login (remote named "gdrive") holds the OAuth
token and refreshes it. We let rclone keep the token fresh, then borrow the
current access token to call the Drive API directly — that gives real
full-text search and clean text export, which rclone's CLI can't do.

Set up once with:
    rclone config create gdrive drive scope drive.readonly

Read-only by design: it never writes, shares, or deletes anything.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

import httpx

_API = "https://www.googleapis.com/drive/v3"
_REMOTE = "gdrive"
_SETUP_HINT = (
    "Drive isn't connected. Run a one-time Google login:\n"
    "  rclone config create gdrive drive scope drive.readonly"
)
_EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


def _rclone() -> str:
    """Locate the rclone binary by absolute path — the agent's shell often
    lacks /opt/homebrew/bin on PATH, so don't rely on it."""
    for cand in (shutil.which("rclone"),
                 "/opt/homebrew/bin/rclone", "/usr/local/bin/rclone"):
        if cand and os.path.exists(cand):
            return cand
    raise RuntimeError("rclone not found")


def _access_token() -> str:
    """A fresh Drive access token. We nudge rclone to refresh if needed (it
    writes the new token back to its config), then read it out."""
    rclone = _rclone()
    subprocess.run([rclone, "about", f"{_REMOTE}:"],
                   capture_output=True, timeout=45)
    dump = subprocess.run([rclone, "config", "dump"],
                          capture_output=True, text=True, timeout=15)
    if dump.returncode != 0:
        raise RuntimeError("rclone not configured")
    conf = json.loads(dump.stdout or "{}")
    if _REMOTE not in conf:
        raise RuntimeError("no 'gdrive' rclone remote")
    return json.loads(conf[_REMOTE]["token"])["access_token"]


def _headers() -> dict:
    return {"Authorization": f"Bearer {_access_token()}"}


def search(query: str, limit: int = 15) -> int:
    safe = query.replace("\\", "\\\\").replace("'", "\\'")
    q = f"(name contains '{safe}' or fullText contains '{safe}') and trashed = false"
    r = httpx.get(
        f"{_API}/files", headers=_headers(),
        params={
            "q": q, "pageSize": limit, "corpora": "user",
            "fields": "files(id,name,mimeType,modifiedTime)",
            "orderBy": "modifiedTime desc",
        },
        timeout=45,
    )
    r.raise_for_status()
    files = r.json().get("files", [])
    if not files:
        print(f"No Drive files match '{query}'.")
        return 0
    print(f"Found {len(files)} file(s) for '{query}':")
    for f in files:
        kind = f.get("mimeType", "").split(".")[-1]
        print(f"  • {f['name']}  [{kind}]  id={f['id']}  ({f.get('modifiedTime','')[:10]})")
    print('\nRead one with:  python -m tools.drive read <id>')
    return 0


def read(file_id: str, max_chars: int = 8000) -> int:
    meta = httpx.get(f"{_API}/files/{file_id}", headers=_headers(),
                     params={"fields": "id,name,mimeType"}, timeout=30)
    meta.raise_for_status()
    m = meta.json()
    mime = m.get("mimeType", "")

    if mime in _EXPORTS:
        r = httpx.get(f"{_API}/files/{file_id}/export", headers=_headers(),
                      params={"mimeType": _EXPORTS[mime]}, timeout=60)
    elif mime.startswith("application/vnd.google-apps"):
        print(f"“{m['name']}” is a {mime} with no text export — open it in Drive.")
        return 0
    elif mime.startswith("text/") or mime in ("application/json", "text/csv"):
        r = httpx.get(f"{_API}/files/{file_id}", headers=_headers(),
                      params={"alt": "media"}, timeout=60)
    else:
        print(f"“{m['name']}” is binary ({mime}); not extracting text here.")
        return 0

    r.raise_for_status()
    text = r.text
    print(f"# {m['name']}\n")
    print(text[:max_chars] + ("\n\n…(truncated)" if len(text) > max_chars else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Read the operator's Google Drive.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search", help="find files by name or content")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=15)
    rd = sub.add_parser("read", help="read a file's text by id")
    rd.add_argument("file_id")
    rd.add_argument("--max-chars", type=int, default=8000)
    args = ap.parse_args()

    try:
        if args.cmd == "search":
            return search(args.query, args.limit)
        return read(args.file_id, args.max_chars)
    except httpx.HTTPStatusError as exc:
        print(f"Drive API error {exc.response.status_code}.", file=sys.stderr)
        if exc.response.status_code in (401, 403):
            print(_SETUP_HINT, file=sys.stderr)
        return 1
    except (RuntimeError, FileNotFoundError) as exc:
        print(f"Drive error: {exc}", file=sys.stderr)
        print(_SETUP_HINT, file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Drive error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
