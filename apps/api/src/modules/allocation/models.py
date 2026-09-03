"""Reservation and reallocation tables - who a quantity of stock is promised to.

Master rules section 13, database contract section 4.5. Two tables, and the
distinction between them is the whole point of the module:

``stock_reservations``
    A claim on stock that is *physically present*. A reservation never moves
    on-hand quantity - it moves ``inventory_balances.reserved_quantity``, which
    the generated ``available_quantity`` column subtracts. So the stock is still
    in the warehouse, still owned, still countable, and simply not promisable to
    anybody else.

``reallocation_requests``
    The record of taking a promise away from one demand and giving it to
    another. It exists as a table rather than as a direct update because a
    reallocation is a business decision somebody must be accountable for: who
    asked, who approved, what the losing demand looks like afterwards. Master
    rules section 13 forbids implementing reallocation as a stock update, and a
    schema with no request row is exactly how that rule gets broken.

Why the reservation row is not the source of truth for availability
-------------------------------------------------------------------
``reserved_quantity`` on the balance is. Summing open reservations on a request
path would be the same mistake as summing the ledger to get on-hand: correct
once, then progressively slower, and impossible to lock. The balance row is the
thing ``SELECT ... FOR UPDATE`` arbitrates, so it is the thing that decides
whether a reservation may be created. These rows explain *why* the balance says
what it says, and are what a reallocation reasons over.

The check constraints are the floor
-----------------------------------
``consumed + released <= quantity`` is enforced by the database, not by the
service. A reservation that has been drawn down more than it ever promised is a
corruption that would silently unbalance ``reserved_quantity`` against every
demand document, and no amount of service-level care survives a console session.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models import TenantEntity
from src.core.types import GUID, Quantity
from src.modules.inventory.models import StatusEnum


class DemandType(StrEnum):
    """What kind of document is asking for the stock.

    A closed vocabulary, and deliberately *not* a foreign key: the demand lives
    in six different tables and a polymorphic FK is not expressible in SQL. The
    pair ``(demand_type, demand_id)`` is the reference, and the index on it is
    what makes "release everything this cancelled order was holding" one query.
    """

    SALES_ORDER = "SALES_ORDER"
    PRODUCTION_ORDER = "PRODUCTION_ORDER"
    ASSEMBLY_ORDER = "ASSEMBLY_ORDER"
    JOB_WORK_ORDER = "JOB_WORK_ORDER"
    SPARE = "SPARE"
    TRANSFER = "TRANSFER"


class ReservationType(StrEnum):
    """How firm the promise is.

    ``HARD`` is a commitment somebody has made to a customer or to a shop floor
    that is already tooled up for it; it can only be taken away through an
    approved :class:`ReallocationRequest`. ``SOFT`` is a planning placeholder -
    MRP's opinion about what a future order will need - and exists precisely so
    that a firm order can outrank it.
    """

    HARD = "HARD"
    SOFT = "SOFT"


class ReservationStatus(StrEnum):
    """Lifecycle of one reservation.

    ``PARTIALLY_CONSUMED`` is a state of its own rather than an inference from
    the quantities because a partial issue is the normal case in manufacturing -
    a production order draws its components over several days - and the status
    index is what keeps the "still holding stock" query cheap.
    """

    ACTIVE = "ACTIVE"
    PARTIALLY_CONSUMED = "PARTIALLY_CONSUMED"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


#: Priority given to a reservation whose caller did not state one. Mid-scale on
#: purpose: a planner must be able to raise a rush order above the default *and*
#: drop a speculative one below it without renumbering everything already open.
#: Also the column's server default, so a row inserted by a migration or a
#: back-fill lands in the same place as one inserted by the service.
DEFAULT_RESERVATION_PRIORITY = 100

#: Statuses in which a reservation is still holding ``reserved_quantity`` on the
#: balance. Every query that asks "what is still promised?" filters on exactly
#: this set, and the partial indexes below are built for it.
OPEN_RESERVATION_STATUSES: frozenset[ReservationStatus] = frozenset(
    {ReservationStatus.ACTIVE, ReservationStatus.PARTIALLY_CONSUMED}
)

#: SQL spelling of the same set, for the partial index predicates. Written once
#: so the index and the Python filter cannot drift apart - a mismatch would make
#: PostgreSQL silently decline to use the index rather than fail.
OPEN_STATUS_SQL = "status IN ('ACTIVE', 'PARTIALLY_CONSUMED')"


class ReallocationStatus(StrEnum):
    """Lifecycle of a request to move a promise from one demand to another.

    ``APPROVED`` and ``EXECUTED`` are separate states even though the service
    performs both inside one transaction: the approval is the human decision and
    the execution is its effect, and an audit that cannot distinguish them
    cannot answer "was this signed off before the stock moved?".
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"


