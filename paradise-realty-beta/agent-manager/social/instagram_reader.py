"""Instagram reader — reads YOUR logged-in account's activity and DMs.

Pilot for the "social-reader" worker. It drives a real, logged-in browser
session (a dedicated Chromium profile you sign into once), then reads
Instagram's own private web JSON endpoints — the same ones the website calls —
rather than scraping the obfuscated HTML, which breaks constantly.

It runs ONLY on your Mac (it needs your logged-in session); it can never run on
Cloud Run. Read-only: it fetches, it never posts, likes, or replies.

  First run:   a Chromium window opens — log into Instagram once. The session
               persists in the profile dir, so later runs skip the login.
  Every run:   fetches new comments on your recent posts and new direct
               messages, shows only what's new since last time, and writes a
               plain-English digest. (Instagram retired the combined
               notifications endpoint, so likes/follows/mentions aren't
               available via a stable call — comments and DMs are.)

Usage:
    python -m social.instagram_reader                 # read + digest
    python -m social.instagram_reader --headless      # no visible window (after first login)
    python -m social.instagram_reader --all           # show everything, not just new
    python -m social.instagram_reader --login          # just open for login, then exit

Compliance note: automating a logged-in social session is against Instagram's
ToS and can get the account rate-limited or locked. This is deliberately
read-only and human-paced. Run it a few times a day, not in a tight loop.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Instagram's public web app id — required header for its private JSON API.
_IG_APP_ID = "936619743392459"
_PROFILE_DIR = Path.home() / ".agentmgr-social-browser"
_STATE_DIR = Path.home() / ".agentmgr-social"
_SEEN_FILE = _STATE_DIR / "instagram_seen.json"
_DIGEST_FILE = _STATE_DIR / "instagram_digest.txt"
_RAW_FILE = _STATE_DIR / "instagram_last_raw.json"

_DM_URL = "/api/v1/direct_v2/inbox/?persistentBadging=true&limit=20"
_LOGIN_WAIT_S = 360  # how long to wait for a manual login on the first run


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def _load_seen() -> dict:
    if _SEEN_FILE.exists():
        try:
            return json.loads(_SEEN_FILE.read_text())
        except Exception:  # noqa: BLE001
            pass
    return {"activity": [], "messages": []}


def _save_seen(seen: dict) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    # keep the lists from growing without bound
    seen["activity"] = seen["activity"][-2000:]
    seen["messages"] = seen["messages"][-2000:]
    _SEEN_FILE.write_text(json.dumps(seen, indent=2))


def _ago(ts_seconds: float | None) -> str:
    if not ts_seconds:
        return ""
    delta = max(0, time.time() - ts_seconds)
    if delta < 90:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _ig_ts_to_seconds(value) -> float | None:
    """Instagram timestamps are microseconds since epoch (sometimes seconds)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v > 1e14:        # microseconds
        return v / 1_000_000
    if v > 1e11:        # milliseconds
        return v / 1000
    return v             # seconds


# --------------------------------------------------------------------------- #
# in-page fetch against Instagram's own JSON API (carries your session cookies)
# --------------------------------------------------------------------------- #

_FETCH_JS = """
async ({path, csrf}) => {
  const claim = window.sessionStorage.getItem('www-claim-v2') || '0';
  const headers = {
    'x-ig-app-id': '%s',
    'x-asbd-id': '129477',
    'x-requested-with': 'XMLHttpRequest',
    'x-ig-www-claim': claim,
  };
  if (csrf) headers['x-csrftoken'] = csrf;
  const r = await fetch(path, { headers, credentials: 'include' });
  return { status: r.status, body: await r.text() };
}
""" % _IG_APP_ID


def _cookie(ctx, name: str) -> str | None:
    for c in ctx.cookies():
        if c.get("name") == name and c.get("value"):
            return c["value"]
    return None


def _logged_in(ctx) -> bool:
    """Reliable, navigation-proof login check: Instagram sets the `ds_user_id`
    (and `sessionid`) cookie only once you're authenticated."""
    return bool(_cookie(ctx, "ds_user_id") or _cookie(ctx, "sessionid"))


