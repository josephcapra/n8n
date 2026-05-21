"""Master Agent — the private, always-on Cloud Run Service.

The Master is the manager *and* the hub: it interprets natural-language
commands, routes work to worker Jobs, brokers all inter-agent messages, and
reports back in the chat. Workers never talk to each other directly.
"""
