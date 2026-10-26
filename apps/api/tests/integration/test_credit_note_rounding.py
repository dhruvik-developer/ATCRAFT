"""Partial fiscal reversals reconcile exactly to the issued invoice snapshot."""

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from src.core.clock import today
from src.modules.finance import credit_note_service as credits
from src.modules.finance.credit_note_schemas import CreditNoteCreate, CreditNoteLineIn
from src.modules.finance.models import (
    CreditNote,
    CreditNoteReason,
    CreditNoteStatus,
    LedgerEntry,
    SalesInvoice,
    SalesInvoiceLine,
)
from tests.integration.test_sales_invoice_service import (
    _return_for_invoice,
    issue,
    make_dispatch,
)
from tests.integration.test_sales_invoice_service import bench as bench

pytestmark = pytest.mark.integration


async def small_invoice(b, *, tax_rate="0", interstate=False):
    b.line.unit_price = Decimal("0.3333")
    b.line.discount_amount = Decimal("0")
    b.line.tax_rate = Decimal(tax_rate)
    if interstate:
        b.order.is_interstate = True
        b.customer.state_code = "29"
        b.customer.state = "Karnataka"
    await b.session.flush()
    dispatch = await make_dispatch(b, quantity="3")
    invoice = await issue(b, dispatch)
    assert invoice.lines[0].quantity == 3
    assert invoice.taxable_amount == Decimal("1.00")
    return dispatch, invoice


async def note(b, invoice, *, quantity=None, amount=None, return_id=None):
    return await credits.issue_credit_note(
        b.session,
        b.tenant,
        CreditNoteCreate(
            sales_invoice_id=invoice.id,
            sales_return_id=return_id,
            note_date=today(),
            reason_type=CreditNoteReason.RETURN if return_id else CreditNoteReason.PRICE_ADJUSTMENT,
            lines=[
                CreditNoteLineIn(
                    sales_invoice_line_id=invoice.lines[0].id,
                    quantity=Decimal(quantity) if quantity else None,
                    taxable_amount=Decimal(amount) if amount else None,
                )
            ],
        ),
    )


async def assert_reconciled(b, invoice):
    rows = list(
        (
            await b.session.scalars(
                select(CreditNote).where(
                    CreditNote.sales_invoice_id == invoice.id,
                    CreditNote.status == CreditNoteStatus.ISSUED,
                )
            )
        ).all()
    )
    for field in (
        "taxable_amount",
        "cgst_amount",
        "sgst_amount",
        "igst_amount",
        "tax_amount",
        "round_off_amount",
        "total_amount",
    ):
        assert sum((getattr(row, field) for row in rows), Decimal("0")) == getattr(
            invoice, field
        ), field
    source = await b.session.get(SalesInvoiceLine, invoice.lines[0].id)
    assert source.credited_quantity == 3
    assert source.credited_amount == invoice.taxable_amount
    stored = await b.session.get(SalesInvoice, invoice.id)
    assert stored.status == "CREDITED"
    await b.session.refresh(b.customer)
    assert b.customer.outstanding_amount == 0
    debit, credit = (
        await b.session.execute(
            select(func.sum(LedgerEntry.debit_amount), func.sum(LedgerEntry.credit_amount)).where(
                LedgerEntry.journal_id.in_([row.journal_id for row in rows])
            )
        )
    ).one()
    assert debit == credit


@pytest.mark.parametrize("with_return", [False, True])
@pytest.mark.parametrize("tax_rate", ["0", "18"])
async def test_three_single_unit_credits_close_value_gst_and_round_off(
    bench, with_return, tax_rate
):
    b = bench
    dispatch, invoice = await small_invoice(b, tax_rate=tax_rate)
    if with_return:
        notes = []
        for _ in range(3):
            returned = await _return_for_invoice(
                b, dispatch, quantity="1", requires_qc=False, receive=True
            )
            identity = await b.session.scalar(
                select(CreditNote.id).where(
                    CreditNote.sales_return_id == returned.id,
                    CreditNote.status == CreditNoteStatus.ISSUED,
                )
            )
            notes.append(await credits.get_credit_note(b.session, b.tenant, identity))
    else:
        notes = [await note(b, invoice, quantity="1") for _ in range(3)]
    assert [row.taxable_amount for row in notes] == [
        Decimal("0.33"),
        Decimal("0.34"),
        Decimal("0.33"),
    ]
    await assert_reconciled(b, invoice)


async def test_partial_interstate_credits_preserve_igst_subtotal(bench):
    b = bench
    _, invoice = await small_invoice(b, tax_rate="18", interstate=True)
    assert invoice.igst_amount > 0
    for _ in range(3):
        await note(b, invoice, quantity="1")
    await assert_reconciled(b, invoice)


@pytest.mark.parametrize("timing", ["before", "between", "cancelled"])
async def test_amount_only_discount_reduces_remaining_unit_value_without_losing_paisa(
    bench, timing
):
    b = bench
    _, invoice = await small_invoice(b, tax_rate="18")
    remaining = 3
    if timing != "before":
        await note(b, invoice, quantity="1")
        remaining -= 1
    adjustment = await note(b, invoice, amount="0.10")
    if timing == "cancelled":
        await credits.cancel_credit_note(
            b.session,
            b.tenant,
            adjustment.id,
            reason="Correct the price adjustment before final credit",
        )
    for _ in range(remaining):
        await note(b, invoice, quantity="1")
    await assert_reconciled(b, invoice)


async def test_cancelled_partial_quantity_credit_is_not_counted_when_reissuing(bench):
    b = bench
    _, invoice = await small_invoice(b, tax_rate="18")
    first = await note(b, invoice, quantity="1")
    await credits.cancel_credit_note(
        b.session, b.tenant, first.id, reason="Reissue with corrected credit reference"
    )
    for _ in range(3):
        await note(b, invoice, quantity="1")
    await assert_reconciled(b, invoice)
