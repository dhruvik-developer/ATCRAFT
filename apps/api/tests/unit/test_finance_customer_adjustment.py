"""Customer debit and credit notes: the posting shape, the sign, and the gate.

``POST /finance/customer-adjustments`` replaced the customers module's
``POST /customers/{id}/outstanding``, which moved the cached balance and wrote
nothing to the ledger. The service posts a receivable journal and then moves the
balance by the same amount in one transaction; what is pinned here without a
database is the pair of legs it posts, the sign it applies to the balance, the
payload bounds that keep the cross-module hand-off from failing validation
downstream, and the permission the route is gated on.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError as PydanticValidationError

from src.core.permissions import Permission
from src.modules.customers.schemas import OutstandingAdjustment
from src.modules.finance.models import CustomerAdjustmentType, PartyType
from src.modules.finance.router import router
from src.modules.finance.schemas import CustomerAdjustmentCreate
from src.modules.finance.service import (
    REFERENCE_CUSTOMER_ADJUSTMENT,
    assert_balanced,
    customer_adjustment_delta,
    customer_adjustment_lines,
)

pytestmark = pytest.mark.unit

RECEIVABLE = uuid.uuid4()
CONTRA = uuid.uuid4()
CUSTOMER = uuid.uuid4()


def _lines(adjustment_type: CustomerAdjustmentType, amount: str = "1180.40") -> list[Any]:
    return customer_adjustment_lines(
        receivable_account_id=RECEIVABLE,
        contra_account_id=CONTRA,
        customer_id=CUSTOMER,
        amount=Decimal(amount),
        adjustment_type=adjustment_type,
        narration="Debit note CADJ/2026-27/00001 for CUST-01: freight",
    )


class TestTheJournalLegs:
    def test_a_charge_debits_the_receivable_and_credits_the_contra(self) -> None:
        receivable, contra = _lines(CustomerAdjustmentType.CHARGE)

        assert receivable.account_id == RECEIVABLE
        assert (receivable.debit, receivable.credit) == (Decimal("1180.40"), Decimal("0"))
        assert contra.account_id == CONTRA
        assert (contra.debit, contra.credit) == (Decimal("0"), Decimal("1180.40"))

    def test_a_credit_is_the_mirror_image(self) -> None:
        receivable, contra = _lines(CustomerAdjustmentType.CREDIT)

        assert (receivable.debit, receivable.credit) == (Decimal("0"), Decimal("1180.40"))
        assert (contra.debit, contra.credit) == (Decimal("1180.40"), Decimal("0"))

    @pytest.mark.parametrize("adjustment_type", list(CustomerAdjustmentType))
    def test_only_the_receivable_leg_carries_the_customer(
        self, adjustment_type: CustomerAdjustmentType
    ) -> None:
        receivable, contra = _lines(adjustment_type)

        assert (receivable.party_type, receivable.party_id) == (PartyType.CUSTOMER, CUSTOMER)
        assert (contra.party_type, contra.party_id) == (PartyType.NONE, None)

    @pytest.mark.parametrize("adjustment_type", list(CustomerAdjustmentType))
    def test_the_pair_balances_at_money_scale(
        self, adjustment_type: CustomerAdjustmentType
    ) -> None:
        # The same gate post_journal applies; a pair that fails it never posts.
        totals = assert_balanced(_lines(adjustment_type, amount="0.005"))
        assert totals.is_balanced
        assert totals.line_count == 2


class TestTheBalanceDelta:
    def test_a_charge_raises_what_the_customer_owes(self) -> None:
        assert customer_adjustment_delta(Decimal("250"), CustomerAdjustmentType.CHARGE) == (
            Decimal("250.0000")
        )

    def test_a_credit_lowers_it(self) -> None:
        assert customer_adjustment_delta(Decimal("250"), CustomerAdjustmentType.CREDIT) == (
            Decimal("-250.0000")
        )

    @pytest.mark.parametrize("adjustment_type", list(CustomerAdjustmentType))
    def test_the_delta_agrees_with_the_receivable_leg(
        self, adjustment_type: CustomerAdjustmentType
    ) -> None:
        """The cached balance moves exactly as the receivable account does."""
        receivable, _ = _lines(adjustment_type, amount="99.99")
        delta = customer_adjustment_delta(Decimal("99.99"), adjustment_type)
        assert delta == receivable.debit - receivable.credit


class TestThePayload:
    def _payload(self, **overrides: Any) -> CustomerAdjustmentCreate:
        data: dict[str, Any] = {
            "customer_id": CUSTOMER,
            "adjustment_type": "CREDIT",
            "amount": "150.00",
            "account_id": CONTRA,
            "reason": "Goods returned damaged",
        }
        data.update(overrides)
        return CustomerAdjustmentCreate(**data)

    def test_the_entry_date_is_optional(self) -> None:
        assert self._payload().entry_date is None
        assert self._payload(entry_date="2026-09-01").entry_date == date(2026, 9, 1)

    @pytest.mark.parametrize("amount", ["0", "-1"])
    def test_the_amount_must_be_positive(self, amount: str) -> None:
        # The direction is the type, never the sign: a negative credit is a typo.
        with pytest.raises(PydanticValidationError):
            self._payload(amount=amount)

    def test_the_reason_fits_the_customers_module_payload(self) -> None:
        """The reason is forwarded verbatim into ``OutstandingAdjustment``.

        Its bound must be no looser than that model's, or a reason accepted at
        the edge would fail validation inside the transaction - a 500.
        """
        longest = "x" * 255
        payload = self._payload(reason=longest)
        forwarded = OutstandingAdjustment(
            delta=Decimal("-150"),
            reason=payload.reason,
            reference_type=REFERENCE_CUSTOMER_ADJUSTMENT,
            reference_id=uuid.uuid4(),
            reference_code="CADJ/2026-27/00001",
        )
        assert forwarded.reason == longest
        with pytest.raises(PydanticValidationError):
            self._payload(reason="x" * 256)

    def test_unknown_fields_are_refused(self) -> None:
        with pytest.raises(PydanticValidationError):
            self._payload(currency="USD")


class TestTheRoute:
    def _route(self) -> APIRoute:
        for route in router.routes:
            if (
                isinstance(route, APIRoute)
                and route.path == "/finance/customer-adjustments"
                and "POST" in route.methods
            ):
                return route
        raise AssertionError("POST /finance/customer-adjustments is not a finance route")

    def test_the_route_is_gated_on_payment_approve(self) -> None:
        """A credit note is money the company agrees not to collect."""
        required: set[Permission] = set()
        for dependency in self._route().dependencies:
            closure = inspect.getclosurevars(dependency.dependency).nonlocals
            required.update(closure.get("permissions", ()))
        assert required == {Permission.PAYMENT_APPROVE}
