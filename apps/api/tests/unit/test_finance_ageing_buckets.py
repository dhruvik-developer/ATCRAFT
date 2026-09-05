"""Ageing buckets, and the arithmetic that decides what a document owes.

The bucket boundaries decide which column a debt lands in on the report the
finance team chases from, and off-by-one at a boundary moves real money between
columns. The allocation planner and the two status derivations are the other
pure pieces of the payment path: everything they get wrong shows up as an
invoice claiming to be paid when it is not, or the reverse.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from src.modules.finance.models import (
    AGEING_BUCKET_BOUNDARIES,
    AGEING_BUCKET_LABELS,
    PartyType,
    PaymentStatus,
    VendorInvoiceStatus,
)
from src.modules.finance.service import (
    NOT_DUE_LABEL,
    PAYMENT_LIFECYCLE,
    VENDOR_INVOICE_LIFECYCLE,
    ageing_bucket,
    invoice_status_after_payment,
    payment_status_after_allocation,
    plan_allocation,
    statement_direction,
)

pytestmark = pytest.mark.unit


class TestAgeingBuckets:
    def test_a_debt_not_yet_due_is_not_aged(self) -> None:
        assert ageing_bucket(-1) == NOT_DUE_LABEL
        assert ageing_bucket(-90) == NOT_DUE_LABEL

    def test_due_today_starts_the_first_bucket(self) -> None:
        assert ageing_bucket(0) == "0-30"

    @pytest.mark.parametrize(
        ("days", "expected"),
        [
            (1, "0-30"),
            (30, "0-30"),
            (31, "31-60"),
            (60, "31-60"),
            (61, "61-90"),
            (90, "61-90"),
            (91, "90+"),
            (365, "90+"),
        ],
    )
    def test_every_boundary_falls_on_the_expected_side(self, days: int, expected: str) -> None:
        assert ageing_bucket(days) == expected

    def test_the_labels_and_the_boundaries_describe_the_same_buckets(self) -> None:
        # Four labels for three boundaries plus the open-ended tail. If these
        # ever disagree the SQL CASE and the Python label would split a debt
        # across two columns.
        assert len(AGEING_BUCKET_LABELS) == len(AGEING_BUCKET_BOUNDARIES) + 1
        for index, boundary in enumerate(AGEING_BUCKET_BOUNDARIES):
            assert ageing_bucket(boundary) == AGEING_BUCKET_LABELS[index]
            assert ageing_bucket(boundary + 1) == AGEING_BUCKET_LABELS[index + 1]


class TestStatementDirection:
    def test_a_customer_balance_is_debits_less_credits(self) -> None:
        assert statement_direction(PartyType.CUSTOMER) == 1

    def test_a_vendor_balance_is_credits_less_debits(self) -> None:
        # Opposite sign, so a positive balance means "owed" on both statements.
        assert statement_direction(PartyType.VENDOR) == -1

    def test_a_statement_needs_a_real_party(self) -> None:
        from src.core.errors import ValidationError

        with pytest.raises(ValidationError):
            statement_direction(PartyType.NONE)


class TestInvoiceStatusFromMoney:
    def test_nothing_paid_leaves_the_invoice_approved(self) -> None:
        assert (
            invoice_status_after_payment(
                VendorInvoiceStatus.APPROVED,
                total_amount=Decimal("1000"),
                paid_amount=Decimal("0"),
            )
            is VendorInvoiceStatus.APPROVED
        )

    def test_part_paid_is_partially_paid(self) -> None:
        assert (
            invoice_status_after_payment(
                VendorInvoiceStatus.APPROVED,
                total_amount=Decimal("1000"),
                paid_amount=Decimal("400"),
            )
            is VendorInvoiceStatus.PARTIALLY_PAID
        )

    def test_paid_to_the_last_paisa_is_paid(self) -> None:
        assert (
            invoice_status_after_payment(
                VendorInvoiceStatus.PARTIALLY_PAID,
                total_amount=Decimal("1000.00"),
                paid_amount=Decimal("1000.00"),
            )
            is VendorInvoiceStatus.PAID
        )

    def test_one_paisa_short_is_not_paid(self) -> None:
        # The difference between chasing a vendor and not.
        assert (
            invoice_status_after_payment(
                VendorInvoiceStatus.PARTIALLY_PAID,
                total_amount=Decimal("1000.00"),
                paid_amount=Decimal("999.99"),
            )
            is VendorInvoiceStatus.PARTIALLY_PAID
        )

    def test_a_disputed_invoice_keeps_its_status_when_part_paid(self) -> None:
        # Settling part of a disputed bill does not end the dispute.
        assert (
            invoice_status_after_payment(
                VendorInvoiceStatus.DISPUTED,
                total_amount=Decimal("1000"),
                paid_amount=Decimal("1000"),
            )
            is VendorInvoiceStatus.DISPUTED
        )

    def test_a_cancelled_invoice_is_never_revived_by_money(self) -> None:
        assert (
            invoice_status_after_payment(
                VendorInvoiceStatus.CANCELLED,
                total_amount=Decimal("1000"),
                paid_amount=Decimal("1000"),
            )
            is VendorInvoiceStatus.CANCELLED
        )


class TestPaymentStatusFromAllocation:
    def test_unallocated_money_stays_approved(self) -> None:
        assert (
            payment_status_after_allocation(amount=Decimal("5000"), allocated_amount=Decimal("0"))
            is PaymentStatus.APPROVED
        )

    def test_part_allocated_money_is_partially_allocated(self) -> None:
        assert (
            payment_status_after_allocation(
                amount=Decimal("5000"), allocated_amount=Decimal("2500")
            )
            is PaymentStatus.PARTIALLY_ALLOCATED
        )

    def test_fully_allocated_money_is_allocated(self) -> None:
        assert (
            payment_status_after_allocation(
                amount=Decimal("5000.00"), allocated_amount=Decimal("5000.00")
            )
            is PaymentStatus.ALLOCATED
        )


class TestAllocationPlanning:
    def setup_method(self) -> None:
        self.invoice_a = uuid.uuid4()
        self.invoice_b = uuid.uuid4()

    def test_a_first_allocation_is_the_whole_plan(self) -> None:
        plan = plan_allocation(
            existing={}, requested={self.invoice_a: Decimal("100")}, replace=False
        )

        assert plan.final == {self.invoice_a: Decimal("100")}
        assert plan.removed == {}
        assert plan.total == Decimal("100.00")

    def test_naming_a_new_invoice_adds_to_what_is_already_settled(self) -> None:
        plan = plan_allocation(
            existing={self.invoice_a: Decimal("100")},
            requested={self.invoice_b: Decimal("50")},
            replace=False,
        )

        assert plan.total == Decimal("150.00")
        assert set(plan.final) == {self.invoice_a, self.invoice_b}

    def test_naming_the_same_invoice_amends_rather_than_stacks(self) -> None:
        # The unique (payment, invoice) constraint says the same thing: one
        # payment settles one invoice once, for some amount.
        plan = plan_allocation(
            existing={self.invoice_a: Decimal("100")},
            requested={self.invoice_a: Decimal("250")},
            replace=False,
        )

        assert plan.final == {self.invoice_a: Decimal("250")}
        assert plan.total == Decimal("250.00")

    def test_replace_discards_everything_the_payment_previously_settled(self) -> None:
        plan = plan_allocation(
            existing={self.invoice_a: Decimal("100"), self.invoice_b: Decimal("200")},
            requested={self.invoice_b: Decimal("50")},
            replace=True,
        )

        assert plan.final == {self.invoice_b: Decimal("50")}
        assert plan.removed == {self.invoice_a: Decimal("100"), self.invoice_b: Decimal("200")}
        assert plan.total == Decimal("50.00")

    def test_re_sending_the_same_allocation_is_a_no_op(self) -> None:
        # Retry safety: a client that repeats a request must not double-pay.
        existing = {self.invoice_a: Decimal("100")}
        plan = plan_allocation(
            existing=existing, requested={self.invoice_a: Decimal("100")}, replace=False
        )

        assert plan.total == Decimal("100.00")


class TestLifecycles:
    def test_the_invoice_graph_is_structurally_sound(self) -> None:
        VENDOR_INVOICE_LIFECYCLE.validate()

    def test_the_payment_graph_is_structurally_sound(self) -> None:
        PAYMENT_LIFECYCLE.validate()

    def test_an_invoice_cannot_jump_straight_from_draft_to_paid(self) -> None:
        from src.core.errors import InvalidStateTransitionError

        with pytest.raises(InvalidStateTransitionError):
            VENDOR_INVOICE_LIFECYCLE.assert_transition(
                VendorInvoiceStatus.DRAFT, VendorInvoiceStatus.PAID
            )

    def test_a_paid_invoice_can_be_walked_back_when_an_allocation_is_corrected(self) -> None:
        assert VENDOR_INVOICE_LIFECYCLE.can_transition(
            VendorInvoiceStatus.PAID, VendorInvoiceStatus.PARTIALLY_PAID
        )
        assert VENDOR_INVOICE_LIFECYCLE.can_transition(
            VendorInvoiceStatus.PAID, VendorInvoiceStatus.APPROVED
        )

    def test_a_cancelled_invoice_is_terminal(self) -> None:
        assert VENDOR_INVOICE_LIFECYCLE.is_terminal(VendorInvoiceStatus.CANCELLED)
        assert VENDOR_INVOICE_LIFECYCLE.allowed_targets(VendorInvoiceStatus.CANCELLED) == []

    def test_a_payment_cannot_be_allocated_before_it_is_approved(self) -> None:
        from src.core.errors import InvalidStateTransitionError

        with pytest.raises(InvalidStateTransitionError):
            PAYMENT_LIFECYCLE.assert_transition(
                PaymentStatus.DRAFT, PaymentStatus.PARTIALLY_ALLOCATED
            )
