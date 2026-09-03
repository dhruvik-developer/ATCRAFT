"""Payload validation for routings, work centres and machines.

Every rule here also exists in the database. It is duplicated in the schema so
the caller gets a message naming the offending step or field, instead of a
constraint name surfaced as a generic 409 - and so a bad payload never reaches a
transaction at all.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.modules.routing.schemas import (
    MAX_OPERATIONS_PER_ROUTING,
    MachineCreate,
    RoutingCreate,
    RoutingOperationIn,
    RoutingUpdate,
    WorkCenterCreate,
)

WORK_CENTER = uuid.uuid4()
MACHINE = uuid.uuid4()
ITEM = uuid.uuid4()
VENDOR = uuid.uuid4()


def op(**overrides: object) -> RoutingOperationIn:
    payload: dict[str, object] = {
        "sequence": 10,
        "operation_code": "CUT",
        "operation_name": "Cut to length",
        "work_center_id": WORK_CENTER,
    }
    payload.update(overrides)
    return RoutingOperationIn(**payload)  # type: ignore[arg-type]


def routing_payload(**overrides: object) -> RoutingCreate:
    payload: dict[str, object] = {
        "code": "RT-001",
        "item_id": ITEM,
        "operations": [op()],
    }
    payload.update(overrides)
    return RoutingCreate(**payload)  # type: ignore[arg-type]


# -----------------------------------------------------------------------------
# Code normalisation
# -----------------------------------------------------------------------------


def test_codes_are_upper_cased_and_internally_collapsed() -> None:
    """``wc  1``, ``WC 1`` and ``wc 1`` must not become three work centres."""
    assert WorkCenterCreate(code="  wc   1 ", name="Cell 1").code == "WC 1"
    assert op(operation_code="  cut  01 ").operation_code == "CUT 01"
    assert routing_payload(code=" rt-001 ").code == "RT-001"


def test_an_omitted_code_on_a_partial_update_stays_omitted() -> None:
    # exclude_unset is what stops a PATCH from blanking fields it never sent, so
    # the validator must not materialise a default.
    assert RoutingUpdate().model_dump(exclude_unset=True) == {}


# -----------------------------------------------------------------------------
# Operations
# -----------------------------------------------------------------------------


def test_a_routing_needs_at_least_one_operation() -> None:
    with pytest.raises(ValidationError):
        routing_payload(operations=[])


def test_duplicate_sequences_are_rejected() -> None:
    """Two steps numbered 20 cannot both exist: the traveller and the unique
    constraint both address a step by its sequence."""
    with pytest.raises(ValidationError, match="Duplicate operation sequences"):
        routing_payload(operations=[op(sequence=20), op(sequence=20, operation_code="DRILL")])


def test_duplicate_operation_codes_are_rejected() -> None:
    with pytest.raises(ValidationError, match="Duplicate operation codes"):
        routing_payload(operations=[op(sequence=10), op(sequence=20)])


def test_the_operation_count_is_bounded() -> None:
    too_many = [
        op(sequence=n, operation_code=f"OP{n}") for n in range(1, MAX_OPERATIONS_PER_ROUTING + 2)
    ]
    with pytest.raises(ValidationError):
        routing_payload(operations=too_many)


def test_a_job_work_vendor_requires_an_outsourced_step() -> None:
    """Mirrors the CHECK constraint: a vendor on an in-house step is a mistake
    that would otherwise only surface as an integrity error."""
    with pytest.raises(ValidationError, match="outsourced"):
        op(job_work_vendor_id=VENDOR)

    assert op(job_work_vendor_id=VENDOR, is_outsourced=True).job_work_vendor_id == VENDOR


def test_an_outsourced_step_cannot_also_book_one_of_our_machines() -> None:
    # It runs on the vendor's equipment; naming ours would put phantom load on
    # a work centre that is actually idle.
    with pytest.raises(ValidationError, match="outsourced"):
        op(is_outsourced=True, machine_id=MACHINE)


def test_an_outsourced_step_may_have_no_vendor_yet() -> None:
    # A process can be known to be subcontracted long before the vendor is
    # chosen; only the reverse is forbidden.
    assert op(is_outsourced=True).job_work_vendor_id is None


def test_times_and_costs_cannot_be_negative() -> None:
    for field in (
        "setup_time_minutes",
        "run_time_per_unit_minutes",
        "queue_time_minutes",
        "move_time_minutes",
        "process_cost",
    ):
        with pytest.raises(ValidationError):
            op(**{field: Decimal("-1")})


def test_unknown_fields_are_refused_rather_than_silently_dropped() -> None:
    with pytest.raises(ValidationError):
        RoutingOperationIn(
            sequence=10,
            operation_code="CUT",
            operation_name="Cut",
            work_center_id=WORK_CENTER,
            company_id=uuid.uuid4(),  # type: ignore[call-arg]
        )


# -----------------------------------------------------------------------------
# Effectivity
# -----------------------------------------------------------------------------


def test_an_effectivity_window_cannot_end_before_it_starts() -> None:
    with pytest.raises(ValidationError, match="end before it starts"):
        routing_payload(effective_from=date(2026, 6, 1), effective_to=date(2026, 5, 1))


def test_a_window_that_opens_and_closes_on_one_day_is_allowed() -> None:
    payload = routing_payload(effective_from=date(2026, 6, 1), effective_to=date(2026, 6, 1))

    assert payload.effective_to == payload.effective_from


# -----------------------------------------------------------------------------
# Work centres and machines
# -----------------------------------------------------------------------------


def test_a_working_day_longer_than_a_calendar_day_is_rejected() -> None:
    with pytest.raises(ValidationError):
        WorkCenterCreate(code="WC1", name="Cell", working_hours_per_day=Decimal("25"))


def test_a_zero_length_working_day_is_rejected() -> None:
    # It is the divisor in the lead time; zero would be a division by zero on a
    # request path rather than a configuration choice.
    with pytest.raises(ValidationError):
        WorkCenterCreate(code="WC1", name="Cell", working_hours_per_day=Decimal("0"))


def test_the_default_working_day_is_one_shift() -> None:
    assert WorkCenterCreate(code="WC1", name="Cell").working_hours_per_day == Decimal("8")


def test_a_machine_cannot_be_filed_with_a_service_due_before_its_last_one() -> None:
    with pytest.raises(ValidationError, match="next service"):
        MachineCreate(
            code="LATHE-1",
            name="Lathe",
            work_center_id=WORK_CENTER,
            last_maintenance_date=date(2026, 6, 1),
            next_maintenance_date=date(2026, 5, 1),
        )


def test_a_machine_create_cannot_set_its_own_status_or_active_flag() -> None:
    """A new machine starts AVAILABLE and active.

    Accepting either from the payload would make it possible to file a machine
    that is simultaneously available and inactive.
    """
    for field in ("status", "is_active"):
        with pytest.raises(ValidationError):
            MachineCreate(
                code="LATHE-1",
                name="Lathe",
                work_center_id=WORK_CENTER,
                **{field: "RETIRED" if field == "status" else False},  # type: ignore[arg-type]
            )
