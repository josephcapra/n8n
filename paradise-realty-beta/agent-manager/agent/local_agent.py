"""The Mac local agent — outbound-only daemon that executes shell tasks.

Run it on your Mac with ``deploy/run-mac-agent.sh``. It loops:

  1. poll the state store for PENDING tasks addressed to the ``mac-shell`` agent,
  2. for each, GATE it — run only if a session window covers it, otherwise
     block on per-command approval; catastrophic commands always re-prompt,
  3. execute as the current user, with a hard timeout,
  4. write a typed result back and log the command + exit code.

It opens no inbound port. Stop it and nothing can run on your Mac.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from pathlib import Path

from agentmgr.approval_gate import ApprovalDenied, ApprovalGate, ApprovalNotProvisioned
from agentmgr.config import Config, load_config
from agentmgr.logging_utils import get_logger, set_correlation_id
from agentmgr.schemas import TaskResult, TaskSpec, TaskStatus
from agentmgr.session import SessionManager
from agentmgr.state_store import StateStore, make_state_store

log = get_logger("agentmgr.local_agent")

WORKER_NAME = "mac-shell"
_MAX_CAPTURE = 20_000  # trim very large stdout/stderr


def _run_command(command: str, cwd: str | None, timeout_s: float) -> dict:
    """Execute one shell command, capturing output. Never raises."""
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=cwd or None,
        )
        return {
            "command": command,
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-_MAX_CAPTURE:],
            "stderr": proc.stderr[-_MAX_CAPTURE:],
        }
    except subprocess.TimeoutExpired:
        return {
            "command": command,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"command timed out after {timeout_s}s",
        }


def _authorize(command: str, session_mgr: SessionManager, gate: ApprovalGate) -> None:
    """Block until the command is cleared to run, or raise on denial.

    Order matters: the always-confirm denylist is checked FIRST, so a
    catastrophic command re-prompts even with a session window open.
    """
    if session_mgr.requires_fresh_approval(command):
        log.info("command on always-confirm denylist — forcing fresh approval")
        gate.request_approval(
            "shell command (always-confirm)", {"command": command}
        )
        return

    grant = session_mgr.active_grant("shell")
    if grant is not None:
        log.info("authorized by active session window", extra={"session_id": grant.id})
        return

    log.info("no session window — blocking on per-command approval")
    gate.request_approval("shell command", {"command": command})


def process_task(
    task: TaskSpec,
    store: StateStore,
    session_mgr: SessionManager,
    gate: ApprovalGate,
    *,
    timeout_s: float,
) -> TaskResult:
    """Gate, execute, and record one shell task."""
    set_correlation_id(task.correlation_id)
    store.update_task_status(task.id, TaskStatus.RUNNING)
    command = str(task.payload.get("command", "")).strip()

    if not command:
        result = TaskResult(
            task_id=task.id,
            status=TaskStatus.FAILED,
            error="no 'command' in task payload",
            worker=WORKER_NAME,
        )
        store.put_task_result(result)
        return result

    try:
        _authorize(command, session_mgr, gate)
    except ApprovalDenied as exc:
        log.warning("command denied by operator", extra={"task_id": task.id})
        result = TaskResult(
            task_id=task.id,
            status=TaskStatus.FAILED,
            error=f"denied: {exc}",
            worker=WORKER_NAME,
        )
        store.put_task_result(result)
        return result
    except ApprovalNotProvisioned as exc:
        log.error("approval gate not provisioned — refusing to run")
        result = TaskResult(
            task_id=task.id,
            status=TaskStatus.FAILED,
            error=f"not provisioned: {exc}",
            worker=WORKER_NAME,
        )
        store.put_task_result(result)
        return result

    log.info("EXECUTING shell command", extra={"task_id": task.id, "command": command})
    output = _run_command(command, task.payload.get("cwd"), timeout_s)
    status = (
        TaskStatus.COMPLETED if output["exit_code"] == 0 else TaskStatus.FAILED
    )
    result = TaskResult(
        task_id=task.id,
        status=status,
        output=output,
        worker=WORKER_NAME,
        error=None if status == TaskStatus.COMPLETED else f"exit {output['exit_code']}",
    )
    store.put_task_result(result)
    log.info(
        "shell command finished",
        extra={"task_id": task.id, "exit_code": output["exit_code"]},
    )
    return result


def _assistant_shell_runner(
    session_mgr: SessionManager,
    gate: ApprovalGate,
    cfg: Config,
    *,
    cwd: str | None = None,
    timeout_s: float | None = None,
):
    """Build the shell_runner the agentic loop calls: classify, gate, execute.

    Read-only commands run immediately; everything else (and anything on the
    always-confirm denylist) blocks on the approval gate. Never raises — a
    denied command comes back as a ShellResult with gate='denied'. ``cwd`` and
    ``timeout_s`` scope where/how long commands run (the jazzysphotos-site
    agent runs its loop inside the site repo).
    """
    from agentmgr.assistant import ShellResult
    from agentmgr.command_policy import AUTO, classify_command

    def run(command: str) -> ShellResult:
        decision = classify_command(command)
        if decision == AUTO:
            gate_label = "auto"
        elif (
            not session_mgr.requires_fresh_approval(command)
            and session_mgr.active_grant("shell") is not None
        ):
            # An open session window (e.g. desktop password login) covers it —
            # run without prompting. Catastrophic commands fall through to the
            # gate below, since requires_fresh_approval() short-circuits this.
            gate_label = "session"
        else:
            try:
                gate.request_approval(
                    "assistant shell command", {"command": command}
                )
            except (ApprovalDenied, ApprovalNotProvisioned) as exc:
                return ShellResult(command=command, gate="denied", stderr=str(exc))
            gate_label = "approved"
        output = _run_command(command, cwd, timeout_s or cfg.shell_command_timeout_s)
        return ShellResult(
            command=command, stdout=output["stdout"], stderr=output["stderr"],
            exit_code=output["exit_code"], gate=gate_label,
        )

    return run


def process_assistant_task(
    task: TaskSpec,
    store: StateStore,
    session_mgr: SessionManager,
    gate: ApprovalGate,
    cfg: Config,
    *,
    driver=None,
) -> None:
    """Run an agentic-assistant goal — a Claude tool-use loop over the Mac shell."""
    from agentmgr.assistant import make_driver, run_agentic_loop, system_prompt

    set_correlation_id(task.correlation_id)
    store.update_task_status(task.id, TaskStatus.RUNNING)
    goal = str(task.payload.get("goal", "")).strip()
    if not goal:
        store.put_task_result(TaskResult(
            task_id=task.id, status=TaskStatus.FAILED,
            error="no 'goal' in task payload", worker="assistant"))
        return

    memory = str(task.payload.get("memory", "") or "")
    connectors = str(task.payload.get("connectors", "") or "")
    attachments = task.payload.get("attachments") or []
    log.info("assistant goal started",
             extra={"task_id": task.id, "goal": goal, "attachments": len(attachments)})
    runner = _assistant_shell_runner(session_mgr, gate, cfg)
    try:
        result = run_agentic_loop(
            driver or make_driver(cfg), goal,
            shell_runner=runner, max_steps=cfg.assistant_max_steps,
            system=system_prompt(memory, connectors),
            attachments=attachments,
        )
    except Exception as exc:  # noqa: BLE001 - recorded as a failed result
        log.exception("assistant loop failed", extra={"task_id": task.id})
        store.put_task_result(TaskResult(
            task_id=task.id, status=TaskStatus.FAILED,
            error=f"{type(exc).__name__}: {exc}", worker="assistant"))
        return

    store.put_task_result(TaskResult(
        task_id=task.id, status=TaskStatus.COMPLETED,
        output={"answer": result.answer, "transcript": result.transcript,
                "steps": result.steps, "hit_limit": result.hit_limit},
        worker="assistant"))
    log.info("assistant goal finished",
             extra={"task_id": task.id, "steps": result.steps})


def process_security_task(task: TaskSpec, store: StateStore, cfg: Config) -> None:
    """Run the security-health scan on this Mac and (optionally) email it."""
    from tools.security_health import run as run_security

    set_correlation_id(task.correlation_id)
    store.update_task_status(task.id, TaskStatus.RUNNING)
    send = bool(task.payload.get("send", True))
    to = task.payload.get("to")
    try:
        result = run_security(send=send, **({"to": to} if to else {}))
    except Exception as exc:  # noqa: BLE001
        log.exception("security scan failed", extra={"task_id": task.id})
        store.put_task_result(TaskResult(
            task_id=task.id, status=TaskStatus.FAILED,
            error=f"{type(exc).__name__}: {exc}", worker="security-health"))
        return
    store.put_task_result(TaskResult(
        task_id=task.id, status=TaskStatus.COMPLETED,
        output=result, worker="security-health"))
    log.info("security scan finished",
             extra={"task_id": task.id, "grade": result.get("grade")})


# Office-Lead CRM actions -> the office-leads Node script each one runs.
_CRM_ACTIONS = {
    "daily_report": ["office-leads/daily-report.js"],
    "report_only": ["office-leads/daily-report.js", "--no-email"],
    "verify_phantom_tasks": ["office-leads/actions/verify-phantom.js"],
    "clear_phantom_tasks": ["office-leads/actions/bulk-clear-2052.js"],
}
# Destructive actions may run ONLY via the SENSITIVE 'crm-task-cleanup' agent.
_CRM_DESTRUCTIVE = frozenset({"clear_phantom_tasks"})


def process_crm_task(task: TaskSpec, store: StateStore, cfg: Config) -> None:
    """Run an Office-Lead CRM action by shelling out to its Node script.

    The CRM tool lives in ``cfg.crm_project_dir`` (Node + the saved RealGeeks
    browser session). Sensitivity is enforced by the registry: benign actions
    come in as kind ``crm``; the destructive cleanup as kind ``crm_cleanup``,
    which the Master gates behind operator approval.
    """
    set_correlation_id(task.correlation_id)
    store.update_task_status(task.id, TaskStatus.RUNNING)
    worker = "crm-task-cleanup" if task.kind == "crm_cleanup" else "crm-office-leads"
    action = str(task.payload.get("action", "daily_report")).strip()

    if action not in _CRM_ACTIONS:
        store.put_task_result(TaskResult(
            task_id=task.id, status=TaskStatus.FAILED, worker=worker,
            error=f"unknown CRM action {action!r}; valid: {sorted(_CRM_ACTIONS)}"))
        return
    if action in _CRM_DESTRUCTIVE and task.kind != "crm_cleanup":
        store.put_task_result(TaskResult(
            task_id=task.id, status=TaskStatus.FAILED, worker=worker,
            error=f"action {action!r} is destructive — route it to the 'crm-task-cleanup' agent"))
        return

    command = "node " + " ".join(_CRM_ACTIONS[action])
    log.info("EXECUTING CRM action", extra={"task_id": task.id, "action": action})
    output = _run_command(command, cfg.crm_project_dir, cfg.crm_task_timeout_s)
    output["action"] = action
    status = TaskStatus.COMPLETED if output["exit_code"] == 0 else TaskStatus.FAILED
    store.put_task_result(TaskResult(
        task_id=task.id, status=status, output=output, worker=worker,
        error=None if status == TaskStatus.COMPLETED else f"exit {output['exit_code']}"))
    log.info("CRM action finished",
             extra={"task_id": task.id, "action": action, "exit_code": output["exit_code"]})


# --- jazzysphotos.com site agent -----------------------------------------

# Top-level single-line string fields in src/content/settings/site.ts that
# `update_copy` is allowed to set. Structured objects (services, awards,
# analytics) are deliberately excluded — edit those via the `goal` loop.
_JAZZY_COPY_FIELDS = frozenset({
    "tagline", "heroHeading", "heroSubtext", "intro", "name", "brand",
    "titleSuffix", "aboutHeading", "email", "instagramHandle",
    "instagramUrl", "location", "bookingUrl",
})
# Photo categories accepted by the portfolio content schema (content.config.ts).
_JAZZY_CATEGORIES = ("Seniors", "Prom", "Couples", "Portraits", "Other")
# Actions that commit + push to the LIVE site -> SENSITIVE, gated EACH call.
_JAZZY_PUBLISHING = frozenset({"update_copy", "add_photo", "remove_photo", "publish"})

_JAZZY_SYSTEM = (
    "You are the jazzysphotos.com site agent. Every shell command you run is "
    "already executed inside the site's git repository on the operator's Mac — "
    "an Astro photography portfolio. Do NOT touch files outside this repo.\n\n"
    "Layout: copy lives in src/content/settings/site.ts; portfolio photos are a "
    "Markdown file + image under src/content/portfolio[/images]; Instagram embeds "
    "in src/content/settings/instagram.json (refresh with `npm run instagram`).\n\n"
    "Validate any change with `npm run build` before publishing. To make a change "
    "LIVE you must commit and push to main (`git add … && git commit -m … && git "
    "push`) — that triggers a Cloudflare Pages deploy and the site is live in about "
    "90 seconds. Pushing is gated: the operator must approve it, and may deny. Work "
    "in small steps, prefer read-only commands, and when done reply in a couple of "
    "plain sentences describing what you changed and whether it was published."
)


def _jazzy_slugify(title: str) -> str:
    """Mirror the admin app's slug rule: lowercase, non-alnum -> '-', trim, cap."""
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80]
    return s or f"photo-{int(time.time())}"


