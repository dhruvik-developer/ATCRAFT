"""Typed outbound-mail settings and production fail-closed behaviour."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.config import EmailProvider, Settings, SmtpSecurity

pytestmark = pytest.mark.unit


def _production_settings(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "app_env": "production",
        "jwt_secret": "x" * 64,
        "credential_encryption_key": "y" * 64,
        "app_debug": False,
        "cors_origins": "https://erp.example.com",
        "allowed_hosts": "erp.example.com",
        "public_web_url": "https://erp.example.com",
        "email_provider": "smtp",
        "email_from_address": "no-reply@example.com",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_security": "starttls",
        "smtp_username": "mailer",
        "smtp_password": "not-a-real-password",
    }
    values.update(overrides)
    return values


def test_production_refuses_disabled_email_delivery() -> None:
    with pytest.raises(ValidationError, match="EMAIL_PROVIDER"):
        Settings(
            _env_file=None,
            **_production_settings(email_provider="disabled"),
        )


def test_production_accepts_complete_typed_smtp_configuration() -> None:
    configured = Settings(_env_file=None, **_production_settings())

    assert configured.email_provider is EmailProvider.smtp
    assert configured.smtp_security is SmtpSecurity.starttls
    assert configured.smtp_password.get_secret_value() == "not-a-real-password"
    assert "not-a-real-password" not in repr(configured)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"smtp_host": ""}, "SMTP_HOST"),
        ({"email_from_address": ""}, "EMAIL_FROM_ADDRESS"),
        ({"public_web_url": "http://erp.example.com"}, "PUBLIC_WEB_URL"),
        ({"smtp_password": ""}, "configured together"),
    ],
)
def test_production_refuses_incomplete_mail_configuration(
    override: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None, **_production_settings(**override))


def test_local_and_test_processes_may_deliberately_disable_mail() -> None:
    configured = Settings(
        _env_file=None,
        app_env="test",
        jwt_secret="test-secret",
        email_provider="disabled",
        email_from_address="",
    )

    assert configured.email_provider is EmailProvider.disabled
    assert configured.email_from_address is None


def test_staging_refuses_plaintext_smtp() -> None:
    with pytest.raises(ValidationError, match="STARTTLS or TLS"):
        Settings(
            _env_file=None,
            **_production_settings(
                app_env="staging",
                smtp_security="none",
            ),
        )
