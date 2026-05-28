"""Short-term incoming-revenue forecast from the brokerage deal pipeline.

The Brokermint Pipeline agent pulls pending/under-contract deals (each with a
commission amount + expected closing date) and relays them here. This module
buckets them by how soon they close (30 / 60 / 90 day windows) and sums the
projected commission, so the broker sees the money coming in and when.

Deterministic — no LLM, no QBO dependency — so it always produces a number.
"""

from __future__ import annotations

import datetime as dt
import re


def _to_amount(v) -> float:
    """Coerce a commission value (number, or a string like '$12,500.00') to float."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^0-9.\-]", "", str(v))
    try:
        return float(s) if s not in ("", "-", ".") else 0.0
    except ValueError:
        return 0.0


def _to_date(v) -> dt.date | None:
    """Parse an expected-close date in a few common formats; None if unparseable."""
    if not v:
        return None
    s = str(v).strip()[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# (label, inclusive upper bound in days from today). None = no bound (everything after).
_WINDOWS = [
    ("Next 30 days", 30),
    ("31–60 days", 60),
    ("61–90 days", 90),
    ("Beyond 90 days", None),
]


def build_forecast(pipeline: list[dict], *, today: dt.date | None = None) -> str:
    """Return a Markdown short-term revenue forecast from pipeline rows.

    Each row: {address, sale_price, commission, close_date, status, ...}.
    ``commission`` is the brokerage's expected income from that deal.
    """
    today = today or dt.date.today()
    rows = []
    undated_total = 0.0
    undated_count = 0
    for r in pipeline or []:
        amt = _to_amount(r.get("commission"))
        d = _to_date(r.get("close_date") or r.get("closing_date"))
        if d is None:
            undated_total += amt
            undated_count += 1
            continue
        rows.append({
            "address": (r.get("address") or "(unnamed deal)").strip(),
            "commission": amt,
            "close_date": d,
            "days": (d - today).days,
            "status": (r.get("status") or "").strip(),
        })

    if not rows and not undated_count:
        return ("## Short-Term Revenue Forecast\n\n"
                "No pending deals in the Brokermint pipeline right now — nothing to "
                "forecast. (If that's unexpected, re-run the Brokermint Pipeline agent; "
                "it may need a fresh login.)")

    # Bucket each dated deal into the first window it fits.
    buckets = {label: {"count": 0, "total": 0.0, "deals": []} for label, _ in _WINDOWS}
    overdue = {"count": 0, "total": 0.0, "deals": []}  # close date already passed
    for row in rows:
        if row["days"] < 0:
            overdue["count"] += 1
            overdue["total"] += row["commission"]
            overdue["deals"].append(row)
            continue
        for label, ub in _WINDOWS:
            if ub is None or row["days"] <= ub:
                buckets[label]["count"] += 1
                buckets[label]["total"] += row["commission"]
                buckets[label]["deals"].append(row)
                break

    grand = sum(b["total"] for b in buckets.values()) + overdue["total"] + undated_total
    next90 = sum(buckets[l]["total"] for l in ("Next 30 days", "31–60 days", "61–90 days"))

    out = ["## Short-Term Revenue Forecast", ""]
    out.append(f"**Projected company-net commissions — next 90 days: ${next90:,.0f}** "
               f"across {sum(buckets[l]['count'] for l in ('Next 30 days','31–60 days','61–90 days'))} deal(s).")
    out.append("")
    out.append("| Window | Deals | Projected commission |")
    out.append("| --- | ---: | ---: |")
    for label, _ in _WINDOWS:
        b = buckets[label]
        out.append(f"| {label} | {b['count']} | ${b['total']:,.0f} |")
    if overdue["count"]:
        out.append(f"| ⚠️ Past expected close | {overdue['count']} | ${overdue['total']:,.0f} |")
    if undated_count:
        out.append(f"| No close date | {undated_count} | ${undated_total:,.0f} |")
    out.append(f"| **Total pipeline** | **{len(rows) + undated_count}** | **${grand:,.0f}** |")
    out.append("")

    # Per-deal detail, soonest first.
    out.append("### Deals by expected close")
    out.append("")
    out.append("| Close date | Days | Deal | Commission | Status |")
    out.append("| --- | ---: | --- | ---: | --- |")
    for row in sorted(rows, key=lambda x: x["close_date"]):
        flag = " ⚠️" if row["days"] < 0 else ""
        out.append(f"| {row['close_date'].isoformat()}{flag} | {row['days']} | "
                   f"{row['address']} | ${row['commission']:,.0f} | {row['status']} |")
    if undated_count:
        out.append("")
        out.append(f"_Plus {undated_count} deal(s) with no expected close date "
                   f"(${undated_total:,.0f}) — set their dates in Brokermint to include them in the windows above._")

    out.append("")
    out.append("_Source: Brokermint pending-deal pipeline. Amounts are the "
               "brokerage's expected COMPANY-NET commission (office dollar after "
               "agent splits) per deal; closings can slip._")
    return "\n".join(out)
