"""A database error's own text never carries the row it was writing into a log.

``hide_parameters`` keeps bound values out of SQLAlchemy's text (BE2-02). The
driver's text, which SQLAlchemy embeds verbatim, still quoted them - an
integrity violation's ``DETAIL``, a data error's rejected input - and reached
every traceback, ``task.failed``'s ``error=str(exc)``, Celery's failure line
and the error tracker, where the key-based redaction cannot see. Each error
here is built as the application meets it: asyncpg's error, the dialect's
adapted copy raised from it by SQLAlchemy's own translation, and SQLAlchemy's
error raised from that.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import asyncpg
import pytest
from asyncpg import exceptions as pg
from billiard.einfo import ExceptionInfo
from celery import states
from celery.app import trace as celery_trace
from celery.app.task import Context
from fastapi import Request
from sqlalchemy.dialects.postgresql.asyncpg import (
    AsyncAdapt_asyncpg_connection,
    AsyncAdapt_asyncpg_dbapi,
)
from sqlalchemy.exc import DBAPIError

from src.core.database_errors import DRIVER_TEXT_WITHHELD, database_error_redactor
from src.core.exception_handlers import sqlalchemy_error_handler
from src.core.logging import get_logger
from src.core.observability import _scrub_sentry_event
from src.workers import celery_app as worker
from tests.unit.test_worker_failure_logging import _Captured
from tests.unit.test_worker_failure_logging import trace as trace

pytestmark = pytest.mark.unit

log = get_logger("tests.database_error_text")

#: The value no log line may name.
EMAIL = "priya@example.com"
INSERT = "INSERT INTO users (company_id, email) VALUES ($1::UUID, $2::VARCHAR)"
SELECT = "SELECT users.id FROM users WHERE users.id = $1::UUID"
DBAPI = AsyncAdapt_asyncpg_dbapi(asyncpg)


def through_sqlalchemy(driver_error: BaseException, statement: str = INSERT) -> DBAPIError:
    """``driver_error`` as the application catches it, raised through the dialect."""
    connection = object.__new__(AsyncAdapt_asyncpg_connection)
    connection.dbapi = DBAPI
    connection._connection = SimpleNamespace(is_closed=lambda: False)
    try:
        try:
            raise driver_error
        except pg.PostgresError as raised:
            connection._handle_exception(raised)
    except DBAPI.Error as adapted:
        wrapped = DBAPIError.instance(
            statement, ("0190", EMAIL), adapted, DBAPI.Error, hide_parameters=True
        )
        try:
            raise wrapped from adapted
        except DBAPIError as caught:
            return caught
    raise AssertionError("the dialect translated nothing")


def unique_violation() -> BaseException:
    return pg.PostgresError.new(
        {
            "C": "23505",
            "M": 'duplicate key value violates unique constraint "uq_users_company_id_email"',
            "D": f"Key (company_id, email)=(0190, {EMAIL}) already exists.",
            "s": "public",
            "t": "users",
            "n": "uq_users_company_id_email",
        }
    )


def not_null_violation() -> BaseException:
    # The DETAIL of a NOT NULL or CHECK violation is the whole row.
    return pg.PostgresError.new(
        {
            "C": "23502",
            "M": 'null value in column "name" of relation "users" violates not-null constraint',
            "D": f"Failing row contains (0190, null, {EMAIL}, +91 98200 00000).",
            "t": "users",
            "c": "name",
        }
    )


def refused_argument() -> BaseException:
    """asyncpg's refusal to encode an argument, raised from the encoder's error."""
    try:
        try:
            raise ValueError(f"invalid UUID '{EMAIL}': length must be between 32..36 characters")
        except ValueError as encoder:
            raise pg.DataError(
                f"invalid input for query argument $1: '{EMAIL}' ({encoder})"
            ) from encoder
    except pg.DataError as refused:
        return refused


def rejected_text() -> BaseException:
    return pg.PostgresError.new(
        {"C": "22P02", "M": f'invalid input syntax for type uuid: "{EMAIL}"'}
    )


def missing_column() -> BaseException:
    return pg.PostgresError.new({"C": "42703", "M": 'column "emial" does not exist'})


VALUE_BEARING = {
    "unique-violation": (unique_violation, INSERT),
    "not-null-violation": (not_null_violation, INSERT),
    "refused-argument": (refused_argument, SELECT),
    "rejected-text": (rejected_text, SELECT),
}


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/users",
            "headers": [],
            "query_string": b"",
            "scheme": "https",
            "server": ("test", 443),
            "client": ("127.0.0.1", 1),
        }
    )


@pytest.mark.parametrize("case", sorted(VALUE_BEARING))
def test_a_failure_logged_with_its_traceback_names_no_value(
    case: str, capsys: pytest.CaptureFixture[str]
) -> None:
    driver_error, statement = VALUE_BEARING[case]
    error = through_sqlalchemy(driver_error(), statement)

    log.error("task.failed", error=str(error), exc_info=error)

    out = capsys.readouterr().out
    assert EMAIL not in out
    assert "DETAIL" not in out
    assert "Failing row" not in out
    assert DRIVER_TEXT_WITHHELD in out
    # Still a traceback, and still the statement that failed.
    assert "Traceback (most recent call last)" in out
    assert f"[SQL: {statement}]" in out


def test_a_violation_is_named_by_its_sqlstate_constraint_and_table(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = through_sqlalchemy(unique_violation())

    log.error("task.failed", error=str(error), exc_info=error)

    out = capsys.readouterr().out
    assert "SQLSTATE 23505, constraint uq_users_company_id_email, table users" in out
    # Every error in the chain is still named by its type.
    assert "asyncpg.exceptions.UniqueViolationError" in out
    assert "sqlalchemy.exc.IntegrityError" in out


def test_a_refused_argument_is_named_by_its_position(capsys: pytest.CaptureFixture[str]) -> None:
    error = through_sqlalchemy(refused_argument(), SELECT)

    log.error("task.failed", error=str(error), exc_info=error)

    out = capsys.readouterr().out
    assert "SQLSTATE 22000, invalid input for query argument $1" in out
    assert EMAIL not in out


def test_any_other_failure_keeps_its_text(capsys: pytest.CaptureFixture[str]) -> None:
    # A defect's text, and a database error that binds no value, are the
    # diagnosis: a column that does not exist is named in full.
    missing = through_sqlalchemy(missing_column(), SELECT)
    assert database_error_redactor(missing) is None
    assert database_error_redactor(RuntimeError(EMAIL)) is None
    assert database_error_redactor(None) is None

    log.error("task.failed", error=str(missing), exc_info=missing)

    out = capsys.readouterr().out
    assert 'column "emial" does not exist' in out
    assert DRIVER_TEXT_WITHHELD not in out


def test_the_workers_task_failed_line_names_the_rule_not_the_row(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = through_sqlalchemy(unique_violation())

    worker._on_task_failure(
        task_id="t-db", exception=error, sender=SimpleNamespace(name="reports.generate")
    )

    out = capsys.readouterr().out
    assert "task.failed" in out
    assert "uq_users_company_id_email" in out
    assert "Traceback (most recent call last)" in out
    assert EMAIL not in out


async def test_the_database_handler_logs_its_traceback_without_the_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = through_sqlalchemy(refused_argument(), SELECT)

    # Starlette calls the handler inside the except block that caught it.
    try:
        raise error
    except DBAPIError as caught:
        response = await sqlalchemy_error_handler(_request(), caught)

    assert response.status_code == 500
    out = capsys.readouterr().out
    assert "error.database" in out
    assert "Traceback (most recent call last)" in out
    assert "invalid input for query argument $1" in out
    assert EMAIL not in out
    assert EMAIL not in response.body.decode()


def test_a_defect_raised_from_a_violation_keeps_its_own_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    try:
        try:
            raise through_sqlalchemy(not_null_violation())
        except DBAPIError as violation:
            raise RuntimeError("the reminder could not be filed") from violation
    except RuntimeError:
        log.exception("error.unhandled")

    out = capsys.readouterr().out
    assert "RuntimeError: the reminder could not be filed" in out
    assert "column name" in out
    assert EMAIL not in out


def test_the_error_tracker_is_sent_no_value() -> None:
    error = through_sqlalchemy(unique_violation())
    chain: list[BaseException] = [error]
    while chain[-1].__cause__ is not None:
        chain.append(chain[-1].__cause__)
    event: dict[str, Any] = {
        "exception": {
            "values": [{"type": type(each).__name__, "value": str(each)} for each in chain]
        }
    }
    assert EMAIL in repr(event), "the control: Sentry's own values quote the row"

    scrubbed = _scrub_sentry_event(event, {"exc_info": (type(error), error, error.__traceback__)})

    assert EMAIL not in repr(scrubbed)
    values = scrubbed["exception"]["values"]
    assert [value["type"] for value in values] == [
        "IntegrityError",
        "IntegrityError",
        "UniqueViolationError",
    ]
    assert all(DRIVER_TEXT_WITHHELD in value["value"] for value in values)


def test_celerys_failure_line_names_the_rule_not_the_row(trace: _Captured) -> None:
    import src.workers.tasks.reports  # noqa: F401 - registers the task

    task = worker.celery_app.tasks["reports.generate"]
    error = through_sqlalchemy(unique_violation())
    try:
        raise error
    except DBAPIError:
        einfo = ExceptionInfo()
        celery_trace.TraceInfo(states.FAILURE, error)._log_error(
            task, Context(hostname="worker-1", id="task-1", args=[], kwargs={}), einfo
        )

    (record,) = trace.records
    (line,) = trace.lines()
    assert (
        "raised unexpected: '(sqlalchemy.dialects.postgresql.asyncpg.IntegrityError) "
        f"[{DRIVER_TEXT_WITHHELD}: SQLSTATE 23505, constraint uq_users_company_id_email"
    ) in line
    assert "[SQL: INSERT INTO users" in line
    assert EMAIL not in line
    assert EMAIL not in repr(vars(record)), "nor in the record's extras"
    assert record.exc_info is None
