"""The over-receipt tolerance and receipt-driven status derivation.

Master rules section 17. These assertions are about what a vendor is allowed to
invoice for: the tolerance is the only thing standing between "we ordered 100"
and "they shipped 140 and billed us for it".

No database. The same numbers are enforced a second time by a CHECK constraint
generated from the same constant, which the integration suite exercises.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.errors import BusinessRuleError
from src.modules.procurement.models import (
    OVER_RECEIPT_MULTIPLIER,
    OVER_RECEIPT_TOLERANCE,
    PurchaseOrderLineStatus,
    RequisitionLineStatus,
)
from src.modules.procurement.service_rules import (
    assert_within_over_receipt_tolerance,
    max_receivable_quantity,
    order_is_fully_received,
    po_line_status_for,
    receipt_unit_cost,
    remaining_receivable,
    requisition_line_status_for,
)

pytestmark = pytest.mark.unit


def receive(ordered: str, already: str, incoming: str) -> None:
    assert_within_over_receipt_tolerance(
        item_code="RM-001",
        line_number=1,
        ordered_quantity=Decimal(ordered),
        already_received=Decimal(already),
        incoming_quantity=Decimal(incoming),
    )


# -----------------------------------------------------------------------------
# The tolerance itself
# -----------------------------------------------------------------------------


def test_the_tolerance_and_its_multiplier_agree():
    """The CHECK constraint is rendered from the multiplier; they cannot drift."""
    assert Decimal("1") + OVER_RECEIPT_TOLERANCE == OVER_RECEIPT_MULTIPLIER
    assert Decimal("0.05") == OVER_RECEIPT_TOLERANCE


def test_an_exact_delivery_is_accepted():
    receive("100", "0", "100")


def test_a_short_delivery_is_accepted():
    """Short receipt is normal - it leaves the order partially received."""
    receive("100", "0", "60")


def test_a_delivery_inside_the_tolerance_is_accepted():
    receive("100", "0", "105")


def test_a_delivery_exactly_at_the_ceiling_is_accepted():
    assert max_receivable_quantity(Decimal("200")) == Decimal("210")
    receive("200", "0", "210")


def test_a_delivery_one_unit_past_the_ceiling_is_refused():
    with pytest.raises(BusinessRuleError) as exc:
        receive("200", "0", "211")
    assert exc.value.details["maximum_receivable"] == "210.0000"


def test_the_tolerance_applies_to_the_running_total_not_to_each_delivery():
    """Three deliveries of 40 against an order of 100 is 120 - over the ceiling.

    Checking each delivery in isolation is the classic version of this bug: no
    single receipt looks wrong and the vendor still ships 20% extra.
    """
    receive("100", "0", "40")
    receive("100", "40", "40")
    with pytest.raises(BusinessRuleError):
        receive("100", "80", "40")


def test_the_refusal_tells_the_storekeeper_what_they_may_accept():
    """There is a lorry at the gate; "rule violated" is not an actionable message."""
    with pytest.raises(BusinessRuleError) as exc:
        receive("100", "80", "40")

    error = exc.value
    assert "RM-001" in error.message
    assert error.details["remaining_receivable"] == "25.0000"
    assert error.details["already_received"] == "80.0000"
    assert error.details["ordered_quantity"] == "100.0000"


def test_remaining_receivable_accounts_for_the_tolerance():
    assert remaining_receivable(Decimal("100"), Decimal("0")) == Decimal("105")
    assert remaining_receivable(Decimal("100"), Decimal("90")) == Decimal("15")


def test_remaining_receivable_is_never_negative():
    """A fully over-received line has nothing left to take, not a negative amount."""
    assert remaining_receivable(Decimal("100"), Decimal("105")) == Decimal("0")


def test_fractional_quantities_respect_the_tolerance():
    """Sheet metal and wire are ordered in decimal quantities, not whole units."""
    assert max_receivable_quantity(Decimal("12.5")) == Decimal("13.125")
    receive("12.5", "0", "13.125")
    with pytest.raises(BusinessRuleError):
        receive("12.5", "0", "13.2")


# -----------------------------------------------------------------------------
# Line status is derived, never assigned
# -----------------------------------------------------------------------------


def test_nothing_received_is_pending():
    assert (
        po_line_status_for(quantity=Decimal("100"), received_quantity=Decimal("0"))
        is PurchaseOrderLineStatus.PENDING
    )


def test_part_received_is_partially_received():
    assert (
        po_line_status_for(quantity=Decimal("100"), received_quantity=Decimal("60"))
        is PurchaseOrderLineStatus.PARTIALLY_RECEIVED
    )


def test_fully_received_is_received():
    assert (
        po_line_status_for(quantity=Decimal("100"), received_quantity=Decimal("100"))
        is PurchaseOrderLineStatus.RECEIVED
    )


def test_an_over_receipt_inside_the_tolerance_is_received_not_something_else():
    assert (
        po_line_status_for(quantity=Decimal("100"), received_quantity=Decimal("104"))
        is PurchaseOrderLineStatus.RECEIVED
    )


def test_a_short_closed_line_stops_expecting_the_balance():
    assert (
        po_line_status_for(
            quantity=Decimal("100"), received_quantity=Decimal("60"), short_closed=True
        )
        is PurchaseOrderLineStatus.CLOSED
    )


def test_cancellation_wins_over_every_other_derivation():
    assert (
        po_line_status_for(
            quantity=Decimal("100"),
            received_quantity=Decimal("100"),
            cancelled=True,
            short_closed=True,
        )
        is PurchaseOrderLineStatus.CANCELLED
    )


# -----------------------------------------------------------------------------
# When is the whole order received?
# -----------------------------------------------------------------------------


def test_an_order_is_received_only_when_every_live_line_is():
    assert (
        order_is_fully_received(
            [(Decimal("100"), Decimal("100"), False), (Decimal("50"), Decimal("50"), False)]
        )
        is True
    )
    assert (
        order_is_fully_received(
            [(Decimal("100"), Decimal("100"), False), (Decimal("50"), Decimal("30"), False)]
        )
        is False
    )


def test_a_short_closed_line_does_not_hold_the_order_open():
    """That is the entire point of short closing it."""
    assert (
        order_is_fully_received(
            [(Decimal("100"), Decimal("100"), False), (Decimal("50"), Decimal("30"), True)]
        )
        is True
    )


def test_an_order_whose_lines_are_all_closed_is_received():
    assert order_is_fully_received([(Decimal("100"), Decimal("0"), True)]) is True


def test_an_order_with_no_lines_is_not_reported_as_short():
    assert order_is_fully_received([]) is True


# -----------------------------------------------------------------------------
# Receipt valuation
# -----------------------------------------------------------------------------


def test_receipt_cost_is_the_net_price_after_discount():
    """100 units at 50.00 with a 500.00 discount values stock at 45.00 a unit."""
    assert receipt_unit_cost(
        quantity=Decimal("100"),
        taxable_amount=Decimal("4500.00"),
        fallback_unit_price=Decimal("50"),
    ) == Decimal("45.000000")


def test_receipt_cost_excludes_tax():
    """GST on a purchase is an input credit, not part of what the material cost.

    The taxable amount passed in is already net of tax; this asserts the caller's
    contract by showing that a 18% inclusive figure would value stock wrongly.
    """
    net = receipt_unit_cost(
        quantity=Decimal("10"),
        taxable_amount=Decimal("1000.00"),
        fallback_unit_price=Decimal("100"),
    )
    with_tax = receipt_unit_cost(
        quantity=Decimal("10"),
        taxable_amount=Decimal("1180.00"),
        fallback_unit_price=Decimal("100"),
    )
    assert net == Decimal("100.000000")
    assert with_tax != net


def test_receipt_cost_falls_back_when_the_quantity_is_zero():
    """A zero-quantity line cannot divide; the ordered price is the honest answer."""
    assert receipt_unit_cost(
        quantity=Decimal("0"), taxable_amount=Decimal("0"), fallback_unit_price=Decimal("12.5")
    ) == Decimal("12.500000")


# -----------------------------------------------------------------------------
# Requisition line progress
# -----------------------------------------------------------------------------


def test_requisition_line_status_tracks_what_has_been_ordered():
    assert (
        requisition_line_status_for(quantity=Decimal("100"), ordered_quantity=Decimal("0"))
        is RequisitionLineStatus.PENDING
    )
    assert (
        requisition_line_status_for(quantity=Decimal("100"), ordered_quantity=Decimal("40"))
        is RequisitionLineStatus.PARTIALLY_ORDERED
    )
    assert (
        requisition_line_status_for(quantity=Decimal("100"), ordered_quantity=Decimal("100"))
        is RequisitionLineStatus.ORDERED
    )


def test_over_ordering_a_requisition_line_still_reads_as_ordered():
    """A vendor's minimum order quantity legitimately rounds the buy upwards."""
    assert (
        requisition_line_status_for(quantity=Decimal("80"), ordered_quantity=Decimal("100"))
        is RequisitionLineStatus.ORDERED
    )
