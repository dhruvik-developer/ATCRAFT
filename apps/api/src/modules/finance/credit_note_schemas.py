from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.modules.finance.models import CreditNoteReason, CreditNoteStatus


class CreditNoteLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sales_invoice_line_id: uuid.UUID
    quantity: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=4)
    taxable_amount: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=4)

    @model_validator(mode="after")
    def validate_credit_basis(self) -> Self:
        if (self.quantity is None) == (self.taxable_amount is None):
            raise ValueError(
                "Each credit-note line needs exactly one of quantity or taxable amount."
            )
        return self


class CreditNoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sales_invoice_id: uuid.UUID
    sales_return_id: uuid.UUID | None = None
    note_date: date
    reason_type: CreditNoteReason
    lines: list[CreditNoteLineIn] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_lines_and_return(self) -> Self:
        line_ids = [line.sales_invoice_line_id for line in self.lines]
        if len(line_ids) != len(set(line_ids)):
            raise ValueError("Each invoice line can appear only once in a credit note.")
        if self.reason_type is CreditNoteReason.RETURN:
            if self.sales_return_id is None:
                raise ValueError("A return credit note must reference a sales return.")
            if any(line.quantity is None for line in self.lines):
                raise ValueError("Return credit notes must specify accepted quantities.")
        elif self.sales_return_id is not None:
            raise ValueError("Only a return credit note can reference a sales return.")
        return self


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
    round_off_amount: Decimal = Decimal("0")
    buyer_name: str
    issued_at: datetime
    pdf_document_id: uuid.UUID | None
    cancelled_at: datetime | None
    cancellation_reason: str | None


class CreditNoteDetail(CreditNoteSummary):
    lines: list[CreditNoteLineRead]
