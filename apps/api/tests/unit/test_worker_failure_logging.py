"""What the task-failure signal - and Celery's own trace - write for a mail task."""

from __future__ import annotations

import logging
import smtplib
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from billiard.einfo import ExceptionInfo
from celery import states
from celery.app import trace as celery_trace
from celery.app.task import Context
from celery.exceptions import Retry

from src.workers import celery_app as worker

pytestmark = pytest.mark.unit

#: Who the relay refused. It must never reach a log line or a stored result.
RECIPIENT = "person@example.com"


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def error(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    log = _Recorder()
    monkeypatch.setattr(worker, "log", log)
    return log


def test_a_mail_tasks_transport_failure_is_logged_by_type_only(recorder: _Recorder) -> None:
    """SMTPRecipientsRefused is keyed by recipient address and carries the
    relay's reply - exactly what core.mail forbids from the log."""
    refused = smtplib.SMTPRecipientsRefused({"person@example.com": (550, b"User unknown")})

    worker._on_task_failure(
        task_id="t-1",
        exception=refused,
        sender=SimpleNamespace(name="notifications.send_password_reset_email"),
    )

    ((event, fields),) = recorder.events
    assert event == "task.failed"
    assert fields == {
        "task": "notifications.send_password_reset_email",
        "task_id": "t-1",
        "error_type": "SMTPRecipientsRefused",
    }
    assert "person@example.com" not in repr(recorder.events)


def test_a_socket_failure_in_a_mail_task_is_confidential_too(recorder: _Recorder) -> None:
    worker._on_task_failure(
        task_id="t-2",
        exception=OSError("[Errno 111] Connection refused to smtp.example.com"),
        sender=SimpleNamespace(name="notifications.send_email"),
    )

    ((_, fields),) = recorder.events
    assert "error" not in fields
    assert "exc_info" not in fields
    assert fields["error_type"] == "OSError"


def test_a_programming_error_in_a_mail_task_keeps_its_traceback(recorder: _Recorder) -> None:
    boom = KeyError("company_id")

    worker._on_task_failure(
        task_id="t-3", exception=boom, sender=SimpleNamespace(name="notifications.send_email")
    )

    ((_, fields),) = recorder.events
    assert fields["exc_info"] is boom
    assert fields["error_type"] == "KeyError"


def test_every_other_task_keeps_the_full_detail(recorder: _Recorder) -> None:
    boom = RuntimeError("explosion failed at level 3")

    worker._on_task_failure(task_id="t-4", exception=boom, sender=SimpleNamespace(name="mrp.run"))

    ((_, fields),) = recorder.events
    assert fields["error"] == "explosion failed at level 3"
    assert fields["exc_info"] is boom
    assert fields["error_type"] == "RuntimeError"


def test_worker_ready_starts_the_aggregated_metrics_exporter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(worker, "_worker_metrics_started", False)
    monkeypatch.setattr(worker.settings, "prometheus_metrics_enabled", True)
    monkeypatch.setattr(worker.settings, "prometheus_worker_metrics_address", "0.0.0.0")
    monkeypatch.setattr(worker, "metrics_registry", lambda: "registry")
    monkeypatch.setattr(
        worker,
        "start_http_server",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    worker._start_worker_metrics_exporter()

    assert calls == [
        (
            (worker.WORKER_METRICS_PORT,),
            {"addr": "0.0.0.0", "registry": "registry"},
        )
    ]
    assert worker._worker_metrics_started


def test_the_mail_task_set_names_every_registered_mail_task() -> None:
    from src.workers.tasks import notifications

    registered = {
        getattr(task, "name", None)
        for task in (
            notifications.send_email,
            notifications.send_password_reset_email,
            notifications.dispatch_pending_password_resets,
            notifications.send_push,
            notifications.send_whatsapp,
            notifications.send_sms,
            notifications.dispatch_pending_deliveries,
        )
    }
    assert registered == worker.MAIL_TASKS


# -----------------------------------------------------------------------------
# Celery's own trace lines
# -----------------------------------------------------------------------------


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


def _refused() -> smtplib.SMTPRecipientsRefused:
    return smtplib.SMTPRecipientsRefused({RECIPIENT: (550, b"5.1.1 User unknown")})


def _request() -> Context:
    return Context(hostname="worker-1", id="task-1", args=[], kwargs={})


def _retry_line(task_name: str) -> None:
    """What ``handle_retry`` writes when autoretry re-queues a refused send."""
    task = worker.celery_app.tasks[task_name]
    retry = Retry(exc=_refused(), when=4)
    try:
        raise retry
    except Retry:
        celery_trace.TraceInfo(states.RETRY, retry).handle_retry(
            task, _request(), store_errors=False
        )


def _failure_line(task_name: str) -> None:
    """What ``handle_failure`` writes once the retries are spent."""
    task = worker.celery_app.tasks[task_name]
    refused = _refused()
    try:
        raise refused
    except smtplib.SMTPRecipientsRefused:
        einfo = ExceptionInfo()
        celery_trace.TraceInfo(states.FAILURE, refused)._log_error(task, _request(), einfo)


def _mail_task_names() -> list[str]:
    import src.workers.tasks.notifications  # noqa: F401 - registers the tasks

    return sorted(worker.MAIL_TASKS)


@pytest.mark.parametrize("task_name", _mail_task_names())
def test_celerys_retry_line_never_names_a_mail_tasks_recipient(
    trace: _Captured, task_name: str
) -> None:
    _retry_line(task_name)

    (record,) = trace.records
    (line,) = trace.lines()
    assert f"{task_name}[task-1] retry: {worker.MAIL_EXCEPTION_WITHHELD}" in line
    assert RECIPIENT not in line
    assert RECIPIENT not in repr(vars(record)), "nor in the record's extras"


@pytest.mark.parametrize("task_name", _mail_task_names())
def test_celerys_failure_line_never_names_a_mail_tasks_recipient(
    trace: _Captured, task_name: str
) -> None:
    _failure_line(task_name)

    (record,) = trace.records
    (line,) = trace.lines()
    assert f"raised unexpected: {worker.MAIL_EXCEPTION_WITHHELD}" in line
    assert RECIPIENT not in line
    assert "Traceback" not in line
    assert record.exc_info is None
    assert RECIPIENT not in repr(vars(record)), "nor in the record's extras"


def test_every_other_tasks_trace_lines_keep_the_exception_but_not_its_traceback(
    trace: _Captured,
) -> None:
    """The control: the lines above would carry the address unfiltered. Only the
    traceback goes - ``task.failed`` has just logged it."""
    import src.workers.tasks.reports  # noqa: F401 - registers the task

    _retry_line("reports.generate")
    _failure_line("reports.generate")

    retry, failure = trace.lines()
    assert RECIPIENT in retry
    assert RECIPIENT in failure
    assert "Traceback" not in failure
    assert trace.records[1].exc_info is None


@pytest.mark.parametrize("task_name", _mail_task_names())
def test_a_mail_task_keeps_no_result_for_its_exception_to_sit_in(task_name: str) -> None:
    """A stored retry or failure is the exception again, in Redis for a day."""
    assert worker.celery_app.tasks[task_name].ignore_result is True


def test_the_worker_logging_setup_installs_the_trace_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace_logger = logging.getLogger(worker.CELERY_TRACE_LOGGER)
    monkeypatch.setattr(trace_logger, "filters", [])
    # The session already logs through the application's pipeline; installing
    # it again here would rebind its handler to this test's stdout.
    monkeypatch.setattr(worker, "configure_logging", lambda: None)

    worker._configure_worker_logging()
    worker._configure_worker_logging()

    assert trace_logger.filters == [worker._TRACE_FILTER]
