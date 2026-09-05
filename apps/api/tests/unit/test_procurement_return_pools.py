"""Which stock pool a purchase return leaves.

A return always posted out of ``AVAILABLE``. Quality moves a lot that fails
incoming inspection from QC hold into ``DAMAGED``, so the canonical purchase
return - goods held, failed, sent back - either could not post at all (a
batch-tracked item's AVAILABLE grain for that lot is empty: "available 0") or
deducted good stock and left the damaged units in place.

The rule is pure and lives in ``service_rules``: given what a receipt line
holds in each pool, decide and bound the pool one return line leaves. No
database; the service builds the facts from two reads and hands them here.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.errors import BusinessRuleError, ValidationError
from src.modules.inventory.models import StockState
from src.modules.procurement.models import RETURNABLE_STOCK_STATES
from src.modules.procurement.schemas import PurchaseReturnLineIn
from src.modules.procurement.service import _inspection_outcome
from src.modules.procurement.service_rules import (
    RETURN_POOL_AVAILABLE,
    RETURN_POOL_DAMAGED,
    InspectionOutcome,
    ReturnPoolFacts,
    resolve_return_pool,
    returnable_by_pool,
)

pytestmark = pytest.mark.unit

D = Decimal


def resolve(quantity: str, facts: ReturnPoolFacts, pool: str | None = None) -> str:
    return resolve_return_pool(
        line_number=1,
        item_code="RM-001",
        receipt_code="GRN-0001",
        quantity=D(quantity),
        requested_pool=pool,
        facts=facts,
    )


def released(accepted: str, returned: str = "0") -> ReturnPoolFacts:
    """A line received straight into available stock."""
    return ReturnPoolFacts(
        accepted_quantity=D(accepted), held_for_qc=False, returned_available=D(returned)
    )


def inspected(
    accepted: str,
    rejected: str,
    *,
    still_held: str = "0",
    returned_available: str = "0",
    returned_damaged: str = "0",
) -> ReturnPoolFacts:
    """A line received into QC hold whose inspection has decided."""
    total = D(accepted) + D(rejected) + D(still_held)
    return ReturnPoolFacts(
        accepted_quantity=total,
        held_for_qc=True,
        inspection=InspectionOutcome(
            accepted=D(accepted), rejected=D(rejected), still_held=D(still_held)
        ),
        returned_available=D(returned_available),
        returned_damaged=D(returned_damaged),
    )


# -----------------------------------------------------------------------------
# The vocabulary agrees across the module and with inventory
# -----------------------------------------------------------------------------


def test_the_pools_a_return_may_leave_are_real_inventory_pools() -> None:
    assert RETURNABLE_STOCK_STATES == (RETURN_POOL_AVAILABLE, RETURN_POOL_DAMAGED)
    for state in RETURNABLE_STOCK_STATES:
        assert StockState(state)  # the movement is posted with StockState(line.stock_state)
    assert PurchaseReturnLineIn.model_fields["stock_state"].default is None


# -----------------------------------------------------------------------------
# A line received into available stock: unchanged behaviour
# -----------------------------------------------------------------------------


def test_released_stock_returns_from_available_and_has_nothing_in_damaged() -> None:
    assert returnable_by_pool(released("100", "25")) == {
        RETURN_POOL_AVAILABLE: D("75"),
        RETURN_POOL_DAMAGED: D("0"),
    }
    assert resolve("75", released("100", "25")) == RETURN_POOL_AVAILABLE


def test_asking_for_damaged_on_a_released_line_is_refused_with_the_pool_named() -> None:
    with pytest.raises(BusinessRuleError, match="returnable from damaged stock"):
        resolve("1", released("100"), pool=RETURN_POOL_DAMAGED)


# -----------------------------------------------------------------------------
# A line held for inspection: the inspection decides the pool
# -----------------------------------------------------------------------------


def test_a_fully_rejected_lot_returns_from_damaged() -> None:
    """The canonical purchase return, which used to fail with 'available 0'."""
    facts = inspected(accepted="0", rejected="100")
    assert resolve("100", facts) == RETURN_POOL_DAMAGED
    assert resolve("100", facts, pool=RETURN_POOL_DAMAGED) == RETURN_POOL_DAMAGED
    with pytest.raises(BusinessRuleError, match="returnable from available stock"):
        resolve("1", facts, pool=RETURN_POOL_AVAILABLE)


def test_a_fully_accepted_lot_returns_from_available() -> None:
    assert resolve("40", inspected(accepted="100", rejected="0")) == RETURN_POOL_AVAILABLE


def test_rejected_stock_goes_back_first_when_it_covers_the_line() -> None:
    """ASSUMPTION in ``resolve_return_pool``: damaged first, then available."""
    facts = inspected(accepted="60", rejected="40")
    assert resolve("40", facts) == RETURN_POOL_DAMAGED
    assert resolve("10", facts) == RETURN_POOL_DAMAGED
    # More than damaged holds, but available covers it alone.
    assert resolve("50", facts) == RETURN_POOL_AVAILABLE


def test_a_line_that_would_need_both_pools_is_asked_to_split() -> None:
    facts = inspected(accepted="60", rejected="40")
    with pytest.raises(BusinessRuleError, match="Split the line") as excinfo:
        resolve("70", facts)
    assert excinfo.value.details["returnable_available"] == "60.0000"
    assert excinfo.value.details["returnable_damaged"] == "40.0000"


def test_each_pool_is_bounded_net_of_what_earlier_returns_took_from_it() -> None:
    facts = inspected(accepted="60", rejected="40", returned_damaged="30", returned_available="50")
    assert returnable_by_pool(facts) == {
        RETURN_POOL_AVAILABLE: D("10"),
        RETURN_POOL_DAMAGED: D("10"),
    }
    with pytest.raises(BusinessRuleError, match=r"only 10\.0000 of RM-001"):
        resolve("11", facts, pool=RETURN_POOL_DAMAGED)
    with pytest.raises(BusinessRuleError, match=r"only 10\.0000 of RM-001"):
        resolve("11", facts, pool=RETURN_POOL_AVAILABLE)


def test_quantity_the_inspection_left_on_hold_is_in_neither_pool() -> None:
    facts = inspected(accepted="50", rejected="20", still_held="30")
    assert returnable_by_pool(facts) == {
        RETURN_POOL_AVAILABLE: D("50"),
        RETURN_POOL_DAMAGED: D("20"),
    }
    with pytest.raises(BusinessRuleError):
        resolve("71", facts)


# -----------------------------------------------------------------------------
# Still on QC hold: nothing can leave until quality decides
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "facts",
    [
        # No inspection known at all - none raised, or the quality module is absent.
        ReturnPoolFacts(accepted_quantity=D("100"), held_for_qc=True, inspection=None),
        # Raised but not decided: the row carries no disposition yet.
        ReturnPoolFacts(
            accepted_quantity=D("100"), held_for_qc=True, inspection=InspectionOutcome()
        ),
    ],
    ids=["no-inspection", "undecided"],
)
def test_a_held_line_cannot_be_returned_before_the_inspection_decides(
    facts: ReturnPoolFacts,
) -> None:
    with pytest.raises(BusinessRuleError, match="still on QC hold"):
        resolve("1", facts)
    with pytest.raises(BusinessRuleError, match="still on QC hold"):
        resolve("1", facts, pool=RETURN_POOL_DAMAGED)


def test_an_unknown_pool_is_a_validation_error_not_a_stock_error() -> None:
    with pytest.raises(ValidationError):
        resolve("1", released("100"), pool="QC_HOLD")


# -----------------------------------------------------------------------------
# Reading an inspection row without importing quality's types
# -----------------------------------------------------------------------------


class _Inspection:
    def __init__(self, **quantities: str) -> None:
        for name, value in quantities.items():
            setattr(self, name, Decimal(value))


def test_an_inspection_row_is_read_by_disposition_not_by_status_vocabulary() -> None:
    outcome = _inspection_outcome(
        _Inspection(
            accepted_quantity="50",
            rejected_quantity="20",
            rework_quantity="10",
            hold_quantity="5",
            scrap_quantity="15",
        )
    )
    assert outcome == InspectionOutcome(
        accepted=D("50"), rejected=D("20"), still_held=D("15"), scrap=D("15")
    )
    assert outcome.decided


def test_a_row_with_no_disposition_is_undecided_and_none_is_no_row() -> None:
    assert _inspection_outcome(None) is None
    undecided = _inspection_outcome(_Inspection(accepted_quantity="0", rejected_quantity="0"))
    assert undecided is not None and not undecided.decided
