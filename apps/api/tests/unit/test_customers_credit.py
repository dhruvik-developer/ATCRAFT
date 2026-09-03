"""The customer credit check - pure rules, no database.

This is the calculation sales order creation depends on, so the cases below pin
down the decisions that are easy to get subtly wrong: the difference between an
unlimited limit and a zero one, what happens exactly *on* the limit, and which
refusal wins when an account is both blocked and over its limit.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.core.errors import BusinessRuleError, ErrorCode, ValidationError
from src.modules.customers.service import (
    assert_within_credit_limit,
    credit_status_of,
    evaluate_credit,
)

pytestmark = pytest.mark.unit


def _customer(
    *,
    credit_limit: Decimal | None,
    outstanding: Decimal = Decimal("0"),
    is_blocked: bool = False,
    block_reason: str | None = None,
    is_active: bool = True,
) -> SimpleNamespace:
    """Minimal stand-in for a persisted customer.

    Deliberately not an ORM instance: the rule reads seven attributes and
    nothing else, which is exactly why it is a pure function.
    """
    return SimpleNamespace(
        id=uuid.uuid4(),
        code="AC-0001",
        is_active=is_active,
        is_blocked=is_blocked,
        block_reason=block_reason,
        credit_limit=credit_limit,
        credit_days=30,
        outstanding_amount=outstanding,
    )


class TestUnlimitedVersusZero:
    """The decision the module states as an ASSUMPTION: NULL is unlimited,
    0 is strictly no credit. Conflating them is the classic ERP defect."""

    def test_null_limit_allows_any_order(self) -> None:
        customer = _customer(credit_limit=None, outstanding=Decimal("9999999"))
        assessment = assert_within_credit_limit(customer, Decimal("1000000"))

        assert assessment.is_unlimited is True
        assert assessment.within_limit is True
        # Headroom is undefined, not infinite - a UI must not render a number.
        assert assessment.headroom is None

    def test_zero_limit_refuses_any_positive_order(self) -> None:
        customer = _customer(credit_limit=Decimal("0"), outstanding=Decimal("0"))

        with pytest.raises(BusinessRuleError) as exc:
            assert_within_credit_limit(customer, Decimal("0.01"))

        assert exc.value.code is ErrorCode.BUSINESS_RULE_VIOLATION
        assert exc.value.details["credit_limit"] == "0.00"

    def test_zero_limit_allows_a_zero_value_order(self) -> None:
        """A zero-value order (a sample, a free replacement) adds no exposure,
        so a no-credit account may still take it."""
        customer = _customer(credit_limit=Decimal("0"))
        assessment = assert_within_credit_limit(customer, Decimal("0"))
        assert assessment.within_limit is True


class TestLimitBoundary:
    def test_order_exactly_on_the_limit_is_allowed(self) -> None:
        customer = _customer(credit_limit=Decimal("1000"), outstanding=Decimal("400"))
        assessment = assert_within_credit_limit(customer, Decimal("600"))

        assert assessment.exposure == Decimal("1000.00")
        assert assessment.headroom == Decimal("0.00")

    def test_one_paisa_over_the_limit_is_refused(self) -> None:
        customer = _customer(credit_limit=Decimal("1000"), outstanding=Decimal("400"))

        with pytest.raises(BusinessRuleError) as exc:
            assert_within_credit_limit(customer, Decimal("600.01"))

        details = exc.value.details
        assert details["exposure"] == "1000.01"
        assert details["headroom"] == "-0.01"
        assert details["customer_code"] == "AC-0001"

    def test_existing_outstanding_counts_towards_the_limit(self) -> None:
        """The limit caps total exposure, not the size of one order - otherwise
        ten orders of the limit each would all pass."""
        customer = _customer(credit_limit=Decimal("1000"), outstanding=Decimal("950"))
        assessment = evaluate_credit(customer, Decimal("100"))

        assert assessment.within_limit is False
        assert assessment.headroom == Decimal("-50.00")

    def test_amounts_are_compared_at_money_scale(self) -> None:
        """Sub-paisa noise must not decide a credit refusal.

        999.994 rounds to 999.99 at money scale, which is inside a limit of
        999.99. Comparing the raw Decimals would refuse this order.
        """
        customer = _customer(credit_limit=Decimal("999.99"), outstanding=Decimal("999.994"))
        assessment = evaluate_credit(customer, Decimal("0"))

        assert assessment.outstanding_amount == Decimal("999.99")
        assert assessment.within_limit is True


class TestBlockedAndInactive:
    def test_blocked_customer_is_refused_even_with_headroom(self) -> None:
        customer = _customer(
            credit_limit=Decimal("100000"),
            outstanding=Decimal("0"),
            is_blocked=True,
            block_reason="Cheque bounced twice",
        )

        with pytest.raises(BusinessRuleError) as exc:
            assert_within_credit_limit(customer, Decimal("100"))

        assert "credit hold" in exc.value.message
        assert "Cheque bounced twice" in exc.value.message
        assert exc.value.details["is_blocked"] is True

    def test_block_outranks_the_limit_breach(self) -> None:
        """A blocked account that is also over its limit must report the hold:
        clearing the balance would otherwise look like it would let the order
        through."""
        customer = _customer(
            credit_limit=Decimal("100"),
            outstanding=Decimal("500"),
            is_blocked=True,
            block_reason="Legal dispute",
        )
        assessment = evaluate_credit(customer, Decimal("10"))

        assert assessment.within_limit is False
        assert assessment.failure_reason is not None
        assert "credit hold" in assessment.failure_reason

    def test_inactive_customer_cannot_order(self) -> None:
        customer = _customer(credit_limit=None, is_active=False)

        with pytest.raises(BusinessRuleError) as exc:
            assert_within_credit_limit(customer, Decimal("1"))

        assert "not active" in exc.value.message


class TestAssessmentShape:
    def test_success_returns_a_usable_assessment(self) -> None:
        customer = _customer(credit_limit=Decimal("5000"), outstanding=Decimal("1234.50"))
        assessment = assert_within_credit_limit(customer, Decimal("765.50"))

        assert assessment.customer_code == "AC-0001"
        assert assessment.outstanding_amount == Decimal("1234.50")
        assert assessment.order_value == Decimal("765.50")
        assert assessment.exposure == Decimal("2000.00")
        assert assessment.headroom == Decimal("3000.00")
        assert assessment.failure_reason is None

    def test_negative_order_value_is_a_validation_error(self) -> None:
        """A credit check is not a refund path: a negative order value is a
        programming mistake and must not silently increase headroom."""
        customer = _customer(credit_limit=Decimal("1000"))

        with pytest.raises(ValidationError):
            evaluate_credit(customer, Decimal("-100"))

    def test_credit_status_response_mirrors_the_assessment(self) -> None:
        customer = _customer(credit_limit=Decimal("2000"), outstanding=Decimal("500"))
        status = credit_status_of(evaluate_credit(customer, Decimal("250")))

        assert status.exposure == Decimal("750.00")
        assert status.headroom == Decimal("1250.00")
        assert status.is_unlimited is False
        assert status.within_limit is True
