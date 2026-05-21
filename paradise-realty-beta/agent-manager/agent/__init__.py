"""The Mac local agent.

A long-lived daemon you run on your own Mac. It is a worker with runtime
``local-agent``: instead of being triggered like a Cloud Run Job, it polls the
shared state store for tasks addressed to it, executes them, and writes
results back.

SECURITY: this process opens NO inbound network port. All its connectivity is
outbound to the state store. If it is not running, nothing runs on your Mac.
Every command is gated (session window or per-command approval) and logged.
"""
