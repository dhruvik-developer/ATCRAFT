"""Payload validation for bills of materials.

These checks exist so a malformed structure is rejected with a message naming
the offending line, rather than reaching PostgreSQL and coming back as a generic
integrity error the user cannot act on. Each one mirrors a constraint that also
exists in the database - the schema is the friendly half, never the only half.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from src.modules.bom.models import BomType
from src.modules.bom.schemas import (
    MAX_LINES_PER_BOM,
    BomCreate,
    BomLineIn,
    BomOperationIn,
    BomStatusChange,
    BomUpdate,
)

PARENT = uuid.uuid4()
SCREW = uuid.uuid4()
BUSH = uuid.uuid4()


def line(number: int, component: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "line_number": number,
        "component_item_id": component,
        "quantity_per": Decimal("1"),
    }
    payload.update(overrides)
    return payload


def bom(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "item_id": PARENT,
        "lines": [line(1, SCREW), line(2, BUSH)],
    }
    payload.update(overrides)
    return payload


# -----------------------------------------------------------------------------
# Lines
# -----------------------------------------------------------------------------


def test_a_valid_structure_is_accepted() -> None:
    payload = BomCreate(**bom())
    assert len(payload.lines) == 2
    assert payload.output_quantity == Decimal("1")
    assert payload.is_default is True


def test_duplicate_line_numbers_are_refused() -> None:
    """``(bom_id, line_number)`` is unique; catching it here names the number."""
    with pytest.raises(ValidationError, match="Duplicate line numbers"):
        BomCreate(**bom(lines=[line(1, SCREW), line(1, BUSH)]))


def test_the_same_component_on_two_primary_lines_is_refused() -> None:
    """The explosion would sum them into a quantity nobody typed."""
    with pytest.raises(ValidationError, match="more than one primary line"):
        BomCreate(**bom(lines=[line(1, SCREW), line(2, SCREW)]))


def test_a_component_may_repeat_when_the_second_line_is_a_substitute() -> None:
    payload = BomCreate(**bom(lines=[line(1, SCREW), line(2, SCREW, substitute_for_line_number=1)]))
    assert payload.lines[1].substitute_for_line_number == 1


def test_a_substitute_pointing_at_a_line_that_does_not_exist_is_refused() -> None:
    with pytest.raises(ValidationError, match="unknown line numbers"):
        BomCreate(**bom(lines=[line(1, SCREW), line(2, BUSH, substitute_for_line_number=9)]))


def test_a_line_cannot_substitute_for_itself() -> None:
    with pytest.raises(ValidationError, match="substitute for itself"):
        BomLineIn(**line(1, SCREW, substitute_for_line_number=1))


def test_a_zero_or_negative_quantity_is_refused() -> None:
    for bad in (Decimal("0"), Decimal("-1")):
        with pytest.raises(ValidationError):
            BomLineIn(**line(1, SCREW, quantity_per=bad))


def test_scrap_of_a_hundred_percent_or_more_is_refused() -> None:
    """100% scrap means every piece made is thrown away - a typo, not a process."""
    with pytest.raises(ValidationError):
        BomLineIn(**line(1, SCREW, scrap_percentage=Decimal("100")))


def test_a_realistic_scrap_allowance_is_accepted() -> None:
    parsed = BomLineIn(**line(1, SCREW, scrap_percentage=Decimal("2.5")))
    assert parsed.scrap_percentage == Decimal("2.5")


def test_the_unit_defaults_to_unset_so_the_service_can_supply_the_stock_unit() -> None:
    assert BomLineIn(**line(1, SCREW)).uom_id is None


def test_an_unknown_field_on_a_line_is_rejected() -> None:
    # extra="forbid": a typo'd field name must not be silently ignored.
    with pytest.raises(ValidationError):
        BomLineIn(**line(1, SCREW, quantiy_per=Decimal("2")))


# -----------------------------------------------------------------------------
# Header
# -----------------------------------------------------------------------------


def test_a_structure_with_no_lines_is_refused() -> None:
    with pytest.raises(ValidationError):
        BomCreate(**bom(lines=[]))


def test_more_lines_than_the_cap_are_refused() -> None:
    too_many = [line(n, uuid.uuid4()) for n in range(1, MAX_LINES_PER_BOM + 2)]
    with pytest.raises(ValidationError):
        BomCreate(**bom(lines=too_many))


def test_an_effectivity_window_that_ends_before_it_starts_is_refused() -> None:
    with pytest.raises(ValidationError, match="effective_to cannot precede"):
        BomCreate(**bom(effective_from=date(2026, 6, 1), effective_to=date(2026, 5, 1)))


def test_an_open_ended_window_is_accepted() -> None:
    payload = BomCreate(**bom(effective_from=date(2026, 6, 1)))
    assert payload.effective_to is None


def test_an_alternate_must_name_the_structure_it_replaces() -> None:
    with pytest.raises(ValidationError, match="must name the BOM"):
        BomCreate(**bom(bom_type=BomType.ALTERNATE))

    accepted = BomCreate(**bom(bom_type=BomType.ALTERNATE, parent_bom_id=uuid.uuid4()))
    assert accepted.parent_bom_id is not None


def test_a_zero_output_quantity_is_refused() -> None:
    # Everything downstream divides by it.
    with pytest.raises(ValidationError):
        BomCreate(**bom(output_quantity=Decimal("0")))


def test_the_payload_carries_no_company_id() -> None:
    """Master rules section 40: the tenant comes from the token, and a client
    that tries to supply one is rejected rather than quietly ignored."""
    assert "company_id" not in BomCreate.model_fields
    with pytest.raises(ValidationError):
        BomCreate(**bom(company_id=uuid.uuid4()))


def test_the_payload_carries_no_code_or_version() -> None:
    # Both are issued server-side: the code from the document sequence, the
    # version under a per-item lock.
    assert "code" not in BomCreate.model_fields
    assert "version" not in BomCreate.model_fields


# -----------------------------------------------------------------------------
# Update semantics
# -----------------------------------------------------------------------------


def test_an_empty_update_changes_nothing() -> None:
    """``exclude_unset`` is what stops a PATCH from blanking fields it omitted."""
    assert BomUpdate().model_dump(exclude_unset=True) == {}


def test_an_update_may_clear_the_end_of_the_effectivity_window() -> None:
    sent = BomUpdate(effective_to=None).model_dump(exclude_unset=True)
    assert sent == {"effective_to": None}


def test_an_update_cannot_re_point_the_structure_at_another_item() -> None:
    # item_id and version are identity; a new version is created by copying.
    assert "item_id" not in BomUpdate.model_fields
    assert "version" not in BomUpdate.model_fields
    with pytest.raises(ValidationError):
        BomUpdate(item_id=uuid.uuid4())


def test_a_replacement_line_set_is_validated_the_same_way() -> None:
    with pytest.raises(ValidationError, match="Duplicate line numbers"):
        BomUpdate(lines=[BomLineIn(**line(1, SCREW)), BomLineIn(**line(1, BUSH))])


# -----------------------------------------------------------------------------
# Operations and commands
# -----------------------------------------------------------------------------


def test_duplicate_operation_sequences_are_refused() -> None:
    operations = [
        {"operation_sequence": 10, "name": "Machine"},
        {"operation_sequence": 10, "name": "Polish"},
    ]
    with pytest.raises(ValidationError, match="Duplicate operation sequences"):
        BomCreate(**bom(operations=operations))


def test_operation_times_and_rates_cannot_be_negative() -> None:
    for field, value in (
        ("setup_time_minutes", Decimal("-1")),
        ("run_time_minutes_per_unit", Decimal("-1")),
        ("cost_per_hour", Decimal("-1")),
        ("fixed_cost", Decimal("-1")),
    ):
        with pytest.raises(ValidationError):
            BomOperationIn(operation_sequence=10, name="Machine", **{field: value})


def test_a_status_change_must_carry_a_reason() -> None:
    """Master rules section 83: a state change with no recorded reason makes the
    audit trail useless six months later."""
    with pytest.raises(ValidationError):
        BomStatusChange(reason="")
    with pytest.raises(ValidationError):
        BomStatusChange(reason="ok")
    assert BomStatusChange(reason="Approved after DFM review").reason