def _api_get(page, ctx, path: str) -> tuple[int, dict | None]:
    """Fetch one JSON endpoint from inside the page, with the session cookies
    and the headers Instagram's web app sends. Tolerant of in-flight
    navigations — returns (0, None) instead of raising so callers can retry."""
    csrf = _cookie(ctx, "csrftoken")
    for _ in range(3):
        try:
            res = page.evaluate(_FETCH_JS, {"path": path, "csrf": csrf})
        except Exception:  # noqa: BLE001 - page navigating / context torn down
            time.sleep(1.5)
            continue
        status = int(res.get("status", 0))
        body = res.get("body") or ""
        try:
            return status, json.loads(body)
        except Exception:  # noqa: BLE001
            return status, None
    return 0, None


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

def _fetch_activity(page, ctx, max_posts: int = 8) -> tuple[list[dict], int]:
    """Activity = comments on your recent posts. (Instagram retired the combined
    notifications endpoint, so likes/follows/mentions aren't available via a
    stable call; comments are, and they're the part that matters.)"""
    uid = _cookie(ctx, "ds_user_id")
    if not uid:
        return [], 0
    status, feed = _api_get(page, ctx, f"/api/v1/feed/user/{uid}/?count={max_posts}")
    if status != 200 or not isinstance(feed, dict):
        return [], status
    out: list[dict] = []
    for item in (feed.get("items", []) or [])[:max_posts]:
        if (item.get("comment_count") or 0) <= 0:
            continue
        media_id = item.get("pk") or item.get("id")
        caption = ((item.get("caption") or {}).get("text") or "").strip()
        cap_short = (caption[:40] + "…") if len(caption) > 40 else caption
        cstatus, cdata = _api_get(
            page, ctx,
            f"/api/v1/media/{media_id}/comments/"
            "?can_support_threading=true&permalink_enabled=false",
        )
        if cstatus != 200 or not isinstance(cdata, dict):
            continue
        for c in (cdata.get("comments") or []):
            uname = (c.get("user") or {}).get("username", "?")
            if uname == "joecaprarealtor":   # skip your own replies
                continue
            text = (c.get("text") or "").strip()
            ts = _ig_ts_to_seconds(c.get("created_at"))
            cid = str(c.get("pk") or f"{uname}|{text}|{ts}")
            out.append({
                "id": cid,
                "text": f'@{uname} commented "{text}" on "{cap_short}"',
                "ts": ts,
                "when": _ago(ts),
            })
        time.sleep(0.6)  # human-paced between posts
    return out, 200


def _parse_messages(data: dict) -> list[dict]:
    """Flatten the DM inbox into one entry per thread (last message preview)."""
    out: list[dict] = []
    threads = (data.get("inbox", {}) or {}).get("threads", []) or []
    for th in threads:
        users = th.get("users", []) or []
        names = ", ".join("@" + (u.get("username") or "?") for u in users) or \
            (th.get("thread_title") or "(unknown)")
        items = th.get("items", []) or []
        last = items[0] if items else {}
        itype = last.get("item_type")
        if itype == "text":
            preview = (last.get("text") or "").strip()
        elif itype:
            preview = f"[{itype.replace('_', ' ')}]"
        else:
            preview = ""
        ts = _ig_ts_to_seconds(last.get("timestamp"))
        item_id = str(last.get("item_id") or th.get("thread_id") or names)
        # Prefer Instagram's own read_state (0 = read, 1 = unread); fall back to
        # comparing the last message against your last-seen marker.
        if "read_state" in th:
            unread = th.get("read_state") == 1
        else:
            last_seen = th.get("last_seen_at", {}) or {}
            seen_ts = max(
                (_ig_ts_to_seconds(v.get("timestamp")) or 0 for v in last_seen.values()),
                default=0,
            )
            unread = bool(ts and seen_ts and ts > seen_ts)
        out.append({
            "id": item_id,
            "from": names,
            "text": preview,
            "ts": ts,
            "when": _ago(ts),
            "unread": unread,
        })
    return out


# --------------------------------------------------------------------------- #
# digest
# --------------------------------------------------------------------------- #

