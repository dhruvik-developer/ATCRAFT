"""Inbound consignments, owned by procurement."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models import TenantEntity
from src.core.types import GUID, Money, Quantity


class PurchaseOrderShipment(TenantEntity):
    __tablename__ = "purchase_order_shipments"

    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("purchase_orders.id", ondelete="RESTRICT"), nullable=False
    )
    shipment_number: Mapped[int] = mapped_column(Integer, nullable=False)
    transporter_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("transporters.id", ondelete="RESTRICT")
    )
    courier_name: Mapped[str | None] = mapped_column(String(120))
    tracking_reference: Mapped[str | None] = mapped_column(String(120))
    lr_number: Mapped[str | None] = mapped_column(String(64))
    vehicle_number: Mapped[str | None] = mapped_column(String(32))
    driver_name: Mapped[str | None] = mapped_column(String(120))
    driver_mobile: Mapped[str | None] = mapped_column(String(32))
    shipped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expected_delivery_date: Mapped[date | None] = mapped_column(Date)
    packages: Mapped[int | None] = mapped_column(Integer)
    weight_kg: Mapped[Decimal | None] = mapped_column(Quantity)
    freight_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "company_id", "purchase_order_id", "shipment_number", name="uq_po_shipments_number"
        ),
        CheckConstraint("shipment_number >= 1", name="number_positive"),
        CheckConstraint("packages IS NULL OR packages >= 1", name="packages_positive"),
        CheckConstraint("weight_kg IS NULL OR weight_kg >= 0", name="weight_nonnegative"),
        CheckConstraint("freight_amount >= 0", name="freight_nonnegative"),
        CheckConstraint(
            "transporter_id IS NOT NULL OR courier_name IS NOT NULL", name="carrier_required"
        ),
        CheckConstraint(
            "delivered_at IS NULL OR delivered_at >= shipped_at", name="delivery_after_dispatch"
        ),
        Index("ix_po_shipments_order", "company_id", "purchase_order_id", "shipment_number"),
    )


class PurchaseOrderDocument(TenantEntity):
    """Typed attachment link; the generic document schema remains unchanged."""

    __tablename__ = "purchase_order_documents"
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("purchase_orders.id", ondelete="RESTRICT"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    shipment_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey(
            "purchase_order_shipments.id", ondelete="RESTRICT", name="fk_po_documents_shipment"
        ),
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    vendor_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("vendor_invoices.id", ondelete="RESTRICT")
    )
    __table_args__ = (
        CheckConstraint(
            "kind IN ('INVOICE','DISPATCH_PHOTO','LR_COPY','DELIVERY_CHALLAN','OTHER')",
            name="kind_known",
        ),
        Index("ix_po_documents_order", "company_id", "purchase_order_id"),
    )
