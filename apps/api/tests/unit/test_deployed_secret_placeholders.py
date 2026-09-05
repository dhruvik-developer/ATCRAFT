"""A deployed process must refuse every secret that ships in a template.

The production guard once listed the JWT placeholder from ``.env.example``
verbatim and nothing else. The CREDENTIAL_ENCRYPTION_KEY placeholder in the
same file was 52 characters, spelled differently, and not equal to the JWT
one - so it passed all three checks, and a ``.env.production`` built from the
template with only JWT_SECRET regenerated booted with an encryption key anyone
holding the repository could read.

These tests read the templates at test time rather than hard-coding the
placeholder strings: a re-worded placeholder is then caught the day it is
written, which is exactly how the previous one slipped past.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.core.config import (
    MIN_DEPLOYED_SECRET_LENGTH,
    REPO_ROOT,
    AppEnv,
    Settings,
    is_placeholder_secret,
)

pytestmark = pytest.mark.unit

SECRET_KEYS = ("JWT_SECRET", "CREDENTIAL_ENCRYPTION_KEY")

TEMPLATES = [REPO_ROOT / ".env.example", *sorted((REPO_ROOT / "deploy").glob("*.env.example"))]


def _template_values(path: Path) -> dict[str, str]:
    """``KEY=value`` pairs for the two secret keys, as an operator would copy them."""
    found: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() in SECRET_KEYS:
            found[key.strip()] = value.split(" #", 1)[0].strip().strip('"').strip("'")
    return found


def _deployed(**overrides: object) -> Settings:
    common: dict[str, object] = {
        "app_env": AppEnv.production,
        "jwt_secret": "s" * 64,
        "credential_encryption_key": "e" * 64,
        "cors_origins": "https://erp.acme.test",
        "allowed_hosts": "erp.acme.test",
        "email_provider": "smtp",
        "public_web_url": "https://erp.acme.test",
        "email_from_address": "no-reply@acme.com",
        "smtp_host": "smtp.acme.test",
    }
    common.update(overrides)
    return Settings(_env_file=None, **common)  # type: ignore[arg-type]


def _placeholders() -> list[tuple[str, str, str]]:
    """Every non-empty secret value in every template, as (file, key, value)."""
    cases: list[tuple[str, str, str]] = []
    for path in TEMPLATES:
        for key, value in _template_values(path).items():
            if value:
                cases.append((path.name, key, value))
    return cases


def test_every_template_declares_both_secrets() -> None:
    assert TEMPLATES, "no templates found under the repository root"
    for path in TEMPLATES:
        values = _template_values(path)
        assert set(values) == set(SECRET_KEYS), f"{path.name} does not declare both secret keys"


@pytest.mark.parametrize(
    ("template", "key", "value"),
    _placeholders(),
    ids=[f"{t}:{k}" for t, k, _ in _placeholders()],
)
def test_a_template_placeholder_is_refused_as_either_secret(
    template: str, key: str, value: str
) -> None:
    # As the signing key, and as the encryption key: the guard covers both.
    with pytest.raises(ValidationError, match="JWT_SECRET must be a strong random value"):
        _deployed(jwt_secret=value)
    with pytest.raises(
        ValidationError, match="CREDENTIAL_ENCRYPTION_KEY must be a strong random value"
    ):
        _deployed(credential_encryption_key=value)


def test_deploy_templates_leave_the_secrets_for_init_to_generate() -> None:
    """``scripts/deploy.sh <name> init`` fills these in; the template must not."""
    for path in TEMPLATES:
        if path.parent.name != "deploy":
            continue
        for key, value in _template_values(path).items():
            assert value == "", f"{path.name} ships a value for {key}"
            assert is_placeholder_secret(value)


def test_the_exact_placeholder_that_shipped_is_refused() -> None:
    """The regression, pinned by value in case the template is ever cleaned up."""
    assert is_placeholder_secret("CHANGE-ME-generate-a-different-64-byte-random-secret")
    assert is_placeholder_secret("CHANGE-ME-generate-a-64-byte-random-secret")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "secret",
        "password",
        "change-me",
        "changeme",
        "x" * (MIN_DEPLOYED_SECRET_LENGTH - 1),
        "please-Generate-A-real-key-before-you-deploy-this-anywhere",
        "PLACEHOLDER-" + "0" * 40,
    ],
)
def test_placeholder_detection_covers_short_weak_and_marked_values(value: str) -> None:
    assert is_placeholder_secret(value)


def test_a_generated_value_is_accepted() -> None:
    import secrets

    generated = secrets.token_urlsafe(64)
    assert not is_placeholder_secret(generated)
    config = _deployed(jwt_secret=generated, credential_encryption_key=secrets.token_urlsafe(64))
    assert config.is_deployed


def test_staging_is_guarded_the_same_way_as_production() -> None:
    with pytest.raises(ValidationError, match="not a template placeholder"):
        _deployed(
            app_env=AppEnv.staging,
            credential_encryption_key="CHANGE-ME-generate-a-different-64-byte-random-secret",
        )
