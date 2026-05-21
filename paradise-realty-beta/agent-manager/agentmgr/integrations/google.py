"""Google account integration — STUBBED, OUT OF SCOPE for this build.

Every method raises ``NotImplementedError`` on purpose. Google account API
calls are classified SENSITIVE by the approval gate, and real OAuth wiring is
intentionally deferred to a later, manually reviewed phase so that credential
handling gets dedicated human review. Do NOT implement these here without
that review.

The classes exist now only so the rest of the system can depend on a stable
interface shape.
"""

from __future__ import annotations

from typing import Any

_DEFERRED = (
    "Google API integration is deliberately out of scope for this build; "
    "real OAuth wiring happens in a later, manually-reviewed phase."
)


class GmailClient:
    def search(self, query: str) -> list[dict[str, Any]]:
        raise NotImplementedError(_DEFERRED)

    def get_message(self, message_id: str) -> dict[str, Any]:
        raise NotImplementedError(_DEFERRED)

    def send(self, to: str, subject: str, body: str) -> dict[str, Any]:
        raise NotImplementedError(_DEFERRED)


class DriveClient:
    def search(self, query: str) -> list[dict[str, Any]]:
        raise NotImplementedError(_DEFERRED)

    def read_file(self, file_id: str) -> bytes:
        raise NotImplementedError(_DEFERRED)


class CalendarClient:
    def list_events(self, calendar_id: str = "primary") -> list[dict[str, Any]]:
        raise NotImplementedError(_DEFERRED)

    def create_event(self, calendar_id: str, event: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(_DEFERRED)
