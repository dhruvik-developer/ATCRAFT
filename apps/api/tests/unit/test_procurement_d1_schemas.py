"""Boundary validation for duplicate resolution and PO amendments.

The source-allocation payload makes the buyer choose which requisition loses
quantity when an amended physical line can no longer cover all linked demand.
These tests pin the request contract before exercising the transactional rules.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from src.modules.procurement.schemas import (
    PurchaseOrderAmendLineIn,
    PurchaseOrderAmendRequest,
    PurchaseOrderApprovalRequest,
    PurchaseOrderLineIn,
    PurchaseOrderSourceAllocationIn,
)

pytestmark = pytest.mark.unit


def test_source_allocations_preserve_distinct_requisition_quantities() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    line = PurchaseOrderLineIn(
        line_number=1,
        item_id=uuid.uuid4(),
        quantity="12",
        unit_price="20.50",
        source_allocations=[
            PurchaseOrderSourceAllocationIn(requisition_line_id=first, quantity="4"),
            PurchaseOrderSourceAllocationIn(requisition_line_id=second, quantity="6"),
        ],
    )
    assert [allocation.requisition_line_id for allocation in line.source_allocations] == [
        first,
        second,
    ]
    assert sum(allocation.quantity for allocation in line.source_allocations) == 10


@pytest.mark.parametrize("quantity", ["0", "-1", "0.00001"])
def test_source_allocation_quantity_must_be_positive_and_representable(quantity: str) -> None:
    with pytest.raises(ValidationError):
        PurchaseOrderSourceAllocationIn(requisition_line_id=uuid.uuid4(), quantity=quantity)


def test_a_requisition_cannot_be_allocated_twice_on_one_physical_line() -> None:
    source = uuid.uuid4()
    with pytest.raises(ValidationError, match="once"):
        PurchaseOrderLineIn(
            line_number=1,
            item_id=uuid.uuid4(),
            quantity="12",
            unit_price="20",
            source_allocations=[
                {"requisition_line_id": source, "quantity": "4"},
                {"requisition_line_id": source, "quantity": "6"},
            ],
        )


def test_source_allocations_cannot_exceed_the_physical_order_quantity() -> None:
    with pytest.raises(ValidationError, match="quantity"):
        PurchaseOrderLineIn(
            line_number=1,
            item_id=uuid.uuid4(),
            quantity="5",
            unit_price="20",
            source_allocations=[{"requisition_line_id": uuid.uuid4(), "quantity": "6"}],
        )


def test_scalar_and_explicit_source_allocations_are_not_ambiguous() -> None:
    with pytest.raises(ValidationError, match="requisition_line_id"):
        PurchaseOrderLineIn(
            line_number=1,
            item_id=uuid.uuid4(),
            quantity="5",
            unit_price="20",
            requisition_line_id=uuid.uuid4(),
            source_allocations=[{"requisition_line_id": uuid.uuid4(), "quantity": "5"}],
        )


def test_existing_amendment_line_can_keep_its_item_and_commercial_terms() -> None:
    line_id = uuid.uuid4()
    payload = PurchaseOrderAmendRequest(
        expected_version=1,
        reason="Supplier can provide twelve units",
        lines=[PurchaseOrderAmendLineIn(line_id=line_id, quantity="12")],
    )
    assert payload.lines[0].line_id == line_id
    assert payload.lines[0].item_id is None
    assert payload.lines[0].unit_price is None


def test_new_amendment_line_requires_item_and_unit_price() -> None:
    with pytest.raises(ValidationError):
        PurchaseOrderAmendLineIn(quantity="2")


def test_amendment_cannot_repeat_an_existing_physical_line() -> None:
    line_id = uuid.uuid4()
    with pytest.raises(ValidationError, match="once"):
        PurchaseOrderAmendRequest(
            expected_version=1,
            reason="Correct the agreed quantities",
            lines=[
                {"line_id": line_id, "quantity": "2"},
                {"line_id": line_id, "quantity": "3"},
            ],
        )


@pytest.mark.parametrize("reason", ["", "   "])
def test_amendment_needs_a_meaningful_reason(reason: str) -> None:
    with pytest.raises(ValidationError):
        PurchaseOrderAmendRequest(
            expected_version=1,
            reason=reason,
            lines=[{"line_id": uuid.uuid4(), "quantity": "3"}],
        )


def test_duplicate_confirmation_is_specific_to_purchase_order_approval() -> None:
    payload = PurchaseOrderApprovalRequest(
        reason="The second delivery covers additional confirmed demand",
        confirm_duplicate=True,
    )
    assert payload.confirm_duplicate is True


def test_duplicate_confirmation_requires_its_approval_reason() -> None:
    with pytest.raises(ValidationError):
        PurchaseOrderApprovalRequest(confirm_duplicate=True)


def test_standard_approval_keeps_the_existing_optional_reason() -> None:
    payload = PurchaseOrderApprovalRequest()
    assert payload.reason is None
    assert payload.confirm_duplicate is False


def test_approval_rejects_client_supplied_confirmation_identity() -> None:
    with pytest.raises(ValidationError):
        PurchaseOrderApprovalRequest(duplicate_confirmed_by_id=uuid.uuid4())