class StockReservation(TenantEntity):
    """A quantity of one item at one position promised to one demand line."""

    __tablename__ = "stock_reservations"

    # eager_defaults makes SQLAlchemy fetch the generated remaining_quantity with
    # INSERT/UPDATE ... RETURNING. Without it the column is expired after every
    # write and the next read - typically serialising the response - becomes an
    # async lazy load that raises MissingGreenlet. Same trap as a server-side
    # onupdate; see CLAUDE.md and InventoryBalance.
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012 - SQLAlchemy API

    # --- what is promised ---------------------------------------------------
    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    # The position is pinned to the same grain the balance uses, so releasing a
    # reservation always decrements the same row that creating it incremented.
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("storage_locations.id", ondelete="RESTRICT"), nullable=True
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("stock_batches.id", ondelete="RESTRICT"), nullable=True
    )

    # --- who it is promised to ----------------------------------------------
    demand_type: Mapped[DemandType] = mapped_column(StatusEnum(DemandType), nullable=False)
    #: Not a foreign key - the demand is in one of six tables. See DemandType.
    demand_id: Mapped[uuid.UUID] = mapped_column(GUID, nullable=False)
    demand_line_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    #: Denormalised document number so an allocation screen, a reallocation
    #: approval and an audit entry can name the losing demand without joining
    #: six possible tables - several of which the reallocation approver has no
    #: permission to read.
    demand_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    reservation_type: Mapped[ReservationType] = mapped_column(
        StatusEnum(ReservationType), nullable=False, default=ReservationType.HARD
    )

    # --- quantities ---------------------------------------------------------
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    consumed_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    released_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: GENERATED ALWAYS AS ... STORED: how much of the promise is still standing.
    #: Generated rather than maintained so it cannot disagree with its inputs,
    #: and indexable so "what could a reallocation take?" is an index scan
    #: rather than an arithmetic filter over every historical reservation.
    #: Application code never assigns it.
    remaining_quantity: Mapped[Decimal] = mapped_column(
        Quantity,
        Computed("quantity - consumed_quantity - released_quantity", persisted=True),
        nullable=False,
    )

    status: Mapped[ReservationStatus] = mapped_column(
        StatusEnum(ReservationStatus),
        nullable=False,
        default=ReservationStatus.ACTIVE,
        server_default="ACTIVE",
    )

    # --- ranking inputs -----------------------------------------------------
    #: Lower is more urgent, matching how planners already speak ("priority 1").
    #: An integer rather than an enum because priority is business data that
    #: changes per order, and a reallocation compares two of them numerically.
    priority: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_RESERVATION_PRIORITY,
        server_default=str(DEFAULT_RESERVATION_PRIORITY),
    )
    #: When the demand actually needs the material. Nullable because a spare-part
    #: or safety-stock reservation legitimately has no date; the ranking treats
    #: a missing date as "no urgency information", never as "needed today".
    required_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: After this moment the reservation is stale and the housekeeping job may
    #: release it. Nullable: a reservation against a live order does not expire.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    reason: Mapped[str] = mapped_column(String(255), nullable=False)

    # --- release trail ------------------------------------------------------
    # Kept on the row as well as in the audit log: an allocation screen has to
    # show "released by X because Y" without querying the audit table for every
    # line, and the audit entry remains the authoritative history of *each*
    # partial release.
    released_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    release_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        # --- the correctness floor ------------------------------------------
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("consumed_quantity >= 0", name="consumed_non_negative"),
        CheckConstraint("released_quantity >= 0", name="released_non_negative"),
        # Database contract: a reservation can never be drawn down past what it
        # promised. Written against the base columns rather than the generated
        # one so it is checked on every INSERT and UPDATE regardless of
        # generation order.
        CheckConstraint(
            "consumed_quantity + released_quantity <= quantity",
            name="consumed_and_released_within_quantity",
        ),
        CheckConstraint("priority >= 0", name="priority_non_negative"),
        # --- access paths ----------------------------------------------------
        # "What is promised for this item here?" - read on every reserve, every
        # availability question and every reallocation search.
        Index("ix_stock_reservations_item", "company_id", "item_id", "warehouse_id", "status"),
        # "Release everything this cancelled order was holding."
        Index("ix_stock_reservations_demand", "company_id", "demand_type", "demand_id"),
        # The reallocation candidate search, in the order it ranks: least urgent
        # first, then latest required date. Partial, because a reallocation only
        # ever looks at reservations that still hold stock - on a mature system
        # that is a small fraction of the table.
        Index(
            "ix_stock_reservations_candidates",
            "company_id",
            "item_id",
            "warehouse_id",
            "priority",
            "required_date",
            postgresql_where=text(OPEN_STATUS_SQL),
        ),
        # The housekeeping sweep that releases stale reservations. Partial on the
        # same predicate plus a non-null expiry, so the job scans only rows that
        # can actually expire.
        Index(
            "ix_stock_reservations_expiring",
            "company_id",
            "expires_at",
            postgresql_where=text(f"{OPEN_STATUS_SQL} AND expires_at IS NOT NULL"),
        ),
    )