def _jazzy_set_copy_field(src: str, field: str, value: str) -> str:
    """Return ``site.ts`` text with the first ``field: '<str>'`` value replaced.

    Matches a top-level ``field:`` followed by a single- or double-quoted string
    and swaps its contents (escaped for a single-quoted JS string). Raises
    ValueError if the field/string isn't found — callers fail without writing.
    """
    esc = (
        value.replace("\\", "\\\\").replace("'", "\\'").replace("\r", "")
        .replace("\n", " ").strip()
    )
    # group 1 = "field: ", group 2 = the opening quote; body is quote-aware.
    pattern = re.compile(
        r"(\b" + re.escape(field) + r"\s*:\s*)(['\"])(?:\\.|(?!\2).)*\2"
    )
    new, n = pattern.subn(lambda m: f"{m.group(1)}'{esc}'", src, count=1)
    if n == 0:
        raise ValueError(f"field {field!r} not found as a string in site.ts")
    return new


def _jazzy_frontmatter(
    image_name: str, alt: str, title: str, category: str,
    featured: bool, order: int,
) -> str:
    """Build a portfolio .md entry matching content.config.ts (JSON-quoted)."""
    return (
        "---\n"
        f"image: ./images/{image_name}\n"
        f"alt: {json.dumps(alt)}\n"
        f"title: {json.dumps(title)}\n"
        f"category: {json.dumps(category)}\n"
        f"featured: {'true' if featured else 'false'}\n"
        f"order: {int(order)}\n"
        "---\n"
    )


