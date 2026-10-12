"""Celery application (master rules section 35).

Celery is the project-standard background job system. Anything that can take
longer than a request should - large MRP runs, exports, PDF and Excel
generation, bulk imports, notification fan-out, scheduled summary refreshes -
runs here rather than holding an HTTP worker open.

Queues are separated by shape, not by module: a 4-minute MRP run must not sit in
front of a 200 ms notification and delay it.
"""

from __future__ import annotations

import asyncio
import contextlib
import smtplib
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from celery import Celery, Task
from celery.signals import setup_logging, task_failure, task_postrun, task_prerun

from src.core.config import settings
from src.core.logging import (
    bind_request_context,
    clear_request_context,
    configure_logging,
    get_logger,
)
from src.core.observability import celery_task_duration, celery_tasks_total

log = get_logger("worker")

celery_app = Celery(
    "atcraft",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "src.workers.tasks.mrp",
        "src.workers.tasks.reports",
        "src.workers.tasks.notifications",
        "src.workers.tasks.maintenance",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Acknowledge only after completion so a worker crash re-delivers the job
    # instead of losing it. Tasks must therefore be idempotent.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Prefetching many long tasks starves other workers; 1 keeps distribution fair.
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=200,  # bounds slow memory growth in long runs
    result_expires=86400,
    task_track_started=True,
    task_time_limit=3600,
    task_soft_time_limit=3300,  # soft first, so a task can clean up and report
    broker_connection_retry_on_startup=True,
    task_default_queue="default",
    task_routes={
        "mrp.*": {"queue": "mrp"},
        "reports.*": {"queue": "reports"},
        "notifications.*": {"queue": "notifications"},
        "maintenance.*": {"queue": "default"},
    },
    beat_schedule={
        "dispatch-pending-deliveries": {
            "task": "notifications.dispatch_pending_deliveries",
            "schedule": 120.0,
            "options": {"queue": "notifications", "expires": 110},
        },
        # Section 25: dashboards read summary tables, not hot transactional ones.
        "refresh-inventory-summary": {
            "task": "maintenance.refresh_summary_views",
            "schedule": 900.0,
            "options": {"queue": "default", "expires": 600},
        },
        # Reservations for cancelled or long-abandoned demand otherwise hold
        # stock hostage and make MRP over-order.
        "release-stale-reservations": {
            "task": "maintenance.release_stale_reservations",
            "schedule": 3600.0,
            "options": {"queue": "default", "expires": 1800},
        },
        # Section 23: a purchase order past its expected delivery date raises a
        # delay alert rather than waiting for someone to notice.
        "dispatch-delay-alerts": {
            "task": "notifications.scan_dispatch_delays",
            "schedule": 3600.0,
            "options": {"queue": "notifications", "expires": 1800},
        },
        "purge-expired-sessions": {
            "task": "maintenance.purge_expired_sessions",
            "schedule": 86400.0,
            "options": {"queue": "default"},
        },
        "purge-temp-files": {
            "task": "maintenance.purge_temp_files",
            "schedule": 86400.0,
            "options": {"queue": "default"},
        },
        # Password-reset rows are a durable encrypted outbox. This sweep
        # recovers an after-commit publish that happened while Redis was down.
        "dispatch-pending-password-resets": {
            "task": "notifications.dispatch_pending_password_resets",
            "schedule": 60.0,
            "options": {"queue": "notifications", "expires": 55},
        },
    },
)

T = TypeVar("T")


class AsyncTask(Task):
    """Base task that can run an async service function.

    The application's data layer is async, and duplicating every repository in a
    sync flavour purely for Celery would double the surface that has to stay
    correct. Each task runs its coroutine on a fresh event loop instead.
    """

    abstract = True

    @staticmethod
    def run_async(coro: Coroutine[Any, Any, T]) -> T:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                raise RuntimeError("event loop is closed")
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)


