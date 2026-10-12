"""Vendor payloads deliberately omit tenant, vendor and internal cost centres."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.core.responses import OffsetPagination
from src.modules.finance.schemas import VendorInvoiceCreate, VendorInvoiceLineIn
from src.modules.procurement.inbound_schemas import (
    InboundDocumentRead,
    InboundShipmentRead,
    InboundTrackingRead,
)
from src.modules.procurement.schemas import PurchaseOrderDetail


class VendorPortalAccept(BaseModel):
    model_config = ConfigDict(extra="forbid")
    committed_delivery_date: date | None = None
    vendor_reference: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=255)


class VendorPortalDateChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: date
    reason: str = Field(min_length=3, max_length=255)


class VendorPortalReady(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ready_on: datetime | None = None
    packages: int | None = Field(default=None, ge=1)
    note: str | None = Field(default=None, max_length=2000)


class VendorPortalInvoiceLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: uuid.UUID | None = None
    purchase_order_line_id: uuid.UUID | None = None
    goods_receipt_line_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=500)
    hsn_code: str | None = Field(default=None, max_length=16)
    quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=4)
    uom_id: uuid.UUID | None = None
    unit_price: Decimal = Field(ge=0, max_digits=18, decimal_places=6)
    discount_amount: Decimal = Field(default=Decimal("0"), ge=0, max_digits=18, decimal_places=4)
    tax_rate: Decimal = Field(default=Decimal("0"), ge=0, le=100, max_digits=9, decimal_places=4)
    is_interstate: bool = False
    notes: str | None = Field(default=None, max_length=2000)


class VendorPortalInvoiceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purchase_order_id: uuid.UUID
    invoice_document_id: uuid.UUID | None = None
    vendor_invoice_number: str = Field(min_length=1, max_length=64)
    invoice_date: date
    due_date: date | None = None
    goods_receipt_id: uuid.UUID | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    round_off_amount: Decimal = Field(default=Decimal("0"), max_digits=18, decimal_places=4)
    notes: str | None = Field(default=None, max_length=2000)
    lines: list[VendorPortalInvoiceLine] = Field(min_length=1, max_length=200)

    def staff_payload(self, vendor_id: uuid.UUID) -> VendorInvoiceCreate:
        return VendorInvoiceCreate(
            **self.model_dump(exclude={"lines", "invoice_document_id"}),
            vendor_id=vendor_id,
            lines=[VendorInvoiceLineIn(**line.model_dump()) for line in self.lines],
        )


class VendorPortalCategory(BaseModel):
    id: uuid.UUID
    code: str
    name: str


class VendorPortalMe(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    roles: list[str]
    categories: list[VendorPortalCategory]
    contact_name: str | None = None
    email: str | None = None
    phone: str | None = None
    unread_count: int = 0


class VendorPortalDashboard(BaseModel):
    to_accept: int = 0
    accepted: int = 0
    ready: int = 0
    in_transit: int = 0
    delivered: int = 0
    payment_pending: int = 0
    qc_rejected: int = 0


class VendorPortalReceiptRead(BaseModel):
    id: uuid.UUID
    code: str
    purchase_order_id: uuid.UUID
    receipt_date: date
    status: str
    lines: list[dict[str, Any]]
    inspections: list[dict[str, Any]]


class VendorPortalOrderDetail(PurchaseOrderDetail):
    tracking: InboundTrackingRead
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    shipment_count: int = 0
    documents_count: int = 0
    invoice_summary: dict[str, Any] = Field(default_factory=dict)
    receipt_summary: dict[str, Any] = Field(default_factory=dict)
    shipments: list[InboundShipmentRead] = Field(default_factory=list)
    shipments_pagination: OffsetPagination | None = None
    documents: list[InboundDocumentRead] = Field(default_factory=list)
    documents_pagination: OffsetPagination | None = None
    receipts: list[VendorPortalReceiptRead] = Field(default_factory=list)
    receipts_pagination: OffsetPagination | None = None


class VendorPortalPaymentSummary(BaseModel):
    currency: str
    outstanding: Decimal
    overdue: Decimal
    due_next_seven_days: Decimal
    paid_this_month: Decimal
    last_payment_date: date | None
    last_payment_amount: Decimal | None
