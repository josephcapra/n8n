"""Send the sitemap sync summary report via SendGrid."""
from __future__ import annotations

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><style>
  body{{font-family:Arial,sans-serif;margin:30px;color:#333}}
  h2{{color:#1a73e8}}
  table{{border-collapse:collapse;width:100%;max-width:560px;margin:16px 0}}
  th,td{{border:1px solid #ddd;padding:9px 14px;text-align:left;font-size:14px}}
  th{{background:#1a73e8;color:#fff}}
  tr:nth-child(even){{background:#f8f9fa}}
  .ok{{color:#2e7d32;font-weight:bold}}
  .err{{color:#c62828;font-weight:bold}}
  .skip{{color:#888}}
  .footer{{color:#aaa;font-size:11px;margin-top:24px}}
</style></head>
<body>
<h2>Sitemap Sync Report</h2>
<p><strong>{date}</strong> &nbsp;·&nbsp; Run completed at {time} ET</p>
<table>
  <tr><th>Step</th><th>Result</th></tr>
  <tr><td>URLs collected</td><td>{urls:,}</td></tr>
  <tr><td>Shards (≤40k URLs each)</td><td>{shards}</td></tr>
  <tr><td>GCS upload</td><td class="{gcs_cls}">{gcs}</td></tr>
  <tr><td>RealGeeks redirects</td><td class="skip">{redirects}</td></tr>
  <tr><td>Google Search Console</td><td class="{gsc_cls}">{gsc}</td></tr>
  <tr><td>Bing Webmaster Tools</td><td class="{bing_cls}">{bing}</td></tr>
  <tr><td>Total runtime</td><td>{runtime:.0f}s</td></tr>
</table>
{errors_html}
<p class="footer">paradiserealtyfla.com &nbsp;·&nbsp; Sitemap Sync Pipeline</p>
</body></html>"""


def _fmt(d: dict | None, count_key: str, del_key: str) -> tuple[str, str]:
    if d is None:
        return "SKIPPED", "skip"
    ok = d.get("success", True)
    cls = "ok" if ok else "err"
    icon = "✅" if ok else "❌"
    return (
        f"{d.get(count_key, 0)} &nbsp;(deleted: {d.get(del_key, 0)}) {icon}",
        cls,
    )


def send_report(
    to_email: str,
    sendgrid_key: str,
    url_count: int,
    shard_count: int,
    gcs: dict | None,
    redirects: dict | None,
    gsc: dict | None,
    bing: dict | None,
    runtime: float,
    success: bool,
) -> None:
    try:
        import sendgrid as sg_lib
        from sendgrid.helpers.mail import Mail
    except ImportError:
        logger.error("sendgrid package not installed — pip install sendgrid")
        return

    now = datetime.now(ZoneInfo("America/New_York"))
    gcs_txt, gcs_cls   = _fmt(gcs,  "uploaded",  "deleted")
    gsc_txt, gsc_cls   = _fmt(gsc,  "submitted", "deleted")
    bing_txt, bing_cls = _fmt(bing, "submitted", "deleted")

    if redirects is None:
        redir_txt = "SKIPPED (managed separately)"
    else:
        ok = redirects.get("success", True)
        redir_txt = (
            f"{redirects.get('upserted', 0)} upserted, "
            f"{redirects.get('deleted', 0)} deleted "
            f"{'✅' if ok else '❌'}"
        )

    all_errors: list[str] = []
    for name, d in [("GCS", gcs), ("GSC", gsc), ("Bing", bing), ("Redirects", redirects)]:
        if d and d.get("errors"):
            all_errors += [f"<li>{name}: {e}</li>" for e in d["errors"]]
    errors_html = (
        f'<p style="color:#c62828"><strong>Errors:</strong><ul>{"".join(all_errors)}</ul></p>'
        if all_errors else ""
    )

    html = _HTML.format(
        date=now.strftime("%A, %B %d, %Y"),
        time=now.strftime("%I:%M %p"),
        urls=url_count,
        shards=shard_count,
        gcs=gcs_txt, gcs_cls=gcs_cls,
        redirects=redir_txt,
        gsc=gsc_txt, gsc_cls=gsc_cls,
        bing=bing_txt, bing_cls=bing_cls,
        runtime=runtime,
        errors_html=errors_html,
    )

    subject = (
        f"{'✅' if success else '❌'} Sitemap Sync — "
        f"{now.strftime('%b %d, %Y')} — {url_count:,} URLs / {shard_count} shards"
    )
    try:
        import agentmgr_reports
        agentmgr_reports.save_report("sitemap-sync-weekly", subject, html,
                                     source="sitemap-sync-weekly")
    except Exception:  # noqa: BLE001 - report capture is best-effort
        pass
    message = Mail(
        from_email="sitemap-sync@paradiserealtyfla.com",
        to_emails=to_email,
        subject=subject,
        html_content=html,
    )

    try:
        client = sg_lib.SendGridAPIClient(sendgrid_key)
        resp = client.send(message)
        logger.info(f"  Email sent to {to_email} — HTTP {resp.status_code}")
    except Exception as e:
        logger.error(f"  Email FAILED: {e}")
