"""The notification filter vocabulary must match the backend enum."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.modules.notifications.models import NotificationType

pytestmark = pytest.mark.unit

FRONTEND_TYPES = (
    Path(__file__).resolve().parents[3]
    / "web"
    / "src"
    / "features"
    / "notifications"
    / "notification-types.ts"
)
_LIST = re.compile(
    r"export const NOTIFICATION_TYPES = \[(.*?)\] as const",
    re.DOTALL,
)
_ENTRY = re.compile(r"'([A-Z][A-Z_]+)'")


def _frontend_types() -> set[str]:
    if not FRONTEND_TYPES.exists():
        pytest.fail(f"{FRONTEND_TYPES} not found - the guarded list has moved")
    text = FRONTEND_TYPES.read_text(encoding="utf-8")
    match = _LIST.search(text)
    if match is None:
        pytest.fail(f"NOTIFICATION_TYPES declaration not found in {FRONTEND_TYPES.name}")
    found = set(_ENTRY.findall(match.group(1)))
    if not found:
        pytest.fail("No entries parsed from the NOTIFICATION_TYPES declaration")
    return found


def test_every_backend_notification_type_is_in_the_frontend() -> None:
    missing = sorted({item.value for item in NotificationType} - _frontend_types())
    assert not missing, f"Frontend is missing notification types: {', '.join(missing)}"


def test_frontend_does_not_invent_notification_types() -> None:
    invented = sorted(_frontend_types() - {item.value for item in NotificationType})
    assert not invented, f"Frontend has unknown notification types: {', '.join(invented)}"
