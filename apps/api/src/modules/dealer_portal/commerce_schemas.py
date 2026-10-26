"""Explicit customer-facing financial projections; internal journals stay private."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.responses import OffsetPagination
from src.modules.finance.credit_note_schemas import CreditNoteLineRead
from src.modules.finance.models import CreditNoteReason, CreditNoteStatus, SalesInvoiceStatus
from src.modules.finance.sales_invoice_schemas import SalesInvoiceLineRead, SalesInvoicePaymentRead
from src.modules.sales_returns.schemas import SalesReturnDetail, SalesReturnSummary


class DealerInvoiceSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    dispatch_id: uuid.UUID
    sales_order_id: uuid.UUID
    invoice_date: date
    due_date: date
    currency: str
    total_amount: Decimal
    paid_amount: Decimal
    credited_amount: Decimal
    balance_amount: Decimal
    overdue_days: int
    status: SalesInvoiceStatus
    pdf_document_id: uuid.UUID | None


class DealerCreditNoteSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    sales_invoice_id: uuid.UUID
    sales_return_id: uuid.UUID | None
    note_date: date
    reason_type: CreditNoteReason
    status: CreditNoteStatus
    currency: str
    taxable_amount: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    round_off_amount: Decimal = Decimal("0")
    pdf_document_id: uuid.UUID | None
    cancelled_at: datetime | None
    cancellation_reason: str | None


class DealerCreditNoteDetail(DealerCreditNoteSummary):
    lines: list[CreditNoteLineRead]


class DealerInvoiceDetail(DealerInvoiceSummary):
    seller_name: str
    seller_gstin: str | None
    seller_address: str
    buyer_name: str
    buyer_gstin: str | None
    billing_address: str
    shipping_address: str
    taxable_amount: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    tax_amount: Decimal
    round_off_amount: Decimal
    lines: list[SalesInvoiceLineRead]
    payments: list[SalesInvoicePaymentRead]
    credit_notes: list[DealerCreditNoteSummary]
    credit_notes_pagination: OffsetPagination


class DealerReturnLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dispatch_line_id: uuid.UUID
    quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=4)
    reason_code: str = Field(min_length=2, max_length=32)
    note: str | None = Field(default=None, max_length=2000)


class DealerReturnCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dispatch_id: uuid.UUID
    lines: list[DealerReturnLineIn] = Field(min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def unique_lines(self) -> DealerReturnCreate:
        ids = [line.dispatch_line_id for line in self.lines]
        if len(ids) != len(set(ids)):
            raise ValueError("Each dispatch line can appear only once in a return request.")
        return self


class DealerReturnSummary(SalesReturnSummary):
    credited_amount: Decimal = Decimal("0")


class DealerReturnDetail(SalesReturnDetail):
    credited_amount: Decimal = Decimal("0")


class DealerEligibleDispatchLine(BaseModel):
    dispatch_line_id: uuid.UUID
    item_id: uuid.UUID
    item_code: str
    item_name: str
    returnable_quantity: Decimal


class DealerEligibleDispatch(BaseModel):
    id: uuid.UUID
    code: str
    dispatched_at: datetime | None
    lines: list[DealerEligibleDispatchLine]
