"""Celery's trace logger, captured as the worker's own logging setup filters it.

``trace`` hangs a handler on the trace logger and applies the filter
``filter_celery_trace`` installs, and ``_Captured.lines()`` renders each record
through the application's own structlog formatter - so a test reads the line an
operator would read. The mail-task failure suite and the database-error
redaction suite both assert on those lines; the fixture lives here so neither
is the other's library.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
import structlog

from src.workers import celery_app as worker


class _Captured(logging.Handler):
    """Every record Celery's trace logger writes, and the line each became."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def lines(self) -> list[str]:
        """Each record as the application's own handler renders it.

        pytest adds its capture handlers beside it; the application's is the
        one ``configure_logging`` gave structlog's formatter.
        """
        (formatter,) = (
            handler.formatter
            for handler in logging.getLogger().handlers
            if isinstance(handler.formatter, structlog.stdlib.ProcessorFormatter)
        )
        return [formatter.format(record) for record in self.records]


@pytest.fixture
def trace() -> Iterator[_Captured]:
    """Celery's trace logger, filtered as the worker's logging setup filters it."""
    trace_logger = logging.getLogger(worker.CELERY_TRACE_LOGGER)
    captured = _Captured()
    level = trace_logger.level
    trace_logger.setLevel(logging.DEBUG)
    trace_logger.addHandler(captured)
    worker.filter_celery_trace()
    try:
        yield captured
    finally:
        trace_logger.removeFilter(worker._TRACE_FILTER)
        trace_logger.removeHandler(captured)
        trace_logger.setLevel(level)


__all__ = ["_Captured", "trace"]
