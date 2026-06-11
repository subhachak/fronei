"""Lightweight outbound email for admin notifications (e.g. new signups).

Uses stdlib smtplib only — no extra dependencies. If SMTP isn't configured
(SMTP_HOST/SMTP_USER/SMTP_PASSWORD), notifications are logged instead of
sent, so this is safe to call in any environment.
"""
from __future__ import annotations

import logging
import smtplib
from email.mime.text import MIMEText

from app.config import get_settings

logger = logging.getLogger(__name__)


def send_admin_notification(subject: str, body: str) -> None:
    settings = get_settings()
    recipients = settings.notification_email_list
    if not recipients:
        logger.info("No notification recipients configured. Subject=%r", subject)
        return

    if not settings.smtp_host or not settings.smtp_user or not settings.smtp_password:
        logger.warning(
            "SMTP not configured — skipping email. Subject=%r Body=%r Recipients=%s",
            subject, body, recipients,
        )
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = ", ".join(recipients)

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
            server.starttls()
            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(msg["From"], recipients, msg.as_string())
        logger.info("Sent admin notification %r to %s", subject, recipients)
    except Exception:
        logger.exception("Failed to send admin notification email (subject=%r)", subject)


def notify_new_signup(user_id: str, email: str | None, name: str | None) -> None:
    settings = get_settings()
    subject = f"[Fronei] New signup pending approval: {email or user_id}"
    body = (
        f"A new user signed up and is pending approval.\n\n"
        f"User ID: {user_id}\n"
        f"Email: {email or '(none)'}\n"
        f"Name: {name or '(none)'}\n\n"
        f"Activate them in the Admin > Users panel "
        f"({'https://www.fronei.com' if settings.is_production else 'your app URL'}/?view=admin) "
        f"by setting their status to Active."
    )
    send_admin_notification(subject, body)