def _jazzy_tail(output: dict) -> str:
    """A short error tail from a failed shell step."""
    return (output.get("stderr") or output.get("stdout") or "").strip()[-500:]


def process_jazzysphotos_task(
    task: TaskSpec,
    store: StateStore,
    session_mgr: SessionManager,
    gate: ApprovalGate,
    cfg: Config,
    *,
    driver=None,
) -> None:
    """Run one jazzysphotos.com site action by editing the local Astro repo.

    Benign actions (status / build / refresh_instagram) run immediately.
    Publishing actions (update_copy / add_photo / remove_photo / publish) commit
    and push to the live site, so each blocks on the approval gate first; on
    denial nothing is changed. ``goal`` runs a Claude loop scoped to the repo.
    """
    set_correlation_id(task.correlation_id)
    store.update_task_status(task.id, TaskStatus.RUNNING)
    worker = "jazzysphotos-site"
    action = str(task.payload.get("action", "status")).strip()
    repo = cfg.jazzysphotos_dir
    timeout = cfg.jazzysphotos_timeout_s

    def fail(msg: str) -> None:
        store.put_task_result(TaskResult(
            task_id=task.id, status=TaskStatus.FAILED, worker=worker, error=msg))

    def done(output: dict) -> None:
        output.setdefault("action", action)
        store.put_task_result(TaskResult(
            task_id=task.id, status=TaskStatus.COMPLETED, worker=worker, output=output))

    log.info("jazzysphotos action", extra={"task_id": task.id, "action": action})

    if action == "goal":
        _process_jazzysphotos_goal(task, store, session_mgr, gate, cfg, driver=driver)
        return

    # --- benign: read / validate / stage (no push) -----------------------
    if action == "status":
        out = _run_command(
            "git rev-parse --abbrev-ref HEAD && git status --porcelain && "
            "echo '--- last commit ---' && git log -1 --pretty=format:'%h %s (%cr)'",
            repo, timeout)
        done({**out})
        return
    if action == "build":
        out = _run_command("npm run build", repo, timeout)
        if out["exit_code"] == 0:
            out["note"] = "Build OK — the site compiles. Nothing published."
            done({**out})
        else:
            fail(f"build failed: {_jazzy_tail(out)}")
        return
    if action == "refresh_instagram":
        out = _run_command("npm run instagram", repo, timeout)
        if out["exit_code"] != 0:
            fail(f"instagram refresh failed: {_jazzy_tail(out)}")
            return
        changes = _run_command("git status --porcelain", repo, 60)
        out["pending_changes"] = changes["stdout"].strip()
        out["note"] = ("Instagram images refreshed (not yet published). Use "
                       "Publish to push them live." if changes["stdout"].strip()
                       else "Instagram images already up to date.")
        done({**out})
        return

    # --- publishing: gate FIRST, then edit, validate, commit + push ------
    if action not in _JAZZY_PUBLISHING:
        fail(f"unknown site action {action!r}; valid: status, build, "
             f"refresh_instagram, {', '.join(sorted(_JAZZY_PUBLISHING))}, goal")
        return

    # Validate the action's own inputs BEFORE bothering the operator for approval.
    prepared = _jazzy_prepare(action, task.payload, repo)
    if prepared == "__nothing_to_publish__":  # publish with a clean tree
        done({"published": False, "note": "Nothing to publish — the working tree is clean."})
        return
    if isinstance(prepared, str):  # validation error message
        fail(prepared)
        return

    try:
        gate.request_approval(f"jazzysphotos: {action}", prepared["approval"])
    except ApprovalDenied as exc:
        fail(f"publish denied by operator: {exc}")
        return
    except ApprovalNotProvisioned as exc:
        fail(f"approval gate not provisioned: {exc}")
        return

    # Apply the local change, validate with a build, then commit + push.
    try:
        commit_paths = prepared["apply"]()  # mutate the working tree; returns paths
    except (OSError, ValueError) as exc:
        prepared["revert"]()
        fail(f"could not apply change: {exc}")
        return

    if prepared.get("build", True):
        b = _run_command("npm run build", repo, timeout)
        if b["exit_code"] != 0:
            prepared["revert"]()
            fail(f"build failed after edit — reverted, nothing published: "
                 f"{_jazzy_tail(b)}")
            return

    push = _jazzy_commit_push(repo, commit_paths, prepared["message"], timeout)
    if push["exit_code"] != 0:
        prepared["revert"]()
        fail(f"commit/push failed — reverted, nothing published: {_jazzy_tail(push)}")
        return
    done({**push, "published": True, "message": prepared["message"],
          "note": "Pushed to main — Cloudflare deploys the live site in ~90s."})


