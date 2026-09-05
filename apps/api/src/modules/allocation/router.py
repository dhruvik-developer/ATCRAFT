"""HTTP surface for the reservation and reallocation engine.

Handlers bind parameters, call one service method and wrap the result in the
standard envelope. Every rule, every lock, every audit write and every
notification lives behind the service (section 74).

Three deliberate shapes:

* **There is no "execute reallocation" endpoint.** Approving *is* executing -
  they are one transaction, so that an approval can never sit around as a
  decision nobody applied, and stock can never move without one. See
  :func:`~src.modules.allocation.service.reallocate`.
* **There is no endpoint that edits a reservation.** A reservation changes by
  being released, consumed or reallocated, each of which has its own control and
  its own audit entry. A general-purpose PATCH would be a way around all three.
* **Availability is not cached, at any layer.** Section 34, restated in the
  service. A stale "yes, we have it" becomes a delivery date the company cannot
  meet, and the read is a single indexed sum.

Route ordering: the literal segments are declared before any ``/{id}`` route.
FastAPI matches in declaration order, so the reverse would capture
``/available-to-promise`` as a reservation id and fail on the UUID parse.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from src.core.deps import DbSession, Tenant, require_permission
from src.core.pagination import PageParams, SortParams, page_params, sort_params
from src.core.permissions import Permission
from src.core.responses import COMMON_ERROR_RESPONSES, ApiResponse, PaginatedResponse
from src.modules.allocation.models import (
    DemandType,
    ReallocationStatus,
    ReservationStatus,
    ReservationType,
)
from src.modules.allocation.schemas import (
    AvailableToPromiseRead,
    ReallocationDecision,
    ReallocationFilters,
    ReallocationRequestCreate,
    ReallocationRequestRead,
    ReleaseRequest,
    ReservationCreate,
    ReservationFilters,
    ReservationRead,
)
from src.modules.allocation.service import AllocationService

router = APIRouter(prefix="/allocation", tags=["Allocation"], responses=COMMON_ERROR_RESPONSES)

PageDep = Annotated[PageParams, Depends(page_params)]
SortDep = Annotated[SortParams, Depends(sort_params)]


# -----------------------------------------------------------------------------
# Filter dependencies
# -----------------------------------------------------------------------------


def reservation_filters(
    item_id: Annotated[uuid.UUID | None, Query()] = None,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    demand_type: Annotated[DemandType | None, Query()] = None,
    demand_id: Annotated[uuid.UUID | None, Query()] = None,
    status_filter: Annotated[ReservationStatus | None, Query(alias="status")] = None,
    reservation_type: Annotated[ReservationType | None, Query()] = None,
    open_only: Annotated[bool, Query(description="Only reservations still holding stock")] = False,
) -> ReservationFilters:
    return ReservationFilters(
        item_id=item_id,
        warehouse_id=warehouse_id,
        demand_type=demand_type,
        demand_id=demand_id,
        status=status_filter,
        reservation_type=reservation_type,
        open_only=open_only,
    )


def reallocation_filters(
    status_filter: Annotated[ReallocationStatus | None, Query(alias="status")] = None,
    item_id: Annotated[uuid.UUID | None, Query()] = None,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    to_demand_id: Annotated[uuid.UUID | None, Query()] = None,
) -> ReallocationFilters:
    return ReallocationFilters(
        status=status_filter,
        item_id=item_id,
        warehouse_id=warehouse_id,
        to_demand_id=to_demand_id,
    )


ReservationFilterDep = Annotated[ReservationFilters, Depends(reservation_filters)]
ReallocationFilterDep = Annotated[ReallocationFilters, Depends(reallocation_filters)]


# -----------------------------------------------------------------------------
# Availability
# -----------------------------------------------------------------------------


@router.get(
    "/available-to-promise",
    response_model=ApiResponse[AvailableToPromiseRead],
    summary="What can still be promised for an item",
    description=(
        "Sum of available stock, read from the balance table on every call. "
        "Never cached (section 34): a stale availability answer becomes an "
        "over-promise. Omit the warehouse for a company-wide figure."
    ),
    dependencies=[require_permission(Permission.ALLOCATION_VIEW)],
)
async def get_available_to_promise(
    session: DbSession,
    tenant: Tenant,
    item_id: Annotated[uuid.UUID, Query()],
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
) -> ApiResponse[AvailableToPromiseRead]:
    result = await AllocationService(session, tenant).availability(item_id, warehouse_id)
    return ApiResponse[AvailableToPromiseRead].ok(result)


# -----------------------------------------------------------------------------
# Reservations
# -----------------------------------------------------------------------------


@router.get(
    "/reservations",
    response_model=PaginatedResponse[ReservationRead],
    summary="List stock reservations",
    dependencies=[require_permission(Permission.RESERVATION_VIEW)],
)
async def list_reservations(
    session: DbSession,
    tenant: Tenant,
    filters: ReservationFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[ReservationRead]:
    items, pagination = await AllocationService(session, tenant).list_reservations(
        filters, params, sort
    )
    return PaginatedResponse[ReservationRead](data=items, pagination=pagination)


@router.post(
    "/reservations",
    response_model=ApiResponse[ReservationRead],
    status_code=status.HTTP_201_CREATED,
    summary="Reserve stock for a demand line",
    description=(
        "Locks the stock position, re-reads it, and refuses with "
        "INSUFFICIENT_AVAILABLE_STOCK if the reservation would promise more than "
        "is actually there. On-hand quantity does not change - nothing has moved."
    ),
    dependencies=[require_permission(Permission.RESERVATION_CREATE)],
)
async def create_reservation(
    payload: ReservationCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[ReservationRead]:
    created = await AllocationService(session, tenant).create_reservation(payload)
    return ApiResponse[ReservationRead].ok(created, "Stock reserved")


@router.get(
    "/reservations/{reservation_id}",
    response_model=ApiResponse[ReservationRead],
    summary="Read one reservation",
    dependencies=[require_permission(Permission.RESERVATION_VIEW)],
)
async def get_reservation(
    reservation_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[ReservationRead]:
    found = await AllocationService(session, tenant).get_reservation(reservation_id)
    return ApiResponse[ReservationRead].ok(found)


@router.post(
    "/reservations/{reservation_id}/release",
    response_model=ApiResponse[ReservationRead],
    summary="Release a reservation, in part or in full",
    description=(
        "Omit the quantity to release everything still outstanding. Releasing "
        "more than remains is refused rather than clamped. The reason is "
        "mandatory and is written to the audit log."
    ),
    dependencies=[require_permission(Permission.RESERVATION_RELEASE)],
)
async def release_reservation(
    reservation_id: uuid.UUID,
    payload: ReleaseRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[ReservationRead]:
    released = await AllocationService(session, tenant).release_reservation(
        reservation_id, reason=payload.reason, quantity=payload.quantity
    )
    return ApiResponse[ReservationRead].ok(released, "Reservation released")


# -----------------------------------------------------------------------------
# Reallocation
# -----------------------------------------------------------------------------


@router.get(
    "/reallocation-requests",
    response_model=PaginatedResponse[ReallocationRequestRead],
    summary="List reallocation requests",
    description=(
        "Filter by status to get the approval queue. Oldest first by default: a "
        "queue that shows the newest request at the top is a queue where the "
        "oldest one never gets dealt with."
    ),
    dependencies=[require_permission(Permission.ALLOCATION_VIEW)],
)
async def list_reallocation_requests(
    session: DbSession,
    tenant: Tenant,
    filters: ReallocationFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[ReallocationRequestRead]:
    items, pagination = await AllocationService(session, tenant).list_reallocation_requests(
        filters, params, sort
    )
    return PaginatedResponse[ReallocationRequestRead](data=items, pagination=pagination)


@router.post(
    "/reallocation-requests",
    response_model=ApiResponse[ReallocationRequestRead],
    status_code=status.HTTP_201_CREATED,
    summary="Suggest donors and raise a reallocation request",
    description=(
        "Ranks the reservations that could give up stock and raises a PENDING "
        "request against the best one, returning the alternatives it considered. "
        "Name `from_reservation_id` to choose a donor yourself - it is accepted "
        "only if the engine would have offered it. Nothing is moved until the "
        "request is approved."
    ),
    dependencies=[require_permission(Permission.ALLOCATION_REALLOCATE)],
)
async def create_reallocation_request(
    payload: ReallocationRequestCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[ReallocationRequestRead]:
    created = await AllocationService(session, tenant).create_reallocation_request(payload)
    return ApiResponse[ReallocationRequestRead].ok(created, "Reallocation requested")


@router.get(
    "/reallocation-requests/{request_id}",
    response_model=ApiResponse[ReallocationRequestRead],
    summary="Read one reallocation request",
    dependencies=[require_permission(Permission.ALLOCATION_VIEW)],
)
async def get_reallocation_request(
    request_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[ReallocationRequestRead]:
    found = await AllocationService(session, tenant).get_reallocation_request(request_id)
    return ApiResponse[ReallocationRequestRead].ok(found)


@router.post(
    "/reallocation-requests/{request_id}/approve",
    response_model=ApiResponse[ReallocationRequestRead],
    summary="Approve a reallocation and execute it",
    description=(
        "Releases the quantity from the donor and reserves it for the recipient "
        "in one transaction, audits the change, and notifies whoever committed "
        "the donor stock. Approval and execution are one step so an approval can "
        "never sit unapplied, and stock can never move without one."
    ),
    dependencies=[require_permission(Permission.ALLOCATION_APPROVE)],
)
async def approve_reallocation_request(
    request_id: uuid.UUID,
    payload: ReallocationDecision,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[ReallocationRequestRead]:
    approved = await AllocationService(session, tenant).approve_reallocation_request(
        request_id, decision_note=payload.decision_note
    )
    return ApiResponse[ReallocationRequestRead].ok(approved, "Reallocation approved and executed")


@router.post(
    "/reallocation-requests/{request_id}/reject",
    response_model=ApiResponse[ReallocationRequestRead],
    summary="Reject a reallocation request",
    dependencies=[require_permission(Permission.ALLOCATION_APPROVE)],
)
async def reject_reallocation_request(
    request_id: uuid.UUID,
    payload: ReallocationDecision,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[ReallocationRequestRead]:
    rejected = await AllocationService(session, tenant).reject_reallocation_request(
        request_id, decision_note=payload.decision_note
    )
    return ApiResponse[ReallocationRequestRead].ok(rejected, "Reallocation rejected")


__all__ = ["router"]
