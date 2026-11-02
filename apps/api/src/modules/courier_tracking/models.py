"""Tenant-owned courier connections and immutable normalized tracking events."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models import ImmutableLedgerEntity, TenantMasterEntity, sql_in_list
from src.core.types import GUID

MAX_POLL_PER_ACCOUNT = 100
MAX_WEBHOOK_BYTES = 64 * 1024
WEBHOOK_CLOCK_SKEW_SECONDS = 300


class CourierProvider(StrEnum):
    SHIPROCKET = "SHIPROCKET"
    GENERIC_WEBHOOK = "GENERIC_WEBHOOK"


class CourierAccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class CourierStatus(StrEnum):
    BOOKED = "BOOKED"
    PICKED_UP = "PICKED_UP"
    IN_TRANSIT = "IN_TRANSIT"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    FAILED_DELIVERY = "FAILED_DELIVERY"
    RETURNED = "RETURNED"
    EXCEPTION = "EXCEPTION"


class CourierAccount(TenantMasterEntity):
    __tablename__ = "courier_accounts"

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[CourierProvider] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    credentials_ciphertext: Mapped[str | None] = mapped_column(Text)
    webhook_token_hash: Mapped[str | None] = mapped_column(String(64))
    webhook_secret_ciphertext: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="true")
    auto_confirm_delivery: Mapped[bool] = mapped_column(
        nullable=False, default=True, server_default="true"
    )
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_courier_accounts_company_code"),
        Index(
            "uq_courier_accounts_webhook_token",
            "webhook_token_hash",
            unique=True,
            postgresql_where=text("webhook_token_hash IS NOT NULL"),
        ),
        Index("ix_courier_accounts_company_active", "company_id", "is_active"),
        CheckConstraint(f"provider IN ({sql_in_list(CourierProvider)})", name="provider_is_known"),
        CheckConstraint("length(btrim(code)) > 0", name="code_not_blank"),
        CheckConstraint("length(btrim(display_name)) > 0", name="display_name_not_blank"),
    )


class CourierTrackingEvent(ImmutableLedgerEntity):
    __tablename__ = "courier_tracking_events"

    courier_account_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("courier_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    tracking_number: Mapped[str] = mapped_column(String(120), nullable=False)
    normalized_status: Mapped[CourierStatus] = mapped_column(String(32), nullable=False)
    raw_status: Mapped[str] = mapped_column(String(120), nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    location: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(500))
    provider_event_id: Mapped[str | None] = mapped_column(String(160))
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    dispatch_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("dispatches.id", ondelete="SET NULL")
    )
    shipment_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("purchase_order_shipments.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "courier_account_id",
            "tracking_number",
            "normalized_status",
            "event_time",
            name="uq_courier_event_natural_key",
        ),
        Index(
            "uq_courier_event_provider_id",
            "company_id",
            "courier_account_id",
            "provider_event_id",
            unique=True,
            postgresql_where=provider_event_id.is_not(None),
        ),
        Index("ix_courier_events_keyset", "company_id", "created_at", "id"),
        Index("ix_courier_events_tracking", "company_id", "tracking_number", "event_time"),
        Index("ix_courier_events_dispatch", "company_id", "dispatch_id", "event_time"),
        Index("ix_courier_events_shipment", "company_id", "shipment_id", "event_time"),
        CheckConstraint(
            f"normalized_status IN ({sql_in_list(CourierStatus)})", name="status_is_known"
        ),
        CheckConstraint("length(btrim(tracking_number)) > 0", name="tracking_not_blank"),
    )
