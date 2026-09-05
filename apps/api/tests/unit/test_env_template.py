"""The environment template lists every setting, and nothing that is not one.

``.env.example`` once carried ``REDIS_HOST``, ``REDIS_CACHE_DB``,
``PASSWORD_HASH_SCHEME`` and ``VITE_APP_NAME``, which nothing read: ``Settings``
uses ``extra="ignore"``, so setting ``PASSWORD_HASH_SCHEME=bcrypt`` had no
effect and no warning. In the other direction it lacked ``JWT_ISSUER`` and
``JWT_AUDIENCE``, which every deployment must set, while the runbook said the
template listed every setting.

Both directions are checked against the code rather than against a hand-written
list: a key is legitimate when it is a ``Settings`` field, a ``${VAR}`` the
local Compose files interpolate, a value the test conftest reads, or a
``VITE_`` key the frontend actually references.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.core.config import API_ROOT, REPO_ROOT, Settings

pytestmark = pytest.mark.unit

TEMPLATE = REPO_ROOT / ".env.example"
COMPOSE_FILES = (REPO_ROOT / "docker-compose.yml", REPO_ROOT / "docker-compose.dev.yml")
TEST_CONFTEST = API_ROOT / "tests" / "conftest.py"
VITE_READERS = (
    REPO_ROOT / "apps" / "web" / "vite.config.ts",
    REPO_ROOT / "apps" / "web" / "src" / "lib" / "constants.ts",
)

_KEY_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=")


def _template_keys(path: Path = TEMPLATE) -> list[str]:
    keys: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = _KEY_LINE.match(raw.strip())
        if match:
            keys.append(match.group(1))
    return keys


def _compose_keys() -> set[str]:
    text = "".join(p.read_text(encoding="utf-8") for p in COMPOSE_FILES)
    return set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", text))


def _conftest_keys() -> set[str]:
    return set(re.findall(r'_cfg\("([A-Z][A-Z0-9_]*)"', TEST_CONFTEST.read_text(encoding="utf-8")))


def _vite_keys() -> set[str]:
    text = "".join(p.read_text(encoding="utf-8") for p in VITE_READERS)
    return set(re.findall(r"\b(VITE_[A-Z0-9_]+)\b", text))


def _settings_keys() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


def test_template_has_no_duplicate_keys() -> None:
    keys = _template_keys()
    assert len(keys) == len(set(keys)), sorted(k for k in keys if keys.count(k) > 1)


def test_every_template_key_is_read_by_something() -> None:
    """A key nothing consumes is a knob that turns silently."""
    readers = _settings_keys() | _compose_keys() | _conftest_keys() | _vite_keys()
    dead = [key for key in _template_keys() if key not in readers]
    assert dead == [], f"unread keys in .env.example: {dead}"


def test_every_setting_has_a_template_line() -> None:
    """The runbook promises the template lists every setting with its purpose."""
    present = set(_template_keys())
    missing = sorted(_settings_keys() - present)
    assert missing == [], f"Settings fields absent from .env.example: {missing}"


def test_template_header_points_at_the_file_the_code_reads() -> None:
    """``config.py`` reads ``.env`` at the repository root and nothing else.

    The header used to say "copy to .env.development / .env.staging /
    .env.production", none of which the API or Compose read, so a developer
    following it booted on pydantic defaults with no error.
    """
    header = "\n".join(TEMPLATE.read_text(encoding="utf-8").splitlines()[:12])
    assert "Copy to .env at the repository root" in header
    for stale in (".env.development", ".env.staging", ".env.production"):
        assert stale not in header
