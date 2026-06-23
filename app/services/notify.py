"""Email notifications for backup outcomes.

Sends a success / failure email after each backup (scheduled or manual) using
plain SMTP from the stdlib — no extra dependencies. Fully side-effect isolated:
every error is swallowed and written to the project log, so a flaky mailserver
can never fail a backup.

Config (env vars, set in Coolify):
  SMTP_HOST       SMTP server host. Empty  -> notifications disabled (no-op).
  SMTP_PORT       SMTP port (default 587).
  SMTP_USER       SMTP username (optional; omit for an unauthenticated relay).
  SMTP_PASSWORD   SMTP password.
  SMTP_FROM       From address (default: SMTP_USER).
  SMTP_SECURITY   starttls (default) | ssl | none.
  NOTIFY_ON       all (default) | failure | success.
  NOTIFY_EMAIL    Global fallback recipients (comma-separated), used only when a
                  project has no notify_email of its own.

Per-project recipients come from project['notify_email'] (comma-separated) and
take precedence over the global NOTIFY_EMAIL fallback.

Public surface used by main.py:
  - send_backup_result(project, success, output) -> None
"""
import os
import socket
import smtplib
from datetime import datetime
from email.message import EmailMessage

from app.services import engine


def _recipients(project: dict) -> list[str]:
    """Per-project notify_email, falling back to the global NOTIFY_EMAIL env."""
    raw = (project.get("notify_email") or "").strip() or os.environ.get("NOTIFY_EMAIL", "")
    return [addr.strip() for addr in raw.split(",") if addr.strip()]


def _should_send(success: bool) -> bool:
    """Honour NOTIFY_ON: all (default) | failure | success."""
    mode = os.environ.get("NOTIFY_ON", "all").strip().lower()
    if mode == "failure":
        return not success
    if mode == "success":
        return success
    return True


def _build_message(project: dict, success: bool, output: str,
                   recipients: list[str], sender: str) -> EmailMessage:
    status = "OK" if success else "FAILED"
    ts     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    host   = socket.gethostname()
    name   = project.get("name", project.get("id", "unknown"))

    body = (
        f"Backup {status}\n"
        f"{'=' * 40}\n"
        f"Project : {name}  ({project.get('id', '')})\n"
        f"Engine  : {project.get('db_engine', '')}\n"
        f"When    : {ts}\n"
        f"Host    : {host}\n"
        f"{'=' * 40}\n\n"
        f"{(output or '').strip()[:4000]}\n"
    )

    msg = EmailMessage()
    msg["Subject"] = f"[Backup {status}] {name}"
    msg["From"]    = sender
    msg["To"]      = ", ".join(recipients)
    msg.set_content(body)
    return msg


def _smtp_send(msg: EmailMessage, recipients: list[str]) -> None:
    host     = os.environ.get("SMTP_HOST", "").strip()
    port     = int(os.environ.get("SMTP_PORT", "587"))
    user     = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    security = os.environ.get("SMTP_SECURITY", "starttls").strip().lower()

    if security == "ssl":
        client = smtplib.SMTP_SSL(host, port, timeout=15)
    else:
        client = smtplib.SMTP(host, port, timeout=15)
    try:
        if security == "starttls":
            client.starttls()
        if user:
            client.login(user, password)
        client.send_message(msg, to_addrs=recipients)
    finally:
        client.quit()


def send_backup_result(project: dict, success: bool, output: str) -> None:
    """Send a success/failure email for one backup. Never raises.

    No-op when SMTP_HOST is unset, when NOTIFY_ON filters this outcome out, or
    when neither the project nor NOTIFY_EMAIL supplies a recipient.
    """
    if not os.environ.get("SMTP_HOST", "").strip():
        return
    if not _should_send(success):
        return

    recipients = _recipients(project)
    if not recipients:
        return

    sender = os.environ.get("SMTP_FROM", "").strip() or os.environ.get("SMTP_USER", "backups@localhost")
    try:
        msg = _build_message(project, success, output, recipients, sender)
        _smtp_send(msg, recipients)
        _log(project, f"INFO: Notification email sent to {', '.join(recipients)}")
    except Exception as e:
        _log(project, f"WARN: Notification email failed: {e}")


def _log(project: dict, message: str) -> None:
    """Best-effort write to the project log; never raises into the caller."""
    try:
        engine.append_log(engine.get_log_file(project), message)
    except Exception:
        pass
