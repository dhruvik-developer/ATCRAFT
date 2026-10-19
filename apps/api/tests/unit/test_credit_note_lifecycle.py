from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.errors import InvalidStateTransitionError
from src.core.permissions import Permission
from src.modules.finance.credit_note_service import (
    CREDIT_NOTE_LIFECYCLE,
    sales_invoice_status_after_credit,
)
from src.modules.finance.models import CreditNoteStatus, SalesInvoiceStatus
from src.modules.finance.sales_invoice_service import (
    SALES_INVOICE_LIFECYCLE,
    sales_invoice_status_after_payment,
)

pytestmark = pytest.mark.unit


def test_credit_note_can_only_be_cancelled_while_issued() -> None:
    assert CREDIT_NOTE_LIFECYCLE.can_transition(CreditNoteStatus.ISSUED, CreditNoteStatus.CANCELLED)
    assert not CREDIT_NOTE_LIFECYCLE.can_transition(
        CreditNoteStatus.CANCELLED, CreditNoteStatus.ISSUED
    )


def test_credit_note_cancellation_requires_cancel_permission() -> None:
    assert (
        CREDIT_NOTE_LIFECYCLE.available_actions(
            CreditNoteStatus.ISSUED, {Permission.CREDIT_NOTE_CANCEL}
        )[0].target
        is CreditNoteStatus.CANCELLED
    )
    assert CREDIT_NOTE_LIFECYCLE.available_actions(CreditNoteStatus.ISSUED, set()) == []


def test_cancelled_credit_note_cannot_change_state() -> None:
    with pytest.raises(InvalidStateTransitionError):
        CREDIT_NOTE_LIFECYCLE.assert_transition(CreditNoteStatus.CANCELLED, CreditNoteStatus.ISSUED)


def test_partial_credit_keeps_invoice_open_until_the_receivable_is_zero() -> None:
    assert (
        sales_invoice_status_after_credit(Decimal("0"), Decimal("25"), Decimal("100"))
        is SalesInvoiceStatus.ISSUED
    )
    assert (
        sales_invoice_status_after_credit(Decimal("20"), Decimal("25"), Decimal("100"))
        is SalesInvoiceStatus.PARTIALLY_PAID
    )
    # Credits and payments together settle the receivable; a real payment was
    # received, so the invoice is PAID rather than wholly credited.
    assert (
        sales_invoice_status_after_credit(Decimal("20"), Decimal("80"), Decimal("100"))
        is SalesInvoiceStatus.PAID
    )
    # With no payment at all, the same settlement is a CREDITED invoice.
    assert (
        sales_invoice_status_after_credit(Decimal("0"), Decimal("100"), Decimal("100"))
        is SalesInvoiceStatus.CREDITED
    )


def test_payment_status_nets_issued_credit_notes() -> None:
    assert (
        sales_invoice_status_after_payment(
            total_amount=Decimal("100"),
            paid_amount=Decimal("80"),
            credited_amount=Decimal("20"),
        )
        is SalesInvoiceStatus.PAID
    )
    assert (
        sales_invoice_status_after_payment(
            total_amount=Decimal("100"),
            paid_amount=Decimal("80"),
            credited_amount=Decimal("10"),
        )
        is SalesInvoiceStatus.PARTIALLY_PAID
    )
    assert (
        sales_invoice_status_after_payment(
            total_amount=Decimal("100"),
            paid_amount=Decimal("0"),
            credited_amount=Decimal("100"),
        )
        is SalesInvoiceStatus.CREDITED
    )


def test_credit_invoice_can_reopen_after_cancellation() -> None:
    assert SALES_INVOICE_LIFECYCLE.can_transition(
        SalesInvoiceStatus.CREDITED, SalesInvoiceStatus.PAID
    )
