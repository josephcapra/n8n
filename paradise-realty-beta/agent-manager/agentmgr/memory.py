"""Persistent long-term memory for the Master / assistant.

A small, file-backed store of durable facts the assistant should carry across
conversations and restarts — the operator's preferences, the people and
businesses they work with, ongoing context. This is deliberately separate from
the conversation log (which is transient): memory is the handful of things
worth remembering forever, not a transcript.

It is a plain JSON file on disk, so it survives process restarts no matter
which state backend is in use, and you can read or edit it by hand.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from pydantic import BaseModel, Field

from .logging_utils import get_logger
from .util import gen_id, now_iso

log = get_logger("agentmgr.memory")


class MemoryEntry(BaseModel):
    id: str = Field(default_factory=lambda: gen_id("mem"))
    text: str
    created_at: str = Field(default_factory=now_iso)
    source: str = "user"             # "user" | "assistant" | "system"
    tags: list[str] = Field(default_factory=list)


class MemoryStore:
    """Thread-safe, file-backed list of durable memories."""

    def __init__(self, path: str | os.PathLike) -> None:
        self._path = Path(path).expanduser()
        self._lock = threading.RLock()
        self._entries: list[MemoryEntry] = self._load()

    # --- persistence -----------------------------------------------------
    def _load(self) -> list[MemoryEntry]:
        try:
            raw = json.loads(self._path.read_text())
        except FileNotFoundError:
            return []
        except Exception:  # noqa: BLE001 - corrupt file shouldn't crash startup
            log.warning("memory file unreadable; starting empty",
                        extra={"path": str(self._path)})
            return []
        out: list[MemoryEntry] = []
        for item in raw if isinstance(raw, list) else []:
            try:
                out.append(MemoryEntry(**item))
            except Exception:  # noqa: BLE001
                continue
        return out

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps([e.model_dump() for e in self._entries], indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(data)
            os.replace(tmp, self._path)        # atomic on POSIX
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    # --- operations ------------------------------------------------------
    def add(self, text: str, source: str = "user",
            tags: list[str] | None = None) -> MemoryEntry:
        text = (text or "").strip()
        if not text:
            raise ValueError("memory text is empty")
        with self._lock:
            for e in self._entries:               # skip exact duplicates
                if e.text.lower() == text.lower():
                    return e
            entry = MemoryEntry(text=text, source=source, tags=list(tags or []))
            self._entries.append(entry)
            self._save()
            log.info("memory added", extra={"id": entry.id})
            return entry

    def all(self) -> list[MemoryEntry]:
        with self._lock:
            return list(self._entries)

    def search(self, query: str, limit: int = 20) -> list[MemoryEntry]:
        q = (query or "").lower().strip()
        with self._lock:
            if not q:
                return list(self._entries)[-limit:]
            words = q.split()
            hits = [e for e in self._entries
                    if any(w in e.text.lower() for w in words)]
            return hits[-limit:]

    def delete(self, entry_id: str) -> bool:
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e.id != entry_id]
            changed = len(self._entries) != before
            if changed:
                self._save()
            return changed

    def delete_matching(self, query: str) -> list[MemoryEntry]:
        q = (query or "").lower().strip()
        if not q:
            return []
        with self._lock:
            removed = [e for e in self._entries if q in e.text.lower()]
            if removed:
                ids = {e.id for e in removed}
                self._entries = [e for e in self._entries if e.id not in ids]
                self._save()
            return removed

    def clear(self) -> int:
        with self._lock:
            n = len(self._entries)
            self._entries = []
            self._save()
            return n

    def prompt_block(self, limit: int = 50) -> str:
        """The memory rendered for injection into the assistant's system
        prompt. Empty string when there's nothing to remember."""
        with self._lock:
            entries = self._entries[-limit:]
        if not entries:
            return ""
        lines = "\n".join(f"- {e.text}" for e in entries)
        return (
            "Here is what you remember about the operator and their work from "
            "previous conversations. Use it naturally when relevant; don't "
            "recite it back unless asked:\n" + lines
        )