def _jazzy_prepare(action: str, payload: dict, repo: str):
    """Validate a publishing action and return its plan, or an error string.

    The plan is a dict with: ``approval`` (details shown to the operator),
    ``message`` (commit message), ``apply`` (callable that mutates the working
    tree and returns the paths to commit), ``revert`` (callable undoing apply),
    and optional ``build`` (default True).
    """
    portfolio = Path(repo) / "src" / "content" / "portfolio"

    if action == "update_copy":
        field = str(payload.get("field", "")).strip()
        value = str(payload.get("value", ""))
        if field not in _JAZZY_COPY_FIELDS:
            return (f"field {field!r} is not an editable copy field; "
                    f"allowed: {', '.join(sorted(_JAZZY_COPY_FIELDS))}")
        if not value.strip():
            return "value is empty — nothing to set"
        site_ts = Path(repo) / "src" / "content" / "settings" / "site.ts"
        rel = "src/content/settings/site.ts"

        def apply() -> list[str]:
            src = site_ts.read_text()
            site_ts.write_text(_jazzy_set_copy_field(src, field, value))
            return [rel]

        return {
            "approval": {"field": field, "value": value, "site": "jazzysphotos.com"},
            "message": f"Update {field} via agent-manager",
            "apply": apply,
            "revert": lambda: _run_command(f"git checkout -- {shlex.quote(rel)}", repo, 60),
        }

    if action == "add_photo":
        image_path = str(payload.get("image_path", "")).strip()
        title = str(payload.get("title", "")).strip()
        alt = str(payload.get("alt", "")).strip()
        category = str(payload.get("category", "")).strip()
        featured = bool(payload.get("featured", False))
        try:
            order = int(payload.get("order", 99))
        except (TypeError, ValueError):
            order = 99
        if not image_path or not Path(image_path).is_file():
            return f"image_path {image_path!r} does not exist on this Mac"
        if not title or not alt:
            return "add_photo needs a title and alt text"
        if category not in _JAZZY_CATEGORIES:
            return f"category must be one of {', '.join(_JAZZY_CATEGORIES)}"
        slug = _jazzy_slugify(title)
        image_name = f"{slug}.jpg"
        rel_img = f"src/content/portfolio/images/{image_name}"
        rel_md = f"src/content/portfolio/{slug}.md"
        dest_img = portfolio / "images" / image_name
        dest_md = portfolio / f"{slug}.md"

        def apply() -> list[str]:
            dest_img.parent.mkdir(parents=True, exist_ok=True)
            # Resize/re-encode to a web JPEG with macOS's built-in `sips`; if that
            # is unavailable, fall back to a straight copy.
            r = _run_command(
                f"sips -Z 2000 -s format jpeg {shlex.quote(image_path)} "
                f"--out {shlex.quote(str(dest_img))}", repo, 180)
            if r["exit_code"] != 0:
                _run_command(
                    f"cp {shlex.quote(image_path)} {shlex.quote(str(dest_img))}",
                    repo, 60)
            dest_md.write_text(_jazzy_frontmatter(
                image_name, alt, title, category, featured, order))
            return [rel_img, rel_md]

        def revert() -> None:
            for p in (dest_img, dest_md):
                try:
                    p.unlink()
                except OSError:
                    pass

        return {
            "approval": {"title": title, "category": category, "slug": slug,
                         "featured": featured, "site": "jazzysphotos.com"},
            "message": f"Add photo: {title}",
            "apply": apply,
            "revert": revert,
        }

    if action == "remove_photo":
        slug = re.sub(r"[^a-z0-9-]", "", str(payload.get("slug", "")).lower())
        if not slug:
            return "remove_photo needs a photo slug"
        md = portfolio / f"{slug}.md"
        if not md.is_file():
            return f"no photo named {slug!r} (looked for {slug}.md)"
        m = re.search(r"image:\s*\.?/?(?:images/)?([^\s'\"]+)", md.read_text())
        image_name = m.group(1) if m else ""
        rel_md = f"src/content/portfolio/{slug}.md"
        rel_paths = [rel_md]
        if image_name:
            rel_paths.append(f"src/content/portfolio/images/{image_name}")

        def apply() -> list[str]:
            for rel in rel_paths:
                try:
                    (Path(repo) / rel).unlink()
                except OSError:
                    pass
            return rel_paths

        return {
            "approval": {"slug": slug, "site": "jazzysphotos.com"},
            "message": f"Remove photo: {slug}",
            "apply": apply,
            "revert": lambda: _run_command(
                "git checkout -- " + " ".join(shlex.quote(p) for p in rel_paths),
                repo, 60),
        }

    # publish: commit + push whatever is already pending.
    message = str(payload.get("message", "")).strip() or "Publish site update via agent-manager"
    status = _run_command("git status --porcelain", repo, 60)
    if not status["stdout"].strip():
        return "__nothing_to_publish__"

    return {
        "approval": {"message": message, "pending": status["stdout"].strip()[:1000],
                     "site": "jazzysphotos.com"},
        "message": message,
        "apply": lambda: ["-A"],   # stage everything
        "revert": lambda: None,    # publish doesn't create changes to undo
        "build": True,
    }


