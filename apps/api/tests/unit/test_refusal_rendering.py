"""A refusal reads as a sentence: no Python list, no record UUID, no exception class.

Nine validators interpolated a list straight into their message, so a person
read ``Duplicate line numbers: [3, 5]`` - or, for the BOM's repeated
component, a list of raw item UUIDs. Three copies of the background-job
sanitiser persisted "failed with an unexpected KeyError" on the job row, which
the MRP and report screens show verbatim. These pin the sentences that replaced
them; ``test_user_message_guards.py`` keeps the shapes from coming back.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.core.errors import BusinessRuleError, failure_message
from src.core.messages import (
    duplicate_line_numbers,
    duplicate_operation_sequences,
    failed_unexpectedly,
    listed,
    plural,
)
from src.modules.bom.schemas import BomCreate
from src.modules.procurement.schemas import RequisitionConversionRequest, _validate_line_numbers
from src.modules.routing.schemas import RoutingCreate
from src.modules.sales.schemas import SalesOrderCreate

pytestmark = pytest.mark.unit

UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _only_message(error: pytest.ExceptionInfo[ValidationError]) -> str:
    [problem] = error.value.errors()
    message = str(problem["ctx"]["error"])
    assert "[" not in message and not UUID_TEXT.search(message), message
    return message


# --- the shared builders ---------------------------------------------------------


def test_plural_writes_the_noun_in_the_number_the_count_needs() -> None:
    assert plural(0, "line") == "0 lines"
    assert plural(1, "line") == "1 line"
    assert plural(2, "box", "boxes") == "2 boxes"
    # A quantity keeps its own scale, and exactly one is still one.
    assert plural(Decimal("1.000"), "unit") == "1.000 unit"
    assert plural(Decimal("2.5"), "unit") == "2.5 units"


def test_listed_writes_values_for_a_sentence() -> None:
    assert listed([3, 5, 9]) == "3, 5, 9"
    assert listed(["CUT", "WELD"]) == "CUT, WELD"
    assert listed([]) == ""


def test_duplicate_numbers_read_as_one_or_many() -> None:
    assert duplicate_line_numbers([3]) == "Duplicate line number: 3."
    assert duplicate_line_numbers([3, 5]) == "Duplicate line numbers: 3, 5."
    assert (
        duplicate_line_numbers([2], "purchase order")
        == "Duplicate line number on the purchase order: 2."
    )
    assert duplicate_operation_sequences([10, 20]) == "Duplicate operation sequences: 10, 20."


# --- BOM ---------------------------------------------------------------------------


def _bom(lines: list[dict[str, object]]) -> dict[str, object]:
    return {"item_id": uuid.uuid4(), "lines": lines}


def _bom_line(number: int, component: uuid.UUID, **extra: object) -> dict[str, object]:
    return {
        "line_number": number,
        "component_item_id": component,
        "quantity_per": Decimal("1"),
        **extra,
    }


def test_a_bom_names_its_duplicate_line_numbers_without_a_list() -> None:
    screw, bush = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(ValidationError) as error:
        BomCreate(
            **_bom(
                [
                    _bom_line(1, screw),
                    _bom_line(1, bush),
                    _bom_line(4, uuid.uuid4()),
                    _bom_line(4, uuid.uuid4()),
                ]
            )
        )

    assert _only_message(error) == "Duplicate line numbers: 1, 4."


def test_a_bom_names_the_lines_that_repeat_a_component_not_its_id() -> None:
    screw = uuid.uuid4()
    with pytest.raises(ValidationError) as error:
        BomCreate(**_bom([_bom_line(1, screw), _bom_line(2, uuid.uuid4()), _bom_line(3, screw)]))

    assert _only_message(error) == (
        "An item appears on more than one primary line: lines 1, 3. "
        "Combine them into one line, or mark the extra one as a substitute."
    )


def test_a_bom_names_the_unknown_lines_its_substitutes_point_at() -> None:
    with pytest.raises(ValidationError) as error:
        BomCreate(
            **_bom(
                [
                    _bom_line(1, uuid.uuid4()),
                    _bom_line(2, uuid.uuid4(), substitute_for_line_number=7),
                    _bom_line(3, uuid.uuid4(), substitute_for_line_number=9),
                ]
            )
        )

    assert _only_message(error) == "Substitute lines reference unknown line numbers: 7, 9."


# --- routing ---------------------------------------------------------------------


def _operation(sequence: int, code: str) -> dict[str, object]:
    return {
        "sequence": sequence,
        "operation_code": code,
        "operation_name": code.title(),
        "work_center_id": uuid.uuid4(),
    }


def test_a_routing_names_its_repeated_operation_codes_as_text() -> None:
    with pytest.raises(ValidationError) as error:
        RoutingCreate(
            code="RT-001",
            item_id=uuid.uuid4(),
            operations=[
                _operation(10, "CUT"),
                _operation(20, "CUT"),
                _operation(30, "WELD"),
                _operation(40, "WELD"),
            ],
        )

    assert _only_message(error) == "Duplicate operation codes: CUT, WELD."


# --- sales and procurement ----------------------------------------------------------


def test_a_sales_order_names_its_duplicate_line_number() -> None:
    line = {"line_number": 2, "item_id": str(uuid.uuid4()), "quantity": "1"}
    with pytest.raises(ValidationError) as error:
        SalesOrderCreate(
            customer_id=uuid.uuid4(),
            required_date=date(2026, 12, 1),
            warehouse_id=uuid.uuid4(),
            lines=[line, {**line, "item_id": str(uuid.uuid4())}],
        )

    assert _only_message(error) == "Duplicate line number: 2."


def test_a_purchase_document_names_its_duplicate_lines_and_itself() -> None:
    with pytest.raises(ValueError, match=r"^Duplicate line numbers on the RFQ: 1, 3\.$"):
        _validate_line_numbers([1, 1, 2, 3, 3], "RFQ")


def test_a_repeated_requisition_override_is_named_by_its_position_not_its_id() -> None:
    repeated, other = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(ValidationError) as error:
        RequisitionConversionRequest(
            lines=[
                {"requisition_line_id": str(repeated), "exclude": True},
                {"requisition_line_id": str(other), "exclude": True},
                {"requisition_line_id": str(repeated), "quantity": "2"},
            ]
        )

    assert _only_message(error) == (
        "Each requisition line can be overridden once; overrides 1, 3 repeat a line."
    )


# --- a failed background job ----------------------------------------------------------


def test_a_defect_persists_that_the_job_failed_and_not_the_exception_class() -> None:
    message = failure_message(
        KeyError("item_id"), "The report", logged_against="worker log against this task id"
    )

    assert message == failed_unexpectedly("The report", "worker log against this task id")
    assert message == (
        "The report failed unexpectedly. The full error is in the worker log against this task id."
    )
    assert "KeyError" not in message and "item_id" not in message


def test_a_refusal_persists_its_own_sentence() -> None:
    refusal = BusinessRuleError("Warehouse WH-01 is inactive and cannot receive stock.")

    assert (
        failure_message(refusal, "The planning run", logged_against="worker log")
        == "Warehouse WH-01 is inactive and cannot receive stock."
    )
