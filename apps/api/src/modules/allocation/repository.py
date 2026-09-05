"""Queries and persistence for the reservation and reallocation engine.

Three things in this file are deliberate rather than incidental.

**Row locks are taken here, in a fixed order.** ``lock_reservations`` orders by
primary key for the same reason
:meth:`~src.modules.inventory.repository.InventoryRepository.lock_balances`
does: a reallocation touches two reservations at once, and two reallocations
running in opposite directions must queue rather than deadlock. The service
additionally always takes the *balance* lock before any reservation lock, so the
two lock classes are always acquired in the same sequence.

**The candidate search returns a narrow projection, not ORM rows.**
:class:`ReservationFacts` is a frozen dataclass carrying exactly the columns the
ranking reads. That keeps the ranking a pure function that unit tests can drive
without a database, and it keeps a wide row out of a query that runs on every
shortage screen (section 33).

**The candidate pool is bounded.** A busy item can have thousands of open
reservations; ranking all of them in Python to show a list of ten would be a
slow query pretending to be a fast one. The pool is capped and ordered by the
policy's dominant term, so the cap can only ever discard rows that were already
losing on priority.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, nulls_last, select
from sqlalchemy.orm import InstrumentedAttribute

from src.common.repository import BaseRepository
from src.core.pagination import SortParams
from src.modules.allocation.models import (
    OPEN_RESERVATION_STATUSES,
    DemandType,
    ReallocationRequest,
    ReallocationStatus,
    ReservationStatus,
    ReservationType,
    StockReservation,
)
from src.modules.allocation.schemas import ReallocationFilters, ReservationFilters

#: How many open reservations the candidate search will rank. Ordered by the
#: policy's dominant term first, so the cap can only discard rows that already
#: lost on priority - never a row that would have ranked first.
CANDIDATE_POOL_LIMIT = 200

#: Statuses of a reallocation request that still have a claim on the donor. A
#: PENDING request has not taken the stock yet, but promising the same
#: reservation to two requesters would produce an approval that cannot execute.
CLAIMING_REALLOCATION_STATUSES: frozenset[ReallocationStatus] = frozenset(
    {ReallocationStatus.PENDING, ReallocationStatus.APPROVED}
)


@dataclass(frozen=True, slots=True)
class ReservationFacts:
    """The columns the reallocation ranking reads, and nothing else.

    A plain dataclass rather than the ORM row so the ranking is pure: it can be
    exercised in a unit test with a handful of literals, and no ranking rule can
    quietly grow a database round trip.
    """

    id: uuid.UUID
    item_id: uuid.UUID
    warehouse_id: uuid.UUID
    location_id: uuid.UUID | None
    batch_id: uuid.UUID | None
    demand_type: DemandType
    demand_id: uuid.UUID
    demand_code: str | None
    reservation_type: ReservationType
    status: ReservationStatus
    priority: int
    required_date: date | None
    quantity: Decimal
    consumed_quantity: Decimal
    released_quantity: Decimal
    remaining_quantity: Decimal
    #: Who created the reservation. The notification on a reallocation goes to
    #: this person - see the service for why the demand document's owner is not
    #: reachable from here.
    created_by_id: uuid.UUID | None
    #: Quantity of this reservation already claimed by an undecided or approved
    #: reallocation request. Subtracted from what a new request may ask for.
    claimed_quantity: Decimal = Decimal("0")

    def with_claim(self, claimed: Decimal) -> ReservationFacts:
        """Copy carrying the outstanding reallocation claim against this donor."""
        return replace(self, claimed_quantity=claimed)

    @property
    def unclaimed_quantity(self) -> Decimal:
        """What a new request could still ask this donor for."""
        return max(Decimal("0"), self.remaining_quantity - self.claimed_quantity)


#: The projection behind :class:`ReservationFacts`, written once so the column
#: list and the dataclass field order cannot drift.
_FACT_COLUMNS = (
    StockReservation.id,
    StockReservation.item_id,
    StockReservation.warehouse_id,
    StockReservation.location_id,
    StockReservation.batch_id,
    StockReservation.demand_type,
    StockReservation.demand_id,
    StockReservation.demand_code,
    StockReservation.reservation_type,
    StockReservation.status,
    StockReservation.priority,
    StockReservation.required_date,
    StockReservation.quantity,
    StockReservation.consumed_quantity,
    StockReservation.released_quantity,
    StockReservation.remaining_quantity,
    StockReservation.created_by_id,
)


class StockReservationRepository(BaseRepository[StockReservation]):
    """Reservation reads, the candidate search, and the reservation row lock."""

    model = StockReservation
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "created_at": StockReservation.created_at,
        "priority": StockReservation.priority,
        "required_date": StockReservation.required_date,
        "quantity": StockReservation.quantity,
        "remaining_quantity": StockReservation.remaining_quantity,
        "expires_at": StockReservation.expires_at,
    }

    # --- locking ------------------------------------------------------------

    async def lock_reservations(
        self, reservation_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, StockReservation]:
        """Lock reservation rows for update and re-read them.

        Ordered by primary key so a reallocation taking a donor and a recipient
        lock cannot deadlock against one running the other way.

        ``populate_existing`` is not optional: without it SQLAlchemy hands back
        whatever version of the row is already in the identity map, and the
        re-read after the lock silently becomes a no-op - which is precisely the
        bug the lock exists to prevent.
        """
        if not reservation_ids:
            return {}
        stmt = (
            select(StockReservation)
            .where(
                StockReservation.company_id == self.tenant.company_id,
                StockReservation.id.in_(list(reservation_ids)),
            )
            .order_by(StockReservation.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.id: row for row in rows}

    # --- reads --------------------------------------------------------------

    def build_list_stmt(
        self, filters: ReservationFilters, sort: SortParams | None = None
    ) -> Select[tuple[StockReservation]]:
        stmt = self._scoped()

        if filters.item_id is not None:
            stmt = stmt.where(StockReservation.item_id == filters.item_id)
        if filters.warehouse_id is not None:
            stmt = stmt.where(StockReservation.warehouse_id == filters.warehouse_id)
        if filters.demand_type is not None:
            stmt = stmt.where(StockReservation.demand_type == filters.demand_type)
        if filters.demand_id is not None:
            stmt = stmt.where(StockReservation.demand_id == filters.demand_id)
        if filters.status is not None:
            stmt = stmt.where(StockReservation.status == filters.status)
        if filters.reservation_type is not None:
            stmt = stmt.where(StockReservation.reservation_type == filters.reservation_type)
        if filters.open_only:
            stmt = stmt.where(StockReservation.status.in_(sorted(OPEN_RESERVATION_STATUSES)))

        default_order = (StockReservation.created_at.desc(), StockReservation.id.desc())
        if sort is not None and sort.sort_by:
            return stmt.order_by(
                sort.resolve(self.sortable_fields, StockReservation.created_at.desc()),
                StockReservation.id.desc(),
            )
        return stmt.order_by(*default_order)

    async def open_for_demand(
        self, demand_type: DemandType, demand_id: uuid.UUID
    ) -> list[StockReservation]:
        """Everything this demand is still holding.

        Bounded by construction: a demand document has as many reservations as
        it has lines, and a document with an unbounded line count is rejected
        long before it reaches here.
        """
        stmt = self._scoped().where(
            StockReservation.demand_type == demand_type,
            StockReservation.demand_id == demand_id,
            StockReservation.status.in_(sorted(OPEN_RESERVATION_STATUSES)),
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def reserved_total(
        self, item_id: uuid.UUID, warehouse_id: uuid.UUID | None = None
    ) -> Decimal:
        """Sum of what open reservations still hold for one item.

        A reconciliation read, not an availability read: availability comes from
        ``inventory_balances.reserved_quantity``, which is the column the row
        lock arbitrates. This is what an operator compares it against when the
        two are suspected of disagreeing.
        """
        stmt = select(func.coalesce(func.sum(StockReservation.remaining_quantity), 0)).where(
            StockReservation.company_id == self.tenant.company_id,
            StockReservation.item_id == item_id,
            StockReservation.status.in_(sorted(OPEN_RESERVATION_STATUSES)),
        )
        if warehouse_id is not None:
            stmt = stmt.where(StockReservation.warehouse_id == warehouse_id)
        return Decimal(str((await self.session.execute(stmt)).scalar_one()))

    async def due_for_expiry(self, moment: datetime, *, limit: int) -> list[StockReservation]:
        """Open reservations whose expiry has passed, oldest first.

        Bounded by ``limit`` because the housekeeping sweep locks a balance row
        per result: an unbounded batch would hold every one of those locks while
        live orders queued behind it.
        """
        stmt = (
            self._scoped()
            .where(
                StockReservation.status.in_(sorted(OPEN_RESERVATION_STATUSES)),
                StockReservation.expires_at.is_not(None),
                StockReservation.expires_at <= moment,
            )
            .order_by(StockReservation.expires_at, StockReservation.id)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def candidate_pool(
        self,
        item_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        *,
        worse_than_priority: int,
        exclude_demand_type: DemandType | None = None,
        exclude_demand_id: uuid.UUID | None = None,
        limit: int = CANDIDATE_POOL_LIMIT,
    ) -> list[ReservationFacts]:
        """Open reservations a reallocation could conceivably take from.

        The eligibility that can be expressed in SQL is expressed in SQL - open
        status, something still remaining, a strictly less urgent priority, and
        not the requesting demand's own reservation. Everything that is a
        *policy* judgement stays in :func:`~src.modules.allocation.service.rank_candidates`
        so it can be changed without touching a query.
        """
        stmt = (
            select(*_FACT_COLUMNS)
            .where(
                StockReservation.company_id == self.tenant.company_id,
                StockReservation.item_id == item_id,
                StockReservation.warehouse_id == warehouse_id,
                StockReservation.status.in_(sorted(OPEN_RESERVATION_STATUSES)),
                StockReservation.remaining_quantity > 0,
                # Strictly worse: a demand of equal urgency has no claim on
                # another's stock, and allowing equality would let two orders
                # take turns reallocating from each other forever.
                StockReservation.priority > worse_than_priority,
            )
            # Least urgent first, then needed latest. Matches the partial index
            # (scanned backwards) and the policy's dominant weights, so the LIMIT
            # below can only drop rows that were already losing.
            .order_by(
                StockReservation.priority.desc(),
                # A donor with no required date sorts last within its priority
                # band: the ranking cannot argue it is less urgent than one that
                # states a late date, so it must not displace one.
                nulls_last(StockReservation.required_date.desc()),
                StockReservation.id,
            )
            .limit(limit)
        )
        if exclude_demand_id is not None and exclude_demand_type is not None:
            # A demand cannot reallocate from itself - it would release and
            # re-reserve the same stock and call it progress.
            stmt = stmt.where(
                ~(
                    (StockReservation.demand_type == exclude_demand_type)
                    & (StockReservation.demand_id == exclude_demand_id)
                )
            )

        rows = (await self.session.execute(stmt)).all()
        return [ReservationFacts(*row) for row in rows]


class ReallocationRequestRepository(BaseRepository[ReallocationRequest]):
    """Reallocation request reads and the request row lock."""

    model = ReallocationRequest
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "created_at": ReallocationRequest.created_at,
        "quantity": ReallocationRequest.quantity,
        "approved_at": ReallocationRequest.approved_at,
    }

    async def lock_request(self, request_id: uuid.UUID) -> ReallocationRequest | None:
        """Lock one request for update and re-read it.

        Two approvers clicking at the same moment must not both execute the same
        reallocation; the lock plus the state-machine check is what makes the
        second one fail cleanly instead of releasing the donor twice.
        """
        stmt = (
            select(ReallocationRequest)
            .where(
                ReallocationRequest.company_id == self.tenant.company_id,
                ReallocationRequest.id == request_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def build_list_stmt(
        self, filters: ReallocationFilters, sort: SortParams | None = None
    ) -> Select[tuple[ReallocationRequest]]:
        stmt = self._scoped()

        if filters.status is not None:
            stmt = stmt.where(ReallocationRequest.status == filters.status)
        if filters.item_id is not None:
            stmt = stmt.where(ReallocationRequest.item_id == filters.item_id)
        if filters.warehouse_id is not None:
            stmt = stmt.where(ReallocationRequest.warehouse_id == filters.warehouse_id)
        if filters.to_demand_id is not None:
            stmt = stmt.where(ReallocationRequest.to_demand_id == filters.to_demand_id)

        if sort is not None and sort.sort_by:
            return stmt.order_by(
                sort.resolve(self.sortable_fields, ReallocationRequest.created_at.desc()),
                ReallocationRequest.id.desc(),
            )
        # Oldest pending first: an approval queue that shows the newest request
        # at the top is a queue where the oldest one is never dealt with.
        return stmt.order_by(ReallocationRequest.created_at, ReallocationRequest.id)

    async def claimed_quantities(
        self, reservation_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, Decimal]:
        """How much of each donor is already spoken for by an open request.

        One grouped query for the whole candidate list - the alternative is a
        per-candidate count, which is the N+1 the module map warns about
        (section 32).
        """
        if not reservation_ids:
            return {}
        stmt = (
            select(
                ReallocationRequest.from_reservation_id,
                func.coalesce(func.sum(ReallocationRequest.quantity), 0),
            )
            .where(
                ReallocationRequest.company_id == self.tenant.company_id,
                ReallocationRequest.from_reservation_id.in_(list(reservation_ids)),
                ReallocationRequest.status.in_(sorted(CLAIMING_REALLOCATION_STATUSES)),
            )
            .group_by(ReallocationRequest.from_reservation_id)
        )
        rows = (await self.session.execute(stmt)).all()
        return {row[0]: Decimal(str(row[1])) for row in rows}


__all__ = [
    "CANDIDATE_POOL_LIMIT",
    "CLAIMING_REALLOCATION_STATUSES",
    "ReallocationRequestRepository",
    "ReservationFacts",
    "StockReservationRepository",
]
