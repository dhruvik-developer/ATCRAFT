"""The pure rules WP E4 adds to assembly: the kit's gap, lateness, top-ups, the
reservation date and the structures an order may be built from.

No database: each rule takes plain values (or transient rows) so the arithmetic
is pinned on its own, and the integration tests only have to show it is wired.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.core.errors import BusinessRuleError, NotFoundError
from src.modules.assembly import service
from src.modules.assembly.models import (
    DEFAULT_ASSEMBLY_PRIORITY,
    AssemblyOrderComponent,
    AssemblyOrderStatus,
)
from src.modules.assembly.schemas import AssemblyBoardFilters, AssemblyOrderCreate
from src.modules.bom.models import BomType

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
HANDLE, KEYS = uuid.uuid4(), uuid.uuid4()


def need(line: int, item: uuid.UUID, required: str, *, reserved: bool = False):
    return service.ComponentNeed(
        line_number=line, item_id=item, required=Decimal(required), is_reserved=reserved
    )


# -----------------------------------------------------------------------------
# The kit's gap
# -----------------------------------------------------------------------------


def test_a_covered_kit_lacks_nothing() -> None:
    gaps = service.component_shortages(
        [need(1, HANDLE, "10"), need(2, KEYS, "20")], {HANDLE: Decimal("10"), KEYS: Decimal("25")}
    )
    assert gaps == {1: 0, 2: 0}


def test_a_component_with_no_stock_at_all_lacks_its_whole_requirement() -> None:
    gaps = service.component_shortages([need(1, HANDLE, "10")], {})
    assert gaps == {1: Decimal("10.0000")}


def test_two_lines_of_one_item_cannot_both_be_promised_the_same_shelf() -> None:
    gaps = service.component_shortages(
        [need(1, HANDLE, "6"), need(2, HANDLE, "6")], {HANDLE: Decimal("10")}
    )
    assert gaps == {1: 0, 2: Decimal("2.0000")}


def test_a_line_already_reserved_lacks_nothing_and_takes_no_shelf() -> None:
    gaps = service.component_shortages(
        [need(1, HANDLE, "8", reserved=True), need(2, HANDLE, "4")], {HANDLE: Decimal("5")}
    )
    assert gaps == {1: 0, 2: 0}


def test_negative_availability_counts_as_none() -> None:
    assert service.component_shortages([need(1, KEYS, "3")], {KEYS: Decimal("-2")}) == {
        1: Decimal("3.0000")
    }


# -----------------------------------------------------------------------------
# Lateness on the board (docs/assumptions.md §E4-10)
# -----------------------------------------------------------------------------


def late(status: AssemblyOrderStatus, **times: datetime | None) -> service.Lateness:
    return service.lateness(
        status=status,
        planned_start=times.get("planned_start"),
        planned_end=times.get("planned_end"),
        actual_start=times.get("actual_start"),
        now=NOW,
    )


def test_an_open_order_past_its_planned_end_is_late_by_whole_days() -> None:
    result = late(AssemblyOrderStatus.IN_PROGRESS, planned_end=NOW - timedelta(days=2, hours=3))
    assert result.is_late
    assert result.days_late == 2


def test_output_still_on_qc_hold_is_still_late() -> None:
    assert late(AssemblyOrderStatus.QC_PENDING, planned_end=NOW - timedelta(hours=1)).is_late


def test_a_completed_order_is_never_late() -> None:
    result = late(AssemblyOrderStatus.COMPLETED, planned_end=NOW - timedelta(days=9))
    assert not result.is_late
    assert result.days_late == 0


def test_a_start_is_overdue_only_while_the_bench_has_not_begun() -> None:
    past = NOW - timedelta(hours=2)
    assert late(AssemblyOrderStatus.RELEASED, planned_start=past).start_overdue
    assert late(AssemblyOrderStatus.COMPONENTS_ISSUED, planned_start=past).start_overdue
    assert not late(AssemblyOrderStatus.IN_PROGRESS, planned_start=past).start_overdue
    assert not late(
        AssemblyOrderStatus.APPROVED, planned_start=past, actual_start=past
    ).start_overdue


def test_an_order_with_no_plan_is_never_late() -> None:
    result = late(AssemblyOrderStatus.APPROVED)
    assert result == service.Lateness(is_late=False, start_overdue=False, days_late=0)


# -----------------------------------------------------------------------------
# Top-ups (docs/assumptions.md §E4-5)
# -----------------------------------------------------------------------------


def component(line: int, required: str, issued: str = "0") -> AssemblyOrderComponent:
    return AssemblyOrderComponent(
        line_number=line,
        item_id=uuid.uuid4(),
        required_quantity=Decimal(required),
        issued_quantity=Decimal(issued),
    )


def test_the_first_issue_hands_over_every_line_in_full_unless_told_otherwise() -> None:
    plan = service.plan_issue(
        [component(1, "10"), component(2, "20")], {2: Decimal("15")}, top_up=False
    )
    assert plan == {1: Decimal("10.0000"), 2: Decimal("15.0000")}


def test_nothing_beyond_the_reserved_requirement_is_issued() -> None:
    with pytest.raises(BusinessRuleError, match="at most"):
        service.plan_issue([component(1, "10")], {1: Decimal("11")}, top_up=False)


def test_a_top_up_raises_the_totals_and_brings_omitted_lines_up_to_the_requirement() -> None:
    plan = service.plan_issue(
        [component(1, "10", issued="10"), component(2, "20", issued="5")], {}, top_up=True
    )
    assert plan == {1: Decimal("10.0000"), 2: Decimal("20.0000")}


def test_a_top_up_cannot_lower_what_is_already_on_the_bench() -> None:
    with pytest.raises(BusinessRuleError, match="cannot lower"):
        service.plan_issue([component(1, "10", issued="8")], {1: Decimal("6")}, top_up=True)


def test_a_top_up_that_changes_nothing_is_refused() -> None:
    with pytest.raises(BusinessRuleError, match="nothing to top up"):
        service.plan_issue([component(1, "10", issued="10")], {}, top_up=True)


def test_an_unknown_line_is_refused_before_anything_is_planned() -> None:
    with pytest.raises(NotFoundError):
        service.plan_issue([component(1, "10")], {9: Decimal("1")}, top_up=False)


# -----------------------------------------------------------------------------
# The reservation's date, and the structures an order may use
# -----------------------------------------------------------------------------


def test_the_reservation_date_is_the_planned_start_as_a_utc_business_date() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    # 02:00 IST on the 13th is 20:30 UTC on the 12th.
    assert service.reservation_required_date(datetime(2026, 10, 13, 2, 0, tzinfo=ist)) == date(
        2026, 10, 12
    )
    assert service.reservation_required_date(None) is None


def test_an_order_is_built_from_an_assembly_or_a_production_structure() -> None:
    assert set(service.ASSEMBLABLE_BOM_TYPES) == {BomType.ASSEMBLY, BomType.PRODUCTION}


# -----------------------------------------------------------------------------
# Schemas
# -----------------------------------------------------------------------------


def _create(**fields: object) -> AssemblyOrderCreate:
    return AssemblyOrderCreate(
        item_id=uuid.uuid4(),
        planned_quantity=Decimal("5"),
        warehouse_id=uuid.uuid4(),
        **fields,  # type: ignore[arg-type]
    )


def test_an_order_defaults_to_the_allocation_scales_default_priority() -> None:
    assert _create().priority == DEFAULT_ASSEMBLY_PRIORITY == 100


@pytest.mark.parametrize("priority", [-1, 10_000])
def test_a_priority_off_the_scale_is_refused(priority: int) -> None:
    with pytest.raises(ValidationError):
        _create(priority=priority)


def test_the_board_takes_one_line_selector_at_a_time() -> None:
    with pytest.raises(ValidationError, match="not both"):
        AssemblyBoardFilters(assembly_line="Bench 1", unassigned_line=True)
    assert AssemblyBoardFilters(unassigned_line=True).assembly_line is None
