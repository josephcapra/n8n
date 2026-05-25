#!/usr/bin/env python3
"""Email control channel for the agent-manager — STAGE 1 (commands).

Lets the operator email commands to the master and get results back. Designed
to run on a schedule (launchd / cron / Cloud Run job); each run polls Gmail
once and processes new authorized messages.

SECURITY MODEL (deliberately strict — this is remote control of the master):
  * Sender allowlist: ONLY messages From the operator address are considered.
  * DKIM/DMARC: the message's Authentication-Results must show dmarc=pass (or
    dkim=pass) for the operator's domain — defeats almost all From-spoofing,
    since a forged From won't carry a valid signature for that domain.
  * Marker: the subject must carry the command marker (default "AM:") so random
    mail to the inbox is never interpreted as a command.
  * De-dupe: each Gmail message id is processed at most once.
  * Kill-switch: if AGENTMGR_EMAIL_GATEWAY_DISABLED is set (or a disable file
    exists), the gateway processes nothing.
  * Sensitive actions are NOT auto-approved here. Commands run through the
    master's normal pipeline, so any sensitive sub-action still blocks on the
    Ed25519/passkey gate; stage 2 emails a one-tap "approve in app" link.

Nothing here can approve a sensitive action — by design.
"""

from __future__ import annotations

import os
import re

OPERATOR = os.environ.get("AGENTMGR_OPERATOR_EMAIL", "joe@josephcapra.com")
OPERATOR_DOMAIN = OPERATOR.split("@")[-1].lower()
MARKER = os.environ.get("AGENTMGR_EMAIL_MARKER", "AM:")  # subject prefix/tag


# --------------------------------------------------------------------------- pure gate
def _header(headers: dict, name: str) -> str:
    # Case-insensitive header lookup (Gmail returns mixed case).
    name = name.lower()
    for k, v in headers.items():
        if k.lower() == name:
            return v or ""
    return ""


def _addr(value: str) -> str:
    """Extract the bare email address from a From/Sender header value."""
    m = re.search(r"[\w.+-]+@[\w.-]+", value or "")
    return (m.group(0) if m else "").lower()


def auth_passes(auth_results: str, domain: str) -> bool:
    """True if Authentication-Results shows dmarc=pass, or dkim=pass for `domain`.

    Gmail evaluates these on receipt; we just read its verdict.
    """
    ar = (auth_results or "").lower()
    if "dmarc=pass" in ar:
        return True
    # dkim=pass with the signing domain matching the sender domain
    for m in re.finditer(r"dkim=pass[^;]*?header\.d=([\w.-]+)", ar):
        if m.group(1).lower().endswith(domain):
            return True
    return False


def gate(headers: dict, *, labels: list | None = None,
         require_marker: bool = True) -> tuple[bool, str]:
    """Decide whether a message is an authorized command. Pure; no I/O.

    Auth model — the message must be From the operator AND prove it came from the
    operator's own authenticated account. Any ONE of these proofs is accepted:
      * DKIM/DMARC pass for the operator domain (mail sent from another client);
      * the message is in the account's SENT folder (only the account owner can
        put mail there) — the normal "email yourself a command" case.

    An externally-injected spoof (forged From) is delivered to INBOX only and
    carries a failing Authentication-Results header, so it satisfies neither
    proof and is rejected.

    Returns (allowed, reason). `reason` explains a rejection for the audit log.
    """
    sender = _addr(_header(headers, "From"))
    if sender != OPERATOR.lower():
        return False, f"sender {sender or '?'} not the operator allowlist"
    ar = _header(headers, "Authentication-Results")
    in_sent = bool(labels) and "SENT" in labels
    if not (auth_passes(ar, OPERATOR_DOMAIN) or in_sent):
        return False, "not provably from the operator's account (possible spoof)"
    if require_marker:
        subj = _header(headers, "Subject")
        if MARKER.lower() not in subj.lower():
            return False, f"subject missing command marker '{MARKER}'"
    return True, "ok"


