"""What a client is told about a database constraint or a schema validator.

Two translations that were wrong in the same way - the text the server
produces was not the text the handler matched on - and one that leaked a
framework prefix into every hand-written field message.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import IntegrityError

from src.core.errors import ErrorCode
from src.core.exception_handlers import (
    classify_integrity_error,
    integrity_error_handler,
    validation_error_handler,
)
from src.core.messages import SHIFT_ASSIGNMENT_OVERLAP

pytestmark = pytest.mark.unit


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "PATCH",
            "path": "/api/v1/products/items/1",
            "headers": [],
            "query_string": b"",
            "scheme": "https",
            "server": ("test", 443),
            "client": ("127.0.0.1", 1),
        }
    )


class _DriverError(Exception):
    """What SQLAlchemy hands back as ``exc.orig``: a message plus a SQLSTATE."""

    def __init__(self, message: str, sqlstate: str | None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate
        self.pgcode = sqlstate


def _integrity(message: str, sqlstate: str | None) -> IntegrityError:
    return IntegrityError("INSERT ...", {}, _DriverError(message, sqlstate))


def _body(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


# -----------------------------------------------------------------------------
# Integrity errors
# -----------------------------------------------------------------------------

NOT_NULL = 'null value in column "name" of relation "items" violates not-null constraint'
FK_INSERT = (
    'insert or update on table "item_vendor_links" violates foreign key constraint '
    '"fk_item_vendor_links_vendor_id_vendors"'
)
FK_DELETE = (
    'update or delete on table "vendors" violates foreign key constraint '
    '"fk_item_vendor_links_vendor_id_vendors" on table "item_vendor_links"'
)
UNIQUE = 'duplicate key value violates unique constraint "uq_items_company_id_code"'
CHECK = 'new row for relation "items" violates check constraint "ck_items_positive"'


async def test_a_not_null_violation_is_a_missing_required_value() -> None:
    """PostgreSQL spells it "not-null"; the branch that matched "not null"
    never ran and every NOT NULL violation was reported as a 409 conflict."""
    response = await integrity_error_handler(_request(), _integrity(NOT_NULL, "23502"))

    assert response.status_code == 422
    body = _body(response)
    assert body["error"]["code"] == ErrorCode.VALIDATION_ERROR
    assert body["error"]["message"] == "A required value is missing."


async def test_a_dangling_reference_on_insert_is_not_reported_as_in_use() -> None:
    response = await integrity_error_handler(_request(), _integrity(FK_INSERT, "23503"))

    assert response.status_code == 422
    body = _body(response)
    assert body["error"]["code"] == ErrorCode.VALIDATION_ERROR
    assert body["error"]["message"] == "A referenced record does not exist."


async def test_a_referenced_row_on_delete_is_reported_as_in_use() -> None:
    response = await integrity_error_handler(_request(), _integrity(FK_DELETE, "23503"))

    assert response.status_code == 409
    assert _body(response)["error"]["code"] == ErrorCode.REFERENCED_RECORD_IN_USE


@pytest.mark.parametrize(
    ("message", "sqlstate", "code", "status"),
    [
        (UNIQUE, "23505", ErrorCode.DUPLICATE_DOCUMENT, 409),
        (CHECK, "23514", ErrorCode.BUSINESS_RULE_VIOLATION, 422),
        ("something else entirely", "23P01", ErrorCode.CONFLICT, 409),
    ],
)
def test_classification_is_by_sqlstate(
    message: str, sqlstate: str, code: ErrorCode, status: int
) -> None:
    got_code, _, got_status = classify_integrity_error(_integrity(message, sqlstate))
    assert (got_code, got_status) == (code, status)


ROSTER_EXCLUSION = (
    'conflicting key value violates exclusion constraint "ex_shift_assignments_operator_window"'
)


async def test_a_roster_overlap_that_won_a_race_is_named_as_one() -> None:
    """SQLSTATE 23P01 from the roster's EXCLUDE constraints is a 409 in roster words.

    The service checks overlaps first, so this is what the *loser* of two
    concurrent plans sees - and it must still say what clashed, without the
    constraint name.
    """
    response = await integrity_error_handler(_request(), _integrity(ROSTER_EXCLUSION, "23P01"))

    assert response.status_code == 409
    body = _body(response)
    assert body["error"]["code"] == ErrorCode.CONFLICT
    assert body["error"]["message"] == SHIFT_ASSIGNMENT_OVERLAP
    assert "ex_shift_assignments" not in response.body.decode()


def test_another_exclusion_constraint_does_not_borrow_the_roster_sentence() -> None:
    other = 'conflicting key value violates exclusion constraint "ex_financial_years_no_overlap"'
    code, message, status = classify_integrity_error(_integrity(other, "23P01"))
    assert (code, status) == (ErrorCode.CONFLICT, 409)
    assert message != SHIFT_ASSIGNMENT_OVERLAP
    assert "overlaps" in message


def test_sqlstate_outranks_the_message_text() -> None:
    """A driver that attaches a SQLSTATE is believed even if the text is odd."""
    code, _, status = classify_integrity_error(_integrity("violates foreign key", "23502"))
    assert (code, status) == (ErrorCode.VALIDATION_ERROR, 422)


@pytest.mark.parametrize(
    ("message", "code", "status"),
    [
        (NOT_NULL, ErrorCode.VALIDATION_ERROR, 422),
        ("NOT NULL constraint failed: items.name", ErrorCode.VALIDATION_ERROR, 422),
        (FK_INSERT, ErrorCode.VALIDATION_ERROR, 422),
        (FK_DELETE, ErrorCode.REFERENCED_RECORD_IN_USE, 409),
        (UNIQUE, ErrorCode.DUPLICATE_DOCUMENT, 409),
        (CHECK, ErrorCode.BUSINESS_RULE_VIOLATION, 422),
        ("deadlock detected", ErrorCode.CONFLICT, 409),
    ],
)
def test_text_fallback_covers_a_driver_without_a_sqlstate(
    message: str, code: ErrorCode, status: int
) -> None:
    got_code, _, got_status = classify_integrity_error(_integrity(message, None))
    assert (got_code, got_status) == (code, status)


def test_sqlstate_is_read_off_the_wrapped_cause_too() -> None:
    """SQLAlchemy's adapted asyncpg error keeps the real one as ``__cause__``."""
    adapted = Exception(NOT_NULL)
    adapted.__cause__ = _DriverError(NOT_NULL, "23502")
    code, _, status = classify_integrity_error(IntegrityError("INSERT ...", {}, adapted))
    assert (code, status) == (ErrorCode.VALIDATION_ERROR, 422)