def _jazzy_commit_push(repo: str, paths: list[str], message: str, timeout: float) -> dict:
    """git add <paths> && commit && push — returns the combined shell result."""
    add_target = " ".join(shlex.quote(p) for p in paths) if paths else "-A"
    cmd = (f"git add {add_target} && git commit -m {shlex.quote(message)} "
           f"&& git push")
    return _run_command(cmd, repo, timeout)


def _process_jazzysphotos_goal(
    task: TaskSpec, store: StateStore, session_mgr: SessionManager,
    gate: ApprovalGate, cfg: Config, *, driver=None,
) -> None:
    """Free-form site edit — a Claude tool-use loop scoped to the site repo."""
    from agentmgr.assistant import make_driver, run_agentic_loop

    goal = str(task.payload.get("goal", "")).strip()
    if not goal:
        store.put_task_result(TaskResult(
            task_id=task.id, status=TaskStatus.FAILED,
            error="no 'goal' in task payload", worker="jazzysphotos-site"))
        return
    runner = _assistant_shell_runner(
        session_mgr, gate, cfg,
        cwd=cfg.jazzysphotos_dir, timeout_s=cfg.jazzysphotos_timeout_s)
    try:
        result = run_agentic_loop(
            driver or make_driver(cfg), goal,
            shell_runner=runner, max_steps=cfg.assistant_max_steps,
            system=_JAZZY_SYSTEM,
        )
    except Exception as exc:  # noqa: BLE001 - recorded as a failed result
        log.exception("jazzysphotos goal failed", extra={"task_id": task.id})
        store.put_task_result(TaskResult(
            task_id=task.id, status=TaskStatus.FAILED,
            error=f"{type(exc).__name__}: {exc}", worker="jazzysphotos-site"))
        return
    store.put_task_result(TaskResult(
        task_id=task.id, status=TaskStatus.COMPLETED,
        output={"action": "goal", "answer": result.answer,
                "transcript": result.transcript, "steps": result.steps,
                "hit_limit": result.hit_limit},
        worker="jazzysphotos-site"))


