#!/usr/bin/env python3
"""Google Chat → agent-manager relay (Cloud Pub/Sub transport).

Google Chat delivers every message your app receives to a Pub/Sub topic; this
daemon PULLS them on the Mac (no public endpoint, no open ports), verifies the
sender is the operator, runs the command through the agent-manager — or full
Claude Code if you've explicitly opted in — and posts the reply back into the
Chat space, threaded. The phone-friendly sibling of tools/email_gateway.py.

Auth: one service account (key from setup-google-chat.sh) does both jobs —
pull from Pub/Sub (scope pubsub) and post as the Chat app (scope chat.bot).

Run:  python -m tools.chat_gateway       (normally launchd keeps it running)
Kill-switch:  touch ~/.agentmgr-chat-relay.disabled
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time

PROJECT = os.environ.get("AGENTMGR_GCP_PROJECT", "paradise-automation")
SUBSCRIPTION = os.environ.get(
    "AGENTMGR_CHAT_SUB", f"projects/{PROJECT}/subscriptions/agentmgr-chat-sub")
SA_KEY = os.environ.get(
    "AGENTMGR_CHAT_SA_KEY", os.path.expanduser("~/paradise-realty/api/agentmgr-chat-sa.json"))
OPERATOR = os.environ.get("AGENTMGR_OPERATOR_EMAIL", "joe@josephcapra.com").lower()
SCOPES = ["https://www.googleapis.com/auth/pubsub",
          "https://www.googleapis.com/auth/chat.bot"]


def _log(msg: str, **kw) -> None:
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "msg": msg, **kw}
    print(json.dumps(rec, default=str), flush=True)


def kill_switched() -> bool:
    flag = os.environ.get("AGENTMGR_CHAT_DISABLE_FILE",
                          os.path.expanduser("~/.agentmgr-chat-relay.disabled"))
    return os.path.exists(flag)


def _services():
    """Build the Pub/Sub + Chat API clients from the relay service account."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(SA_KEY, scopes=SCOPES)
    pubsub = build("pubsub", "v1", credentials=creds, cache_discovery=False)
    chat = build("chat", "v1", credentials=creds, cache_discovery=False)
    return pubsub, chat


def _sender_email(event: dict) -> str:
    msg = event.get("message", {}) or {}
    sender = msg.get("sender", {}) or {}
    return (sender.get("email") or (event.get("user", {}) or {}).get("email") or "").lower()


def _post(chat, space: str, text: str, thread: str | None = None) -> None:
    """Post a message into the Chat space, threaded under `thread` when given."""
    body: dict = {"text": text[:4000] or "(no output)"}
    kwargs = {"parent": space, "body": body}
    if thread:
        body["thread"] = {"name": thread}
        kwargs["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    chat.spaces().messages().create(**kwargs).execute()


def _dispatch(command: str) -> str:
    """Route the command exactly like the email gateway (AGENTMGR_GATEWAY_TARGET:
    'master' default → the agent-manager; 'claude' → headless Claude Code)."""
    from tools.email_gateway import _dispatch_command
    return _dispatch_command(command)


def _handle(event: dict, chat) -> dict:
    etype = event.get("type")
    space = (event.get("space", {}) or {}).get("name") or \
            ((event.get("message", {}) or {}).get("space", {}) or {}).get("name")
    sender = _sender_email(event)

    if etype == "ADDED_TO_SPACE":
        if space:
            _post(chat, space, "Agent-Manager here. Message me a command and I'll "
                               "run it through your agents and reply here.")
        return {"type": etype, "greeted": bool(space)}

    if etype != "MESSAGE":
        return {"type": etype, "skipped": True}

    if sender != OPERATOR:
        if space:
            _post(chat, space, "Sorry — I only take commands from the account owner.")
        return {"type": etype, "rejected": f"sender {sender or '?'} not operator"}

    text = ((event.get("message", {}) or {}).get("text") or "").strip()
    thread = ((event.get("message", {}) or {}).get("thread", {}) or {}).get("name")
    if not text:
        return {"type": etype, "empty": True}

    try:
        reply = _dispatch(text)
        ok = True
    except Exception as exc:  # noqa: BLE001
        reply = f"Command failed: {exc}"
        ok = False
    if space:
        _post(chat, space, reply, thread=thread)
    return {"type": etype, "command": text, "ok": ok}


def poll_forever() -> None:
    if not os.path.exists(SA_KEY):
        _log("no service-account key — run tools/setup-google-chat.sh first", key=SA_KEY)
        sys.exit(1)
    pubsub, chat = _services()
    _log("chat relay started", subscription=SUBSCRIPTION, operator=OPERATOR)
    sub = pubsub.projects().subscriptions()
    while True:
        if kill_switched():
            time.sleep(10)
            continue
        try:
            resp = sub.pull(subscription=SUBSCRIPTION,
                            body={"maxMessages": 10, "returnImmediately": False}).execute()
        except Exception as exc:  # noqa: BLE001 - transient API/network → back off
            _log("pull error; backing off", error=f"{type(exc).__name__}: {exc}")
            time.sleep(5)
            continue
        received = resp.get("receivedMessages", []) or []
        if not received:
            time.sleep(2)
            continue
        ack_ids = []
        for rm in received:
            ack_ids.append(rm["ackId"])
            data = (rm.get("message", {}) or {}).get("data")
            try:
                event = json.loads(base64.b64decode(data)) if data else {}
                _log("handled", **_handle(event, chat))
            except Exception as exc:  # noqa: BLE001 - never let one bad msg wedge the loop
                _log("handle error", error=f"{type(exc).__name__}: {exc}")
        try:
            sub.acknowledge(subscription=SUBSCRIPTION, body={"ackIds": ack_ids}).execute()
        except Exception as exc:  # noqa: BLE001
            _log("ack error", error=f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    poll_forever()
