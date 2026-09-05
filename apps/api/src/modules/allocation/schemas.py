"""Request and response schemas for the reservation and reallocation engine.

Conventions, all of them load-bearing:

* **Quantities are ``Decimal``**, never float (section 84). A reservation that
  drifted by 1e-16 would eventually refuse a release for "more than remains".
* **No schema carries ``company_id``.** It comes from the authenticated
  principal's tenant context (section 40).
* **No schema carries a balance quantity.** A client never states how much stock
  exists; the service re-reads that under a row lock and would ignore anything
  the request claimed.
* **A release always carries a reason.** Section 83 requires one for every stock
  event, and "the user clicked release" is not a reason. It is a required field
  rather than an optional note precisely so the API cannot be used without one.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.common.codes import clean_text
from src.modules.allocation.models import (
    DEFAULT_RESERVATION_PRIORITY,
    DemandType,
    ReallocationStatus,
    ReservationStatus,
    ReservationType,
)

Reason = Annotated[str, Field(min_length=3, max_length=255)]
PositiveQuantity = Annotated[Decimal, Field(gt=0, le=Decimal("99999999999999"))]
NonNegativeQuantity = Annotated[Decimal, Field(ge=0, le=Decimal("99999999999999"))]
#: Bounded so a client cannot express a priority the ranking cannot compare
#: meaningfully, and so the column's integer range is never the limiting factor.
Priority = Annotated[int, Field(ge=0, le=9999)]


# -----------------------------------------------------------------------------
# Reservations
# -----------------------------------------------------------------------------


class ReservationFilters(BaseModel):
    """Filters for the reservation list."""

    item_id: uuid.UUID | None = None
    warehouse_id: uuid.UUID | None = None
    demand_type: DemandType | None = None
    demand_id: uuid.UUID | None = None
    status: ReservationStatus | None = None
    reservation_type: ReservationType | None = None
    #: Convenience for the allocation screen: everything still holding stock,
    #: without the caller having to know which two statuses that means.
    open_only: bool = False


class ReservationCreate(BaseModel):
    """Ask for a quantity of stock to be held for a demand line."""

    item_id: uuid.UUID
    warehouse_id: uuid.UUID
    quantity: PositiveQuantity
    demand_type: DemandType
    demand_id: uuid.UUID
    demand_line_id: uuid.UUID | None = None
    demand_code: str | None = Field(default=None, max_length=64)
    # HARD by default: the conservative reading. A reservation created without
    # stating its firmness is treated as a real commitment, so it cannot be
    # taken by a reallocation without somebody approving the loss.
    reservation_type: ReservationType = ReservationType.HARD
    priority: Priority = DEFAULT_RESERVATION_PRIORITY
    required_date: date | None = None
    expires_at: datetime | None = None
    reason: Reason
    # Pinning a reservation to a location or batch is optional, but when the
    # caller does pin it the release and the consume must use the same position,
    # which is why it is stored rather than resolved again later.
    location_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None

    @field_validator("demand_code")
    @classmethod
    def _tidy_code(cls, value: str | None) -> str | None:
        return clean_text(value)


class ReservationRead(BaseModel):
    """One reservation, with the labels a screen needs to render it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    item_id: uuid.UUID
    item_code: str | None = None
    item_name: str | None = None
    warehouse_id: uuid.UUID
    warehouse_code: str | None = None
    location_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None

    demand_type: DemandType
    demand_id: uuid.UUID
    demand_line_id: uuid.UUID | None = None
    demand_code: str | None = None

    reservation_type: ReservationType
    status: ReservationStatus
    quantity: Decimal
    consumed_quantity: Decimal
    released_quantity: Decimal
    #: Computed in the service from the three columns above rather than read off
    #: the generated column, so the value is right immediately after a write
    #: without depending on when the row is refreshed.
    remaining_quantity: Decimal

    priority: int
    required_date: date | None = None
    expires_at: datetime | None = None
    reason: str

    released_by_id: uuid.UUID | None = None
    released_at: datetime | None = None
    release_reason: str | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None
    created_by_id: uuid.UUID | None = None


class ReleaseRequest(BaseModel):
    """Give back part or all of a reservation.

    ``quantity`` omitted means "release whatever is left", which is what
    cancelling a demand document does. A number releases exactly that much and
    leaves the rest standing.
    """

    reason: Reason
    quantity: PositiveQuantity | None = None


