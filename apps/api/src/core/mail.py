"""Outbound e-mail construction and SMTP transport.

The API never opens an SMTP connection.  It commits the business transaction
and hands a small job to the notifications Celery queue; workers call this
module to perform the blocking SMTP conversation in a thread.

Message bodies and recipient addresses are intentionally absent from logs.  A
password-reset URL contains a live credential, so callers must log only stable
row identifiers and delivery status, never the message or exception arguments
that may echo SMTP content.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr

from src.core.config import EmailProvider, SmtpSecurity, settings


class MailNotConfiguredError(RuntimeError):
    """Raised when code attempts delivery with the provider disabled."""


@dataclass(frozen=True, slots=True)
class OutboundEmail:
    """A fully rendered message ready for the configured transport."""

    recipient: str
    subject: str
    text_body: str
    html_body: str | None = None
    message_id: str | None = None


def build_message(mail: OutboundEmail) -> EmailMessage:
    """Build a standards-compliant multipart message without logging content."""
    if settings.email_from_address is None:
        raise MailNotConfiguredError("EMAIL_FROM_ADDRESS is not configured")

    message = EmailMessage()
    message["From"] = formataddr((settings.email_from_name, str(settings.email_from_address)))
    message["To"] = mail.recipient
    message["Subject"] = mail.subject
    if mail.message_id:
        message["Message-ID"] = mail.message_id
    message.set_content(mail.text_body)
    if mail.html_body is not None:
        message.add_alternative(mail.html_body, subtype="html")
    return message


def _send_smtp(mail: OutboundEmail) -> None:
    """Perform one blocking SMTP delivery.

    Connection and authentication failures deliberately propagate.  Celery's
    retry policy owns backoff; swallowing an error here would acknowledge and
    permanently lose a reset message.
    """
    if settings.email_provider is not EmailProvider.smtp:
        raise MailNotConfiguredError("Outbound e-mail is disabled")

    message = build_message(mail)
    context = ssl.create_default_context()
    password = settings.smtp_password.get_secret_value()

    if settings.smtp_security is SmtpSecurity.tls:
        with smtplib.SMTP_SSL(
            settings.smtp_host,
            settings.smtp_port,
            timeout=settings.smtp_timeout_seconds,
            context=context,
        ) as client:
            if settings.smtp_username:
                client.login(settings.smtp_username, password)
            client.send_message(message)
        return

    with smtplib.SMTP(
        settings.smtp_host,
        settings.smtp_port,
        timeout=settings.smtp_timeout_seconds,
    ) as client:
        client.ehlo()
        if settings.smtp_security is SmtpSecurity.starttls:
            client.starttls(context=context)
            client.ehlo()
        if settings.smtp_username:
            client.login(settings.smtp_username, password)
        client.send_message(message)


async def deliver_email(mail: OutboundEmail) -> None:
    """Deliver without blocking the Celery worker's asyncio event loop."""
    await asyncio.to_thread(_send_smtp, mail)


__all__ = [
    "MailNotConfiguredError",
    "OutboundEmail",
    "build_message",
    "deliver_email",
]
