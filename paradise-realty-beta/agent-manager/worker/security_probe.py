"""security-probe — a Cloud Run Job that probes the operator's public risk.

Subordinate to the Master (registered as "security-probe", runtime
cloudrun-job): the Master can trigger it like any other Cloud Run job, and it
also runs fine on a schedule (Cloud Scheduler). It probes the operator's OWN
public web assets — non-intrusively — for common, real risks:

  * TLS certificate validity and days-to-expiry
  * HTTPS enforcement (HTTP -> HTTPS redirect)
  * missing security headers (HSTS, CSP, X-Frame-Options, nosniff, Referrer)
  * accidentally-exposed sensitive paths (/.env, /.git/HEAD, /.git/config)

It then grades the result and emails the operator via SendGrid. Authorized,
read-only probing of the operator's own domains — a handful of GET requests per
site, no fuzzing, no exploitation.

    python -m worker.security_probe --dry-run
    python -m worker.security_probe                 # probe + email
    PROBE_TARGETS="example.com,foo.com" python -m worker.security_probe
"""

from __future__ import annotations

import argparse
import os
import socket
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

_DEFAULT_TARGETS = (
    "paradiserealtyfla.com",
    "www.paradiserealtyfla.com",
    "jazzysphotos.com",
    "jasminecapra.com",
)
_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "ok": 4}
_SECURITY_HEADERS = {
    "strict-transport-security": ("HSTS", "medium"),
    "content-security-policy": ("Content-Security-Policy", "medium"),
    "x-content-type-options": ("X-Content-Type-Options (nosniff)", "low"),
    "x-frame-options": ("X-Frame-Options (clickjacking)", "low"),
    "referrer-policy": ("Referrer-Policy", "low"),
}
_SENSITIVE_PATHS = ("/.env", "/.git/HEAD", "/.git/config")


@dataclass
class Finding:
    target: str
    title: str
    severity: str
    detail: str
    fix: str = ""


def _targets() -> list[str]:
    raw = os.environ.get("PROBE_TARGETS", "")
    items = [t.strip() for t in raw.split(",") if t.strip()]
    return items or list(_DEFAULT_TARGETS)


def _cert_days_left(host: str) -> int | None:
    try:
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:  # noqa: BLE001
            ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
        exp = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(
            tzinfo=timezone.utc)
        return (exp - datetime.now(timezone.utc)).days
    except Exception:  # noqa: BLE001
        return None


def probe_target(host: str, client: httpx.Client | None = None) -> list[Finding]:
    own = client or httpx.Client(timeout=12, follow_redirects=True,
                                 headers={"User-Agent": "agentmgr-security-probe/1.0"})
    out: list[Finding] = []
    try:
        # --- TLS expiry ---
        days = _cert_days_left(host)
        if days is None:
            out.append(Finding(host, "TLS certificate", "high",
                               "Could not establish a valid TLS connection.",
                               "Check the certificate / HTTPS setup."))
        elif days < 0:
            out.append(Finding(host, "TLS certificate", "critical",
                               "Certificate is EXPIRED.", "Renew it now."))
        elif days < 21:
            out.append(Finding(host, "TLS certificate", "high",
                               f"Certificate expires in {days} days.", "Renew soon."))
        else:
            out.append(Finding(host, "TLS certificate", "ok",
                               f"Valid, {days} days remaining."))

        # --- HTTPS reachable + headers ---
        try:
            r = own.get(f"https://{host}/")
            headers = {k.lower(): v for k, v in r.headers.items()}
            for key, (label, sev) in _SECURITY_HEADERS.items():
                if key not in headers:
                    out.append(Finding(host, f"Missing header: {label}", sev,
                                       "Header not set on the homepage response.",
                                       f"Add the {label} response header."))
        except Exception as exc:  # noqa: BLE001
            out.append(Finding(host, "HTTPS reachability", "high",
                               f"HTTPS request failed: {exc}", ""))
            headers = {}

        # --- HTTP -> HTTPS redirect ---
        try:
            nr = httpx.get(f"http://{host}/", follow_redirects=False, timeout=10,
                           headers={"User-Agent": "agentmgr-security-probe/1.0"})
            loc = nr.headers.get("location", "")
            if not (nr.status_code in (301, 302, 307, 308) and loc.startswith("https")):
                out.append(Finding(host, "HTTP not redirected to HTTPS", "medium",
                                   f"http:// returned {nr.status_code} (location='{loc}').",
                                   "Force a 301 redirect from HTTP to HTTPS."))
        except Exception:  # noqa: BLE001
            pass

        # --- exposed sensitive paths ---
        for path in _SENSITIVE_PATHS:
            try:
                pr = own.get(f"https://{host}{path}")
                ctype = pr.headers.get("content-type", "")
                body = pr.text[:200]
                exposed = (
                    pr.status_code == 200 and "text/html" not in ctype
                    and (("=" in body and path == "/.env")
                         or body.startswith("ref:")
                         or "[core]" in body)
                )
                if exposed:
                    out.append(Finding(host, f"Exposed path {path}", "critical",
                                       f"{path} is publicly served (HTTP 200).",
                                       f"Block public access to {path}."))
            except Exception:  # noqa: BLE001
                pass
    finally:
        if client is None:
            own.close()
    return out


