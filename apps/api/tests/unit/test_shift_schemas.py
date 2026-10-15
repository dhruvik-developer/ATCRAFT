"""The shift and roster contracts, and the two state machines behind them.

Each refusal here is one a form would otherwise discover as a database error: a
break as long as its shift, a code changed under the bookings that copied it, a
roster read over a year, a copy onto the day it copies.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, time, timedelta

import pytest
from pydantic import ValidationError

from src.core.errors import InvalidStateTransitionError
from src.modules.routing.models import ShiftAssignmentStatus
from src.modules.routing.schemas import (
    MAX_ROSTER_BULK,
    MAX_ROSTER_RANGE_DAYS,
    ShiftAssignmentBulkCreate,
    ShiftAssignmentCreate,
    ShiftAssignmentUpdate,
    ShiftCreate,
    ShiftRosterCopyRequest,
    ShiftRosterFilters,
    ShiftUpdate,
    break_problem,
)
from src.modules.routing.service import (
    SHIFT_ASSIGNMENT_FLOW,
    SHIFT_LIFECYCLE,
    MasterLifecycleState,
)

pytestmark = pytest.mark.unit

DAY = date(2026, 10, 12)


def _shift(**overrides: object) -> ShiftCreate:
    values: dict[str, object] = {
        "code": "A",
        "name": "Morning",
        "start_time": time(6),
        "end_time": time(14),
    }
    values.update(overrides)
    return ShiftCreate.model_validate(values)


def _fields(error: ValidationError) -> set[str]:
    return {".".join(str(part) for part in item["loc"]) for item in error.errors()}


# --- the shift master --------------------------------------------------------


def test_the_code_is_normalised_on_the_way_in() -> None:
    assert _shift(code=" gen ").code == "GEN"


def test_the_midnight_flag_is_not_something_a_client_states() -> None:
    with pytest.raises(ValidationError):
        _shift(crosses_midnight=True)


def test_a_night_shift_needs_no_flag_to_be_valid() -> None:
    night = _shift(code="C", start_time=time(22), end_time=time(6))
    assert (night.start_time, night.end_time) == (time(22), time(6))


@pytest.mark.parametrize("break_minutes", [480, 600])
def test_a_break_as_long_as_the_shift_is_refused_against_the_break(break_minutes: int) -> None:
    with pytest.raises(ValidationError) as caught:
        _shift(break_minutes=break_minutes)
    assert _fields(caught.value) == {"break_minutes"}
    assert "480 minutes" in str(caught.value)


def test_a_break_inside_the_shift_is_accepted() -> None:
    assert _shift(code="GEN", end_time=time(17, 30), break_minutes=30).break_minutes == 30


def test_the_break_is_measured_across_midnight_for_a_night_shift() -> None:
    assert break_problem(time(22), time(6), 479) is None
    assert break_problem(time(22), time(6), 480) is not None


@pytest.mark.parametrize(
    "moment",
    [time(6, 0, 30), time(6, 0, 0, 5), time(6, tzinfo=UTC)],
)
def test_a_shift_time_is_a_local_clock_reading_in_whole_minutes(moment: time) -> None:
    with pytest.raises(ValidationError) as caught:
        _shift(start_time=moment)
    assert _fields(caught.value) == {"start_time"}


def test_the_code_cannot_be_changed_after_creation() -> None:
    with pytest.raises(ValidationError):
        ShiftUpdate.model_validate({"code": "B"})


def test_activation_is_not_a_field_of_the_edit() -> None:
    # It moves through the lifecycle endpoints, which carry a reason.
    with pytest.raises(ValidationError):
        ShiftUpdate.model_validate({"is_active": False})


@pytest.mark.parametrize("field", ["name", "start_time", "end_time", "break_minutes", "sort_order"])
def test_an_edit_cannot_blank_a_required_column(field: str) -> None:
    with pytest.raises(ValidationError) as caught:
        ShiftUpdate.model_validate({field: None})
    assert _fields(caught.value) == {field}


def test_a_partial_edit_only_carries_what_was_sent() -> None:
    edit = ShiftUpdate.model_validate({"name": "Early"})
    assert edit.model_dump(exclude_unset=True) == {"name": "Early"}


# --- roster requests -----------------------------------------------------------


def _assignment(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "roster_date": DAY.isoformat(),
        "shift_id": str(uuid.uuid4()),
        "work_center_id": str(uuid.uuid4()),
        "operator_user_id": str(uuid.uuid4()),
    }
    values.update(overrides)
    return values


def test_an_assignment_without_a_machine_is_a_cell_level_plan() -> None:
    assert ShiftAssignmentCreate.model_validate(_assignment()).machine_id is None


def test_a_bulk_request_is_bounded_both_ways() -> None:
    with pytest.raises(ValidationError):
        ShiftAssignmentBulkCreate.model_validate({"assignments": []})
    with pytest.raises(ValidationError):
        ShiftAssignmentBulkCreate.model_validate(
            {"assignments": [_assignment() for _ in range(MAX_ROSTER_BULK + 1)]}
        )
    full = ShiftAssignmentBulkCreate.model_validate(
        {"assignments": [_assignment() for _ in range(MAX_ROSTER_BULK)]}
    )
    assert len(full.assignments) == MAX_ROSTER_BULK


def test_re_staffing_keeps_the_operator_and_the_cell_but_may_drop_the_machine() -> None:
    for field in ("operator_user_id", "work_center_id"):
        with pytest.raises(ValidationError) as caught:
            ShiftAssignmentUpdate.model_validate({field: None})
        assert _fields(caught.value) == {field}
    cell_level = ShiftAssignmentUpdate.model_validate({"machine_id": None})
    assert cell_level.model_dump(exclude_unset=True) == {"machine_id": None}


def test_re_staffing_cannot_move_the_window() -> None:
    with pytest.raises(ValidationError):
        ShiftAssignmentUpdate.model_validate({"roster_date": DAY.isoformat()})
    with pytest.raises(ValidationError):
        ShiftAssignmentUpdate.model_validate({"shift_id": str(uuid.uuid4())})


def test_a_roster_read_covers_at_most_a_month_both_ends_included() -> None:
    last = DAY + timedelta(days=MAX_ROSTER_RANGE_DAYS - 1)
    assert ShiftRosterFilters(date_from=DAY, date_to=last).date_to == last
    with pytest.raises(ValidationError) as caught:
        ShiftRosterFilters(date_from=DAY, date_to=last + timedelta(days=1))
    assert _fields(caught.value) == {"date_to"}


def test_a_roster_read_the_wrong_way_round_is_refused() -> None:
    with pytest.raises(ValidationError, match="ends before it starts"):
        ShiftRosterFilters(date_from=DAY, date_to=DAY - timedelta(days=1))


def test_a_copy_may_not_land_on_the_day_it_copies() -> None:
    with pytest.raises(ValidationError, match="cannot include the day being copied"):
        ShiftRosterCopyRequest(
            source_date=DAY, target_from=DAY - timedelta(days=1), target_to=DAY + timedelta(days=1)
        )
    copy = ShiftRosterCopyRequest(
        source_date=DAY, target_from=DAY + timedelta(days=1), target_to=DAY + timedelta(days=6)
    )
    assert copy.work_center_id is None


def test_a_copy_range_is_bounded_like_a_read() -> None:
    with pytest.raises(ValidationError):
        ShiftRosterCopyRequest(
            source_date=DAY,
            target_from=DAY + timedelta(days=1),
            target_to=DAY + timedelta(days=MAX_ROSTER_RANGE_DAYS + 1),
        )


# --- state machines ----------------------------------------------------------


def test_both_machines_are_well_formed() -> None:
    SHIFT_LIFECYCLE.validate()
    SHIFT_ASSIGNMENT_FLOW.validate()
    assert SHIFT_LIFECYCLE.entity == "Shift"
    assert SHIFT_ASSIGNMENT_FLOW.entity == "Shift assignment"


def test_a_planned_assignment_can_only_be_cancelled_and_never_revived() -> None:
    SHIFT_ASSIGNMENT_FLOW.assert_transition(
        ShiftAssignmentStatus.PLANNED, ShiftAssignmentStatus.CANCELLED
    )
    assert SHIFT_ASSIGNMENT_FLOW.is_terminal(ShiftAssignmentStatus.CANCELLED)
    with pytest.raises(InvalidStateTransitionError):
        SHIFT_ASSIGNMENT_FLOW.assert_transition(
            ShiftAssignmentStatus.CANCELLED, ShiftAssignmentStatus.PLANNED
        )


def test_a_retired_shift_is_not_restored() -> None:
    for start in (MasterLifecycleState.ACTIVE, MasterLifecycleState.INACTIVE):
        SHIFT_LIFECYCLE.assert_transition(start, MasterLifecycleState.ARCHIVED)
    with pytest.raises(InvalidStateTransitionError):
        SHIFT_LIFECYCLE.assert_transition(
            MasterLifecycleState.ARCHIVED, MasterLifecycleState.INACTIVE
        )