def async_task(*task_args: Any, **task_kwargs: Any) -> Callable[..., Any]:
    """Register an async function as a Celery task.

    @async_task(name="mrp.run", queue="mrp")
    async def run_mrp(run_id: str) -> dict: ...
    """

    def decorator(func: Callable[..., Coroutine[Any, Any, T]]) -> Any:
        @celery_app.task(*task_args, base=AsyncTask, bind=True, **task_kwargs)
        def wrapper(self: AsyncTask, *args: Any, **kwargs: Any) -> T:
            return AsyncTask.run_async(func(*args, **kwargs))

        # Carrying the original name and docstring across is a convenience for
        # tracebacks and `celery inspect registered`, not part of the contract -
        # the task's identity is the `name=` given to the decorator.
        #
        # It has to be best-effort: `celery_app.task` hands back a PromiseProxy,
        # and the Task it resolves to exposes `__name__` as a read-only property
        # (Celery 5.6). Assigning it unguarded raises AttributeError at *import*
        # time, which would take down every module that defines a task.
        with contextlib.suppress(AttributeError):
            wrapper.__name__ = func.__name__
        with contextlib.suppress(AttributeError):
            wrapper.__doc__ = func.__doc__
        return wrapper

    return decorator


# --- Signals ------------------------------------------------------------------


@setup_logging.connect
def _configure_worker_logging(**_: Any) -> None:
    """Use the application's structured logging, not Celery's own format."""
    configure_logging()


@task_prerun.connect
def _on_task_start(task_id: str | None = None, task: Task | None = None, **kwargs: Any) -> None:
    """Carry the originating request id into the worker (section 55).

    Callers pass ``request_id`` in the task kwargs; the same correlation id then
    appears in API logs and worker logs for one business operation.
    """
    task_kwargs = kwargs.get("kwargs") or {}
    bind_request_context(
        request_id=task_kwargs.get("request_id") or (task_id or ""),
        company_id=task_kwargs.get("company_id"),
        user_id=task_kwargs.get("user_id"),
    )
    log.info("task.started", task=getattr(task, "name", "unknown"), task_id=task_id)


@task_postrun.connect
def _on_task_end(
    task_id: str | None = None,
    task: Task | None = None,
    state: str | None = None,
    **kwargs: Any,
) -> None:
    name = getattr(task, "name", "unknown")
    celery_tasks_total.labels(task=name, status=state or "UNKNOWN").inc()
    runtime = getattr(getattr(task, "request", None), "runtime", None)
    if runtime:
        celery_task_duration.labels(task=name).observe(runtime)
    log.info("task.finished", task=name, task_id=task_id, state=state)
    clear_request_context()


#: Tasks that talk SMTP. Their transport exceptions embed exactly what
#: :mod:`src.core.mail` forbids from the log: ``SMTPRecipientsRefused`` is
#: keyed by recipient address, and every ``SMTPResponseException`` carries the
#: relay's response text. The tasks log their own stable row identifiers.
MAIL_TASKS = frozenset(
    {
        "notifications.send_email",
        "notifications.send_push",
        "notifications.dispatch_pending_deliveries",
        "notifications.send_password_reset_email",
        "notifications.dispatch_pending_password_resets",
    }
)

#: Exception families whose ``str()`` can echo message content or addresses.
_TRANSPORT_EXCEPTIONS = (smtplib.SMTPException, OSError)


def _failure_is_confidential(task_name: str, exception: BaseException | None) -> bool:
    return task_name in MAIL_TASKS and isinstance(exception, _TRANSPORT_EXCEPTIONS)


@task_failure.connect
def _on_task_failure(
    task_id: str | None = None,
    exception: BaseException | None = None,
    sender: Task | None = None,
    **kwargs: Any,
) -> None:
    """Section 53: background job failures are handled, not silently dropped.

    A mail task's transport failure is logged by type only: its message and
    traceback would carry the recipient address and the relay's reply, and
    ``error`` is not a redacted key. Everything else keeps the full detail.
    """
    task_name = getattr(sender, "name", "unknown")
    error_type = type(exception).__name__ if exception is not None else "unknown"
    if _failure_is_confidential(task_name, exception):
        log.error("task.failed", task=task_name, task_id=task_id, error_type=error_type)
        return
    log.error(
        "task.failed",
        task=task_name,
        task_id=task_id,
        error_type=error_type,
        error=str(exception),
        exc_info=exception,
    )


__all__ = ["MAIL_TASKS", "AsyncTask", "async_task", "celery_app"]