class ReallocationRequest(TenantEntity):
    """A request to take stock promised to one demand and give it to another.

    The row is the control. Master rules section 13 requires that a HARD
    reservation is never taken without an approval, and an approval that is not
    a persisted, queryable decision is not an approval - it is a log line
    somebody can lose.
    """

    __tablename__ = "reallocation_requests"

    #: The donor. RESTRICT rather than CASCADE: a reservation that a request
    #: refers to cannot be deleted out from under the decision record.
    from_reservation_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("stock_reservations.id", ondelete="RESTRICT"), nullable=False
    )

    # The recipient demand. Same polymorphic-reference reasoning as
    # StockReservation: the document lives in one of six tables.
    to_demand_type: Mapped[DemandType] = mapped_column(StatusEnum(DemandType), nullable=False)
    to_demand_id: Mapped[uuid.UUID] = mapped_column(GUID, nullable=False)
    to_demand_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Copied from the donor rather than joined: the approval queue lists dozens
    # of requests and every one of them shows what item, where.
    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)

    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[ReallocationStatus] = mapped_column(
        StatusEnum(ReallocationStatus),
        nullable=False,
        default=ReallocationStatus.PENDING,
        server_default="PENDING",
    )

    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: What the losing demand looks like if this is approved - captured when the
    #: request is raised and re-stamped with the actual outcome when it executes.
    #: Stored as JSONB rather than recomputed at approval time because the
    #: approver must see the impact that was true when the request was made, and
    #: because the donor's numbers keep moving while the request sits in a queue.
    impact_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        # The approval queue - the screen this table exists for. Partial so it
        # stays small no matter how many decided requests accumulate.
        Index(
            "ix_reallocation_requests_pending",
            "company_id",
            "created_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index("ix_reallocation_requests_status", "company_id", "status", "created_at"),
        # "Has anybody asked for this reservation?" - checked before suggesting a
        # donor, so the same reservation is not queued up twice over.
        Index("ix_reallocation_requests_donor", "company_id", "from_reservation_id", "status"),
        Index("ix_reallocation_requests_item", "company_id", "item_id", "warehouse_id"),
    )


__all__ = [
    "DEFAULT_RESERVATION_PRIORITY",
    "OPEN_RESERVATION_STATUSES",
    "OPEN_STATUS_SQL",
    "DemandType",
    "ReallocationRequest",
    "ReallocationStatus",
    "ReservationStatus",
    "ReservationType",
    "StockReservation",
]
