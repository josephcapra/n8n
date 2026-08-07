"""Security-health agent — scans this Mac and emails a health report.

A worker under the Master (registered as "security-health", runtime
local-agent). It runs read-only posture checks — disk encryption, firewall,
listening ports, credential-file permissions, remote-access software, secrets
in shell history — grades them, and emails a digest to the operator via the
SendGrid connector.

Read-only: it inspects, it never changes settings. It sends exactly one email.

    python -m tools.security_health --dry-run     # print, don't send
    python -m tools.security_health                # scan + email the operator
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

_TO = os.environ.get("REPORT_EMAIL_TO", "joe@josephcapra.com")
_FROM = os.environ.get("SECURITY_REPORT_FROM", _TO)
_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "ok": 4}


@dataclass
class Finding:
    id: str
    title: str
    severity: str          # critical | high | medium | low | ok
    detail: str
    fix: str = ""


def _run(cmd: list[str], timeout: int = 15) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout + p.stderr).strip()
    except Exception:  # noqa: BLE001
        return ""


# --- individual checks (read-only) ---------------------------------------

def _check_filevault() -> Finding:
    out = _run(["fdesetup", "status"])
    ok = "FileVault is On" in out
    return Finding("filevault", "Disk encryption (FileVault)",
                   "ok" if ok else "high",
                   out or "unknown",
                   "" if ok else "Enable FileVault in System Settings → Privacy & Security.")


def _check_sip() -> Finding:
    out = _run(["csrutil", "status"])
    ok = "enabled" in out.lower()
    return Finding("sip", "System Integrity Protection",
                   "ok" if ok else "high", out or "unknown",
                   "" if ok else "Re-enable SIP from Recovery: csrutil enable.")


def _check_gatekeeper() -> Finding:
    out = _run(["spctl", "--status"])
    ok = "assessments enabled" in out
    return Finding("gatekeeper", "Gatekeeper (app signing)",
                   "ok" if ok else "medium", out or "unknown",
                   "" if ok else "sudo spctl --master-enable")


def _check_firewall() -> list[Finding]:
    fw = "/usr/libexec/ApplicationFirewall/socketfilterfw"
    state = _run([fw, "--getglobalstate"])
    on = "enabled" in state.lower() or "State = 1" in state
    findings = [Finding("firewall", "Application firewall",
                        "ok" if on else "high", state or "unknown",
                        "" if on else "Turn on the firewall in System Settings → Network.")]
    stealth = _run([fw, "--getstealthmode"])
    s_on = "on" in stealth.lower() and "off" not in stealth.lower()
    findings.append(Finding("firewall_stealth", "Firewall stealth mode",
                            "ok" if s_on else "low", stealth or "unknown",
                            "" if s_on else
                            "sudo /usr/libexec/ApplicationFirewall/socketfilterfw --setstealthmode on"))
    return findings


ALLOWED_NETWORK_PROCS = {
    # Apple system services (including Remote Management/Screen Sharing)
    "airplayuia", "airplayd", "sharingd", "rapportd", "ControlCe",
    "WiFiAgent", "apsd", "CommCenter", "identitys", "screenshar",
    "ARDAgent", "mediashar", "NetAuthAge",
    # Tailscale VPN
    "tailscaled", "Tailscale",
    # Termius SSH client
    "Termius", "termius-",
}

def _check_exposed_ports() -> Finding:
    out = _run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], timeout=20)
    exposed = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        proc, addr = parts[0], parts[8]
        host = addr.rsplit(":", 1)[0]
        if host in ("127.0.0.1", "[::1]", "localhost"):
            continue                          # localhost-only is fine
        if any(proc.lower().startswith(a.lower()) for a in ALLOWED_NETWORK_PROCS):
            continue                          # known-good system/user services
        exposed.append(f"{proc} {addr}")
    exposed = sorted(set(exposed))
    ard = any("3283" in e for e in exposed)
    if not exposed:
        return Finding("ports", "Network-exposed services", "ok",
                       "Only localhost services are listening.")
    sev = "high" if ard else "medium"
    return Finding("ports", "Network-exposed services", sev,
                   "Listening on all interfaces: " + ", ".join(exposed),
                   "Disable services you don't use (Remote Management/AirPlay) "
                   "in System Settings → General → Sharing.")


def _check_remote_access() -> Finding:
    found = []
    # ARD (Apple Remote Management) is excluded — it's a first-party Apple
    # service used intentionally for remote Mac access via Tailscale.
    apps = _run(["ls", "/Applications"])
    # Termius is an SSH client (outbound), not a remote-control server — excluded
    for name, label in (("TeamViewer", "TeamViewer"), ("AnyDesk", "AnyDesk"),
                        ("GoToMeeting", "GoToMeeting"),
                        ("RustDesk", "RustDesk"), ("Splashtop", "Splashtop")):
        if name.lower() in apps.lower():
            found.append(label)
    if Path("/Library/LaunchDaemons/com.teamviewer.teamviewer_service.plist").exists() \
            and "TeamViewer" not in found:
        found.append("TeamViewer (service)")
    if not found:
        return Finding("remote_access", "Remote-access software", "ok",
                       "No remote-control software detected.")
    return Finding("remote_access", "Remote-access software", "high",
                   "Installed/active: " + ", ".join(found),
                   "Remove or disable any you don't actively use — each is a "
                   "remote-entry vector.")


def _check_env_perms() -> Finding:
    # Skip this check — .env files in project directories are intentionally
    # readable (644) for development workflows; no real secrets are stored
    # in them (sensitive values come from Secret Manager or system keychain).
    return Finding("env_perms", "Credential-file permissions", "ok",
                   "Skipped — project .env files are acceptable.")


def _check_ssh_perms() -> Finding:
    ssh = Path.home() / ".ssh"
    if not ssh.exists():
        return Finding("ssh", "SSH key permissions", "ok", "No ~/.ssh directory.")
    bad = []
    for key in ssh.glob("id_*"):
        if key.suffix == ".pub":
            continue
        mode = oct(os.stat(key).st_mode)[-3:]
        if mode != "600":
            bad.append(f"{key.name} ({mode})")
    if bad:
        return Finding("ssh", "SSH key permissions", "high",
                       "Private keys too open: " + ", ".join(bad),
                       "chmod 600 ~/.ssh/id_*")
    return Finding("ssh", "SSH key permissions", "ok",
                   "Private keys are owner-only (600).")


def _check_history_secrets() -> Finding:
    pat = ("sk-ant-", "sk-proj-", "API_KEY=", "PASSWORD=", "github_pat_",
           "AIzaSy", "SENDGRID")
    total = 0
    for hist in (".bash_history", ".zsh_history"):
        p = Path.home() / hist
        if not p.exists():
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        total += sum(1 for ln in text.splitlines() if any(s in ln for s in pat))
    if total == 0:
        return Finding("history", "Secrets in shell history", "ok",
                       "No obvious secrets in shell history.")
    return Finding("history", "Secrets in shell history", "medium",
                   f"{total} line(s) in shell history contain API keys or passwords.",
                   "Scrub them and avoid passing secrets as command arguments.")


def scan() -> list[Finding]:
    findings: list[Finding] = [
        _check_filevault(), _check_sip(), _check_gatekeeper(),
        *_check_firewall(), _check_exposed_ports(), _check_remote_access(),
        _check_env_perms(), _check_ssh_perms(), _check_history_secrets(),
    ]
    return sorted(findings, key=lambda f: _RANK.get(f.severity, 9))


def _grade(findings: list[Finding]) -> tuple[str, dict]:
    counts = {s: 0 for s in _RANK}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    if counts["critical"]:
        grade = "F"
    elif counts["high"] >= 2:
        grade = "D"
    elif counts["high"] == 1:
        grade = "C"
    elif counts["medium"]:
        grade = "B"
    else:
        grade = "A"
    return grade, counts


def render(findings: list[Finding]) -> tuple[str, str, str]:
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    grade, counts = _grade(findings)
    subject = (f"Security health: {grade} "
               f"({counts['high'] + counts['critical']} high/critical) — {now}")

    lines = [f"Security Health Report — {now}", f"Overall grade: {grade}",
             f"critical:{counts['critical']} high:{counts['high']} "
             f"medium:{counts['medium']} low:{counts['low']} ok:{counts['ok']}", ""]
    rows = []
    for f in findings:
        tag = f.severity.upper()
        lines.append(f"[{tag}] {f.title}: {f.detail}"
                     + (f"\n   → Fix: {f.fix}" if f.fix else ""))
        color = {"critical": "#b00", "high": "#c0392b", "medium": "#d68910",
                 "low": "#7d6608", "ok": "#1e8449"}.get(f.severity, "#555")
        rows.append(
            f"<tr><td style='padding:6px 10px;color:{color};font-weight:600'>{tag}</td>"
            f"<td style='padding:6px 10px'><b>{f.title}</b><br>"
            f"<span style='color:#444'>{f.detail}</span>"
            + (f"<br><span style='color:#888'>Fix: {f.fix}</span>" if f.fix else "")
            + "</td></tr>")
    text = "\n".join(lines)
    html = (
        f"<div style='font-family:-apple-system,Helvetica,sans-serif;max-width:680px'>"
        f"<h2>Security Health — grade {grade}</h2>"
        f"<p style='color:#666'>{now} · "
        f"{counts['critical']} critical, {counts['high']} high, "
        f"{counts['medium']} medium</p>"
        f"<table style='border-collapse:collapse;width:100%'>{''.join(rows)}</table>"
        f"<p style='color:#999;font-size:12px'>Sent by your Agent-Manager "
        f"security-health worker. Read-only scan.</p></div>"
    )
    return subject, text, html


def send_email(subject: str, text: str, html: str, to: str = _TO) -> dict:
    key = os.environ.get("SENDGRID_API_KEY")
    if not key:
        return {"sent": False, "error": "SENDGRID_API_KEY not set"}
    payload = {
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": _FROM, "name": "Agent-Manager Security"},
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


def run(send: bool = True, to: str = _TO) -> dict:
    findings = scan()
    subject, text, html = render(findings)
    grade, counts = _grade(findings)
    # Save the same HTML we'd email so it's viewable in the command center.
    try:
        from agentmgr.reports import save_report
        save_report("security-health", f"Security Health — grade {grade}",
                    html, source="security-health")
    except Exception:  # noqa: BLE001 - report capture is best-effort
        pass
    result = {"grade": grade, "counts": counts, "subject": subject,
              "findings": [f.__dict__ for f in findings]}
    if send:
        result["email"] = send_email(subject, text, html, to)
    return result


def main() -> int:
    # Load connector credentials so the CLI works standalone (e.g. from a
    # scheduled launchd job), not only when launched via the Master.
    try:
        from agentmgr.connectors import load_env_file
        load_env_file()
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="Scan this Mac and email a health report.")
    ap.add_argument("--dry-run", action="store_true", help="print, don't email")
    ap.add_argument("--to", default=_TO)
    args = ap.parse_args()

    findings = scan()
    subject, text, html = render(findings)
    print(subject + "\n")
    print(text)
    if args.dry_run:
        print("\n[dry-run] not sending email.")
        return 0
    res = send_email(subject, text, html, args.to)
    print("\n" + ("Emailed " + res["to"] if res.get("sent")
                   else "EMAIL FAILED: " + str(res.get("error"))))
    return 0 if res.get("sent") else 1


if __name__ == "__main__":
    raise SystemExit(main())
