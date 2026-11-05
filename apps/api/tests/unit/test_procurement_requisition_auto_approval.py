"""The rule that lets a small requisition skip the approval queue.

The rule is pure, so the boundaries are pinned here without a database: a
disabled limit, an empty document, a line nobody priced, and the exact point at
which the estimated value stops being small. Getting the boundary wrong in either
direction is expensive - one way an unpriced document is approved, the other a
buyer waits for a signature on a trivial purchase.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.modules.procurement.service_rules import requisition_auto_approval

pytestmark = pytest.mark.unit


def test_a_zero_limit_disables_the_rule() -> None:
    assert requisition_auto_approval([(Decimal("1"), Decimal("1"))], limit=Decimal("0")) is None


def test_a_negative_limit_disables_the_rule() -> None:
    assert requisition_auto_approval([(Decimal("1"), Decimal("1"))], limit=Decimal("-5")) is None


def test_an_empty_requisition_is_never_auto_approved() -> None:
    assert requisition_auto_approval([], limit=Decimal("100")) is None


def test_a_line_with_no_estimate_keeps_the_document_in_the_queue() -> None:
    assert (
        requisition_auto_approval(
            [(Decimal("1"), Decimal("10")), (Decimal("2"), None)],
            limit=Decimal("100"),
        )
        is None
    )


def test_the_limit_is_inclusive() -> None:
    approved = requisition_auto_approval([(Decimal("10"), Decimal("5"))], limit=Decimal("50"))

    assert approved is not None
    assert approved.estimated_value == Decimal("50.00")
    assert approved.line_count == 1


def test_above_the_limit_stays_in_the_queue() -> None:
    assert (
        requisition_auto_approval([(Decimal("10"), Decimal("5.01"))], limit=Decimal("50")) is None
    )


def test_the_value_sums_every_line_at_the_requesters_estimate() -> None:
    approved = requisition_auto_approval(
        [
            (Decimal("3"), Decimal("10.50")),
            (Decimal("2"), Decimal("0.25")),
            (Decimal("1"), None),
        ],
        limit=Decimal("1000"),
    )
    # One unpriced line is enough to disqualify the whole document.
    assert approved is None

    approved = requisition_auto_approval(
        [
            (Decimal("3"), Decimal("10.50")),
            (Decimal("2"), Decimal("0.25")),
        ],
        limit=Decimal("1000"),
    )
    assert approved is not None
    assert approved.estimated_value == Decimal("32.00")
    assert approved.line_count == 2


def test_the_value_is_rounded_to_money_scale_before_comparison() -> None:
    """A half-paisa is money-rounded, not truncated, exactly as a PO would be."""
    approved = requisition_auto_approval([(Decimal("1"), Decimal("0.005"))], limit=Decimal("0.01"))

    assert approved is not None
    assert approved.estimated_value == Decimal("0.01")