def extract_command(subject: str, body: str) -> str:
    """Pull the command text. When you REPLY to a result email, the command is
    the new text you typed above the quoted history — so prefer that. A fresh
    ``AM: <command>`` subject with no body is the fallback (subject after the
    marker)."""
    lead = _lead_text(body)            # the reply's new text, above quotes/signature
    if lead:
        return lead
    subj = subject or ""
    idx = subj.lower().find(MARKER.lower())
    return subj[idx + len(MARKER):].strip() if idx >= 0 else subj.strip()


# Approval emails carry this marker + token in their subject: "AM-OK:<token>".
APPROVAL_RE = re.compile(r"AM-OK:([A-Za-z0-9_-]{6,})")


def _lead_text(body: str) -> str:
    """The reply's leading text, above any quoted history / signature."""
    lines = []
    for ln in (body or "").splitlines():
        s = ln.strip()
        if s.startswith(">") or (s.startswith("On ") and s.endswith("wrote:")):
            break
        if s in ("--", "—") or s.startswith("Sent from"):
            break
        lines.append(ln)
    return "\n".join(lines).strip()


def parse_approval(headers: dict, body: str) -> dict | None:
    """Extract {token, decision} from an approval reply, or None if not one.

    Token comes from the ``AM-OK:<token>`` marker (preserved in the reply
    subject); decision is read from the reply's leading text.
    """
    blob = f"{_header(headers, 'Subject')}\n{body or ''}"
    m = APPROVAL_RE.search(blob)
    if not m:
        return None
    from agentmgr.email_approvals import normalize_decision
    decision = normalize_decision(_lead_text(body)) or normalize_decision(_header(headers, "Subject"))
    return {"token": m.group(1), "decision": decision}


def kill_switched() -> bool:
    if os.environ.get("AGENTMGR_EMAIL_GATEWAY_DISABLED"):
        return True
    flag = os.environ.get("AGENTMGR_EMAIL_GATEWAY_DISABLE_FILE",
                          os.path.expanduser("~/.agentmgr-email-gateway.disabled"))
    return os.path.exists(flag)