def process_incentive_social_task(task: TaskSpec, store: StateStore) -> None:
    """Run the incentive-social pipeline locally — sheet scan -> drafts ->
    approval email. The Cloud Run job was never deployed; the pipeline lives in
    ~/incentive-social-agent and runs on the Mac via its worker adapter, which
    sets RUNNING and writes the TaskResult itself."""
    set_correlation_id(task.correlation_id)
    from worker.incentive_social import run_task
    try:
        run_task(task.id, store)
    except Exception as exc:  # noqa: BLE001 - keep the daemon alive; record failure
        log.exception("incentive-social run failed", extra={"task_id": task.id})
        store.put_task_result(TaskResult(
            task_id=task.id, status=TaskStatus.FAILED,
            error=f"{type(exc).__name__}: {exc}", worker="incentive-social"))


def run_agent(config: Config | None = None) -> None:
    """Main poll loop. Runs until interrupted (Ctrl-C)."""
    cfg = config or load_config()
    store = make_state_store(cfg)
    session_mgr = SessionManager(
        store, cfg.approval_public_key, cfg.always_confirm_patterns,
        rp_id=cfg.rp_id, origin=cfg.origin, api_token=cfg.api_token,
    )
    gate = ApprovalGate(
        store,
        public_key_b64=cfg.approval_public_key,
        channel=cfg.approval_channel,
        poll_interval_s=cfg.poll_interval_s,
        rp_id=cfg.rp_id,
        origin=cfg.origin,
    )
    # The Mac daemon serves the local-agent agents: direct shell ('mac-shell'),
    # the agentic assistant ('assistant'), the security-health scanner, the
    # Office-Lead CRM agent ('crm-office-leads' reports + 'crm-task-cleanup'),
    # and the jazzysphotos.com site agent ('jazzysphotos-site').
    handled = (cfg.local_agent_name, "assistant", "security-health",
               "crm-office-leads", "crm-task-cleanup", "jazzysphotos-site",
               "incentive-social")
    log.info(
        "Mac local agent started — polling (outbound only, no inbound port)",
        extra={"agents": list(handled), "poll_s": cfg.local_agent_poll_s},
    )
    while True:
        try:
            for agent_name in handled:
                for task in store.get_pending_tasks(agent_name):
                    if task.kind == "assistant":
                        process_assistant_task(task, store, session_mgr, gate, cfg)
                    elif task.kind == "security":
                        process_security_task(task, store, cfg)
                    elif task.kind in ("crm", "crm_cleanup"):
                        process_crm_task(task, store, cfg)
                    elif task.kind == "site":
                        process_jazzysphotos_task(
                            task, store, session_mgr, gate, cfg)
                    elif task.kind == "incentive_social":
                        process_incentive_social_task(task, store)
                    else:
                        process_task(
                            task, store, session_mgr, gate,
                            timeout_s=cfg.shell_command_timeout_s,
                        )
        except KeyboardInterrupt:
            log.info("Mac local agent stopped")
            return
        except Exception:  # noqa: BLE001 - keep the daemon alive
            log.exception("poll cycle error; continuing")
        time.sleep(cfg.local_agent_poll_s)


if __name__ == "__main__":
    try:
        run_agent()
    except KeyboardInterrupt:
        log.info("Mac local agent stopped")
