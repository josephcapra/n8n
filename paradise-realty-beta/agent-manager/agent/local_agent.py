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

import subprocess
import time

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


def _assistant_shell_runner(session_mgr: SessionManager, gate: ApprovalGate, cfg: Config):
    """Build the shell_runner the agentic loop calls: classify, gate, execute.

    Read-only commands run immediately; everything else (and anything on the
    always-confirm denylist) blocks on the approval gate. Never raises — a
    denied command comes back as a ShellResult with gate='denied'.
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
        output = _run_command(command, None, cfg.shell_command_timeout_s)
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
    log.info("assistant goal started", extra={"task_id": task.id, "goal": goal})
    runner = _assistant_shell_runner(session_mgr, gate, cfg)
    try:
        result = run_agentic_loop(
            driver or make_driver(cfg), goal,
            shell_runner=runner, max_steps=cfg.assistant_max_steps,
            system=system_prompt(memory),
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
    # The Mac daemon serves two local-agent agents: direct shell ('mac-shell')
    # and the agentic assistant ('assistant').
    handled = (cfg.local_agent_name, "assistant")
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
