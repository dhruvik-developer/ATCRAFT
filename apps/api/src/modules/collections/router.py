"""Collections HTTP surface.

The worklist is a read model over invoices; the follow-up routes are the write
surface. Both are gated on ``collection.view`` or ``collection.manage``
literally - the permission strings are part of the API contract, so they are not
derived from a dictionary that could be renamed silently.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from src.core.deps import (
    CurrentUser,
    DbSession,
    Tenant,
    forbid_dealer_users,
    forbid_vendor_users,
    require_permission,
)
from src.core.messages import success_message
from src.core.pagination import (
    CursorParams,
    PageParams,
    SortParams,
    bind_query_filters,
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
from src.modules.collections import service
from src.modules.collections.models import FollowUpStatus
from src.modules.collections.schemas import (
    FollowUpCancel,
    FollowUpCreate,
    FollowUpDetail,
    FollowUpEscalate,
    FollowUpFilters,
    FollowUpNoteCreate,
    FollowUpNoteRead,
    FollowUpPromise,
    FollowUpResolve,
    FollowUpSummary,
    FollowUpUpdate,
    WorklistFilters,
    WorklistRow,
)

router = APIRouter(
    prefix="/collections",
    tags=["Collections"],
    responses=COMMON_ERROR_RESPONSES,
    dependencies=[Depends(forbid_vendor_users), Depends(forbid_dealer_users)],
)
PageDep = Annotated[PageParams, Depends(page_params)]
SortDep = Annotated[SortParams, Depends(sort_params)]
CursorDep = Annotated[CursorParams, Depends(cursor_params)]


def worklist_filters(
    search: Annotated[str | None, Query(max_length=120)] = None,
    customer_id: uuid.UUID | None = None,
    status_filter: Annotated[FollowUpStatus | None, Query(alias="status")] = None,
    assigned_to_me: bool = False,
    min_overdue_days: Annotated[int | None, Query(ge=0, le=3650)] = None,
    dealer_only: bool = False,
) -> WorklistFilters:
    return bind_query_filters(
        WorklistFilters,
        search=search,
        customer_id=customer_id,
        status=status_filter,
        assigned_to_me=assigned_to_me,
        min_overdue_days=min_overdue_days,
        dealer_only=dealer_only,
    )


def follow_up_filters(
    search: Annotated[str | None, Query(max_length=120)] = None,
    status_filter: Annotated[FollowUpStatus | None, Query(alias="status")] = None,
    customer_id: uuid.UUID | None = None,
    assigned_to_id: uuid.UUID | None = None,
    assigned_to_me: bool = False,
    live_only: bool = False,
) -> FollowUpFilters:
    return bind_query_filters(
        FollowUpFilters,
        search=search,
        status=status_filter,
        customer_id=customer_id,
        assigned_to_id=assigned_to_id,
        assigned_to_me=assigned_to_me,
        live_only=live_only,
    )


WorklistFilterDep = Annotated[WorklistFilters, Depends(worklist_filters)]
FollowUpFilterDep = Annotated[FollowUpFilters, Depends(follow_up_filters)]


# -----------------------------------------------------------------------------
# Worklist
# -----------------------------------------------------------------------------


@router.get(
    "/worklist",
    response_model=PaginatedResponse[WorklistRow],
    summary="Overdue receivables worklist",
    description=(
        "One row per customer and currency with invoices past their due date and an "
        "unsettled balance. The follow-up columns are null until somebody opens one."
    ),
    dependencies=[require_permission(Permission.COLLECTION_VIEW)],
)
async def collection_worklist(
    session: DbSession,
    tenant: Tenant,
    filters: WorklistFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[WorklistRow]:
    rows, pagination = await service.worklist(session, tenant, filters, params, sort)
    return PaginatedResponse[WorklistRow](data=rows, pagination=pagination)


# -----------------------------------------------------------------------------
# Follow-ups
# -----------------------------------------------------------------------------


@router.get(
    "/follow-ups",
    response_model=PaginatedResponse[FollowUpSummary],
    summary="List payment follow-ups",
    dependencies=[require_permission(Permission.COLLECTION_VIEW)],
)
async def list_follow_ups(
    session: DbSession,
    tenant: Tenant,
    filters: FollowUpFilterDep,
    params: PageDep,
) -> PaginatedResponse[FollowUpSummary]:
    rows, pagination = await service.list_follow_ups(session, tenant, filters, params)
    return PaginatedResponse[FollowUpSummary](data=rows, pagination=pagination)


@router.post(
    "/follow-ups",
    response_model=ApiResponse[FollowUpDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Open a payment follow-up",
    dependencies=[require_permission(Permission.COLLECTION_MANAGE)],
)
async def create_follow_up(
    payload: FollowUpCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[FollowUpDetail]:
    return ApiResponse[FollowUpDetail].ok(
        await service.create_follow_up(session, tenant, payload),
        success_message("Payment follow-up", "opened"),
    )


@router.get(
    "/follow-ups/{follow_up_id}",
    response_model=ApiResponse[FollowUpDetail],
    summary="Read a payment follow-up and its overdue invoices",
    dependencies=[require_permission(Permission.COLLECTION_VIEW)],
)
async def get_follow_up(
    follow_up_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[FollowUpDetail]:
    return ApiResponse[FollowUpDetail].ok(
        await service.get_follow_up(session, tenant, follow_up_id)
    )


@router.patch(
    "/follow-ups/{follow_up_id}",
    response_model=ApiResponse[FollowUpDetail],
    summary="Reassign or reschedule a follow-up",
    dependencies=[require_permission(Permission.COLLECTION_MANAGE)],
)
async def update_follow_up(
    follow_up_id: uuid.UUID,
    payload: FollowUpUpdate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[FollowUpDetail]:
    return ApiResponse[FollowUpDetail].ok(
        await service.update_follow_up(session, tenant, follow_up_id, payload),
        success_message("Payment follow-up", "updated"),
    )


@router.get(
    "/follow-ups/{follow_up_id}/notes",
    response_model=CursorPaginatedResponse[FollowUpNoteRead],
    summary="Follow-up notes, newest first",
    description=(
        "Keyset-paginated by `(created_at, id)`: pass the `next_cursor` from the "
        "previous page rather than a page number."
    ),
    dependencies=[require_permission(Permission.COLLECTION_VIEW)],
)
async def list_follow_up_notes(
    follow_up_id: uuid.UUID, session: DbSession, tenant: Tenant, params: CursorDep
) -> CursorPaginatedResponse[FollowUpNoteRead]:
    notes, pagination = await service.list_notes(session, tenant, follow_up_id, params)
    return CursorPaginatedResponse[FollowUpNoteRead](data=notes, pagination=pagination)


@router.post(
    "/follow-ups/{follow_up_id}/notes",
    response_model=ApiResponse[FollowUpNoteRead],
    status_code=status.HTTP_201_CREATED,
    summary="Log a contact attempt",
    dependencies=[require_permission(Permission.COLLECTION_MANAGE)],
)
async def add_follow_up_note(
    follow_up_id: uuid.UUID,
    payload: FollowUpNoteCreate,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[FollowUpNoteRead]:
    return ApiResponse[FollowUpNoteRead].ok(
        await service.add_note(
            session, tenant, follow_up_id, payload, author_name=principal.full_name
        ),
        success_message("Follow-up note", "recorded"),
    )


@router.post(
    "/follow-ups/{follow_up_id}/promise",
    response_model=ApiResponse[FollowUpDetail],
    summary="Record a dated promise from the customer",
    dependencies=[require_permission(Permission.COLLECTION_MANAGE)],
)
async def record_promise(
    follow_up_id: uuid.UUID,
    payload: FollowUpPromise,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[FollowUpDetail]:
    return ApiResponse[FollowUpDetail].ok(
        await service.record_promise(session, tenant, follow_up_id, payload),
        "Payment promise recorded",
    )


@router.post(
    "/follow-ups/{follow_up_id}/escalate",
    response_model=ApiResponse[FollowUpDetail],
    summary="Escalate a follow-up, optionally putting the customer on hold",
    description=(
        "Blocking the customer requires `customer.update` in addition to "
        "`collection.manage`; without it the request is refused before anything moves."
    ),
    dependencies=[require_permission(Permission.COLLECTION_MANAGE)],
)
async def escalate_follow_up(
    follow_up_id: uuid.UUID,
    payload: FollowUpEscalate,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[FollowUpDetail]:
    return ApiResponse[FollowUpDetail].ok(
        await service.escalate(session, tenant, principal, follow_up_id, payload),
        "Payment follow-up escalated",
    )


@router.post(
    "/follow-ups/{follow_up_id}/resolve",
    response_model=ApiResponse[FollowUpDetail],
    summary="Resolve a follow-up",
    dependencies=[require_permission(Permission.COLLECTION_MANAGE)],
)
async def resolve_follow_up(
    follow_up_id: uuid.UUID,
    payload: FollowUpResolve,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[FollowUpDetail]:
    return ApiResponse[FollowUpDetail].ok(
        await service.resolve(session, tenant, follow_up_id, payload),
        success_message("Payment follow-up", "resolved"),
    )


@router.post(
    "/follow-ups/{follow_up_id}/cancel",
    response_model=ApiResponse[FollowUpDetail],
    summary="Cancel a follow-up opened in error",
    dependencies=[require_permission(Permission.COLLECTION_MANAGE)],
)
async def cancel_follow_up(
    follow_up_id: uuid.UUID,
    payload: FollowUpCancel,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[FollowUpDetail]:
    return ApiResponse[FollowUpDetail].ok(
        await service.cancel(session, tenant, follow_up_id, payload),
        success_message("Payment follow-up", "cancelled"),
    )


__all__ = ["router"]
