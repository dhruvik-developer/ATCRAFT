"""SMTP transport selection without making a network connection."""

from __future__ import annotations

from email.message import EmailMessage
from typing import Any

import pytest

from src.core import mail as mail_module
from src.core.config import EmailProvider, SmtpSecurity, settings
from src.core.mail import OutboundEmail, build_message

pytestmark = pytest.mark.unit


class _FakeSmtp:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.events: list[object] = []

    def __enter__(self) -> _FakeSmtp:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def ehlo(self) -> None:
        self.events.append("ehlo")

    def starttls(self, **kwargs: Any) -> None:
        self.events.append(("starttls", kwargs))

    def login(self, username: str, password: str) -> None:
        self.events.append(("login", username, password))

    def send_message(self, message: EmailMessage) -> None:
        self.events.append(("send", message))


@pytest.fixture
def smtp_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "email_provider", EmailProvider.smtp)
    monkeypatch.setattr(settings, "email_from_address", "no-reply@example.com")
    monkeypatch.setattr(settings, "email_from_name", "ATCRAFT")
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_username", "mailer")
    monkeypatch.setattr(settings, "smtp_password", settings.smtp_password.__class__("secret"))
    monkeypatch.setattr(settings, "smtp_timeout_seconds", 9)


def test_build_message_contains_text_and_html_without_mutating_content(
    smtp_settings: None,
) -> None:
    message = build_message(
        OutboundEmail(
            recipient="person@example.com",
            subject="Account notice",
            text_body="Plain text",
            html_body="<p>HTML</p>",
            message_id="<notice.1@example.com>",
        )
    )

    assert message["From"] == "ATCRAFT <no-reply@example.com>"
    assert message["To"] == "person@example.com"
    assert message["Message-ID"] == "<notice.1@example.com>"
    assert message.is_multipart()
    assert "Plain text" in message.get_body(preferencelist=("plain",)).get_content()


def test_starttls_is_negotiated_before_authentication_and_send(
    smtp_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients: list[_FakeSmtp] = []

    def factory(*args: Any, **kwargs: Any) -> _FakeSmtp:
        client = _FakeSmtp(*args, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(settings, "smtp_security", SmtpSecurity.starttls)
    monkeypatch.setattr(mail_module.smtplib, "SMTP", factory)

    mail_module._send_smtp(OutboundEmail("person@example.com", "Subject", "Body"))

    client = clients[0]
    assert client.args == ("smtp.example.com", 587)
    assert client.kwargs["timeout"] == 9
    assert client.events[0] == "ehlo"
    assert client.events[1][0] == "starttls"
    assert client.events[2] == "ehlo"
    assert client.events[3] == ("login", "mailer", "secret")
    assert client.events[4][0] == "send"


def test_implicit_tls_uses_smtp_ssl_without_starttls(
    smtp_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients: list[_FakeSmtp] = []

    def factory(*args: Any, **kwargs: Any) -> _FakeSmtp:
        client = _FakeSmtp(*args, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(settings, "smtp_security", SmtpSecurity.tls)
    monkeypatch.setattr(mail_module.smtplib, "SMTP_SSL", factory)

    mail_module._send_smtp(OutboundEmail("person@example.com", "Subject", "Body"))

    assert clients[0].events[0] == ("login", "mailer", "secret")
    assert all(event != "ehlo" for event in clients[0].events)
    assert all(
        not isinstance(event, tuple) or event[0] != "starttls" for event in clients[0].events
    )
