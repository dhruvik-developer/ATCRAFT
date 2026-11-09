"""One defect, one traceback - in the API and in a worker.

An unhandled error in a request used to print its traceback three times: the
access log's ``request.failed``, the final handler's ``error.unhandled``, and -
because Starlette re-raises after answering 500 - uvicorn's own "Exception in
ASGI application". A failing report task did the same through its own
``*_failed`` line, ``task.failed`` and Celery's trace line, and a deliberate
refusal inside a task was logged as an ERROR three times over.

What is counted is what the application's handler printed, read at the moment
it printed it: a traceback logged with ``exc_info=True`` is resolved then,
inside the ``except`` block, and cannot be rendered again afterwards.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from billiard.einfo import ExceptionInfo
from celery import states
from celery.app import trace as celery_trace
from celery.app.task import Context
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import Response
from starlette.routing import Route

from src.core.errors import BusinessRuleError
from src.core.exception_handlers import unhandled_exception_handler
from src.core.logging import SERVER_ERROR_LOGGER, mark_traceback_logged
from src.core.middleware import RequestContextMiddleware
from src.workers import celery_app as worker

pytestmark = pytest.mark.unit

TRACEBACK = "Traceback (most recent call last)"
ASGI_FAILURE = "Exception in ASGI application"


class _Logged(logging.Handler):
    """Every record that reaches the root, beside what the application printed."""

    def __init__(self, capsys: pytest.CaptureFixture[str]) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self._capsys = capsys
        self._out = ""

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def output(self) -> str:
        self._out += self._capsys.readouterr().out
        return self._out

    def tracebacks(self) -> int:
        return self.output().count(TRACEBACK)

    def events(self) -> list[str]:
        """Each structured event's name, and each foreign record's message."""
        names = []
        for record in self.records:
            if isinstance(record.msg, dict):
                names.append(str(record.msg.get("event")))
            else:
                names.append(record.getMessage())
        return names


@pytest.fixture
def logged(capsys: pytest.CaptureFixture[str]) -> Iterator[_Logged]:
    root = logging.getLogger()
    captured = _Logged(capsys)
    root.addHandler(captured)
    trace_logger = logging.getLogger(worker.CELERY_TRACE_LOGGER)
    worker.filter_celery_trace()
    try:
        yield captured
    finally:
        trace_logger.removeFilter(worker._TRACE_FILTER)
        root.removeHandler(captured)


# -----------------------------------------------------------------------------
# The API
# -----------------------------------------------------------------------------


def _application() -> Starlette:
    async def boom(_request: Any) -> Response:
        raise RuntimeError("the defect")

    return Starlette(
        routes=[Route("/boom", boom)],
        middleware=[Middleware(RequestContextMiddleware)],
        exception_handlers={Exception: unhandled_exception_handler},
    )


async def _serve_like_uvicorn(app: Starlette) -> list[dict[str, Any]]:
    """Call the application exactly as uvicorn's ``run_asgi`` does."""
    sent: list[dict[str, Any]] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/boom",
        "raw_path": b"/boom",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 50000),
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    try:
        await app(scope, receive, send)
    except BaseException as exc:
        logging.getLogger(SERVER_ERROR_LOGGER).error(f"{ASGI_FAILURE}\n", exc_info=exc)
    return sent


async def test_an_unhandled_api_error_prints_one_traceback(logged: _Logged) -> None:
    sent = await _serve_like_uvicorn(_application())

    assert sent[0]["status"] == 500
    assert logged.tracebacks() == 1
    assert "RuntimeError: the defect" in logged.output()
    events = logged.events()
    assert "error.unhandled" in events
    assert "request.failed" in events
    assert not any(ASGI_FAILURE in event for event in events)


def test_the_server_still_logs_an_exception_the_application_never_handled(
    logged: _Logged,
) -> None:
    """The handler itself failing, a lifespan error: uvicorn's line is all there is."""
    handled, unhandled = RuntimeError("handled"), RuntimeError("never handled")
    mark_traceback_logged(handled)
    server = logging.getLogger(SERVER_ERROR_LOGGER)

    for exc in (handled, unhandled):
        try:
            raise exc
        except RuntimeError as raised:
            server.error(f"{ASGI_FAILURE}\n", exc_info=raised)

    assert logged.tracebacks() == 1
    assert "RuntimeError: never handled" in logged.output()
    assert "RuntimeError: handled" not in logged.output()


def test_a_traceback_logged_after_its_except_block_is_still_printed(logged: _Logged) -> None:
    """asyncio reports "Task exception was never retrieved" outside any ``except``.

    The redaction step used to turn the record's ``exc_info`` tuple into a list,
    and structlog then looked for the exception being handled at format time -
    none - and printed the line without its traceback.
    """
    try:
        raise RuntimeError("never retrieved")
    except RuntimeError as exc:
        failure = exc

    logging.getLogger("asyncio").error(
        "Task exception was never retrieved",
        exc_info=(type(failure), failure, failure.__traceback__),
    )

    assert logged.tracebacks() == 1
    assert "RuntimeError: never retrieved" in logged.output()


# -----------------------------------------------------------------------------
# A worker
# -----------------------------------------------------------------------------


def _fail_like_celery(task_name: str, exc: Exception) -> None:
    """The failure path Celery runs: the task-failure signal, then its trace line."""
    task = worker.celery_app.tasks[task_name]
    try:
        raise exc
    except Exception:
        einfo = ExceptionInfo()
        worker._on_task_failure(task_id="task-1", exception=exc, sender=task)
        celery_trace.TraceInfo(states.FAILURE, exc)._log_error(
            task, Context(hostname="worker-1", id="task-1", args=[], kwargs={}), einfo
        )


def test_a_failing_report_task_prints_one_traceback(logged: _Logged) -> None:
    from src.workers.tasks import reports

    exc = RuntimeError("the renderer ran out of memory")
    reports._log_job_failure("reports.generate_failed", exc, snapshot_id="snapshot-1")
    _fail_like_celery("reports.generate", exc)

    assert logged.tracebacks() == 1
    assert "RuntimeError: the renderer ran out of memory" in logged.output()
    events = logged.events()
    assert "reports.generate_failed" in events
    assert "task.failed" in events


def test_a_refusal_inside_a_report_task_is_never_an_error(logged: _Logged) -> None:
    """The job ran into a rule; its sentence is on the job row for the person."""
    from src.workers.tasks import reports

    exc = BusinessRuleError("This job asks for an unsupported file format: 'csv'.")
    reports._log_job_failure("reports.generate_failed", exc, snapshot_id="snapshot-1")
    _fail_like_celery("reports.generate", exc)

    assert [r.levelname for r in logged.records if r.levelno >= logging.ERROR] == []
    assert logged.tracebacks() == 0
    assert "task.refused" in logged.events()
    assert "BUSINESS_RULE_VIOLATION" in logged.output()
    assert "raised expected" in logged.output()


@pytest.mark.parametrize("task_name", ["mrp.run", "reports.generate", "maintenance.expire_batches"])
def test_every_task_expects_an_application_refusal(task_name: str) -> None:
    import src.workers.tasks.maintenance
    import src.workers.tasks.mrp
    import src.workers.tasks.reports  # noqa: F401 - registers the tasks

    assert worker.celery_app.tasks[task_name].throws == (worker.AppError,)
