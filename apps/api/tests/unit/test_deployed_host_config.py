"""Security invariants for production-like host configuration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.config import AppEnv, Settings


def test_allowed_hosts_include_cors_origins_and_internal_probes() -> None:
    config = Settings(
        _env_file=None,
        app_env=AppEnv.staging,
        jwt_secret="s" * 64,
        credential_encryption_key="e" * 64,
        cors_origins="https://erp.acme.test,https://dev.acme.test:8443",
        allowed_hosts="admin.acme.test",
        email_provider="smtp",
        public_web_url="https://erp.acme.test",
        email_from_address="no-reply@acme.com",
        smtp_host="smtp.acme.test",
    )

    assert config.allowed_host_list == [
        "127.0.0.1",
        "admin.acme.test",
        "backend",
        "dev.acme.test",
        "erp.acme.test",
        "localhost",
    ]


def test_deployed_environment_rejects_wildcard_host() -> None:
    with pytest.raises(ValidationError, match="without wildcards"):
        Settings(
            _env_file=None,
            app_env=AppEnv.production,
            jwt_secret="s" * 64,
            credential_encryption_key="e" * 64,
            cors_origins="https://erp.acme.test",
            allowed_hosts="*",
            email_provider="smtp",
            public_web_url="https://erp.acme.test",
            email_from_address="no-reply@acme.com",
            smtp_host="smtp.acme.test",
        )


def test_staging_gets_the_same_secret_guard_as_production() -> None:
    with pytest.raises(ValidationError, match="strong random value"):
        Settings(
            _env_file=None,
            app_env=AppEnv.staging,
            jwt_secret="weak",
            cors_origins="https://dev.acme.test",
            allowed_hosts="dev.acme.test",
        )


@pytest.mark.parametrize(
    "host",
    [
        "https://erp.acme.test",
        "erp.acme.test:443",
        "erp.acme.test/path",
        "bad*pattern.acme.test",
        "*.acme.test",
        "-bad.acme.test",
        "erp.acme.test,,admin.acme.test",
    ],
)
def test_allowed_hosts_rejects_ambiguous_or_invalid_syntax(host: str) -> None:
    with pytest.raises(ValidationError, match=r"bare DNS names|empty entry"):
        Settings(_env_file=None, app_env=AppEnv.test, jwt_secret="test", allowed_hosts=host)


def test_deployed_encryption_key_is_strong_and_independent() -> None:
    common = {
        "app_env": AppEnv.production,
        "jwt_secret": "s" * 64,
        "cors_origins": "https://erp.acme.test",
        "allowed_hosts": "erp.acme.test",
        "email_provider": "smtp",
        "public_web_url": "https://erp.acme.test",
        "email_from_address": "no-reply@acme.com",
        "smtp_host": "smtp.acme.test",
    }
    with pytest.raises(ValidationError, match="CREDENTIAL_ENCRYPTION_KEY"):
        Settings(_env_file=None, **common)
    with pytest.raises(ValidationError, match="independent from JWT_SECRET"):
        Settings(_env_file=None, credential_encryption_key="s" * 64, **common)
