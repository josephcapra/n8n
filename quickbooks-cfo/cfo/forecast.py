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


def build_forecast(pending: list[dict], *, active_listings: list[dict] | None = None,
                   brokermint_active: dict | None = None, mls_as_of: str | None = None,
                   today: dt.date | None = None) -> str:
    """Return a Markdown report with two sections:

    1. Short-term revenue forecast from PENDING deals (each row {address,
       commission, close_date, status, ...}; ``commission`` = company net).
    2. Active-listings inventory (current on-market listings from Beaches MLS,
       plus a Brokermint-active summary).
    """
    today = today or dt.date.today()
    rows = []
    undated_total = 0.0
    undated_count = 0
    for r in pending or []:
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

    # --- Section 1: pending deals -> revenue forecast --------------------
    out = ["## 1. Pending Deals — Short-Term Revenue Forecast", ""]
    if not rows and not undated_count:
        out.append("_No pending deals in the Brokermint pipeline right now._")
    else:
        buckets = {label: {"count": 0, "total": 0.0} for label, _ in _WINDOWS}
        overdue = {"count": 0, "total": 0.0}  # close date already passed
        for row in rows:
            if row["days"] < 0:
                overdue["count"] += 1
                overdue["total"] += row["commission"]
                continue
            for label, ub in _WINDOWS:
                if ub is None or row["days"] <= ub:
                    buckets[label]["count"] += 1
                    buckets[label]["total"] += row["commission"]
                    break

        grand = sum(b["total"] for b in buckets.values()) + overdue["total"] + undated_total
        win90 = ("Next 30 days", "31–60 days", "61–90 days")
        next90 = sum(buckets[l]["total"] for l in win90)
        n90 = sum(buckets[l]["count"] for l in win90)

        out.append(f"**Projected company-net commissions — next 90 days: ${next90:,.0f}** "
                   f"across {n90} deal(s).")
        out.append("")
        out.append("| Window | Deals | Projected net commission |")
        out.append("| --- | ---: | ---: |")
        for label, _ in _WINDOWS:
            b = buckets[label]
            out.append(f"| {label} | {b['count']} | ${b['total']:,.0f} |")
        if overdue["count"]:
            out.append(f"| ⚠️ Past expected close | {overdue['count']} | ${overdue['total']:,.0f} |")
        if undated_count:
            out.append(f"| No close date | {undated_count} | ${undated_total:,.0f} |")
        out.append(f"| **Total** | **{len(rows) + undated_count}** | **${grand:,.0f}** |")
        out.append("")
        out.append("### Pending deals by expected close")
        out.append("")
        out.append("| Close date | Days | Deal | Net commission | Status |")
        out.append("| --- | ---: | --- | ---: | --- |")
        for row in sorted(rows, key=lambda x: x["close_date"]):
            flag = " ⚠️" if row["days"] < 0 else ""
            out.append(f"| {row['close_date'].isoformat()}{flag} | {row['days']} | "
                       f"{row['address']} | ${row['commission']:,.0f} | {row['status']} |")
        if undated_count:
            out.append("")
            out.append(f"_Plus {undated_count} deal(s) with no close date (${undated_total:,.0f})._")
        out.append("")
        out.append("_Company-net = office dollar after agent splits; closings can slip._")

    # --- Section 2: active listings (current inventory) ------------------
    out += _active_section(active_listings or [], brokermint_active or {}, mls_as_of)
    return "\n".join(out)


def _active_section(active_listings: list[dict], brokermint_active: dict,
                    mls_as_of: str | None) -> list[str]:
    """Markdown for the current on-market inventory (Beaches MLS) + a Brokermint
    active-transaction summary line."""
    out = ["", "## 2. Active Listings — Current Inventory", ""]
    if not active_listings and not brokermint_active.get("count"):
        out.append("_No active listings found._")
        return out
    if active_listings:
        total = sum(_to_amount(l.get("list_price")) for l in active_listings)
        asof = f" _(Beaches MLS, as of {mls_as_of})_" if mls_as_of else " _(Beaches MLS)_"
        out.append(f"**{len(active_listings)} active MLS listings · ${total:,.0f} total list volume**{asof}")
        out.append("")
        out.append("| Listing | List price | DOM | Status | Agent |")
        out.append("| --- | ---: | ---: | --- | --- |")
        for l in sorted(active_listings, key=lambda x: -_to_amount(x.get("list_price"))):
            dom = l.get("days_on_market")
            out.append(f"| {l.get('address', '?')} | ${_to_amount(l.get('list_price')):,.0f} | "
                       f"{'' if dom in (None, '') else dom} | {l.get('status', '')} | {l.get('list_agent') or ''} |")
    if brokermint_active.get("count"):
        out.append("")
        out.append(f"_Brokermint also shows {brokermint_active['count']} active transaction(s) "
                   f"(incl. buyer-side / off-MLS / referrals), "
                   f"~${_to_amount(brokermint_active.get('net_total')):,.0f} company-net._")
    return out
