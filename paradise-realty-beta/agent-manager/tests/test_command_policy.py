"""Command-safety policy — what auto-runs vs. what needs approval (Phase 4)."""

from __future__ import annotations

from agentmgr.command_policy import APPROVAL, AUTO, classify_command


def test_read_only_commands_auto_run():
    for cmd in ["ls -la", "cat README.md", "pwd", "grep foo file.txt",
                "whoami", "head -n5 x", "wc -l x", "diff a b"]:
        assert classify_command(cmd) == AUTO, cmd


def test_git_read_only_auto_runs():
    assert classify_command("git status") == AUTO
    assert classify_command("git log --oneline") == AUTO
    assert classify_command("git diff HEAD~1") == AUTO


def test_git_mutating_needs_approval():
    assert classify_command("git push origin main") == APPROVAL
    assert classify_command("git commit -m wip") == APPROVAL
    assert classify_command("git reset --hard") == APPROVAL


def test_gcloud_reads_auto_mutations_gated():
    assert classify_command("gcloud run services list") == AUTO
    assert classify_command("gcloud run jobs describe x") == AUTO
    assert classify_command("gcloud run deploy svc") == APPROVAL
    assert classify_command("gcloud run jobs run myjob") == APPROVAL


def test_mutating_commands_need_approval():
    for cmd in ["rm file.txt", "mv a b", "pip install requests", "sudo ls",
                "mkdir d", "curl http://x", "brew install y", "npm i"]:
        assert classify_command(cmd) == APPROVAL, cmd


def test_chaining_and_redirection_need_approval():
    assert classify_command("ls; rm -rf /") == APPROVAL
    assert classify_command("ls && rm x") == APPROVAL
    assert classify_command("cat a | sh") == APPROVAL
    assert classify_command("echo x > file.txt") == APPROVAL
    assert classify_command("echo $(whoami)") == APPROVAL


def test_find_is_auto_unless_it_mutates():
    assert classify_command("find . -type f") == AUTO
    assert classify_command("find . -name needle") == AUTO
    assert classify_command("find . -delete") == APPROVAL
    assert classify_command("find . -exec rm {} +") == APPROVAL


def test_empty_command_needs_approval():
    assert classify_command("   ") == APPROVAL
    assert classify_command("") == APPROVAL
