"""What the task-failure signal writes to the log for a mail task."""

from __future__ import annotations

import smtplib
from types import SimpleNamespace
from typing import Any

import pytest

from src.workers import celery_app as worker

pytestmark = pytest.mark.unit


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


def test_the_mail_task_set_names_every_registered_mail_task() -> None:
    from src.workers.tasks import notifications

    registered = {
        getattr(task, "name", None)
        for task in (
            notifications.send_email,
            notifications.send_password_reset_email,
            notifications.dispatch_pending_password_resets,
        )
    }
    assert registered == worker.MAIL_TASKS
