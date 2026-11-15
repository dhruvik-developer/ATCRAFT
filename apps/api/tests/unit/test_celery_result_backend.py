"""A task keeps no result, so the result backend never holds a failure.

Nothing reads a Celery result - every job records its outcome on its own row -
and a stored result was only the failure again: the exception and its
traceback, in Redis until ``result_expires``. For a database error that is the
row it was writing; for a mail task, the recipient the relay refused.

``task_ignore_result`` alone did not hold. ``Celery.send_task`` writes
``ignore_result=False`` into every message unless told otherwise, the worker
lets the message override the task, and the API and the tasks send every job by
name - so even the mail tasks registered ``ignore_result=True`` stored their
failures, and the sending process subscribed to each result's channel. Each
test below publishes through the real ``send_task`` and runs the real tracer on
what was published, against an in-memory backend.
"""

from __future__ import annotations

import contextlib
import smtplib
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from celery import states
from celery.app import trace as celery_trace
from celery.backends.cache import CacheBackend

from src.workers import celery_app as worker

pytestmark = pytest.mark.unit

#: What no stored result may name.
ADDRESS = "priya@example.com"

#: Every module that registers a task, as the worker's ``include`` lists them.
TASK_MODULES = (
    "src.workers.tasks.mrp",
    "src.workers.tasks.reports",
    "src.workers.tasks.notifications",
    "src.workers.tasks.maintenance",
)


def _refused_recipient() -> BaseException:
    return smtplib.SMTPRecipientsRefused({ADDRESS: (550, b"5.1.1 User unknown")})


def _quoted_row() -> BaseException:
    return RuntimeError(
        'duplicate key value violates unique constraint "uq_users_company_id_email" '
        f"DETAIL:  Key (company_id, email)=(0190, {ADDRESS}) already exists."
    )


class _MemoryBackend(CacheBackend):
    """The in-memory result backend, noting each result channel a send subscribes to."""

    def __init__(self) -> None:
        super().__init__(app=worker.celery_app, url="memory://")
        self.subscribed: list[str] = []

    def on_task_call(self, producer: Any, task_id: str) -> dict[str, Any]:
        self.subscribed.append(task_id)
        return {}


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> _MemoryBackend:
    for module in TASK_MODULES:
        __import__(module)
    memory = _MemoryBackend()
    # The app's backend is per thread; this thread's is the one both the send
    # and the tracer below read.
    monkeypatch.setattr(worker.celery_app._local, "backend", memory, raising=False)
    return memory


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Each message ``send_task`` publishes, captured instead of reaching a broker."""
    messages: list[Any] = []
    monkeypatch.setattr(
        worker.celery_app.amqp,
        "send_task_message",
        lambda producer, name, message, **options: messages.append(message),
    )
    return messages


def _send(name: str, published: list[Any], **options: Any) -> tuple[str, dict[str, Any]]:
    """Send ``name`` through the real ``send_task``; its id and the headers it published."""
    producer = SimpleNamespace(
        connection=SimpleNamespace(_reraise_as_library_errors=contextlib.nullcontext)
    )
    result = worker.celery_app.send_task(name, kwargs={}, producer=producer, **options)
    (message,) = published
    published.clear()
    return result.id, dict(message.headers)


def _run_failing(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    task_id: str,
    headers: dict[str, Any],
    failure: Callable[[], BaseException],
) -> None:
    """Run ``name`` as the worker runs the message it received, failing with ``failure``."""
    task = worker.celery_app.tasks[name]

    def fail(*args: Any, **kwargs: Any) -> None:
        raise failure()

    monkeypatch.setattr(task, "run", fail)
    tracer = celery_trace.build_tracer(
        name, task, app=worker.celery_app, eager=False, propagate=False, hostname="worker-1"
    )
    tracer(task_id, [], {}, request=headers)


def test_every_task_is_registered_to_keep_no_result(backend: _MemoryBackend) -> None:
    registered = {
        name: task
        for name, task in worker.celery_app.tasks.items()
        if not name.startswith("celery.")
    }

    assert registered, "no task registered"
    assert [name for name, task in registered.items() if task.ignore_result is not True] == []
    assert [name for name, task in registered.items() if task.store_errors_even_if_ignored] == []


@pytest.mark.parametrize(
    ("name", "failure"),
    [
        ("notifications.send_email", _refused_recipient),
        ("reports.generate", _quoted_row),
        ("mrp.run", _quoted_row),
    ],
)
def test_a_task_sent_by_name_that_fails_leaves_nothing_in_the_result_backend(
    monkeypatch: pytest.MonkeyPatch,
    backend: _MemoryBackend,
    published: list[Any],
    name: str,
    failure: Callable[[], BaseException],
) -> None:
    task_id, headers = _send(name, published)

    assert headers["ignore_result"] is True
    assert backend.subscribed == [], "nor does the sender listen for a result"

    _run_failing(monkeypatch, name, task_id, headers, failure)

    stored = backend.get_task_meta(task_id)
    assert stored["status"] == states.PENDING, "the backend has never heard of the task"
    assert ADDRESS not in repr(stored)


def test_a_caller_that_asks_for_a_result_still_gets_one(
    monkeypatch: pytest.MonkeyPatch, backend: _MemoryBackend, published: list[Any]
) -> None:
    """The control: the backend above is live, and it was the default that kept it empty."""
    task_id, headers = _send("reports.generate", published, ignore_result=False)

    assert headers["ignore_result"] is False
    assert backend.subscribed == [task_id]

    _run_failing(monkeypatch, "reports.generate", task_id, headers, _quoted_row)

    stored = backend.get_task_meta(task_id)
    assert stored["status"] == states.FAILURE
    assert ADDRESS in repr(stored)


def test_send_task_defaults_from_the_configuration_not_from_celery(
    monkeypatch: pytest.MonkeyPatch, backend: _MemoryBackend, published: list[Any]
) -> None:
    monkeypatch.setitem(worker.celery_app.conf, "task_ignore_result", False)

    _, headers = _send("reports.generate", published)

    assert headers["ignore_result"] is False
