"""Dispatch ships sales consignments only.

The defect behind this file: a TRANSFER, JOB_WORK or PURCHASE_RETURN consignment
was released through the same ``DISPATCH`` outflow as a sale - the goods stopped
being ours whatever the type said. The rule itself is tiny; what is pinned here
is that it names where each movement belongs, that every value of the enum is
accounted for, and that the request schema leaves the refusal to it rather than
asking for a vendor first.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from pydantic import ValidationError as PydanticValidationError

from src.core.errors import BusinessRuleError
from src.modules.dispatch.models import DispatchStatus, DispatchType
from src.modules.dispatch.schemas import DispatchCreate, DispatchLineIn, DispatchUpdate
from src.modules.dispatch.service import (
    ACTION_ONLY_STATUSES,
    DISPATCH_LIFECYCLE,
    NON_SALES_MOVEMENT_OWNERS,
    SHIPPABLE_DISPATCH_TYPES,
    assert_sales_consignment,
    non_sales_dispatch_message,
)

OWNING_MODULE = {
    DispatchType.TRANSFER: "Inventory",
    DispatchType.JOB_WORK: "Job Work",
    DispatchType.PURCHASE_RETURN: "Procurement",
}


def test_every_dispatch_type_is_either_shipped_or_sent_somewhere() -> None:
    # A value added to the enum must be decided on, not silently shipped.
    assert frozenset({DispatchType.SALES}) == SHIPPABLE_DISPATCH_TYPES
    assert set(NON_SALES_MOVEMENT_OWNERS) | SHIPPABLE_DISPATCH_TYPES == set(DispatchType)
    assert not set(NON_SALES_MOVEMENT_OWNERS) & SHIPPABLE_DISPATCH_TYPES


def test_a_sales_consignment_passes_silently() -> None:
    assert_sales_consignment(DispatchType.SALES)
    # The column comes back from the database as a plain string.
    assert_sales_consignment("SALES", code="DSP/2026-27/00001")


@pytest.mark.parametrize("dispatch_type", list(OWNING_MODULE))
def test_a_non_sales_consignment_is_refused_naming_its_owning_module(
    dispatch_type: DispatchType,
) -> None:
    with pytest.raises(BusinessRuleError, match=OWNING_MODULE[dispatch_type]) as excinfo:
        assert_sales_consignment(str(dispatch_type))

    assert excinfo.value.details == {
        "dispatch_type": str(dispatch_type),
        "shippable_dispatch_types": ["SALES"],
    }
    assert "cannot be raised" in excinfo.value.message


def test_an_existing_plan_is_told_its_way_out() -> None:
    message = non_sales_dispatch_message(DispatchType.JOB_WORK, code="DSP/2026-27/00042")

    assert message.startswith("DSP/2026-27/00042 is a job work consignment.")
    assert "can be cancelled" in message
    assert "Job Work" in message


def _payload(dispatch_type: DispatchType, **overrides: object) -> DispatchCreate:
    body: dict[str, object] = {
        "dispatch_type": dispatch_type,
        "warehouse_id": uuid.uuid4(),
        "dispatch_date": date(2026, 9, 1),
        "lines": [DispatchLineIn(item_id=uuid.uuid4(), quantity=1)],
    }
    body.update(overrides)
    return DispatchCreate(**body)  # type: ignore[arg-type]


@pytest.mark.parametrize("dispatch_type", list(OWNING_MODULE))
def test_the_schema_leaves_a_non_sales_type_to_the_service(dispatch_type: DispatchType) -> None:
    # Before, a JOB_WORK request with no vendor was told to name one - and then
    # refused anyway. The service's refusal is the one that says where to go.
    payload = _payload(dispatch_type)
    assert payload.dispatch_type is dispatch_type


def test_a_sales_consignment_still_needs_its_customer() -> None:
    with pytest.raises(PydanticValidationError, match="must name the customer"):
        _payload(DispatchType.SALES)


def test_an_amendment_cannot_change_the_type() -> None:
    with pytest.raises(PydanticValidationError, match="dispatch_type"):
        DispatchUpdate(dispatch_type=DispatchType.TRANSFER)  # type: ignore[call-arg]


def test_the_action_only_statuses_are_real_edges_with_their_own_actions() -> None:
    # Each is reachable in the machine - which is exactly why an event could walk
    # a consignment into it without the action's side effects.
    for status in ACTION_ONLY_STATUSES:
        assert any(transition.target is status for transition in DISPATCH_LIFECYCLE.transitions)
    assert set(ACTION_ONLY_STATUSES) == {
        DispatchStatus.READY_FOR_DISPATCH,
        DispatchStatus.DISPATCHED,
        DispatchStatus.CANCELLED,
    }
