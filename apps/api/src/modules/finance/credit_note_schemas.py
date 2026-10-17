from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from src.modules.finance.models import CreditNoteReason, CreditNoteStatus


class CreditNoteLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sales_invoice_line_id: uuid.UUID
    quantity: Decimal | None = Field(default=None, gt=0)
    taxable_amount: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=4)


class CreditNoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sales_invoice_id: uuid.UUID
    sales_return_id: uuid.UUID | None = None
    note_date: date
    reason_type: CreditNoteReason
    lines: list[CreditNoteLineIn] = Field(min_length=1, max_length=200)


class CreditNoteCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)


class CreditNoteLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sales_invoice_line_id: uuid.UUID
    item_id: uuid.UUID
    hsn_code: str | None
    quantity: Decimal | None
    taxable_amount: Decimal
    tax_rate: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    tax_amount: Decimal
    line_total: Decimal


class CreditNoteSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    sales_invoice_id: uuid.UUID
    sales_return_id: uuid.UUID | None
    customer_id: uuid.UUID
    note_date: date
    reason_type: CreditNoteReason
    status: CreditNoteStatus
    currency: str
    taxable_amount: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    buyer_name: str
    issued_at: datetime
    pdf_document_id: uuid.UUID | None
    cancelled_at: datetime | None
    cancellation_reason: str | None


class CreditNoteDetail(CreditNoteSummary):
    lines: list[CreditNoteLineRead]
