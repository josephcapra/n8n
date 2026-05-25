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
import os
import subprocess
import threading
import time
from pathlib import Path

from agentmgr.approval_gate import ApprovalDenied, ApprovalGate, ApprovalNotProvisioned
from agentmgr.config import Config, load_config
from agentmgr.logging_utils import get_logger, set_correlation_id
from agentmgr.schemas import Message, TaskResult, TaskSpec, TaskStatus
from agentmgr.session import SessionManager
from agentmgr.state_store import StateStore, make_state_store

log = get_logger("agentmgr.local_agent")

WORKER_NAME = "mac-shell"
_MAX_CAPTURE = 20_000  # trim very large stdout/stderr


# Run through the operator's LOGIN shell so commands see the same PATH and
# environment as their real terminal (e.g. ~/.local/bin/claude, Homebrew).
# A bare `shell=True` uses /bin/sh -c, which skips the profile and only sees the
# daemon's minimal PATH — so user-installed tools come back "command not found".
_LOGIN_SHELL = os.environ.get("SHELL") or "/bin/zsh"


def _run_command(command: str, cwd: str | None, timeout_s: float) -> dict:
    """Execute one shell command, capturing output. Never raises."""
    try:
        proc = subprocess.run(
            [_LOGIN_SHELL, "-lc", command],
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


def _run_command_streaming(
    command: str, cwd: str | None, timeout_s: float, store, task_id: str
) -> dict:
    """Like ``_run_command`` but publishes stdout to the store line-by-line so a
    polling UI sees the work as it happens (the in-app Claude Code console). The
    in-app console runs ``claude -p --output-format stream-json``, whose NDJSON
    events the frontend renders as progress. stderr is merged into stdout so
    errors appear inline, terminal-style. Never raises.
    """
    try:
        proc = subprocess.Popen(
            [_LOGIN_SHELL, "-lc", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # interleave like a real terminal
            text=True,
            bufsize=1,                  # line-buffered
            cwd=cwd or None,
        )
    except Exception as exc:            # spawn failure (bad cwd, shell missing…)
        msg = f"failed to start: {exc}"
        store.append_task_output(task_id, msg)
        return {"command": command, "exit_code": -1, "stdout": msg, "stderr": ""}

    timed_out = {"v": False}

    def _kill() -> None:
        timed_out["v"] = True
        proc.kill()

    killer = threading.Timer(timeout_s, _kill)
    killer.start()
    chunks: list[str] = []
    try:
        assert proc.stdout is not None
        # readline() returns each line as soon as it's flushed; iterating the
        # file object instead (`for line in proc.stdout`) read-ahead-buffers and
        # would withhold output until EOF — defeating the live stream.
        for line in iter(proc.stdout.readline, ""):
            chunks.append(line)
            store.append_task_output(task_id, line)
        proc.wait()
    finally:
        killer.cancel()

    full = "".join(chunks)[-_MAX_CAPTURE:]
    if timed_out["v"]:
        note = f"\n[timed out after {timeout_s}s]"
        store.append_task_output(task_id, note)
        return {"command": command, "exit_code": -1, "stdout": full + note, "stderr": ""}
    return {
        "command": command,
        "exit_code": proc.returncode if proc.returncode is not None else -1,
        "stdout": full,
        "stderr": "",
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
    if task.payload.get("stream"):
        output = _run_command_streaming(
            command, task.payload.get("cwd"), timeout_s, store, task.id
        )
    else:
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
    from agentmgr.assistant import (
        classify_query, make_driver, run_agentic_loop, select_model, system_prompt,
    )

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
    history = str(task.payload.get("history", "") or "")
    agent_focus = str(task.payload.get("agent_focus", "") or "")
    attachments = task.payload.get("attachments") or []
    chosen = (task.payload.get("model") or "").strip()
    explicit_model = chosen if chosen and chosen != "auto" else ""
    log.info("assistant goal started",
             extra={"task_id": task.id, "goal": goal, "attachments": len(attachments),
                    "model": explicit_model or "auto",
                    "agent_focus": bool(agent_focus)})
    runner = _assistant_shell_runner(session_mgr, gate, cfg)
    sys_prompt = system_prompt(memory, connectors, history, agent_focus)

    # Cheap fast-path: trivial conversational / recall queries that need no Mac
    # tools are answered by a low-cost one-shot model (e.g. Gemini Flash) instead
    # of the Claude agentic loop — keeps everyday chatter off the Anthropic bill.
    if (not explicit_model and classify_query(goal) == "chat" and not attachments
            and cfg.google_api_key and cfg.google_model):
        try:
            from agentmgr.llm import GoogleProvider, LLMRequest
            resp = GoogleProvider(cfg).complete(
                LLMRequest(prompt=goal, system=sys_prompt, max_tokens=1024))
            if resp.text.strip():
                store.record_llm_cost(task.correlation_id, resp.cost_usd)
                store.put_task_result(TaskResult(
                    task_id=task.id, status=TaskStatus.COMPLETED,
                    output={"answer": resp.text.strip(), "transcript": [],
                            "steps": 0, "model": resp.model, "provider": resp.provider},
                    worker="assistant"))
                log.info("assistant chat fast-path",
                         extra={"task_id": task.id, "model": resp.model,
                                "cost_usd": round(resp.cost_usd, 6)})
                return
        except Exception:  # noqa: BLE001 - any issue → fall back to Claude
            log.warning("cheap fast-path failed; using Claude",
                        extra={"task_id": task.id})

    model = explicit_model or select_model(goal)
    log.info("assistant model", extra={"task_id": task.id, "model": model})
    try:
        result = run_agentic_loop(
            driver or make_driver(cfg, model=model), goal,
            shell_runner=runner, max_steps=cfg.assistant_max_steps,
            system=sys_prompt,
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


_LEAD_SCAN = "/Users/User/paradise-crm-audit/office-leads/lead-response-scan.js"


def process_lead_task(task: TaskSpec, store: StateStore, cfg: Config) -> None:
    """Scan NEW RealGeeks leads, score + draft a first-response for each (drafts
    only — never sends). Shells out to the Node scanner that reuses the saved RG
    browser session."""
    set_correlation_id(task.correlation_id)
    store.update_task_status(task.id, TaskStatus.RUNNING)
    limit = str(task.payload.get("limit", 10))
    try:
        proc = subprocess.run(
            ["node", _LEAD_SCAN, "--limit", limit],
            capture_output=True, text=True, timeout=300,
            cwd=str(Path(_LEAD_SCAN).parent),
        )
        line = (proc.stdout.strip().splitlines() or ["{}"])[-1] if proc.stdout.strip() else "{}"
        # the scanner prints a JSON blob (possibly multi-line) — parse the whole stdout
        try:
            data = json.loads(proc.stdout.strip())
        except Exception:
            data = json.loads(line)
    except Exception as exc:  # noqa: BLE001
        store.put_task_result(TaskResult(
            task_id=task.id, status=TaskStatus.FAILED,
            error=f"lead scan failed: {str(exc)[:160]}", worker="lead-response"))
        return

    if data.get("error") == "AUTH_REQUIRED":
        store.put_task_result(TaskResult(
            task_id=task.id, status=TaskStatus.FAILED,
            error="RealGeeks session expired — run office-leads/relogin.js", worker="lead-response"))
        return

    leads = data.get("leads", [])
    if not leads:
        answer = "No new leads to respond to right now."
    else:
        parts = [f"{len(leads)} new lead(s) — drafted replies (review before sending):"]
        for L in leads:
            contact = " · ".join(x for x in (L.get("email"), L.get("phone")) if x)
            parts.append(f"\n• {L.get('name','(no name)')}  [{L.get('score','?')}]  {contact}\n  ↳ {L.get('draft','')}")
        answer = "\n".join(parts)
    store.put_task_result(TaskResult(
        task_id=task.id, status=TaskStatus.COMPLETED,
        output={"answer": answer, "count": len(leads), "leads": leads},
        worker="lead-response"))
    log.info("lead scan done", extra={"task_id": task.id, "count": len(leads)})


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


# --- Taylor: marketing / weekly per-agent reports -------------------------

# Taylor actions -> the agent-reports.js invocation each one runs.
_TAYLOR_ACTIONS = {
    "weekly_send": ["office-leads/agent-reports.js", "--to-agents"],
    "review": ["office-leads/agent-reports.js", "--send-individual"],
    "preview": ["office-leads/agent-reports.js"],
}


def process_taylor_task(task: TaskSpec, store: StateStore, cfg: Config) -> None:
    """Run a Taylor marketing/report action by shelling to agent-reports.js.

    'weekly_send' does a full live pull and emails EACH agent THEIR personalized
    report (CC the broker). 'review' emails all reports to the broker only.
    'preview' just builds the report files. Reuses the CRM project dir + the
    saved RealGeeks browser session.
    """
    set_correlation_id(task.correlation_id)
    store.update_task_status(task.id, TaskStatus.RUNNING)
    worker = "taylor"
    action = str(task.payload.get("action", "weekly_send")).strip()
    if action not in _TAYLOR_ACTIONS:
        store.put_task_result(TaskResult(
            task_id=task.id, status=TaskStatus.FAILED, worker=worker,
            error=f"unknown Taylor action {action!r}; valid: {sorted(_TAYLOR_ACTIONS)}"))
        return
    cmd_args = list(_TAYLOR_ACTIONS[action])
    # If Joe's CRM Report relayed office metrics, stage them + reuse the roster
    # pull Joe just made (so Taylor doesn't re-pull the office).
    office_metrics = task.payload.get("office_metrics")
    if office_metrics:
        import json as _json
        mpath = os.path.join(cfg.crm_project_dir, "office-leads", ".office-metrics.json")
        try:
            with open(mpath, "w") as fh:
                _json.dump(office_metrics, fh)
            cmd_args += ["--office-metrics=office-leads/.office-metrics.json", "--cache", "--session"]
            log.info("Taylor consuming office metrics relayed from Joe's CRM Report",
                     extra={"task_id": task.id})
        except Exception as e:  # noqa: BLE001
            log.warning("Taylor could not stage relayed office metrics: %s", e)
    command = "node " + " ".join(cmd_args)
    log.info("EXECUTING Taylor action", extra={"task_id": task.id, "action": action})
    output = _run_command(command, cfg.crm_project_dir, cfg.crm_task_timeout_s)
    output["action"] = action
    status = TaskStatus.COMPLETED if output["exit_code"] == 0 else TaskStatus.FAILED
    store.put_task_result(TaskResult(
        task_id=task.id, status=status, output=output, worker=worker,
        error=None if status == TaskStatus.COMPLETED else f"exit {output['exit_code']}"))
    log.info("Taylor action finished",
             extra={"task_id": task.id, "action": action, "exit_code": output["exit_code"]})


# --- Joe's CRM Report: office-WIDE operations report (all officelead leads) ---

_JOE_CRM_ACTIONS = {
    "email_report": ["office-leads/daily-report.js"],
    "metrics": ["office-leads/daily-report.js", "--no-email"],
}


def process_joe_crm_task(task: TaskSpec, store: StateStore, cfg: Config) -> None:
    """Joe's CRM Report — office-WIDE RealGeeks operations report (ALL officelead
    leads, not split by agent).

    'email_report' (default — what a GUI click runs) builds + emails the
    office-wide report to Joe. 'metrics' builds it without emailing and returns
    the structured office snapshot JSON in the result output, so other agents
    (e.g. Taylor) can pull office-wide CRM numbers through the Master.
    """
    set_correlation_id(task.correlation_id)
    store.update_task_status(task.id, TaskStatus.RUNNING)
    worker = "joe-crm-report"
    action = str(task.payload.get("action", "email_report")).strip()
    if action not in _JOE_CRM_ACTIONS:
        store.put_task_result(TaskResult(
            task_id=task.id, status=TaskStatus.FAILED, worker=worker,
            error=f"unknown action {action!r}; valid: {sorted(_JOE_CRM_ACTIONS)}"))
        return
    command = "node " + " ".join(_JOE_CRM_ACTIONS[action])
    log.info("EXECUTING Joe CRM action", extra={"task_id": task.id, "action": action})
    output = _run_command(command, cfg.crm_project_dir, cfg.crm_task_timeout_s)
    output["action"] = action
    # For 'metrics', return a COMPACT office-wide summary (headline numbers + a
    # one-line text) so other agents can consume it cleanly through the Master —
    # not the whole snapshot.
    if action == "metrics" and output.get("exit_code") == 0:
        try:
            import glob
            import json as _json
            reports = os.path.join(cfg.crm_project_dir, "office-leads", "reports")
            snaps = sorted(glob.glob(os.path.join(reports, "office-snapshot-*.json")))
            if snaps:
                snap = _json.loads(open(snaps[-1]).read())
                s = snap.get("summary", {}) or {}
                b = snap.get("buckets", {}) or {}
                backlog = s.get("backlog", {}) or {}
                ln = lambda k: len(b.get(k) or [])
                summary = {
                    "generatedAt": snap.get("generatedAt"),
                    "totalOfficeLeads": s.get("totalOfficeLeads"),
                    "needAttention": s.get("needAttention"),
                    "newToday": s.get("newToday"),
                    "activeToday": s.get("activeToday"),
                    "unassigned": s.get("unassigned"),
                    "awaiting": ln("awaiting"),
                    "newUntouched": ln("newUntouched"),
                    "overdue": ln("overdueFollowups"),
                    "cold": backlog.get("cold", 0),
                }
                summary["text"] = (
                    f"Office-wide: {summary['awaiting']} awaiting reply, "
                    f"{summary['newUntouched']} new & untouched, {summary['overdue']} overdue, "
                    f"{summary['cold']} cold of {summary['totalOfficeLeads']} office leads; "
                    f"{summary['needAttention']} need attention today."
                )
                output["summary"] = summary
                # drop the bulky stdout so callers get the compact summary
                output.pop("stdout", None)
                # Optional relay THROUGH the Master to another agent (e.g. Taylor),
                # carrying the compact office metrics so it doesn't recompute them.
                relay_to = task.payload.get("relay_to")
                if relay_to:
                    store.put_message(Message(
                        correlation_id=task.correlation_id,
                        from_agent="joe-crm-report", to_agent=relay_to,
                        payload={"action": task.payload.get("relay_action", "weekly_send"),
                                 "office_metrics": summary},
                        relay_depth=task.depth + 1,
                    ))
                    output["relayed_to"] = relay_to
                    log.info("Joe CRM relayed office metrics", extra={"to": relay_to})
        except Exception as e:  # noqa: BLE001
            output["summary_error"] = str(e)[:120]
    status = TaskStatus.COMPLETED if output["exit_code"] == 0 else TaskStatus.FAILED
    store.put_task_result(TaskResult(
        task_id=task.id, status=status, output=output, worker=worker,
        error=None if status == TaskStatus.COMPLETED else f"exit {output['exit_code']}"))
    log.info("Joe CRM action finished",
             extra={"task_id": task.id, "action": action, "exit_code": output["exit_code"]})


# --- Scout: agent R&D / continuous improvement (advisory) -----------------

_SCOUT_ACTIONS = {
    "report": ["office-leads/agent-scout.js", "--report"],
    "research": ["office-leads/agent-scout.js", "--no-email"],
}


def process_scout_task(task: TaskSpec, store: StateStore, cfg: Config) -> None:
    """Scout — reviews all agents + logs and proposes improvements (advisory).

    'report' researches and EMAILS the R&D report to the operator; 'research'
    researches and appends to LEARNINGS.md without emailing. Never edits other
    agents — it only proposes.
    """
    set_correlation_id(task.correlation_id)
    store.update_task_status(task.id, TaskStatus.RUNNING)
    worker = "scout"
    action = str(task.payload.get("action", "report")).strip()
    if action not in _SCOUT_ACTIONS:
        store.put_task_result(TaskResult(
            task_id=task.id, status=TaskStatus.FAILED, worker=worker,
            error=f"unknown action {action!r}; valid: {sorted(_SCOUT_ACTIONS)}"))
        return
    command = "node " + " ".join(_SCOUT_ACTIONS[action])
    log.info("EXECUTING Scout action", extra={"task_id": task.id, "action": action})
    output = _run_command(command, cfg.crm_project_dir, cfg.crm_task_timeout_s)
    output["action"] = action
    status = TaskStatus.COMPLETED if output["exit_code"] == 0 else TaskStatus.FAILED
    store.put_task_result(TaskResult(
        task_id=task.id, status=status, output=output, worker=worker,
        error=None if status == TaskStatus.COMPLETED else f"exit {output['exit_code']}"))
    log.info("Scout action finished",
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
    "You are Jasmine's assistant for jazzysphotos.com — her Astro photography "
    "portfolio. Every shell command you run is executed inside the site's git "
    "repository, INSIDE A SANDBOX: you can only write within this repo, and the "
    "operator's credentials and other projects are invisible to you. You have NO "
    "access to anything unrelated to this website — don't try to reach it; such "
    "commands are refused. Work only on the site.\n\n"
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


# macOS temp dirs the toolchain (npm/astro/git) needs to write to.
_JAZZY_TMP_WRITABLE = (
    "/private/tmp", "/private/var/folders", "/private/var/tmp", "/tmp",
)


def _jazzy_sandbox_profile(repo: str) -> str:
    """A macOS sandbox-exec (SBPL) profile that confines a command to Jasmine's
    website: writes only inside the repo (+ temp), and the operator's secret
    stores are unreadable. ``(allow default)`` keeps the build/publish toolchain
    working; the explicit denies below are what's enforced (last match wins)."""
    repo = os.path.realpath(repo)
    home = os.path.expanduser("~")
    writable = [repo, *(_JAZZY_TMP_WRITABLE)]
    write_rules = "\n  ".join(f'(subpath "{p}")' for p in writable)
    # Secret stores + the whole Paradise/agent-manager tree are hidden from reads
    # so the bot can never see credentials or other projects.
    secret_paths = [
        os.path.join(home, ".ssh"),
        os.path.join(home, ".aws"),
        os.path.join(home, ".config", "gcloud"),
        os.path.join(home, ".config", "gh"),
        os.path.join(home, ".gnupg"),
        os.path.join(home, ".npmrc"),
        os.path.join(home, ".sitemap-sync-browser"),
        os.path.join(home, ".kolter-browser"),
        "/Users/User/paradise-realty-beta",
        "/Users/User/paradise-crm-audit",
        "/Users/User/incentive-social-agent",
    ]
    read_denies = "\n  ".join(f'(subpath "{p}")' for p in secret_paths)
    return (
        "(version 1)\n"
        "(allow default)\n"
        "(deny file-write*)\n"
        f"(allow file-write*\n  {write_rules}\n"
        '  (literal "/dev/null") (literal "/dev/zero")\n'
        '  (regex #"^/dev/tty") (regex #"^/dev/fd/"))\n'
        f"(deny file-read*\n  {read_denies})\n"
    )


def _jazzy_clean_env(repo: str) -> dict:
    """A minimal environment for the website bot — every connector secret the
    daemon holds (ANTHROPIC/REALGEEKS/BING/SENDGRID/GCS/…) is dropped. Her site's
    build + instagram refresh need no tokens, so nothing sensitive is required."""
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": os.path.expanduser("~"),               # git uses ~/.gitconfig + keychain
        "USER": os.environ.get("USER", ""),
        "LOGNAME": os.environ.get("LOGNAME", ""),
        "SHELL": "/bin/zsh",
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        # npm cache lives in temp (sandbox-writable) so her repo stays clean.
        "npm_config_cache": "/tmp/agentmgr-jazzy-npm-cache",
        "GIT_TERMINAL_PROMPT": "0",                    # never hang on a credential prompt
        "HOMEBREW_NO_AUTO_UPDATE": "1",
    }


_SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def _run_command_confined(command: str, repo: str, timeout_s: float) -> dict:
    """Run one command under sandbox-exec, scoped to the site repo, with a
    scrubbed env. Fails closed: if sandbox-exec is missing, nothing runs."""
    if not os.path.exists(_SANDBOX_EXEC):
        return {"command": command, "exit_code": -1, "stdout": "",
                "stderr": "sandbox unavailable — refusing to run unsandboxed"}
    repo = os.path.realpath(repo)
    os.makedirs("/tmp/agentmgr-jazzy-npm-cache", exist_ok=True)
    profile = _jazzy_sandbox_profile(repo)
    # zsh -f -c: no startup files (.zshenv/.zshrc) so the scrubbed env stands.
    argv = [_SANDBOX_EXEC, "-p", profile, "/bin/zsh", "-f", "-c", command]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s,
            cwd=repo, env=_jazzy_clean_env(repo),
        )
        return {"command": command, "exit_code": proc.returncode,
                "stdout": proc.stdout[-_MAX_CAPTURE:], "stderr": proc.stderr[-_MAX_CAPTURE:]}
    except subprocess.TimeoutExpired:
        return {"command": command, "exit_code": -1, "stdout": "",
                "stderr": f"command timed out after {timeout_s}s"}


# A few things that are never part of editing a photography site — blocked up
# front with a clear message so the model adapts instead of hitting raw sandbox
# errors. The sandbox profile is the real enforcement; this is for good UX.
_JAZZY_FORBIDDEN = re.compile(
    r"(?:^|[\s;&|(])(?:sudo|su)\b"
    r"|security\s+(?:dump-keychain|find-(?:generic|internet)-password)"
    r"|/Users/User/paradise-realty-beta"
    r"|/Users/User/paradise-crm-audit",
    re.IGNORECASE,
)


def _jazzy_confined_shell_runner(
    session_mgr: SessionManager, gate: ApprovalGate, cfg: Config,
):
    """Shell runner for Jasmine's website bot: same classify→gate flow as the
    assistant, but every command runs inside the sandbox (writes confined to her
    repo, secrets scrubbed) and obvious out-of-scope commands are refused."""
    from agentmgr.assistant import ShellResult
    from agentmgr.command_policy import AUTO, classify_command

    repo = cfg.jazzysphotos_dir
    timeout = cfg.jazzysphotos_timeout_s

    def run(command: str) -> ShellResult:
        if _JAZZY_FORBIDDEN.search(command):
            return ShellResult(
                command=command, gate="denied",
                stderr="Out of scope — I can only work on jazzysphotos.com.")
        decision = classify_command(command)
        if decision == AUTO:
            gate_label = "auto"
        elif (
            not session_mgr.requires_fresh_approval(command)
            and session_mgr.active_grant("shell") is not None
        ):
            gate_label = "session"
        else:
            try:
                gate.request_approval(
                    "jazzysphotos site command", {"command": command})
            except (ApprovalDenied, ApprovalNotProvisioned) as exc:
                return ShellResult(command=command, gate="denied", stderr=str(exc))
            gate_label = "approved"
        output = _run_command_confined(command, repo, timeout)
        return ShellResult(
            command=command, stdout=output["stdout"], stderr=output["stderr"],
            exit_code=output["exit_code"], gate=gate_label,
        )

    return run


def _process_jazzysphotos_goal(
    task: TaskSpec, store: StateStore, session_mgr: SessionManager,
    gate: ApprovalGate, cfg: Config, *, driver=None,
) -> None:
    """Free-form site edit — a Claude tool-use loop sandboxed to the site repo."""
    from agentmgr.assistant import make_driver, run_agentic_loop

    goal = str(task.payload.get("goal", "")).strip()
    if not goal:
        store.put_task_result(TaskResult(
            task_id=task.id, status=TaskStatus.FAILED,
            error="no 'goal' in task payload", worker="jazzysphotos-site"))
        return
    runner = _jazzy_confined_shell_runner(session_mgr, gate, cfg)
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
               "incentive-social", "taylor", "joe-crm-report", "scout")
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
                    elif task.kind == "marketing":
                        process_taylor_task(task, store, cfg)
                    elif task.kind == "joe_crm":
                        process_joe_crm_task(task, store, cfg)
                    elif task.kind == "improvement":
                        process_scout_task(task, store, cfg)
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
