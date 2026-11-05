"""The approval-threshold payload is bounded before it reaches PostgreSQL.

The columns are ``NUMERIC(18,4)`` and negative thresholds have no meaning: a
negative PO dual-approval amount would make every order need two approvers, and
an oversized value would fail inside the database as a 500 instead of a field
error the settings form can point at. These tests pin the bounds at the API edge.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.modules.audit.schemas import ApprovalSettingsUpdate

THRESHOLD_FIELDS = (
    "po_dual_approval_amount",
    "adjustment_approval_threshold",
    "internal_issue_approval_threshold",
    "requisition_auto_approve_limit",
)


@pytest.mark.parametrize("field", THRESHOLD_FIELDS)
def test_every_threshold_accepts_zero_and_four_decimal_places(field: str) -> None:
    payload = ApprovalSettingsUpdate.model_validate({field: "0.0001"})

    assert getattr(payload, field) == Decimal("0.0001")


@pytest.mark.parametrize("field", THRESHOLD_FIELDS)
@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("-0.0001", "greater than or equal to 0"),
        ("1.23456", "no more than 4 decimal places"),
        ("123456789012345678", "no more than 14 digits before the decimal point"),
        ("NaN", "finite number"),
        ("Infinity", "finite number"),
    ],
)
def test_a_threshold_outside_the_column_bounds_is_refused(
    field: str, value: str, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ApprovalSettingsUpdate.model_validate({field: value})


def test_an_omitted_threshold_stays_unset_and_an_explicit_null_restores_the_default() -> None:
    sparse = ApprovalSettingsUpdate.model_validate({"po_dual_approval_amount": "10"})
    assert sparse.model_dump(exclude_unset=True) == {"po_dual_approval_amount": Decimal("10")}

    restored = ApprovalSettingsUpdate.model_validate({"po_dual_approval_amount": None})
    assert restored.model_dump(exclude_unset=True) == {"po_dual_approval_amount": None}
