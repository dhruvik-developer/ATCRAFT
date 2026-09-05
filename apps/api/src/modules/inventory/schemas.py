"""Request and response schemas for the inventory engine.

Conventions applied throughout:

* **Quantities and money are ``Decimal``**, never float (section 84). Pydantic
  keeps the exact decimal the client sent, so a quantity of ``0.1`` stays
  ``0.1`` rather than becoming ``0.1000000000000000055``.
* **Line quantities are magnitudes, not signs.** The transfer and count schemas
  accept positive numbers only; the sign of the resulting ledger row comes from
  the transaction type. The one exception is an adjustment line, where the sign
  *is* the business input - a document routinely writes one part up and another
  down - so it is validated as non-zero rather than positive.
* **No schema carries ``company_id``.** It comes from the authenticated
  principal's tenant context and is never accepted from a client (section 40).
* **There is deliberately no "post a movement" request schema.** Stock only ever
  moves through a document - a receipt, an issue, a transfer, an adjustment, a
  count - so exposing a free-form movement endpoint would create an unauditable
  back door around the controls those documents carry.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from src.common.codes import clean_text
from src.modules.inventory.models import (
    AdjustmentReason,
    AdjustmentStatus,
    BatchStatus,
    PhysicalCountStatus,
    SerialStatus,
    StockState,
    TransactionType,
    TransferStatus,
)

Reason = Annotated[str, Field(min_length=3, max_length=255)]
PositiveQuantity = Annotated[Decimal, Field(gt=0, le=Decimal("99999999999999"))]
NonNegativeQuantity = Annotated[Decimal, Field(ge=0, le=Decimal("99999999999999"))]
NonZeroQuantity = Annotated[
    Decimal, Field(ge=Decimal("-99999999999999"), le=Decimal("99999999999999"))
]
UnitCost = Annotated[Decimal, Field(ge=0, le=Decimal("99999999999999"))]

#: A single request may not post more lines than this. An unbounded document
#: would hold row locks on an unbounded number of balances for an unbounded
#: time, which is how one careless import stalls every other stock movement.
MAX_DOCUMENT_LINES = 500


# -----------------------------------------------------------------------------
# Balances and ledger
# -----------------------------------------------------------------------------


class BalanceFilters(BaseModel):
    """Filters for the stock-position list."""

    item_id: uuid.UUID | None = None
    warehouse_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None
    #: Positions whose available quantity has fallen to or below the item's
    #: reorder level. Items with no reorder level set are never "below" it.
    below_reorder: bool = Field(
        default=False, description="Only positions at or under the item's reorder level"
    )
    #: Hides positions that have gone to zero but are kept for their history.
    only_in_stock: bool = Field(default=False, description="Hide positions that are at zero")
    search: str | None = Field(default=None, max_length=120, description="Item code or name")


class BalanceRead(BaseModel):
    """One stock position, with the labels a screen needs to render it.

    Carries no cost. ``average_cost`` and ``stock_value`` are what a competitor
    would want and what ``item.cost_view`` / ``inventory.valuation_view`` exist
    to gate, and a store keeper reading stock levels holds neither. They live on
    :class:`BalanceReadWithCost` instead, chosen per request.

    A subclass rather than a field deleted after the fact, following
    ``products.service.item_response``: without the right, the response model
    has no cost field at all, so no later refactor can put it back by accident.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    item_id: uuid.UUID
    item_code: str | None = None
    item_name: str | None = None
    warehouse_id: uuid.UUID
    warehouse_code: str | None = None
    location_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None
    batch_number: str | None = None

    on_hand_quantity: Decimal
    reserved_quantity: Decimal
    allocated_quantity: Decimal
    in_transit_quantity: Decimal
    qc_hold_quantity: Decimal
    damaged_quantity: Decimal
    job_work_quantity: Decimal
    scrap_quantity: Decimal
    available_quantity: Decimal

    #: Copied from the item master so a screen can show "below reorder" without
    #: a second round trip per row.
    reorder_level: Decimal | None = None
    last_movement_at: datetime | None = None
    updated_at: datetime | None = None


