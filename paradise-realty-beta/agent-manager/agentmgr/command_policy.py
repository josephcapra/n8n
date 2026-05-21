"""Command-safety policy for the agentic assistant (Phase 4).

Implements the operator's chosen posture — *minimal permissions, approval
before major decisions*:

  * a single, un-chained, read-only command  ->  ``AUTO`` (runs immediately)
  * anything that writes, deletes, installs, escalates, chains commands, or
    redirects                                ->  ``APPROVAL`` (blocks on the gate)

This is deliberately conservative: anything not provably safe needs approval.
"""

from __future__ import annotations

AUTO = "auto"
APPROVAL = "approval"

# Single programs that only read state — safe to run without approval.
_READ_ONLY = frozenset({
    "ls", "pwd", "cat", "echo", "head", "tail", "wc", "grep", "egrep", "fgrep",
    "which", "whoami", "date", "env", "printenv", "ps", "df", "du", "uname",
    "hostname", "id", "uptime", "stat", "file", "basename", "dirname",
    "realpath", "sort", "uniq", "cut", "tr", "diff", "shasum", "md5", "sw_vers",
    "tree", "history", "type", "man", "help", "true", "test",
})
_GIT_READ_ONLY = frozenset({
    "status", "log", "diff", "show", "branch", "remote", "ls-files",
    "describe", "config", "rev-parse", "blame", "shortlog", "tag", "fetch",
})
_GCLOUD_READ_VERBS = frozenset({"list", "describe"})

# Chaining / redirection / substitution — a human must review the whole line.
_SHELL_META = (";", "&&", "||", "|", "`", "$(", "${", ">", "<", "&", "\n")
# `find` is read-only unless it mutates or executes.
_FIND_DANGER = ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint")


def classify_command(command: str) -> str:
    """Return ``AUTO`` if ``command`` is safe to run without approval, else
    ``APPROVAL``. Never raises."""
    cmd = command.strip()
    if not cmd:
        return APPROVAL
    if any(meta in cmd for meta in _SHELL_META):
        return APPROVAL

    parts = cmd.split()
    prog = parts[0].rsplit("/", 1)[-1]  # tolerate an absolute path like /bin/ls
    args = parts[1:]

    if prog == "git":
        return AUTO if args and args[0] in _GIT_READ_ONLY else APPROVAL
    if prog == "gcloud":
        # AUTO only if a read verb (list/describe) appears and nothing else
        # claims a mutating verb — describe takes a trailing resource name, so
        # we can't just look at the last token.
        verbs = [a for a in args if not a.startswith("-")]
        return AUTO if _GCLOUD_READ_VERBS.intersection(verbs) else APPROVAL
    if prog == "find":
        return APPROVAL if any(flag in args for flag in _FIND_DANGER) else AUTO
    return AUTO if prog in _READ_ONLY else APPROVAL
