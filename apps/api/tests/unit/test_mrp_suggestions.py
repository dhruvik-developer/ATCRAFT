"""Suggestion type selection, order-quantity policy and the run lifecycle.

Pure logic, no database. Each of these is a decision a planner acts on directly:
the wrong suggestion type raises the wrong document, and the wrong quantity is
either an order a supplier cannot fulfil or one that leaves the shortage in place.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from src.core.errors import InvalidStateTransitionError
from src.modules.mrp.engine import (
    PlanningItem,
    apply_order_policy,
    choose_suggestion_type,
    is_explodable,
)
from src.modules.mrp.models import (
    MrpRunStatus,
    MrpSuggestionStatus,
    MrpSuggestionType,
)
from src.modules.mrp.schemas import (
    DEFAULT_CONVERSION_TARGET,
    BulkIgnoreRequest,
    ConversionTarget,
    ManualDemandIn,
    MrpRunRequest,
)
from src.modules.mrp.service import MRP_TASK_NAME, RUN_LIFECYCLE, SUGGESTION_LIFECYCLE
from src.modules.products.models import ProductionType

D = Decimal


def item(production_type: str, *, can_manufacture: bool = True) -> PlanningItem:
    return PlanningItem(
        id=uuid.uuid4(),
        code="PART-1",
        name="Part",
        base_uom_id=uuid.uuid4(),
        production_type=production_type,
        lead_time_days=7,
        min_order_quantity=D("0"),
        safety_stock=D("0"),
        can_purchase=True,
        can_manufacture=can_manufacture,
        can_job_work=True,
        is_active=True,
    )


# -----------------------------------------------------------------------------
# Suggestion type comes from the item master, never from item_group
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("production_type", "expected"),
    [
        (ProductionType.MAKE, MrpSuggestionType.MANUFACTURE),
        (ProductionType.BUY, MrpSuggestionType.PURCHASE),
        (ProductionType.JOB_WORK, MrpSuggestionType.JOB_WORK),
    ],
)
def test_production_type_decides_the_suggestion(
    production_type: str, expected: MrpSuggestionType
) -> None:
    assert choose_suggestion_type(item(production_type), has_default_bom=True) is expected


def test_make_or_buy_prefers_manufacture_when_an_approved_bom_exists() -> None:
    """docs/assumptions.md section 6.

    An approved default structure is the strongest evidence the part is genuinely
    made in-house; defaulting such an item to purchase would make its BOM
    pointless.
    """
    assert (
        choose_suggestion_type(item(ProductionType.MAKE_OR_BUY), has_default_bom=True)
        is MrpSuggestionType.MANUFACTURE
    )


def test_make_or_buy_falls_back_to_purchase_with_no_bom() -> None:
    assert (
        choose_suggestion_type(item(ProductionType.MAKE_OR_BUY), has_default_bom=False)
        is MrpSuggestionType.PURCHASE
    )


def test_a_make_item_with_no_bom_is_still_suggested_for_manufacture() -> None:
    """The item master says it is made here; a missing BOM is a data gap.

    Silently reclassifying it as a purchase would hide that gap behind a
    purchase order rather than getting the structure built.
    """
    assert (
        choose_suggestion_type(item(ProductionType.MAKE), has_default_bom=False)
        is MrpSuggestionType.MANUFACTURE
    )


def test_the_suggestion_type_does_not_depend_on_capability_flags_alone() -> None:
    """A part that *can* be manufactured but is normally bought is still bought.

    Master rules section 1: capability flags say what is possible, not what is
    planned. ``production_type`` is the planning statement.
    """
    dual = item(ProductionType.BUY, can_manufacture=True)
    assert choose_suggestion_type(dual, has_default_bom=True) is MrpSuggestionType.PURCHASE


# -----------------------------------------------------------------------------
# What gets exploded: the suggestion type settled for the item decides
# -----------------------------------------------------------------------------


def explodes(planning_item: PlanningItem, *, has_bom: bool, has_default_bom: bool) -> bool:
    """The engine's decision, composed as the run settles it.

    Discovery settles the suggestion type once and the explosion reads it, so
    these cases state each combination the way a run meets it.
    """
    return is_explodable(
        planning_item,
        has_bom=has_bom,
        suggestion_type=choose_suggestion_type(planning_item, has_default_bom=has_default_bom),
    )


def test_a_purchased_part_with_a_bom_on_file_is_not_exploded() -> None:
    """A structure kept for costing must not become material demand."""
    assert explodes(item(ProductionType.BUY), has_bom=True, has_default_bom=True) is False


def test_a_make_item_without_a_structure_cannot_be_exploded() -> None:
    assert explodes(item(ProductionType.MAKE), has_bom=False, has_default_bom=False) is False


@pytest.mark.parametrize("production_type", [ProductionType.MAKE, ProductionType.MAKE_OR_BUY])
def test_manufactured_items_with_a_structure_are_exploded(production_type: str) -> None:
    assert explodes(item(production_type), has_bom=True, has_default_bom=True) is True


@pytest.mark.parametrize("can_manufacture", [True, False])
def test_a_make_item_explodes_a_structure_in_force_that_is_not_its_default(
    can_manufacture: bool,
) -> None:
    """A MAKE item is made, so whichever approved structure is in force is exploded."""
    made = item(ProductionType.MAKE, can_manufacture=can_manufacture)
    assert explodes(made, has_bom=True, has_default_bom=False) is True


@pytest.mark.parametrize("can_manufacture", [True, False])
def test_a_make_or_buy_item_the_run_buys_is_not_exploded(can_manufacture: bool) -> None:
    """docs/assumptions.md section 6: the explosion reads the suggestion type.

    With no default structure in force the item is bought, so exploding the
    structure that is in force would plan its parts beside the purchase -
    whatever the item's capability flags allow.
    """
    dual = item(ProductionType.MAKE_OR_BUY, can_manufacture=can_manufacture)
    assert choose_suggestion_type(dual, has_default_bom=False) is MrpSuggestionType.PURCHASE
    assert explodes(dual, has_bom=True, has_default_bom=False) is False


@pytest.mark.parametrize(("can_manufacture", "exploded"), [(True, True), (False, False)])
def test_a_job_worked_item_is_exploded_only_when_it_is_also_manufacturable(
    can_manufacture: bool, exploded: bool
) -> None:
    """docs/assumptions.md section 6: the job-work rule is unchanged.

    A JOB_WORK suggestion is not a purchase, and the item's structure is
    exploded exactly when the item is also flagged manufacturable - the answer
    the predicate gave before the suggestion type decided it.
    """
    worked = item(ProductionType.JOB_WORK, can_manufacture=can_manufacture)
    assert explodes(worked, has_bom=True, has_default_bom=True) is exploded
    assert explodes(worked, has_bom=True, has_default_bom=False) is exploded


@pytest.mark.parametrize("production_type", list(ProductionType))
def test_a_purchase_suggestion_is_never_exploded_whatever_the_production_type(
    production_type: str,
) -> None:
    """The predicate reads the settled suggestion, never the production type again.

    A second reading of the type is how a bought MAKE_OR_BUY item came to be
    exploded; told the run buys the item, no production type may override it.
    """
    assert (
        is_explodable(
            item(production_type), has_bom=True, suggestion_type=MrpSuggestionType.PURCHASE
        )
        is False
    )


# -----------------------------------------------------------------------------
# Order quantity policy
# -----------------------------------------------------------------------------


def test_a_shortage_above_the_minimum_is_ordered_as_it_stands() -> None:
    assert apply_order_policy(D("140"), min_order_quantity=D("50"), order_multiple=D("0")) == D(
        "140.0000"
    )


def test_a_shortage_below_the_minimum_is_raised_to_it() -> None:
    assert apply_order_policy(D("12"), min_order_quantity=D("50"), order_multiple=D("0")) == D(
        "50.0000"
    )


def test_a_quantity_is_rounded_up_to_the_pack_multiple() -> None:
    """A vendor shipping in cartons of 50 cannot supply 51."""
    assert apply_order_policy(D("51"), min_order_quantity=D("0"), order_multiple=D("50")) == D(
        "100.0000"
    )


def test_an_exact_multiple_is_left_alone() -> None:
    assert apply_order_policy(D("100"), min_order_quantity=D("0"), order_multiple=D("50")) == D(
        "100.0000"
    )


def test_the_minimum_is_applied_before_the_multiple() -> None:
    """A 50-piece minimum with a 30-piece pack means 60, not 50.

    Rounding to the multiple first would leave 50 - a number the supplier cannot
    ship. The order of these two operations is the whole test.
    """
    assert apply_order_policy(D("12"), min_order_quantity=D("50"), order_multiple=D("30")) == D(
        "60.0000"
    )


def test_fractional_multiples_round_up_not_to_nearest() -> None:
    """Rounding a shortage *down* would ship a plan that knowingly under-orders."""
    assert apply_order_policy(
        D("10.0001"), min_order_quantity=D("0"), order_multiple=D("2.5")
    ) == D("12.5000")


def test_no_policy_leaves_the_shortage_untouched() -> None:
    assert apply_order_policy(D("7.5"), min_order_quantity=D("0"), order_multiple=D("0")) == D(
        "7.5000"
    )


def test_the_suggested_quantity_never_falls_below_the_shortage() -> None:
    """The property the database also enforces as a check constraint."""
    for shortage in ("1", "49", "50", "51", "999.9999"):
        result = apply_order_policy(D(shortage), min_order_quantity=D("25"), order_multiple=D("7"))
        assert result >= D(shortage)


def test_a_non_positive_shortage_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="positive shortage"):
        apply_order_policy(D("0"), min_order_quantity=D("10"), order_multiple=D("0"))


def test_negative_policy_quantities_are_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        apply_order_policy(D("10"), min_order_quantity=D("-1"), order_multiple=D("0"))


# -----------------------------------------------------------------------------
# State machines
# -----------------------------------------------------------------------------


def test_the_run_lifecycle_graph_is_structurally_sound() -> None:
    """No unreachable state, and no non-terminal state a run cannot leave."""
    RUN_LIFECYCLE.validate()
    SUGGESTION_LIFECYCLE.validate()


def test_a_completed_run_cannot_be_restarted() -> None:
    """The property that makes a re-delivered Celery task safe.

    ``task_acks_late`` means a worker crash re-delivers the task; without this
    refusal the second delivery would write a second set of suggestions against
    the same run.
    """
    with pytest.raises(InvalidStateTransitionError):
        RUN_LIFECYCLE.assert_transition(MrpRunStatus.COMPLETED, MrpRunStatus.RUNNING)


def test_a_queued_run_cannot_jump_straight_to_completed() -> None:
    with pytest.raises(InvalidStateTransitionError):
        RUN_LIFECYCLE.assert_transition(MrpRunStatus.QUEUED, MrpRunStatus.COMPLETED)


def test_a_running_run_may_fail_or_complete() -> None:
    assert RUN_LIFECYCLE.can_transition(MrpRunStatus.RUNNING, MrpRunStatus.COMPLETED)
    assert RUN_LIFECYCLE.can_transition(MrpRunStatus.RUNNING, MrpRunStatus.FAILED)


def test_a_converted_suggestion_cannot_be_reopened() -> None:
    """Re-opening it would invite a second order for a covered shortage."""
    with pytest.raises(InvalidStateTransitionError):
        SUGGESTION_LIFECYCLE.assert_transition(
            MrpSuggestionStatus.CONVERTED, MrpSuggestionStatus.OPEN
        )


def test_an_ignored_suggestion_cannot_be_converted() -> None:
    with pytest.raises(InvalidStateTransitionError):
        SUGGESTION_LIFECYCLE.assert_transition(
            MrpSuggestionStatus.IGNORED, MrpSuggestionStatus.CONVERTED
        )


def test_conversion_is_marked_irreversible() -> None:
    """The UI demands confirmation for it because the flag says so."""
    transition = SUGGESTION_LIFECYCLE.find(MrpSuggestionStatus.OPEN, MrpSuggestionStatus.CONVERTED)
    assert transition is not None
    assert transition.is_irreversible is True


# -----------------------------------------------------------------------------
# Conversion targets
# -----------------------------------------------------------------------------


def test_each_suggestion_type_has_a_default_document() -> None:
    assert set(DEFAULT_CONVERSION_TARGET) == set(MrpSuggestionType)
    assert (
        DEFAULT_CONVERSION_TARGET[MrpSuggestionType.PURCHASE]
        is ConversionTarget.PURCHASE_REQUISITION
    )
    assert (
        DEFAULT_CONVERSION_TARGET[MrpSuggestionType.MANUFACTURE]
        is ConversionTarget.PRODUCTION_ORDER
    )
    assert DEFAULT_CONVERSION_TARGET[MrpSuggestionType.JOB_WORK] is ConversionTarget.JOB_WORK_ORDER
    assert DEFAULT_CONVERSION_TARGET[MrpSuggestionType.TRANSFER] is ConversionTarget.STOCK_TRANSFER
    assert (
        DEFAULT_CONVERSION_TARGET[MrpSuggestionType.REALLOCATION]
        is ConversionTarget.REALLOCATION_REQUEST
    )


# -----------------------------------------------------------------------------
# Request validation
# -----------------------------------------------------------------------------


def test_a_run_request_deduplicates_its_warehouse_scope() -> None:
    warehouse = uuid.uuid4()
    payload = MrpRunRequest(warehouse_ids=[warehouse, warehouse])
    assert payload.warehouse_ids == [warehouse]


def test_a_run_request_refuses_an_unbounded_warehouse_list() -> None:
    with pytest.raises(ValueError, match="at most"):
        MrpRunRequest(warehouse_ids=[uuid.uuid4() for _ in range(51)])


def test_a_run_request_refuses_a_bulk_import_of_manual_demand() -> None:
    line = ManualDemandIn(item_id=uuid.uuid4(), quantity=D("1"), required_date=date(2026, 7, 1))
    with pytest.raises(ValueError, match="manual demand lines"):
        MrpRunRequest(manual_demand=[line] * 201)


def test_manual_demand_must_be_a_positive_quantity() -> None:
    with pytest.raises(ValueError):
        ManualDemandIn(item_id=uuid.uuid4(), quantity=D("0"), required_date=date(2026, 7, 1))


def test_ignoring_suggestions_requires_a_reason() -> None:
    """Section 83: "the planner clicked ignore" is not a reason."""
    with pytest.raises(ValueError):
        BulkIgnoreRequest(suggestion_ids=[uuid.uuid4()], reason="")


def test_bulk_ignore_deduplicates_and_bounds_its_input() -> None:
    duplicated = uuid.uuid4()
    payload = BulkIgnoreRequest(
        suggestion_ids=[duplicated, duplicated], reason="Covered by an existing order"
    )
    assert payload.suggestion_ids == [duplicated]

    with pytest.raises(ValueError, match="At most 500"):
        BulkIgnoreRequest(
            suggestion_ids=[uuid.uuid4() for _ in range(501)], reason="Too many at once"
        )


# -----------------------------------------------------------------------------
# The worker entry point
# -----------------------------------------------------------------------------


def test_the_worker_task_registers_under_the_name_the_service_dispatches() -> None:
    """The API sends by name and never imports the worker, so the two must agree.

    Importing the task module here also exercises the registration itself: a
    decorator that raises at import time would take the whole worker down at
    start-up, where it is much more expensive to discover.
    """
    from src.workers.tasks.mrp import run_mrp

    assert run_mrp.name == MRP_TASK_NAME
    assert run_mrp.queue == "mrp"
