"""Outgoing email over SMTP (the failure report).

Synchronous (``smtplib``); callers use ``asyncio.to_thread``. Port 465 uses
implicit TLS, anything else STARTTLS. TLS verifies against certifi's CA bundle,
like the rest of the app.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

import certifi

from clinic_agent.config import settings

TIMEOUT_SECONDS = 30


class EmailNotConfigured(Exception):
    """SMTP settings are missing; retrying won't help."""


def send_email(to: str, subject: str, text: str, html: str | None = None) -> None:
    """Send one message (plain text, plus an HTML alternative if given). Raises on failure."""
    if not settings.SMTP_HOST:
        raise EmailNotConfigured("SMTP_HOST is not set")
    sender = settings.SMTP_FROM or settings.SMTP_USERNAME
    if not sender:
        raise EmailNotConfigured("set SMTP_FROM or SMTP_USERNAME")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    context = ssl.create_default_context(cafile=certifi.where())
    if settings.SMTP_PORT == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(
            settings.SMTP_HOST, settings.SMTP_PORT, timeout=TIMEOUT_SECONDS, context=context
        )
    else:
        server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=TIMEOUT_SECONDS)
    with server:
        if settings.SMTP_PORT != 465:
            server.starttls(context=context)
        if settings.SMTP_USERNAME:
            server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        server.send_message(msg)