class AvailableToPromiseRead(BaseModel):
    """What can still be promised for one item.

    Read from the balance table on every call and never cached (section 34): a
    stale "yes, we have it" turns into a delivery date the company cannot meet.
    """

    item_id: uuid.UUID
    warehouse_id: uuid.UUID | None = None
    available_quantity: Decimal
    as_of: datetime


# -----------------------------------------------------------------------------
# Reallocation
# -----------------------------------------------------------------------------


class ReallocationCandidateRead(BaseModel):
    """One reservation a reallocation could take from, and why it ranked here.

    ``reasons`` is not decoration. The approver is being asked to disappoint a
    demand, and a ranked list with no explanation is a list nobody trusts.
    """

    reservation_id: uuid.UUID
    item_id: uuid.UUID
    warehouse_id: uuid.UUID
    demand_type: DemandType
    demand_id: uuid.UUID
    demand_code: str | None = None
    reservation_type: ReservationType
    priority: int
    required_date: date | None = None

    quantity: Decimal
    consumed_quantity: Decimal
    remaining_quantity: Decimal
    #: How much of this candidate the request would actually take, given what
    #: earlier candidates in the list already cover.
    takeable_quantity: Decimal

    requires_approval: bool
    score: Decimal
    reasons: list[str] = Field(default_factory=list)


class ReallocationRequestCreate(BaseModel):
    """Ask for stock currently promised elsewhere.

    ``from_reservation_id`` is optional: leave it out and the engine ranks the
    donors and picks the best one, which is the normal path from a shortage
    screen. Name one explicitly when a planner has already decided which demand
    should give way.
    """

    item_id: uuid.UUID
    warehouse_id: uuid.UUID
    quantity: PositiveQuantity
    to_demand_type: DemandType
    to_demand_id: uuid.UUID
    to_demand_code: str | None = Field(default=None, max_length=64)
    #: The requesting demand's own urgency, compared against each donor's. A
    #: request that is not more urgent than the stock it wants has no candidates.
    priority: Priority = DEFAULT_RESERVATION_PRIORITY
    required_date: date | None = None
    reason: Reason
    from_reservation_id: uuid.UUID | None = None

    @field_validator("to_demand_code")
    @classmethod
    def _tidy_code(cls, value: str | None) -> str | None:
        return clean_text(value)


class ReallocationFilters(BaseModel):
    status: ReallocationStatus | None = None
    item_id: uuid.UUID | None = None
    warehouse_id: uuid.UUID | None = None
    to_demand_id: uuid.UUID | None = None


class ReallocationDecision(BaseModel):
    """Approve or reject a request. The note is what the requester will read."""

    decision_note: str | None = Field(default=None, max_length=1000)

    @field_validator("decision_note")
    @classmethod
    def _tidy_note(cls, value: str | None) -> str | None:
        return clean_text(value)


class ReallocationRequestRead(BaseModel):
    """A reallocation request and everything a decision needs."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: ReallocationStatus

    from_reservation_id: uuid.UUID
    from_demand_type: DemandType | None = None
    from_demand_id: uuid.UUID | None = None
    from_demand_code: str | None = None
    from_reservation_type: ReservationType | None = None

    to_demand_type: DemandType
    to_demand_id: uuid.UUID
    to_demand_code: str | None = None

    item_id: uuid.UUID
    item_code: str | None = None
    item_name: str | None = None
    warehouse_id: uuid.UUID
    warehouse_code: str | None = None
    quantity: Decimal
    reason: str

    requested_by_id: uuid.UUID | None = None
    approved_by_id: uuid.UUID | None = None
    approved_at: datetime | None = None
    decision_note: str | None = None
    impact_summary: dict[str, Any] | None = None

    #: The reservation created for the recipient once the request executes.
    #: Present only on an EXECUTED request.
    to_reservation_id: uuid.UUID | None = None

    #: The alternatives the engine considered when the request was raised.
    #: Returned on creation so the approver sees what else was on the table, not
    #: only the donor somebody happened to pick.
    candidates: list[ReallocationCandidateRead] = Field(default_factory=list)

    created_at: datetime | None = None
    updated_at: datetime | None = None


__all__ = [
    "AvailableToPromiseRead",
    "NonNegativeQuantity",
    "PositiveQuantity",
    "Priority",
    "ReallocationCandidateRead",
    "ReallocationDecision",
    "ReallocationFilters",
    "ReallocationRequestCreate",
    "ReallocationRequestRead",
    "Reason",
    "ReleaseRequest",
    "ReservationCreate",
    "ReservationFilters",
    "ReservationRead",
]
