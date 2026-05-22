"""Agentic assistant — the tool-use loop and the Mac-agent integration."""

from __future__ import annotations

from agent.local_agent import process_assistant_task
from agentmgr.approval_gate import ApprovalGate
from agentmgr.assistant import (
    ModelStep,
    ScriptedDriver,
    ShellCall,
    ShellResult,
    run_agentic_loop,
)
from agentmgr.config import Config
from agentmgr.schemas import TaskSpec, TaskStatus
from agentmgr.session import SessionManager


def _ok_runner(command: str) -> ShellResult:
    return ShellResult(command=command, stdout=f"output of {command}",
                       exit_code=0, gate="auto")


# --- the loop ------------------------------------------------------------

def test_loop_runs_a_command_then_finishes():
    driver = ScriptedDriver([
        ModelStep(text="let me check", calls=[ShellCall("c1", "ls")]),
        ModelStep(text="done — found the files", done=True),
    ])
    result = run_agentic_loop(driver, "list files", shell_runner=_ok_runner, max_steps=10)
    assert result.answer == "done — found the files"
    assert result.steps == 2
    assert result.hit_limit is False
    assert [t["type"] for t in result.transcript] == ["thought", "command", "thought"]
    assert driver.results_log[0][0].stdout == "output of ls"


def test_loop_finishes_when_no_commands_requested():
    driver = ScriptedDriver([ModelStep(text="nothing to do", done=True)])
    result = run_agentic_loop(driver, "noop", shell_runner=_ok_runner, max_steps=5)
    assert result.answer == "nothing to do"
    assert result.steps == 1


def test_loop_is_bounded_by_max_steps():
    """A driver that never stops must be cut off at max_steps."""

    class _Infinite:
        def start(self, goal, system, attachments=None): pass
        def next_step(self): return ModelStep(calls=[ShellCall("c", "ls")])
        def add_results(self, results): pass

    result = run_agentic_loop(_Infinite(), "loop forever",
                              shell_runner=_ok_runner, max_steps=4)
    assert result.hit_limit is True
    assert result.steps == 4


def test_loop_handles_a_denied_command():
    def denying_runner(command):
        return ShellResult(command=command, gate="denied", stderr="operator denied")

    driver = ScriptedDriver([
        ModelStep(calls=[ShellCall("c1", "rm -rf /")]),
        ModelStep(text="ok, stopping", done=True),
    ])
    result = run_agentic_loop(driver, "delete things",
                              shell_runner=denying_runner, max_steps=5)
    assert result.answer == "ok, stopping"
    commands = [t for t in result.transcript if t["type"] == "command"]
    assert commands[0]["gate"] == "denied"
    assert driver.results_log[0][0].gate == "denied"


def test_shell_result_feedback_text():
    assert "DENIED" in ShellResult(command="x", gate="denied", stderr="no").feedback
    ok = ShellResult(command="x", stdout="hi", exit_code=0).feedback
    assert "stdout" in ok and "hi" in ok


# --- Mac-agent integration (process_assistant_task) ----------------------

def test_assistant_task_auto_runs_read_only_command(store, keypair):
    """A read-only command (echo) auto-runs — no approval needed."""
    _, pub = keypair
    cfg = Config(approval_public_key=pub)
    mgr = SessionManager(store, pub, cfg.always_confirm_patterns)
    gate = ApprovalGate(store, public_key_b64=pub, poll_interval_s=0.02)
    driver = ScriptedDriver([
        ModelStep(calls=[ShellCall("c1", "echo hello-from-assistant")]),
        ModelStep(text="all done", done=True),
    ])
    task = TaskSpec(
        agent="assistant", kind="assistant", payload={"goal": "say hello"},
        conversation_id="conv", correlation_id="cmd",
    )
    store.put_task(task)
    process_assistant_task(task, store, mgr, gate, cfg, driver=driver)

    result = store.get_task_result(task.id)
    assert result.status == TaskStatus.COMPLETED
    assert result.output["answer"] == "all done"
    commands = [t for t in result.output["transcript"] if t["type"] == "command"]
    assert commands[0]["gate"] == "auto"
    assert "hello-from-assistant" in commands[0]["stdout"]


def test_assistant_task_gates_a_mutating_command(store):
    """A mutating command (rm) is NOT auto-run — it needs approval. With no
    approver provisioned the gate fails closed, so the command is denied and
    never executes."""
    cfg = Config()
    mgr = SessionManager(store, None, cfg.always_confirm_patterns)
    gate = ApprovalGate(store, public_key_b64=None)  # no verifier -> fail closed
    driver = ScriptedDriver([
        ModelStep(calls=[ShellCall("c1", "rm important.txt")]),
        ModelStep(text="that was blocked", done=True),
    ])
    task = TaskSpec(
        agent="assistant", kind="assistant", payload={"goal": "delete a file"},
        conversation_id="conv", correlation_id="cmd",
    )
    store.put_task(task)
    process_assistant_task(task, store, mgr, gate, cfg, driver=driver)

    result = store.get_task_result(task.id)
    assert result.status == TaskStatus.COMPLETED
    commands = [t for t in result.output["transcript"] if t["type"] == "command"]
    assert commands[0]["gate"] == "denied"  # rm needed approval; none available


def test_assistant_task_fails_without_a_goal(store):
    cfg = Config()
    mgr = SessionManager(store, None, cfg.always_confirm_patterns)
    gate = ApprovalGate(store, public_key_b64=None)
    task = TaskSpec(
        agent="assistant", kind="assistant", payload={},
        conversation_id="conv", correlation_id="cmd",
    )
    store.put_task(task)
    process_assistant_task(task, store, mgr, gate, cfg,
                           driver=ScriptedDriver([]))
    assert store.get_task_result(task.id).status == TaskStatus.FAILED
