"""Issued sales invoice contracts. Preview carries no identity or sequence number."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from src.modules.finance.models import SalesInvoiceStatus


class SalesInvoiceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dispatch_id: uuid.UUID
    invoice_date: date | None = None
    notes: Annotated[str, Field(max_length=2000)] | None = None
    transporter_name: Annotated[str, Field(max_length=120)] | None = None
    vehicle_number: Annotated[str, Field(max_length=24)] | None = None
    lr_number: Annotated[str, Field(max_length=64)] | None = None
    eway_bill_number: Annotated[str, Field(max_length=64)] | None = None


class SalesInvoiceCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Annotated[str, Field(min_length=3, max_length=255)]


class SalesInvoiceLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    dispatch_line_id: uuid.UUID
    sales_order_line_id: uuid.UUID
    item_id: uuid.UUID
    uom_id: uuid.UUID
    line_number: int
    description: str
    hsn_code: str | None
    uom_code: str
    quantity: Decimal
    credited_quantity: Decimal
    credited_amount: Decimal
    unit_price: Decimal
    tax_rate: Decimal
    subtotal: Decimal
    discount_amount: Decimal
    taxable_amount: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    tax_amount: Decimal
    line_total: Decimal


class SalesInvoicePreview(BaseModel):
    dispatch_id: uuid.UUID
    sales_order_id: uuid.UUID
    customer_id: uuid.UUID
    invoice_date: date
    due_date: date
    currency: str
    is_interstate: bool
    place_of_supply_state_code: str | None
    seller_name: str
    seller_gstin: str | None
    seller_address: str
    seller_state_code: str | None
    seller_state_name: str | None
    buyer_name: str
    buyer_gstin: str | None
    buyer_state_code: str | None
    buyer_state_name: str | None
    billing_address: str
    shipping_address: str
    transporter_name: str | None
    vehicle_number: str | None
    lr_number: str | None
    eway_bill_number: str | None
    bank_name: str | None
    bank_account_name: str | None
    bank_account_number: str | None
    bank_ifsc: str | None
    bank_branch: str | None
    invoice_terms: str | None
    notes: str | None
    subtotal: Decimal
    discount_amount: Decimal
    taxable_amount: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    tax_amount: Decimal
    round_off_amount: Decimal
    total_amount: Decimal
    lines: list[SalesInvoiceLineRead]


class SalesInvoiceSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    code: str
    dispatch_id: uuid.UUID
    sales_order_id: uuid.UUID
    customer_id: uuid.UUID
    buyer_name: str
    invoice_date: date
    due_date: date
    currency: str
    total_amount: Decimal
    paid_amount: Decimal
    balance_amount: Decimal
    overdue_days: int
    status: SalesInvoiceStatus
    issued_at: datetime
    pdf_document_id: uuid.UUID | None
    can_cancel: bool


class SalesInvoicePaymentRead(BaseModel):
    payment_id: uuid.UUID
    payment_code: str
    payment_date: date
    amount: Decimal


class SalesInvoiceDetail(SalesInvoicePreview, SalesInvoiceSummary):
    issued_by_id: uuid.UUID
    cancelled_at: datetime | None
    cancelled_by_id: uuid.UUID | None
    cancellation_reason: str | None
    journal_id: uuid.UUID
    payments: list[SalesInvoicePaymentRead]


class SalesInvoiceFilters(BaseModel):
    search: str | None = None
    status: SalesInvoiceStatus | None = None
    customer_id: uuid.UUID | None = None
    sales_order_id: uuid.UUID | None = None
    dispatch_id: uuid.UUID | None = None
    date_from: date | None = None
    date_to: date | None = None
    overdue_only: bool = False
    open_only: bool = False
