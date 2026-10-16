from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models import TenantEntity, sql_in_list
from src.core.types import GUID, Quantity


class SalesReturnStatus(StrEnum):
    DRAFT = "DRAFT"
    RECEIVED = "RECEIVED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class SalesReturnSource(StrEnum):
    ERP = "ERP"
    DEALER_PORTAL = "DEALER_PORTAL"


class SalesReturn(TenantEntity):
    __tablename__ = "sales_returns"

    code: Mapped[str] = mapped_column(String(24), nullable=False)
    dispatch_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("dispatches.id", ondelete="RESTRICT"), nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    return_warehouse_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[SalesReturnStatus] = mapped_column(
        String(16), nullable=False, default=SalesReturnStatus.DRAFT, server_default="DRAFT"
    )
    source: Mapped[SalesReturnSource] = mapped_column(
        String(16), nullable=False, default=SalesReturnSource.ERP, server_default="ERP"
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_sales_returns_company_code"),
        Index("ix_sales_returns_customer_date", "company_id", "customer_id", "created_at", "id"),
        Index("ix_sales_returns_dispatch", "company_id", "dispatch_id"),
        Index("ix_sales_returns_status", "company_id", "status", "created_at", "id"),
        CheckConstraint(
            f"status IN ({sql_in_list(SalesReturnStatus)})", name="sales_return_status_known"
        ),
        CheckConstraint(
            f"source IN ({sql_in_list(SalesReturnSource)})", name="sales_return_source_known"
        ),
        CheckConstraint(
            "status <> 'CANCELLED' OR "
            "(cancelled_at IS NOT NULL AND cancellation_reason IS NOT NULL)",
            name="sales_return_cancellation_evidence",
        ),
    )


class SalesReturnLine(TenantEntity):
    __tablename__ = "sales_return_lines"

    sales_return_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("sales_returns.id", ondelete="CASCADE"), nullable=False
    )
    dispatch_line_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("dispatch_lines.id", ondelete="RESTRICT"), nullable=False
    )
    qc_inspection_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("qc_inspections.id", ondelete="SET NULL"), nullable=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    credited_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    requires_qc: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="true")

    __table_args__ = (
        UniqueConstraint(
            "sales_return_id", "dispatch_line_id", name="uq_sales_return_lines_dispatch"
        ),
        Index("ix_sales_return_lines_return", "company_id", "sales_return_id"),
        CheckConstraint("quantity > 0", name="sales_return_line_quantity_positive"),
        CheckConstraint(
            "credited_quantity >= 0 AND credited_quantity <= quantity",
            name="sales_return_line_credited_bounded",
        ),
    )


__all__ = [
    "SalesReturn",
    "SalesReturnLine",
    "SalesReturnSource",
    "SalesReturnStatus",
]