async def test_the_client_never_sees_the_driver_message() -> None:
    response = await integrity_error_handler(_request(), _integrity(FK_INSERT, "23503"))
    text = response.body.decode()
    assert "item_vendor_links" not in text
    assert "constraint" not in text


# -----------------------------------------------------------------------------
# Request validation
# -----------------------------------------------------------------------------


async def test_a_validator_sentence_is_shown_without_the_framework_prefix() -> None:
    error = RequestValidationError(
        [
            {
                "type": "value_error",
                "loc": ("body", "name"),
                "msg": "Value error, Name cannot be blank.",
                "input": "",
                "ctx": {"error": ValueError("Name cannot be blank.")},
            }
        ]
    )

    response = await validation_error_handler(_request(), error)

    assert response.status_code == 422
    body = _body(response)
    assert body["error"]["code"] == ErrorCode.VALIDATION_ERROR
    assert body["error"]["details"]["fields"] == {"name": ["Name cannot be blank."]}


async def test_the_prefix_is_stripped_even_when_the_original_error_is_gone() -> None:
    error = RequestValidationError(
        [
            {
                "type": "value_error",
                "loc": ("body", "gstin"),
                "msg": "Value error, GSTIN must be 15 characters in the standard GST format.",
                "input": "x",
            },
            {
                "type": "assertion_error",
                "loc": ("body", "lines", 0, "quantity"),
                "msg": "Assertion failed, Quantity must be positive.",
                "input": -1,
            },
        ]
    )

    response = await validation_error_handler(_request(), error)

    assert _body(response)["error"]["details"]["fields"] == {
        "gstin": ["GSTIN must be 15 characters in the standard GST format."],
        "lines.0.quantity": ["Quantity must be positive."],
    }


async def test_pydantics_own_messages_pass_through_unchanged() -> None:
    error = RequestValidationError(
        [
            {
                "type": "missing",
                "loc": ("body", "code"),
                "msg": "Field required",
                "input": {},
            }
        ]
    )

    response = await validation_error_handler(_request(), error)

    assert _body(response)["error"]["details"]["fields"] == {"code": ["Field required"]}
