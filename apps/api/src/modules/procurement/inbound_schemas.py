"""Shared staff and vendor consignment contracts."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.modules.documents.schemas import DocumentRead
from src.modules.procurement.schemas import (
    NonNegativeMoney,
    NonNegativeQuantity,
    PurchaseOrderSummary,
)


class InboundTrackingStatus(StrEnum):
    NOT_DISPATCHED = "NOT_DISPATCHED"
    READY_FOR_DISPATCH = "READY_FOR_DISPATCH"
    DISPATCHED = "DISPATCHED"
    IN_TRANSIT = "IN_TRANSIT"
    DELIVERED = "DELIVERED"
    RECEIVED = "RECEIVED"
    QC_PENDING = "QC_PENDING"
    GRN_COMPLETED = "GRN_COMPLETED"


DocumentKind = Literal["INVOICE", "DISPATCH_PHOTO", "LR_COPY", "DELIVERY_CHALLAN", "OTHER"]


class InboundShipmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transporter_id: uuid.UUID | None = None
    courier_name: str | None = Field(default=None, min_length=1, max_length=120)
    tracking_reference: str | None = Field(default=None, max_length=120)
    lr_number: str | None = Field(default=None, max_length=64)
    vehicle_number: str | None = Field(default=None, max_length=32)
    driver_name: str | None = Field(default=None, max_length=120)
    driver_mobile: str | None = Field(default=None, max_length=32)
    shipped_at: datetime
    expected_delivery_date: date | None = None
    packages: int | None = Field(default=None, ge=1)
    weight_kg: NonNegativeQuantity | None = None
    freight_amount: NonNegativeMoney = Decimal("0")
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_consignment(self) -> InboundShipmentCreate:
        if self.transporter_id is None and not (self.courier_name or "").strip():
            raise ValueError("A transporter or courier name is required.")
        if self.shipped_at.tzinfo is None:
            raise ValueError("shipped_at must include a timezone offset.")
        self.shipped_at = self.shipped_at.astimezone(UTC)
        if self.expected_delivery_date and self.expected_delivery_date < self.shipped_at.date():
            raise ValueError("Expected delivery cannot precede dispatch.")
        return self


class InboundShipmentRead(InboundShipmentCreate):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    purchase_order_id: uuid.UUID
    shipment_number: int
    delivered_at: datetime | None
    created_at: datetime


class InboundDeliveredRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delivered_at: datetime | None = None


class InboundDocumentRead(DocumentRead):
    kind: DocumentKind = "OTHER"
    shipment_id: uuid.UUID | None = None


class InboundTrackingRead(PurchaseOrderSummary):
    vendor_name: str
    assignee_name: str | None = None
    tracking_status: InboundTrackingStatus
    latest_shipment: InboundShipmentRead | None = None
    delivery_expected_date: date | None = None
    days_late: int = 0
    received_percentage: Decimal = Decimal("0")
    item_count: int = 0
    grn_pending: bool = False
    qc_pending: bool = False
    documents_count: int = 0
    has_lr_copy: bool = False
    courier_name: str | None = None
    tracking_reference: str | None = None
    shipped_at: datetime | None = None


class InboundTrackingFilters(BaseModel):
    search: str | None = None
    vendor_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    overdue: bool = False
    expected_today: bool = False
    in_transit: bool = False
    not_acknowledged: bool = False
    assigned_to_me: bool = False
    tab: Literal["to_accept", "accepted", "ready", "dispatched", "delivered", "all"] = "all"
