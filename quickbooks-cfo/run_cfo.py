#!/usr/bin/env python3
"""QuickBooks CFO agent — CLI entry point.

    python run_cfo.py check                 # confirm the QBO connection (no LLM)
    python run_cfo.py report                # pull → analyze → EMAIL the digest
    python run_cfo.py preview               # pull → analyze → print (no email)
    python run_cfo.py ask "question"        # answer one finance question
    python run_cfo.py chat                  # interactive Q&A (snapshot cached across turns)
    python run_cfo.py snapshot              # dump the flattened financials (debug, no LLM)

Reads QBO tokens from ~/.quickbooks-cfo/tokens.json (written by authorize.py) and
keys from env / .env / Secret Manager. Run authorize.py once before first use.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys


def _snapshot():
    from cfo.qbo import QBOClient
    from cfo.reports import pull_snapshot

    return pull_snapshot(QBOClient())


def cmd_check(_args) -> int:
    from cfo import config
    from cfo.qbo import QBOClient

    client = QBOClient()
    info = client.company_info().get("CompanyInfo", {})
    print(f"✓ Connected: {info.get('CompanyName', '(unknown)')}")
    print(f"  Environment: {config.environment()}")
    print(f"  Realm/Company ID: {client.store.realm_id}")
    print(f"  Fiscal year start: {info.get('FiscalYearStartMonth', '?')}")
    return 0


def cmd_report(args) -> int:
    from cfo.analyze import build_digest
    from cfo.email_report import send

    snap = _snapshot()
    digest = build_digest(snap)
    month = dt.date.fromisoformat(snap["as_of"]).strftime("%B %Y")
    result = send(
        subject=f"CFO Digest — {snap['company']} — {month}",
        digest_md=digest,
        title=f"CFO Digest — {month}",
        subtitle=snap["company"],
    )
    print(digest)
    print(f"\n[email] {'sent to ' + result['to'] if result.get('emailed') else 'NOT sent: ' + str(result)}")
    return 0


def cmd_preview(_args) -> int:
    from cfo.analyze import build_digest

    print(build_digest(_snapshot()))
    return 0


def cmd_ask(args) -> int:
    from cfo.analyze import answer_question

    question = (args.question or "").strip()
    if not question:
        print("usage: run_cfo.py ask \"your question\"", file=sys.stderr)
        return 2
    print(answer_question(_snapshot(), question))
    return 0


def cmd_recurring(_args) -> int:
    from cfo.analyze import recurring_review
    from cfo.email_report import send
    from cfo.qbo import QBOClient
    from cfo.recurring import pull_recurring, recurring_as_text
    from cfo.reports import pull_snapshot

    client = QBOClient()
    snap = pull_snapshot(client)
    review = recurring_review(snap, recurring_as_text(pull_recurring(client)))
    result = send(
        subject=f"Recurring-expense review — {snap['company']}",
        digest_md=review, title="Recurring-Expense Review", subtitle=snap["company"],
    )
    print(review)
    print(f"\n[email] {'sent to ' + result['to'] if result.get('emailed') else 'NOT sent: ' + str(result)}")
    return 0


def cmd_alerts(_args) -> int:
    from cfo.analyze import find_alerts
    from cfo.email_report import send
    from cfo.qbo import QBOClient
    from cfo.recurring import pull_recurring, recurring_as_text
    from cfo.reports import pull_snapshot

    client = QBOClient()
    snap = pull_snapshot(client)
    res = find_alerts(snap, recurring_as_text(pull_recurring(client)))
    if res["has_findings"]:
        send(
            subject=f"⚠️ Finance Agent alert — {snap['company']}",
            digest_md=res["markdown"],
            title="Finance Agent — Anomalies & Suggestions", subtitle=snap["company"],
        )
        print("FINDINGS — emailed:\n\n" + res["markdown"])
    else:
        print("No anomalies or suggestions worth flagging right now. (Nothing emailed.)")
    return 0


def cmd_improve(_args) -> int:
    from cfo.email_report import send
    from cfo.improve import propose

    text = propose()
    send(
        subject="Finance Agent — improvement proposals", digest_md=text,
        title="Finance Agent — Self-Improvement Proposals",
        subtitle="proposed for your approval — nothing built yet",
    )
    print(text)
    return 0


def cmd_learn(args) -> int:
    from cfo import profile

    note = (args.note or "").strip()
    if not note:
        print('usage: run_cfo.py learn "a fact about the owner/business"', file=sys.stderr)
        return 2
    profile.append_learning(note)
    print(f"Recorded to profile: {note}")
    return 0


def cmd_forecast(args) -> int:
    """Short-term incoming-revenue forecast from a relayed Brokermint pipeline."""
    import json

    from cfo.email_report import send
    from cfo.forecast import build_forecast

    pipeline = []
    if args.pipeline_file:
        with open(args.pipeline_file, encoding="utf-8") as fh:
            pipeline = (json.load(fh) or {}).get("pipeline", [])
    md = build_forecast(pipeline)
    result = send(
        subject=f"Revenue Forecast — {len(pipeline)} pending deal(s)",
        digest_md=md,
        title="Short-Term Revenue Forecast",
        subtitle="from the Brokermint deal pipeline",
    )
    print(md)
    print(f"\n[email] {'sent to ' + result['to'] if result.get('emailed') else 'NOT sent: ' + str(result)}")
    return 0


def cmd_chat(_args) -> int:
    from cfo.analyze import interactive_ask

    interactive_ask(_snapshot())
    return 0


def cmd_snapshot(_args) -> int:
    from cfo.reports import snapshot_as_text

    print(snapshot_as_text(_snapshot()))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="QuickBooks CFO agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="confirm the QBO connection")
    sub.add_parser("report", help="pull, analyze, and email the digest")
    sub.add_parser("preview", help="pull and analyze, print without emailing")
    ask = sub.add_parser("ask", help="answer one finance question")
    ask.add_argument("question", nargs="?", help="the question (quote it)")
    sub.add_parser("chat", help="interactive Q&A loop")
    sub.add_parser("snapshot", help="dump flattened financials (no LLM)")
    sub.add_parser("recurring", help="recurring-expense review (need/plan-fit/cheaper alt); emails it")
    sub.add_parser("alerts", help="scan for anomalies + suggestions; emails only if found")
    sub.add_parser("improve", help="propose new features + learnings (emails proposals)")
    learn = sub.add_parser("learn", help="record a fact about the owner/business into the profile")
    learn.add_argument("note", nargs="?", help="the fact to remember (quote it)")
    fc = sub.add_parser("forecast", help="short-term revenue forecast from the Brokermint pipeline")
    fc.add_argument("--pipeline-file", help="JSON file with {pipeline:[{commission, close_date, ...}]}")

    args = parser.parse_args()
    handler = {
        "check": cmd_check, "report": cmd_report, "preview": cmd_preview,
        "ask": cmd_ask, "chat": cmd_chat, "snapshot": cmd_snapshot,
        "recurring": cmd_recurring, "alerts": cmd_alerts, "improve": cmd_improve,
        "learn": cmd_learn, "forecast": cmd_forecast,
    }[args.cmd]
    try:
        return handler(args)
    except Exception as exc:  # noqa: BLE001 - surface a clean message to the daemon
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