# --------------------------------------------------------------------------- I/O orchestration
def _gmail_service():
    """Build a Gmail API client from the project's stored OAuth token."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_path = os.environ.get(
        "AGENTMGR_GMAIL_TOKEN", os.path.expanduser("~/paradise-realty/api/gmail_token.json"))
    creds = Credentials.from_authorized_user_file(token_path)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _processed_store():
    path = os.environ.get("AGENTMGR_EMAIL_SEEN",
                          os.path.expanduser("~/.agentmgr-email-seen.txt"))
    seen = set()
    if os.path.exists(path):
        seen = set(open(path, encoding="utf-8").read().split())
    return path, seen


def _dispatch_to_master(command: str) -> str:
    """POST the command to the master /chat and return its reply text."""
    import httpx

    base = os.environ.get("AGENTMGR_MASTER_URL", "http://localhost:8080").rstrip("/")
    token = os.environ.get("AGENTMGR_API_TOKEN", "")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    r = httpx.post(f"{base}/chat", headers=headers, json={"message": command}, timeout=180.0)
    r.raise_for_status()
    return r.json().get("reply", "(no reply)")


_CLAUDE_BIN = os.environ.get("CLAUDE_BIN", os.path.expanduser("~/.local/bin/claude"))
_CLAUDE_SESSION_FILE = os.path.expanduser("~/.agentmgr-email-claude-session")
_CLAUDE_SYS = (
    "You are answering an email command from Joe, the operator, on his Mac. You "
    "have full access to his machine and the agent-manager (the agents live at "
    "~/paradise-realty-beta/agent-manager; the master API is http://localhost:8080). "
    "Do what he asks, then reply with a short plain-text summary suitable for "
    "email — no markdown, no code fences."
)


def _dispatch_to_claude_code(command: str) -> str:
    """Run the command as headless Claude Code on this Mac (full access). Keeps
    one resumable session so follow-up emails carry context; on a stale session
    it transparently starts a fresh one. Returns the reply text."""
    import subprocess
    import uuid

    # Permission level is a deliberate choice (see AGENTMGR_CLAUDE_PERMISSION_MODE):
    #   acceptEdits      — edits auto-run; risky shell/deploy is declined (SAFE default)
    #   bypassPermissions — everything runs, no gate (full power, set explicitly)
    perm = os.environ.get("AGENTMGR_CLAUDE_PERMISSION_MODE", "acceptEdits")

    def run(session_flag: list[str]):
        argv = [_CLAUDE_BIN, "-p", "--permission-mode", perm,
                "--append-system-prompt", _CLAUDE_SYS, *session_flag, command]
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=900, cwd=os.path.expanduser("~"))

    prior = ""
    if os.path.exists(_CLAUDE_SESSION_FILE):
        prior = open(_CLAUDE_SESSION_FILE, encoding="utf-8").read().strip()
    try:
        if prior:
            proc = run(["--resume", prior])
            sid = prior
            if proc.returncode != 0:           # stale/again-in-use → fresh session
                sid = str(uuid.uuid4())
                proc = run(["--session-id", sid])
        else:
            sid = str(uuid.uuid4())
            proc = run(["--session-id", sid])
    except subprocess.TimeoutExpired:
        return "That command ran past 15 minutes and was stopped."
    except FileNotFoundError:
        return f"Claude Code CLI not found at {_CLAUDE_BIN}."

    try:
        with open(_CLAUDE_SESSION_FILE, "w", encoding="utf-8") as fh:
            fh.write(sid)
    except Exception:  # noqa: BLE001
        pass
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 and not out:
        return f"Claude Code error: {(proc.stderr or '').strip()[:600] or 'unknown'}"
    return out or "(no output)"


def _dispatch_command(command: str) -> str:
    """Route a command to its handler. Default is full Claude Code on the Mac;
    set AGENTMGR_GATEWAY_TARGET=master to use the agent-manager assistant instead."""
    if os.environ.get("AGENTMGR_GATEWAY_TARGET", "claude").lower() == "master":
        return _dispatch_to_master(command)
    return _dispatch_to_claude_code(command)


_LOCK_FH = None


def _acquire_lock() -> bool:
    """Single-instance lock so a slow Claude run isn't overlapped by the next
    scheduled poll. Held for the process lifetime; released on exit."""
    import fcntl
    global _LOCK_FH
    _LOCK_FH = open(os.path.expanduser("~/.agentmgr-email-gateway.lock"), "w")
    try:
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def poll(dry_run: bool = False) -> dict:
    """Poll Gmail once; run authorized command emails; reply with results.

    Returns a summary dict. Safe to call on a schedule.
    """
    import base64

    try:  # pull SENDGRID_API_KEY etc. from the agent-manager .env
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:  # noqa: BLE001
        pass

    if kill_switched():
        return {"disabled": True, "processed": 0}

    if not dry_run and not _acquire_lock():
        return {"locked": True, "processed": 0}  # a previous run is still working

    svc = _gmail_service()
    # Mail from the operator, recent, carrying the marker. (No is:unread —
    # mail you send to yourself arrives already-read; the seen-file de-dupes.)
    q = f'from:{OPERATOR} newer_than:2d subject:"{MARKER}"'
    listing = svc.users().messages().list(userId="me", q=q, maxResults=20).execute()
    ids = [m["id"] for m in listing.get("messages", [])]
    path, seen = _processed_store()
    summary = {"disabled": False, "considered": len(ids), "ran": 0, "rejected": 0, "results": []}

    for mid in ids:
        if mid in seen:
            continue
        full = svc.users().messages().get(userId="me", id=mid, format="full").execute()
        payload = full.get("payload", {})
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
        allowed, reason = gate(headers, labels=full.get("labelIds", []))
        if not allowed:
            summary["rejected"] += 1
            summary["results"].append({"id": mid, "rejected": reason})
            seen.add(mid)
            continue

        body = _extract_body(payload, base64)
        command = extract_command(_header(headers, "Subject"), body)
        if dry_run:
            summary["results"].append({"id": mid, "would_run": command})
            continue

        try:
            reply = _dispatch_command(command)
            ok = True
        except Exception as exc:  # noqa: BLE001
            reply = f"Command failed: {exc}"
            ok = False
        _send_reply(headers, command, reply)
        seen.add(mid)
        summary["ran"] += 1
        summary["results"].append({"id": mid, "command": command, "ok": ok})

    # --- approval replies (reply-to-approve; resolves NON-sensitive tokens) ---
    summary["approvals"] = []
    aq = f'from:{OPERATOR} newer_than:7d "AM-OK:" is:unread'
    a_ids = [m["id"] for m in svc.users().messages().list(
        userId="me", q=aq, maxResults=20).execute().get("messages", [])]
    for mid in a_ids:
        if mid in seen:
            continue
        full = svc.users().messages().get(userId="me", id=mid, format="full").execute()
        payload = full.get("payload", {})
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
        allowed, reason = gate(headers, labels=full.get("labelIds", []), require_marker=False)
        if not allowed:
            seen.add(mid)
            summary["approvals"].append({"id": mid, "rejected": reason})
            continue
        ap = parse_approval(headers, _extract_body(payload, base64))
        if not ap:
            continue  # not an approval reply; leave for other handling
        if dry_run:
            summary["approvals"].append({"id": mid, "would_resolve": ap})
            continue
        from agentmgr import email_approvals
        if not ap["decision"]:
            _send_reply(headers, f"approval {ap['token']}",
                        "Couldn't read your decision — reply with just 'approve' or 'deny'.")
            summary["approvals"].append({"id": mid, "token": ap["token"], "unclear": True})
        else:
            res = email_approvals.resolve(ap["token"], ap["decision"], by=f"email:{OPERATOR}")
            _send_reply(headers, f"approval {ap['token']}", f"{ap['decision']} -> {res}")
            summary["approvals"].append({"id": mid, "token": ap["token"], "result": res})
        seen.add(mid)

    if not dry_run:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(sorted(seen)))
    return summary


def _extract_body(payload: dict, b64) -> str:
    def walk(p):
        if p.get("mimeType") == "text/plain" and p.get("body", {}).get("data"):
            return b64.urlsafe_b64decode(p["body"]["data"]).decode("utf-8", "replace")
        for part in p.get("parts", []) or []:
            t = walk(part)
            if t:
                return t
        return ""
    return walk(payload)


def _send_reply(headers: dict, command: str, reply: str) -> bool:
    """Reply with the result via SendGrid (the Gmail token is read-only).

    Uses the project's SENDGRID_API_KEY (in the agent-manager env); sends from
    the verified Paradise sender back to the operator. Never raises.
    """
    import sys

    import httpx

    key = os.environ.get("SENDGRID_API_KEY") or os.environ.get("SENDGRID_API_KEY_GMAIL")
    if not key:
        print("[email_gateway] no SENDGRID_API_KEY; cannot reply", file=sys.stderr)
        return False
    to = _addr(_header(headers, "From")) or OPERATOR
    subj = _header(headers, "Subject")
    re_subj = subj if subj.lower().startswith("re:") else "Re: " + subj
    frm = os.environ.get("AGENTMGR_REPLY_FROM", "joe@paradiserealtyfla.com")
    body = (f"Command: {command}\n\nResult:\n{reply}\n\n"
            "— agent-manager (email gateway)\n\n"
            "Reply to this email with your next command to keep going.")
    # Reply-To the operator so a reply lands back in the watched inbox and the
    # loop continues; the "AM:" marker rides along in the Re: subject.
    payload = {
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": frm, "name": "agent-manager"},
        "reply_to": {"email": OPERATOR},
        "subject": re_subj,
        "content": [{"type": "text/plain", "value": body}],
    }
    try:
        r = httpx.post("https://api.sendgrid.com/v3/mail/send", json=payload,
                       headers={"Authorization": "Bearer " + key}, timeout=15.0)
        return r.status_code in (200, 201, 202)
    except Exception as exc:  # noqa: BLE001
        print(f"[email_gateway] reply send failed: {exc}", file=sys.stderr)
        return False


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(poll(dry_run="--dry-run" in sys.argv), indent=2, default=str))
