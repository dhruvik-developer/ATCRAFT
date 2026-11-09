"""Every test logs through the application's pipeline, never structlog's defaults.

The application installs ``configure_logging`` from its lifespan, and no test
runs a lifespan - httpx's ASGI transport never executes one - so
``tests/conftest.py`` installs it for the whole session. Under structlog's
defaults an exception is drawn as a rich traceback with every frame's locals,
the whole FastAPI application among them: seconds of CPU for each unhandled
error a test drives through the application, where the configured pipeline
renders it as plain text first.
"""

from __future__ import annotations

import sys

import pytest
import structlog

from src.core.logging import get_logger

pytestmark = pytest.mark.unit

#: A frame local's value, which only a renderer that prints locals shows.
SENTINEL = "sentinellocalvalue"


def _raise_holding_a_local() -> None:
    held = SENTINEL  # noqa: F841 - the local is the point: only show_locals prints it
    raise RuntimeError("probe failure")


def test_the_session_logs_through_the_application_pipeline() -> None:
    assert structlog.is_configured()
    processors = structlog.get_config()["processors"]
    assert processors[-1] is structlog.stdlib.ProcessorFormatter.wrap_for_formatter


def test_a_logged_traceback_is_plain_text_on_the_tests_own_stdout(capsys) -> None:
    try:
        _raise_holding_a_local()
    except RuntimeError:
        get_logger("tests.session_logging").exception("probe.failed")

    out = capsys.readouterr().out
    assert "probe.failed" in out
    assert "Traceback (most recent call last)" in out
    assert "RuntimeError: probe failure" in out
    assert SENTINEL not in out


def test_the_defaults_print_every_frames_locals() -> None:
    """The check above is not vacuous: structlog's default renderer shows the local."""
    try:
        _raise_holding_a_local()
    except RuntimeError:
        rendered = structlog.dev.ConsoleRenderer(colors=False)(
            None, "error", {"event": "probe.failed", "exc_info": sys.exc_info()}
        )

    assert SENTINEL in rendered
