"""Shared staff and vendor consignment contracts.

Every read model here names its fields explicitly rather than extending the
staff purchase-order schemas. The same rows are served to vendor principals
through ``/vendor-portal``, and a read model that inherited
``PurchaseOrderSummary`` would hand a vendor every field a later change adds to
the buyer's document - internal notes, duplicate-override reasons, the
requisition it was raised from - without anyone deciding to.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.modules.documents.schemas import DocumentRead
from src.modules.procurement.models import PurchaseOrderStatus
from src.modules.procurement.schemas import NonNegativeMoney, NonNegativeQuantity


class InboundTrackingStatus(StrEnum):
    """The proposal's admin tracking vocabulary (Add-on section 07), derived.

    Never stored: computed by ``inbound_repository.inbound_tracking_status``
    from the order, its latest consignment, its receipts and their inspections,
    so the buyer's board, the vendor's order list and the vendor dashboard read
    one rule and cannot disagree.
    """

    NOT_DISPATCHED = "NOT_DISPATCHED"
    READY_FOR_DISPATCH = "READY_FOR_DISPATCH"
    DISPATCHED = "DISPATCHED"
    IN_TRANSIT = "IN_TRANSIT"
    DELIVERED = "DELIVERED"
    RECEIVED = "RECEIVED"
    QC_PENDING = "QC_PENDING"
    GRN_COMPLETED = "GRN_COMPLETED"


DocumentKind = Literal["INVOICE", "DISPATCH_PHOTO", "LR_COPY", "DELIVERY_CHALLAN", "OTHER"]

#: How a kind reads in a notification body ("LR copy uploaded").
DOCUMENT_KIND_LABELS: dict[str, str] = {
    "INVOICE": "Invoice",
    "DISPATCH_PHOTO": "Dispatch photo",
    "LR_COPY": "LR copy",
    "DELIVERY_CHALLAN": "Delivery challan",
    "OTHER": "Document",
}


class InboundShipmentCreate(BaseModel):
    """One consignment, as a vendor or a buyer records it.

    The same body creates a consignment and (``PATCH``) replaces one, so a
    correction is checked by exactly the rules the first entry was.
    """

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


class InboundShipmentRead(BaseModel):
    """A stored consignment. Plain types: reading a row never re-runs write rules."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    purchase_order_id: uuid.UUID
    shipment_number: int
    transporter_id: uuid.UUID | None
    courier_name: str | None
    tracking_reference: str | None
    lr_number: str | None
    vehicle_number: str | None
    driver_name: str | None
    driver_mobile: str | None
    shipped_at: datetime
    delivered_at: datetime | None
    expected_delivery_date: date | None
    packages: int | None
    weight_kg: Decimal | None
    freight_amount: Decimal
    notes: str | None
    created_at: datetime


class InboundDeliveredRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Defaults to now. Must carry a timezone, may not precede the dispatch and
    #: may not lie in the future.
    delivered_at: datetime | None = None


class InboundDocumentRead(DocumentRead):
    kind: DocumentKind = "OTHER"
    shipment_id: uuid.UUID | None = None


class InboundTrackingRead(BaseModel):
    """One row of the inbound board and of the vendor's order list."""

    id: uuid.UUID
    code: str
    po_date: date
    status: PurchaseOrderStatus
    vendor_id: uuid.UUID
    vendor_name: str
    warehouse_id: uuid.UUID
    item_category_id: uuid.UUID | None = None
    item_category_code: str | None = None
    item_category_name: str | None = None
    assigned_vendor_user_id: uuid.UUID | None = None
    assignee_name: str | None = None
    currency: str
    total_amount: Decimal
    item_count: int = 0
    #: The buyer's planning date - what MRP nets on and what "late" is measured against.
    expected_delivery_date: date | None = None
    #: What the vendor promised. Separate until a buyer accepts it into planning.
    vendor_committed_date: date | None = None
    #: The proposal's "Delivery expected date": the latest consignment's own
    #: expectation, else the vendor's commitment, else the planning date.
    delivery_expected_date: date | None = None
    sent_at: datetime | None = None
    acknowledged_at: datetime | None = None
    ready_for_dispatch_at: datetime | None = None
    ready_packages: int | None = None
    vendor_decline_reason: str | None = None
    tracking_status: InboundTrackingStatus
    shipment_count: int = 0
    latest_shipment: InboundShipmentRead | None = None
    courier_name: str | None = None
    tracking_reference: str | None = None
    shipped_at: datetime | None = None
    days_late: int = 0
    received_percentage: Decimal = Decimal("0")
    grn_pending: bool = False
    qc_pending: bool = False
    documents_count: int = 0
    has_lr_copy: bool = False
    created_at: datetime


class InboundTrackingFilters(BaseModel):
    search: str | None = None
    vendor_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    overdue: bool = False
    expected_today: bool = False
    in_transit: bool = False
    not_acknowledged: bool = False
    assigned_to_me: bool = False
    #: The buyer's board shows open inbound work unless this is set; the vendor
    #: list always shows its whole history and its tabs narrow it.
    include_completed: bool = False
    tab: Literal["to_accept", "accepted", "ready", "dispatched", "delivered", "all"] = "all"
