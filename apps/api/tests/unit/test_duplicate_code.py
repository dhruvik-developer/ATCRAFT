"""A code another record already holds is refused one way, on the code field.

Twenty-one services refused a reused code in two wordings - "Vendor code 'V-1'
is already used in this company." at fourteen sites and "An item with code
'X' already exists." at seven - and only the manufacturer and brand refusals
put the error on the form's ``code`` field; finance raised its own as a
business rule with a 409 bolted on. ``src.core.errors.duplicate_code`` is now
the one constructor, and these tests pin what it builds.
"""

from __future__ import annotations

import pytest

from src.core.errors import DuplicateError, ErrorCode, duplicate_code
from src.core.messages import code_already_used

pytestmark = pytest.mark.unit


def test_the_refusal_is_a_409_on_the_code_field() -> None:
    refusal = duplicate_code("Vendor", "V-001")

    assert isinstance(refusal, DuplicateError)
    assert (refusal.status_code, refusal.code) == (409, ErrorCode.DUPLICATE_DOCUMENT)
    assert refusal.message == "Vendor code 'V-001' is already used in this company."
    assert refusal.details == {"code": "V-001", "fields": {"code": [refusal.message]}}


def test_a_scope_a_note_and_the_ids_behind_it_are_carried() -> None:
    refusal = duplicate_code(
        "Location",
        "A-01",
        scope="warehouse WH-1",
        note="Pick another code.",
        warehouse_code="WH-1",
    )

    assert (
        refusal.message
        == "Location code 'A-01' is already used in warehouse WH-1. Pick another code."
    )
    assert refusal.details == {
        "code": "A-01",
        "warehouse_code": "WH-1",
        "fields": {"code": [refusal.message]},
    }


def test_a_code_field_by_another_name_carries_the_refusal_under_that_name() -> None:
    """A user's code is ``employee_code`` on the form, so that is where it is refused."""
    refusal = duplicate_code("Employee", "E-1", field="employee_code")

    assert refusal.message == "Employee code 'E-1' is already used in this company."
    assert refusal.details == {
        "employee_code": "E-1",
        "fields": {"employee_code": [refusal.message]},
    }


def test_the_sentence_is_the_shared_one() -> None:
    assert code_already_used("Cost centre", "CC-1") == (
        "Cost centre code 'CC-1' is already used in this company."
    )
    assert duplicate_code("Cost centre", "CC-1").message == code_already_used("Cost centre", "CC-1")