def _build_digest(activity: list[dict], messages: list[dict], show_all: bool) -> str:
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    lines = [f"Instagram digest for @joecaprarealtor — {now}", ""]

    new_act = [a for a in activity if show_all or a.get("_new")]
    if new_act:
        lines.append(f"Activity ({len(new_act)} new):")
        for a in sorted(new_act, key=lambda x: x.get("ts") or 0, reverse=True)[:30]:
            when = f" ({a['when']})" if a["when"] else ""
            lines.append(f"  - {a['text']}{when}")
    else:
        lines.append("Activity: nothing new.")
    lines.append("")

    new_msgs = [m for m in messages if show_all or m.get("_new") or m.get("unread")]
    if new_msgs:
        lines.append(f"Messages ({len(new_msgs)}):")
        for m in sorted(new_msgs, key=lambda x: x.get("ts") or 0, reverse=True)[:30]:
            flag = "* " if m.get("unread") else "  "
            when = f" ({m['when']})" if m["when"] else ""
            preview = m["text"] or "(no preview)"
            lines.append(f"{flag}{m['from']}: {preview}{when}")
        lines.append("")
        lines.append("  (* = unread)")
    else:
        lines.append("Messages: nothing new.")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def run(headless: bool, show_all: bool, login_only: bool) -> int:
    from playwright.sync_api import sync_playwright

    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(_PROFILE_DIR),
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
            time.sleep(3)

            if not _logged_in(ctx):
                if headless:
                    print(
                        "Not logged in, and running headless. Run once without "
                        "--headless to sign in:\n  python -m social.instagram_reader --login",
                        file=sys.stderr,
                    )
                    return 2
                print("A Chromium window is open — log into Instagram as "
                      "@joecaprarealtor. Waiting up to "
                      f"{_LOGIN_WAIT_S}s for you to finish…", flush=True)
                deadline = time.time() + _LOGIN_WAIT_S
                while time.time() < deadline:
                    if _logged_in(ctx):
                        break
                    remaining = int(deadline - time.time())
                    if remaining % 30 == 0:
                        print(f"  …still waiting for login ({remaining}s left)",
                              flush=True)
                    time.sleep(3)
                else:
                    print("Timed out waiting for login. Re-run when ready.",
                          file=sys.stderr)
                    return 2
                print("Logged in. Reading your activity…", flush=True)
                time.sleep(2)  # let the session settle before the API calls

            if login_only:
                print("Login confirmed and saved. You can run the reader now.")
                return 0

            activity, act_status = _fetch_activity(page, ctx)
            dm_status, dms = _api_get(page, ctx, _DM_URL)
        finally:
            ctx.close()

    messages = _parse_messages(dms) if dms else []
    _RAW_FILE.write_text(
        json.dumps({"activity": activity, "dms": dms}, indent=2)[:500_000]
    )

    # mark what's new vs. the last run
    seen = _load_seen()
    seen_act, seen_msg = set(seen["activity"]), set(seen["messages"])
    for a in activity:
        a["_new"] = a["id"] not in seen_act
    for m in messages:
        m["_new"] = m["id"] not in seen_msg
    first_run = not seen_act and not seen_msg

    digest = _build_digest(activity, messages, show_all or first_run)
    _DIGEST_FILE.write_text(digest)
    print("\n" + digest + "\n")

    seen["activity"] = list(seen_act | {a["id"] for a in activity})
    seen["messages"] = list(seen_msg | {m["id"] for m in messages})
    _save_seen(seen)

    if act_status != 200 or dm_status != 200:
        print(f"(note: comments fetch HTTP {act_status}, messages HTTP {dm_status} "
              "— if these aren't 200, the session may have expired; re-run "
              "without --headless to sign in again)", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Read your Instagram activity + DMs.")
    ap.add_argument("--headless", action="store_true",
                    help="run without a visible window (only after first login)")
    ap.add_argument("--all", action="store_true",
                    help="show everything, not just what's new since last run")
    ap.add_argument("--login", action="store_true",
                    help="just open a window to log in, then exit")
    args = ap.parse_args()
    return run(headless=args.headless, show_all=args.all, login_only=args.login)


if __name__ == "__main__":
    raise SystemExit(main())
