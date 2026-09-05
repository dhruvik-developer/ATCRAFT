"""HTTP surface for dispatches and dispatch tracking.

Handlers bind parameters, call one service function and wrap the result in the
standard envelope. No rule, query or audit write lives here (master rules
section 74).

Route ordering note: ``/tracking`` and ``/delayed`` are declared before
``/{dispatch_id}``. FastAPI matches in declaration order, and a literal segment
declared after a path parameter of the same shape would be captured by it and
rejected as a malformed UUID.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from src.core.deps import DbSession, Tenant, require_permission
from src.core.pagination import (
    CursorParams,
    PageParams,
    SortParams,
    cursor_params,
    page_params,
    sort_params,
)
from src.core.permissions import Permission
from src.core.responses import (
    COMMON_ERROR_RESPONSES,
    ApiResponse,
    CursorPaginatedResponse,
    PaginatedResponse,
)
from src.modules.dispatch import service
from src.modules.dispatch.models import DispatchStatus, DispatchType
from src.modules.dispatch.schemas import (
    DelayedDispatchRow,
    DeliveryConfirmation,
    DispatchCreate,
    DispatchDetail,
    DispatchFilters,
    DispatchRequest,
    DispatchResult,
    DispatchSummary,
    DispatchUpdate,
    InTransitRequest,
    ReasonRequest,
    TrackingBoardRow,
    TrackingEventIn,
    TrackingEventRead,
)

router = APIRouter(prefix="/dispatch", tags=["Dispatch"], responses=COMMON_ERROR_RESPONSES)

PageDep = Annotated[PageParams, Depends(page_params)]
SortDep = Annotated[SortParams, Depends(sort_params)]
CursorDep = Annotated[CursorParams, Depends(cursor_params)]

# Deletion carries its reason in the query string: a DELETE body is legal but
# several HTTP clients and proxies drop it, and the reason is not optional
# (master rules section 83).
ReasonQuery = Annotated[
    str, Query(min_length=3, max_length=255, description="Why this is being removed")
]


def dispatch_filters(
    search: Annotated[
        str | None, Query(max_length=120, description="Code, tracking id, LR or vehicle number")
    ] = None,
    dispatch_status: Annotated[DispatchStatus | None, Query(alias="status")] = None,
    dispatch_type: Annotated[DispatchType | None, Query()] = None,
    customer_id: Annotated[uuid.UUID | None, Query()] = None,
    vendor_id: Annotated[uuid.UUID | None, Query()] = None,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    sales_order_id: Annotated[uuid.UUID | None, Query()] = None,
    transporter_id: Annotated[uuid.UUID | None, Query()] = None,
    tracking_id: Annotated[
        str | None, Query(max_length=120, description="Exact carrier reference")
    ] = None,
    in_flight_only: Annotated[
        bool, Query(description="Only consignments that have left and not arrived")
    ] = False,
    delayed_only: Annotated[
        bool, Query(description="Only consignments past their promised delivery date")
    ] = False,
    dispatch_from: Annotated[date | None, Query()] = None,
    dispatch_to: Annotated[date | None, Query()] = None,
) -> DispatchFilters:
    return DispatchFilters(
        search=search,
        status=dispatch_status,
        dispatch_type=dispatch_type,
        customer_id=customer_id,
        vendor_id=vendor_id,
        warehouse_id=warehouse_id,
        sales_order_id=sales_order_id,
        transporter_id=transporter_id,
        tracking_id=tracking_id,
        in_flight_only=in_flight_only,
        delayed_only=delayed_only,
        dispatch_from=dispatch_from,
        dispatch_to=dispatch_to,
    )


FilterDep = Annotated[DispatchFilters, Depends(dispatch_filters)]


# -----------------------------------------------------------------------------
# Literal routes (declared first - see the module docstring)
# -----------------------------------------------------------------------------


@router.get(
    "/tracking",
    response_model=PaginatedResponse[TrackingBoardRow],
    summary="The dispatch tracking board",
    description=(
        "Master rules section 23: every consignment with its counterparty, the order it "
        "fulfils, the carrier and reference, the current status, the promised delivery date, "
        "whether it is delayed and whether QC or a goods receipt is still outstanding at the "
        "far end. Answered by one joined query, with late consignments first. Inbound "
        "shipments on their way to us are tracked on the purchase order - see "
        "`/procurement` - because a vendor's consignment to us is not a row in this table."
    ),
    dependencies=[require_permission(Permission.DISPATCH_VIEW)],
)
async def tracking_board(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    filters: FilterDep,
) -> PaginatedResponse[TrackingBoardRow]:
    rows, pagination = await service.tracking_board(session, tenant, filters=filters, params=params)
    return PaginatedResponse[TrackingBoardRow](data=rows, pagination=pagination)


@router.get(
    "/delayed",
    response_model=PaginatedResponse[DelayedDispatchRow],
    summary="Consignments past their promised delivery date",
    description=(
        "On the road, promised a date, and that date has passed - longest overdue first. "
        "The same set the hourly `notifications.scan_dispatch_delays` sweep alerts on, so "
        "the screen and the alert can never disagree."
    ),
    dependencies=[require_permission(Permission.DISPATCH_VIEW)],
)
async def delayed_dispatches(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    warehouse_id: Annotated[
        uuid.UUID | None, Query(description="Restrict to one dispatching warehouse")
    ] = None,
) -> PaginatedResponse[DelayedDispatchRow]:
    rows, pagination = await service.list_delayed(
        session, tenant, params=params, warehouse_id=warehouse_id
    )
    return PaginatedResponse[DelayedDispatchRow](data=rows, pagination=pagination)


# -----------------------------------------------------------------------------
# CRUD
# -----------------------------------------------------------------------------


@router.get(
    "",
    response_model=PaginatedResponse[DispatchSummary],
    summary="List consignments",
    dependencies=[require_permission(Permission.DISPATCH_VIEW)],
)
async def list_dispatches(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    sort: SortDep,
    filters: FilterDep,
) -> PaginatedResponse[DispatchSummary]:
    rows, pagination = await service.list_dispatches(
        session, tenant, filters=filters, params=params, sort=sort
    )
    return PaginatedResponse[DispatchSummary](data=rows, pagination=pagination)


@router.post(
    "",
    response_model=ApiResponse[DispatchDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Raise a consignment",
    description=(
        "Creates the loading plan in NOT_DISPATCHED. Nothing moves: stock leaves only when "
        "`POST /{id}/dispatch` runs. Naming a packing order claims its boxes so two "
        "dispatchers cannot plan around the same cartons."
    ),
    dependencies=[require_permission(Permission.DISPATCH_CREATE)],
)
async def create_dispatch(
    session: DbSession, tenant: Tenant, payload: DispatchCreate
) -> ApiResponse[DispatchDetail]:
    detail = await service.create_dispatch(session, tenant, payload)
    return ApiResponse[DispatchDetail].ok(detail, "Dispatch created")


@router.get(
    "/{dispatch_id}",
    response_model=ApiResponse[DispatchDetail],
    summary="One consignment with its lines",
    dependencies=[require_permission(Permission.DISPATCH_VIEW)],
)
async def get_dispatch(
    session: DbSession, tenant: Tenant, dispatch_id: uuid.UUID
) -> ApiResponse[DispatchDetail]:
    return ApiResponse[DispatchDetail].ok(
        await service.get_dispatch_detail(session, tenant, dispatch_id)
    )


@router.patch(
    "/{dispatch_id}",
    response_model=ApiResponse[DispatchDetail],
    summary="Amend a consignment that has not left",
    dependencies=[require_permission(Permission.DISPATCH_UPDATE)],
)
async def update_dispatch(
    session: DbSession, tenant: Tenant, dispatch_id: uuid.UUID, payload: DispatchUpdate
) -> ApiResponse[DispatchDetail]:
    detail = await service.update_dispatch(session, tenant, dispatch_id, payload)
    return ApiResponse[DispatchDetail].ok(detail, "Dispatch updated")


@router.delete(
    "/{dispatch_id}",
    response_model=ApiResponse[None],
    summary="Delete a consignment that was never used",
    description=(
        "Permitted only in NOT_DISPATCHED and only while it holds no packing order. "
        "Anything further along is cancelled instead, so the trail survives."
    ),
    dependencies=[require_permission(Permission.DISPATCH_UPDATE)],
)
async def delete_dispatch(
    session: DbSession, tenant: Tenant, dispatch_id: uuid.UUID, reason: ReasonQuery
) -> ApiResponse[None]:
    await service.delete_draft_dispatch(session, tenant, dispatch_id, reason=reason)
    return ApiResponse[None].ok(None, "Dispatch deleted")


# -----------------------------------------------------------------------------
# Lifecycle actions
# -----------------------------------------------------------------------------


@router.post(
    "/{dispatch_id}/ready",
    response_model=ApiResponse[DispatchDetail],
    summary="Mark a consignment ready to leave",
    dependencies=[require_permission(Permission.DISPATCH_UPDATE)],
)
async def mark_ready(
    session: DbSession, tenant: Tenant, dispatch_id: uuid.UUID, payload: ReasonRequest
) -> ApiResponse[DispatchDetail]:
    detail = await service.mark_ready(session, tenant, dispatch_id, reason=payload.reason)
    return ApiResponse[DispatchDetail].ok(detail, "Dispatch ready")


@router.post(
    "/{dispatch_id}/cancel",
    response_model=ApiResponse[DispatchDetail],
    summary="Call off a consignment before it leaves",
    dependencies=[require_permission(Permission.DISPATCH_UPDATE)],
)
async def cancel_dispatch(
    session: DbSession, tenant: Tenant, dispatch_id: uuid.UUID, payload: ReasonRequest
) -> ApiResponse[DispatchDetail]:
    detail = await service.cancel_dispatch(session, tenant, dispatch_id, reason=payload.reason)
    return ApiResponse[DispatchDetail].ok(detail, "Dispatch cancelled")


@router.post(
    "/{dispatch_id}/dispatch",
    response_model=ApiResponse[DispatchResult],
    summary="Send the consignment",
    description=(
        "The only action that moves stock. In one transaction it consumes the reservations "
        "the sales order was holding, posts the DISPATCH movements, advances each order "
        "line's dispatched quantity and moves the order to PARTIALLY_DISPATCHED or "
        "DISPATCHED. Shipping more than was ordered is refused, not clamped."
    ),
    dependencies=[require_permission(Permission.DISPATCH_CREATE)],
)
async def dispatch_consignment(
    session: DbSession, tenant: Tenant, dispatch_id: uuid.UUID, payload: DispatchRequest
) -> ApiResponse[DispatchResult]:
    result = await service.dispatch_consignment(session, tenant, dispatch_id, payload)
    return ApiResponse[DispatchResult].ok(result, "Dispatch sent")


@router.post(
    "/{dispatch_id}/in-transit",
    response_model=ApiResponse[DispatchDetail],
    summary="Report the consignment as moving",
    dependencies=[require_permission(Permission.DISPATCH_UPDATE)],
)
async def mark_in_transit(
    session: DbSession, tenant: Tenant, dispatch_id: uuid.UUID, payload: InTransitRequest
) -> ApiResponse[DispatchDetail]:
    detail = await service.mark_in_transit(session, tenant, dispatch_id, payload)
    return ApiResponse[DispatchDetail].ok(detail, "Dispatch in transit")


@router.post(
    "/{dispatch_id}/deliver",
    response_model=ApiResponse[DispatchDetail],
    summary="Confirm delivery",
    description=(
        "Records the arrival date, who confirmed it and the receiver's note, and appends the "
        "tracking event. A consignment that arrived after its promised date must carry a "
        "delay reason."
    ),
    dependencies=[require_permission(Permission.DISPATCH_CONFIRM_DELIVERY)],
)
async def confirm_delivery(
    session: DbSession, tenant: Tenant, dispatch_id: uuid.UUID, payload: DeliveryConfirmation
) -> ApiResponse[DispatchDetail]:
    detail = await service.confirm_delivery(session, tenant, dispatch_id, payload)
    return ApiResponse[DispatchDetail].ok(detail, "Delivery confirmed")


# -----------------------------------------------------------------------------
# Tracking events
# -----------------------------------------------------------------------------


@router.get(
    "/{dispatch_id}/tracking-events",
    response_model=CursorPaginatedResponse[TrackingEventRead],
    summary="The consignment's tracking history",
    description=(
        "Append-only and newest first. Keyset-paged rather than offset-paged because a "
        "courier integration makes this table long (section 31)."
    ),
    dependencies=[require_permission(Permission.DISPATCH_VIEW)],
)
async def list_tracking_events(
    session: DbSession, tenant: Tenant, dispatch_id: uuid.UUID, params: CursorDep
) -> CursorPaginatedResponse[TrackingEventRead]:
    rows, pagination = await service.list_tracking_events(
        session, tenant, dispatch_id, params=params
    )
    return CursorPaginatedResponse[TrackingEventRead](data=rows, pagination=pagination)


@router.post(
    "/{dispatch_id}/tracking-events",
    response_model=ApiResponse[TrackingEventRead],
    status_code=status.HTTP_201_CREATED,
    summary="Record a tracking event",
    description=(
        "An event whose status differs from the consignment's current one also moves it, "
        "through the state machine - so a courier feed cannot walk a consignment into a "
        "status the lifecycle does not allow. An event repeating the current status is a "
        "position update. Events are never edited or deleted; a correction is a later event."
    ),
    dependencies=[require_permission(Permission.DISPATCH_UPDATE)],
)
async def add_tracking_event(
    session: DbSession, tenant: Tenant, dispatch_id: uuid.UUID, payload: TrackingEventIn
) -> ApiResponse[TrackingEventRead]:
    event = await service.add_tracking_event(session, tenant, dispatch_id, payload)
    return ApiResponse[TrackingEventRead].ok(event, "Tracking event recorded")