class BalanceReadWithCost(BalanceRead):
    """A stock position including what it is worth.

    Served only to a caller holding ``item.cost_view``.
    """

    average_cost: Decimal
    stock_value: Decimal


class WarehouseStockRow(BaseModel):
    """Stock of one item in one warehouse, aggregated over locations and batches."""

    warehouse_id: uuid.UUID
    warehouse_code: str | None = None
    warehouse_name: str | None = None
    on_hand_quantity: Decimal
    reserved_quantity: Decimal
    available_quantity: Decimal
    in_transit_quantity: Decimal
    qc_hold_quantity: Decimal
    damaged_quantity: Decimal
    job_work_quantity: Decimal


class WarehouseStockRowWithCost(WarehouseStockRow):
    """One warehouse's line of an item summary, including its value."""

    stock_value: Decimal


class ItemStockSummary(BaseModel):
    """An item's stock across every warehouse that holds it.

    ``total_owned_quantity`` deliberately adds the pools that are not on hand
    here - in transit and out at a job-work vendor - because that stock is still
    the company's and a planner who cannot see it will order it twice.
    """

    item_id: uuid.UUID
    item_code: str | None = None
    item_name: str | None = None
    reorder_level: Decimal | None = None
    total_on_hand: Decimal
    total_reserved: Decimal
    total_available: Decimal
    total_in_transit: Decimal
    total_job_work: Decimal
    total_owned_quantity: Decimal
    warehouses: Sequence[WarehouseStockRow] = Field(default_factory=list)


class ItemStockSummaryWithCost(ItemStockSummary):
    """An item's stock across every warehouse, including what it is worth.

    Served only to a caller holding ``inventory.valuation_view``: a total
    valuation across the company is exactly the figure that permission gates.
    """

    total_value: Decimal
    warehouses: Sequence[WarehouseStockRowWithCost] = Field(default_factory=list)


class LedgerFilters(BaseModel):
    """Filters for the append-only movement ledger."""

    item_id: uuid.UUID | None = None
    warehouse_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None
    serial_id: uuid.UUID | None = None
    transaction_type: TransactionType | None = None
    stock_state: StockState | None = None
    reference_type: str | None = Field(default=None, max_length=48)
    reference_id: uuid.UUID | None = None
    occurred_from: datetime | None = Field(default=None, description="ISO 8601 lower bound")
    occurred_to: datetime | None = Field(default=None, description="ISO 8601 upper bound")

    @field_validator("occurred_to")
    @classmethod
    def _ordered_window(cls, value: datetime | None, info: ValidationInfo) -> datetime | None:
        # A field validator rather than a model one so the failure is reported
        # against ``occurred_to`` - ``query.occurred_to`` in the error envelope -
        # and the frontend can attach it to the control that produced it.
        lower = info.data.get("occurred_from")
        if value is None or lower is None:
            return value
        # Python refuses to compare a naive datetime with an aware one. The
        # window is then passed through as given and applied by the database
        # like any other bound, rather than crashing the request.
        if (value.tzinfo is None) != (lower.tzinfo is None):
            return value
        if value < lower:
            raise ValueError("occurred_to must not be earlier than occurred_from.")
        return value


