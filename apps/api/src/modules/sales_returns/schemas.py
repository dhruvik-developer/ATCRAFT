from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from src.modules.sales_returns.models import SalesReturnSource, SalesReturnStatus


class SalesReturnLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dispatch_line_id: uuid.UUID
    qc_inspection_id: uuid.UUID | None = None
    serial_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10_000)
    quantity: Decimal = Field(gt=0)
    reason_code: str = Field(min_length=2, max_length=32)
    note: str | None = Field(default=None, max_length=2000)
    requires_qc: bool = True


class SalesReturnCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dispatch_id: uuid.UUID
    return_warehouse_id: uuid.UUID | None = None
    lines: list[SalesReturnLineIn] = Field(min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=4000)
    source: SalesReturnSource = SalesReturnSource.ERP


class SalesReturnCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=255)


class SalesReturnLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    dispatch_line_id: uuid.UUID
    item_id: uuid.UUID
    serial_ids: list[uuid.UUID]
    qc_inspection_id: uuid.UUID | None
    quantity: Decimal
    credited_quantity: Decimal
    reason_code: str
    note: str | None
    requires_qc: bool


class SalesReturnSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    dispatch_id: uuid.UUID
    customer_id: uuid.UUID
    return_warehouse_id: uuid.UUID | None
    status: SalesReturnStatus
    source: SalesReturnSource
    notes: str | None
    received_at: datetime | None
    cancelled_at: datetime | None
    cancellation_reason: str | None


class SalesReturnDetail(SalesReturnSummary):
    lines: list[SalesReturnLineRead]