def probe(targets: list[str] | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for host in (targets or _targets()):
        findings.extend(probe_target(host))
    return sorted(findings, key=lambda f: _RANK.get(f.severity, 9))


def grade(findings: list[Finding]) -> tuple[str, dict]:
    counts = {s: 0 for s in _RANK}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    if counts["critical"]:
        g = "F"
    elif counts["high"] >= 2:
        g = "D"
    elif counts["high"] == 1:
        g = "C"
    elif counts["medium"]:
        g = "B"
    else:
        g = "A"
    return g, counts


def render(findings: list[Finding]) -> tuple[str, str, str]:
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    g, counts = grade(findings)
    subject = (f"External risk probe: {g} "
               f"({counts['critical'] + counts['high']} high/critical) — {now}")
    lines = [f"External Risk Probe — {now}", f"Overall grade: {g}",
             f"critical:{counts['critical']} high:{counts['high']} "
             f"medium:{counts['medium']} low:{counts['low']} ok:{counts['ok']}", ""]
    rows = []
    for f in findings:
        lines.append(f"[{f.severity.upper()}] {f.target} — {f.title}: {f.detail}"
                     + (f"\n   → {f.fix}" if f.fix else ""))
        color = {"critical": "#b00", "high": "#c0392b", "medium": "#d68910",
                 "low": "#7d6608", "ok": "#1e8449"}.get(f.severity, "#555")
        rows.append(
            f"<tr><td style='padding:6px 10px;color:{color};font-weight:600'>"
            f"{f.severity.upper()}</td><td style='padding:6px 10px'>"
            f"<b>{f.target}</b> — {f.title}<br><span style='color:#444'>{f.detail}</span>"
            + (f"<br><span style='color:#888'>{f.fix}</span>" if f.fix else "")
            + "</td></tr>")
    html = (f"<div style='font-family:-apple-system,Helvetica,sans-serif;max-width:680px'>"
            f"<h2>External Risk Probe — grade {g}</h2>"
            f"<p style='color:#666'>{now}</p>"
            f"<table style='border-collapse:collapse;width:100%'>{''.join(rows)}</table>"
            f"<p style='color:#999;font-size:12px'>Sent by your Agent-Manager "
            f"security-probe (Cloud Run). Non-intrusive scan of your own domains.</p></div>")
    return subject, "\n".join(lines), html


def _send_email(subject: str, text: str, html: str) -> dict:
    """Self-contained SendGrid send (no dependency on tools/, so the Cloud Run
    image needs only agentmgr/master/worker)."""
    key = os.environ.get("SENDGRID_API_KEY")
    to = os.environ.get("REPORT_EMAIL_TO", "joe@josephcapra.com")
    sender = os.environ.get("SECURITY_REPORT_FROM", "noreply@paradiserealtyfla.com")
    if not key:
        return {"sent": False, "error": "SENDGRID_API_KEY not set"}
    payload = {
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": sender, "name": "Agent-Manager Security Probe"},
        "subject": subject,
        "content": [{"type": "text/plain", "value": text},
                    {"type": "text/html", "value": html}],
    }
    try:
        r = httpx.post("https://api.sendgrid.com/v3/mail/send",
                       headers={"Authorization": f"Bearer {key}",
                                "Content-Type": "application/json"},
                       json=payload, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return {"sent": False, "error": str(exc)}
    if r.status_code in (200, 202):
        return {"sent": True, "to": to, "status": r.status_code}
    return {"sent": False, "status": r.status_code, "error": r.text[:300]}


def run(send: bool = True) -> dict:
    findings = probe()
    subject, text, html = render(findings)
    g, counts = grade(findings)
    result = {"grade": g, "counts": counts, "subject": subject,
              "findings": [f.__dict__ for f in findings]}
    if send:
        result["email"] = _send_email(subject, text, html)
    return result


def run_task(task_id, store):
    """Master-bus entrypoint (parity with other workers)."""
    from agentmgr.schemas import TaskResult, TaskStatus
    try:
        out = run(send=True)
        result = TaskResult(task_id=task_id, status=TaskStatus.COMPLETED,
                            output=out, worker="security-probe")
    except Exception as exc:  # noqa: BLE001
        result = TaskResult(task_id=task_id, status=TaskStatus.FAILED,
                            error=f"{type(exc).__name__}: {exc}", worker="security-probe")
    store.put_task_result(result)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe the operator's public assets for risk.")
    ap.add_argument("--dry-run", action="store_true", help="print, don't email")
    args = ap.parse_args()
    try:
        from agentmgr.connectors import load_env_file
        load_env_file()
    except Exception:  # noqa: BLE001
        pass
    findings = probe()
    subject, text, html = render(findings)
    print(subject + "\n\n" + text)
    if args.dry_run:
        print("\n[dry-run] not sending email.")
        return 0
    res = _send_email(subject, text, html)
    print("\n" + ("Emailed " + str(res.get("to")) if res.get("sent")
                   else "EMAIL FAILED: " + str(res.get("error"))))
    return 0 if res.get("sent") else 1


if __name__ == "__main__":
    sys.exit(main())
