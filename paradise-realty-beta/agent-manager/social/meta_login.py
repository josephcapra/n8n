"""Open a logged-in Meta Business session and capture a Graph API token.

Reuses the same Chromium profile as instagram_reader (one Meta login covers FB +
IG). A real window opens on the Mac; the operator logs in and generates a
System User token. This script watches the page and, when the token appears in
the generate-token field, writes it to a local file (never to logs/stdout) so it
can be moved into Secret Manager without pasting it anywhere.

    python -m social.meta_login
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE = Path.home() / ".agentmgr-social-browser"
STATE = Path.home() / ".agentmgr-social"
STATE.mkdir(parents=True, exist_ok=True)
SHOT = STATE / "meta_login.png"
TOKEN_FILE = STATE / "meta_token.txt"
START_URL = "https://business.facebook.com/settings/system-users"
TOKEN_RE = re.compile(r"EAA[A-Za-z0-9]{40,}")   # FB access tokens start with EAA
RUN_S = int(os.environ.get("META_LOGIN_RUN_S", "1500"))   # window stays open this long


def _logged_in(ctx) -> bool:
    cookies = ctx.cookies(["https://www.facebook.com", "https://business.facebook.com"])
    return any(c["name"] == "c_user" for c in cookies)


def main() -> int:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=False,
            viewport={"width": 1320, "height": 920},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(START_URL, wait_until="domcontentloaded")
        print("WINDOW OPEN — log into Facebook/Business if prompted.", flush=True)

        deadline = time.time() + RUN_S
        while time.time() < deadline:
            try:
                page.screenshot(path=str(SHOT))
            except Exception:
                pass
            print(f"[{int(deadline - time.time())}s] logged_in={_logged_in(ctx)} "
                  f"url={page.url[:70]}", flush=True)
            # Read only form-field values — page JS embeds OTHER tokens we must
            # not grab; the generated System User token shows in a readonly field.
            try:
                vals = page.eval_on_selector_all(
                    "input,textarea", "els => els.map(e => e.value || '')")
                for v in vals:
                    m = TOKEN_RE.search(v or "")
                    if m:
                        TOKEN_FILE.write_text(m.group(0))
                        print(f"TOKEN CAPTURED -> {TOKEN_FILE} (length {len(m.group(0))})",
                              flush=True)
                        ctx.close()
                        return 0
            except Exception:
                pass
            time.sleep(6)
        print("timed out without capturing a token", flush=True)
        ctx.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