class LedgerEntryRead(BaseModel):
    """One ledger row as the API reports it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    transaction_type: TransactionType
    item_id: uuid.UUID
    item_code: str | None = None
    item_name: str | None = None
    warehouse_id: uuid.UUID
    warehouse_code: str | None = None
    location_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None
    serial_id: uuid.UUID | None = None
    quantity: Decimal
    unit_cost: Decimal
    total_cost: Decimal
    balance_after: Decimal
    stock_state: StockState
    reference_type: str
    reference_id: uuid.UUID | None = None
    reference_code: str | None = None
    reason: str
    occurred_at: datetime
    created_at: datetime
    created_by_id: uuid.UUID | None = None


class AvailabilityRead(BaseModel):
    """Answer to "how much can I promise?" - never served from cache (section 34)."""

    item_id: uuid.UUID
    warehouse_id: uuid.UUID | None = None
    available_quantity: Decimal


# -----------------------------------------------------------------------------
# Batches and serials
# -----------------------------------------------------------------------------


class StockBatchCreate(BaseModel):
    item_id: uuid.UUID
    batch_number: Annotated[str, Field(min_length=1, max_length=64)]
    supplier_batch_number: str | None = Field(default=None, max_length=64)
    vendor_id: uuid.UUID | None = None
    manufactured_on: date | None = None
    expires_on: date | None = None
    received_on: date | None = None
    initial_quantity: NonNegativeQuantity = Decimal("0")
    unit_cost: UnitCost = Decimal("0")
    notes: str | None = None

    @field_validator("batch_number")
    @classmethod
    def _normalise(cls, value: str) -> str:
        cleaned = " ".join(value.split()).upper()
        if not cleaned:
            raise ValueError("Batch number cannot be blank.")
        return cleaned

    @model_validator(mode="after")
    def _dates_ordered(self) -> StockBatchCreate:
        if self.expires_on and self.manufactured_on and self.expires_on < self.manufactured_on:
            raise ValueError("Expiry date cannot be before the manufacturing date.")
        return self


class StockBatchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    item_id: uuid.UUID
    batch_number: str
    supplier_batch_number: str | None
    vendor_id: uuid.UUID | None
    manufactured_on: date | None
    expires_on: date | None
    received_on: date
    initial_quantity: Decimal
    unit_cost: Decimal
    status: BatchStatus
    notes: str | None
    created_at: datetime


class StockSerialCreate(BaseModel):
    item_id: uuid.UUID
    serial_number: Annotated[str, Field(min_length=1, max_length=64)]
    batch_id: uuid.UUID | None = None
    warehouse_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    received_on: date | None = None
    unit_cost: UnitCost = Decimal("0")
    notes: str | None = None

    @field_validator("serial_number")
    @classmethod
    def _normalise(cls, value: str) -> str:
        cleaned = "".join(value.split()).upper()
        if not cleaned:
            raise ValueError("Serial number cannot be blank.")
        return cleaned


class StockSerialRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    item_id: uuid.UUID
    serial_number: str
    batch_id: uuid.UUID | None
    warehouse_id: uuid.UUID | None
    location_id: uuid.UUID | None
    status: SerialStatus
    received_on: date | None
    unit_cost: Decimal
    notes: str | None
    created_at: datetime


# -----------------------------------------------------------------------------
# Stock transfer
# -----------------------------------------------------------------------------


class StockTransferLineCreate(BaseModel):
    item_id: uuid.UUID
    quantity: PositiveQuantity
    batch_id: uuid.UUID | None = None
    from_location_id: uuid.UUID | None = None
    to_location_id: uuid.UUID | None = None
    notes: str | None = None


class StockTransferCreate(BaseModel):
    from_warehouse_id: uuid.UUID
    to_warehouse_id: uuid.UUID
    reason: Reason
    transfer_date: date | None = None
    expected_date: date | None = None
    notes: str | None = None
    lines: Annotated[list[StockTransferLineCreate], Field(min_length=1)]

    @model_validator(mode="after")
    def _validate(self) -> StockTransferCreate:
        if self.from_warehouse_id == self.to_warehouse_id:
            raise ValueError("Source and destination warehouses must differ.")
        if len(self.lines) > MAX_DOCUMENT_LINES:
            raise ValueError(f"A transfer may not exceed {MAX_DOCUMENT_LINES} lines.")
        # Two lines for the same position would take the same row lock twice and
        # make the dispatched quantity ambiguous; merge them before submitting.
        seen = {(line.item_id, line.batch_id, line.from_location_id) for line in self.lines}
        if len(seen) != len(self.lines):
            raise ValueError("The same item, batch and source location appears on two lines.")
        return self


class StockTransferUpdate(BaseModel):
    """Header edits, allowed only while the transfer is still a draft."""

    model_config = ConfigDict(extra="forbid")

    reason: Reason | None = None
    expected_date: date | None = None
    notes: str | None = None


class StockTransferLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    item_code: str | None = None
    item_name: str | None = None
    batch_id: uuid.UUID | None
    from_location_id: uuid.UUID | None
    to_location_id: uuid.UUID | None
    quantity: Decimal
    dispatched_quantity: Decimal
    received_quantity: Decimal
    unit_cost: Decimal
    notes: str | None


class StockTransferRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    status: TransferStatus
    from_warehouse_id: uuid.UUID
    from_warehouse_code: str | None = None
    to_warehouse_id: uuid.UUID
    to_warehouse_code: str | None = None
    transfer_date: date
    expected_date: date | None
    dispatched_at: datetime | None
    received_at: datetime | None
    approved_by_id: uuid.UUID | None
    approved_at: datetime | None
    reason: str
    notes: str | None
    line_count: int
    total_quantity: Decimal
    created_at: datetime
    updated_at: datetime
    lines: list[StockTransferLineRead] = Field(default_factory=list)


class TransferFilters(BaseModel):
    status: TransferStatus | None = None
    from_warehouse_id: uuid.UUID | None = None
    to_warehouse_id: uuid.UUID | None = None
    date_from: date | None = Field(default=None, description="ISO date lower bound")
    date_to: date | None = Field(default=None, description="ISO date upper bound")
    search: str | None = Field(default=None, max_length=64)


# -----------------------------------------------------------------------------
# Stock adjustment
# -----------------------------------------------------------------------------


class StockAdjustmentLineCreate(BaseModel):
    item_id: uuid.UUID
    #: Signed: positive writes stock up, negative writes it down.
    quantity: NonZeroQuantity
    location_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None
    #: Valuation for a write-up. Ignored on a write-down, which is always valued
    #: at what the position's FIFO cost layers cost so the balance cannot be drained
    #: of value by quoting a low price.
    unit_cost: UnitCost | None = None
    notes: str | None = None

    @field_validator("quantity")
    @classmethod
    def _non_zero(cls, value: Decimal) -> Decimal:
        if value == 0:
            raise ValueError("An adjustment line of zero changes nothing.")
        return value


class StockAdjustmentCreate(BaseModel):
    warehouse_id: uuid.UUID
    reason_code: AdjustmentReason
    reason: Reason
    adjustment_date: date | None = None
    notes: str | None = None
    lines: Annotated[list[StockAdjustmentLineCreate], Field(min_length=1)]

    @model_validator(mode="after")
    def _validate(self) -> StockAdjustmentCreate:
        if len(self.lines) > MAX_DOCUMENT_LINES:
            raise ValueError(f"An adjustment may not exceed {MAX_DOCUMENT_LINES} lines.")
        seen = {(line.item_id, line.batch_id, line.location_id) for line in self.lines}
        if len(seen) != len(self.lines):
            raise ValueError("The same item, batch and location appears on two lines.")
        return self


class StockAdjustmentLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    item_code: str | None = None
    item_name: str | None = None
    location_id: uuid.UUID | None
    batch_id: uuid.UUID | None
    quantity: Decimal
    unit_cost: Decimal
    total_cost: Decimal
    system_quantity: Decimal | None
    notes: str | None


class StockAdjustmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    status: AdjustmentStatus
    warehouse_id: uuid.UUID
    warehouse_code: str | None = None
    adjustment_date: date
    reason_code: AdjustmentReason
    reason: str
    notes: str | None
    total_value: Decimal
    requires_approval: bool
    approved_by_id: uuid.UUID | None
    approved_at: datetime | None
    posted_at: datetime | None
    decision_note: str | None
    line_count: int
    created_at: datetime
    updated_at: datetime
    created_by_id: uuid.UUID | None = None
    lines: list[StockAdjustmentLineRead] = Field(default_factory=list)


class AdjustmentFilters(BaseModel):
    status: AdjustmentStatus | None = None
    warehouse_id: uuid.UUID | None = None
    reason_code: AdjustmentReason | None = None
    date_from: date | None = Field(default=None, description="ISO date lower bound")
    date_to: date | None = Field(default=None, description="ISO date upper bound")
    search: str | None = Field(default=None, max_length=64)


class DecisionRequest(BaseModel):
    """Approval or rejection note, recorded on the document and in the audit log."""

    note: str | None = Field(default=None, max_length=255)

    _clean = field_validator("note")(clean_text)


class ReasonRequest(BaseModel):
    """A mandatory reason for an irreversible action (section 83)."""

    reason: Reason


# -----------------------------------------------------------------------------
# Physical count
# -----------------------------------------------------------------------------


class PhysicalCountCreate(BaseModel):
    """Creates the sheet and snapshots system quantities for its scope."""

    warehouse_id: uuid.UUID
    reason: Reason
    location_id: uuid.UUID | None = None
    count_date: date | None = None
    #: Narrows the sheet to specific items. Empty means every position in scope
    #: that currently carries stock or has carried it.
    item_ids: list[uuid.UUID] = Field(default_factory=list)
    #: Include positions that are currently zero. Off by default: a full-zero
    #: sheet for a large warehouse is thousands of lines nobody counts.
    include_zero_positions: bool = False
    notes: str | None = None


class PhysicalCountEntry(BaseModel):
    line_id: uuid.UUID
    counted_quantity: NonNegativeQuantity
    notes: str | None = None


class PhysicalCountEntryRequest(BaseModel):
    entries: Annotated[list[PhysicalCountEntry], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique_lines(self) -> PhysicalCountEntryRequest:
        if len({entry.line_id for entry in self.entries}) != len(self.entries):
            raise ValueError("The same line was submitted twice in one request.")
        if len(self.entries) > MAX_DOCUMENT_LINES:
            raise ValueError(f"At most {MAX_DOCUMENT_LINES} lines may be entered per request.")
        return self


class PhysicalCountLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    item_code: str | None = None
    item_name: str | None = None
    location_id: uuid.UUID | None
    batch_id: uuid.UUID | None
    system_quantity: Decimal
    counted_quantity: Decimal | None
    variance_quantity: Decimal | None
    unit_cost: Decimal
    variance_value: Decimal | None
    counted_at: datetime | None
    counted_by_id: uuid.UUID | None
    notes: str | None


class PhysicalCountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    status: PhysicalCountStatus
    warehouse_id: uuid.UUID
    warehouse_code: str | None = None
    location_id: uuid.UUID | None
    count_date: date
    started_at: datetime | None
    counted_at: datetime | None
    confirmed_at: datetime | None
    confirmed_by_id: uuid.UUID | None
    line_count: int
    variance_line_count: int
    variance_value: Decimal
    reason: str
    notes: str | None
    created_at: datetime
    updated_at: datetime


class PhysicalCountFilters(BaseModel):
    status: PhysicalCountStatus | None = None
    warehouse_id: uuid.UUID | None = None
    date_from: date | None = Field(default=None, description="ISO date lower bound")
    date_to: date | None = Field(default=None, description="ISO date upper bound")
    search: str | None = Field(default=None, max_length=64)


__all__ = [
    "MAX_DOCUMENT_LINES",
    "AdjustmentFilters",
    "AvailabilityRead",
    "BalanceFilters",
    "BalanceRead",
    "DecisionRequest",
    "ItemStockSummary",
    "LedgerEntryRead",
    "LedgerFilters",
    "PhysicalCountCreate",
    "PhysicalCountEntry",
    "PhysicalCountEntryRequest",
    "PhysicalCountFilters",
    "PhysicalCountLineRead",
    "PhysicalCountRead",
    "ReasonRequest",
    "StockAdjustmentCreate",
    "StockAdjustmentLineCreate",
    "StockAdjustmentLineRead",
    "StockAdjustmentRead",
    "StockBatchCreate",
    "StockBatchRead",
    "StockSerialCreate",
    "StockSerialRead",
    "StockTransferCreate",
    "StockTransferLineCreate",
    "StockTransferLineRead",
    "StockTransferRead",
    "StockTransferUpdate",
    "TransferFilters",
    "WarehouseStockRow",
]
