"""HTTP surface for routings, work centres and machines.

Handlers bind parameters, call one service method and wrap the result in the
standard envelope. No rule, query or audit write lives here (master rules
section 74).

Route ordering note: every literal-prefixed route - ``/work-centers``,
``/machines``, ``/items/...`` - is declared **before** ``/{routing_id}``.
FastAPI matches in declaration order, so with the reverse order a request for
``/work-centers`` would be captured by the routing route and rejected as a
malformed UUID instead of reaching its handler.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from src.core.deps import CurrentUser, DbSession, Tenant, require_permission
from src.core.pagination import PageParams, SortParams, page_params, sort_params
from src.core.permissions import Permission
from src.core.responses import COMMON_ERROR_RESPONSES, ApiResponse, PaginatedResponse
from src.modules.routing.models import MachineStatus, RoutingStatus
from src.modules.routing.schemas import (
    MAX_COSTING_QUANTITY,
    CopyAsNewVersionRequest,
    LeadTimeRead,
    MachineCreate,
    MachineFilters,
    MachineRead,
    MachineStatusChange,
    MachineUpdate,
    ReasonRequest,
    RoutingCostRead,
    RoutingCreate,
    RoutingDetail,
    RoutingFilters,
    RoutingOperationRead,
    RoutingStatusChange,
    RoutingSummary,
    RoutingUpdate,
    WorkCenterCreate,
    WorkCenterFilters,
    WorkCenterRead,
    WorkCenterUpdate,
)
from src.modules.routing.service import MachineService, RoutingService, WorkCenterService

router = APIRouter(
    prefix="/routing", tags=["Routing & Work Centers"], responses=COMMON_ERROR_RESPONSES
)

PageDep = Annotated[PageParams, Depends(page_params)]
SortDep = Annotated[SortParams, Depends(sort_params)]

# Retirement and deletion carry their reason in the query string rather than a
# request body: a DELETE body is legal but several HTTP clients and proxies drop
# it, and the reason is not optional (master rules section 83).
ReasonQuery = Annotated[
    str, Query(min_length=3, max_length=255, description="Why the record is being retired")
]

QuantityQuery = Annotated[
    Decimal,
    Query(
        gt=0,
        le=MAX_COSTING_QUANTITY,
        max_digits=18,
        decimal_places=4,
        description="Units the cost and lead time are calculated for",
    ),
]


def work_center_filters(
    search: Annotated[str | None, Query(max_length=120, description="Code or name")] = None,
    plant_id: Annotated[uuid.UUID | None, Query()] = None,
    department_id: Annotated[uuid.UUID | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
    include_deleted: Annotated[bool, Query(description="Include retired centres")] = False,
) -> WorkCenterFilters:
    return WorkCenterFilters(
        search=search,
        plant_id=plant_id,
        department_id=department_id,
        is_active=is_active,
        include_deleted=include_deleted,
    )


def machine_filters(
    search: Annotated[
        str | None, Query(max_length=120, description="Code, name or machine type")
    ] = None,
    work_center_id: Annotated[uuid.UUID | None, Query()] = None,
    machine_status: Annotated[MachineStatus | None, Query(alias="status")] = None,
    is_active: Annotated[bool | None, Query()] = None,
    maintenance_due_before: Annotated[
        date | None, Query(description="Only machines with a service due on or before this date")
    ] = None,
    include_deleted: Annotated[bool, Query(description="Include retired machines")] = False,
) -> MachineFilters:
    return MachineFilters(
        search=search,
        work_center_id=work_center_id,
        status=machine_status,
        is_active=is_active,
        maintenance_due_before=maintenance_due_before,
        include_deleted=include_deleted,
    )


def routing_filters(
    search: Annotated[str | None, Query(max_length=120, description="Code or name")] = None,
    item_id: Annotated[uuid.UUID | None, Query(description="Routings for this item")] = None,
    routing_status: Annotated[RoutingStatus | None, Query(alias="status")] = None,
    is_default: Annotated[bool | None, Query()] = None,
    work_center_id: Annotated[
        uuid.UUID | None, Query(description="Routings with any operation at this centre")
    ] = None,
    effective_on: Annotated[
        date | None, Query(description="Only routings effective on this date")
    ] = None,
) -> RoutingFilters:
    return RoutingFilters(
        search=search,
        item_id=item_id,
        status=routing_status,
        is_default=is_default,
        work_center_id=work_center_id,
        effective_on=effective_on,
    )


WorkCenterFilterDep = Annotated[WorkCenterFilters, Depends(work_center_filters)]
MachineFilterDep = Annotated[MachineFilters, Depends(machine_filters)]
RoutingFilterDep = Annotated[RoutingFilters, Depends(routing_filters)]


# -----------------------------------------------------------------------------
# Work centres (declared first - see the module docstring)
# -----------------------------------------------------------------------------


@router.get(
    "/work-centers",
    response_model=PaginatedResponse[WorkCenterRead],
    summary="List work centres",
    dependencies=[require_permission(Permission.WORK_CENTER_VIEW)],
)
async def list_work_centers(
    session: DbSession,
    tenant: Tenant,
    filters: WorkCenterFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[WorkCenterRead]:
    rows, pagination = await WorkCenterService(session, tenant).list_work_centers(
        filters, params, sort
    )
    return PaginatedResponse[WorkCenterRead](data=rows, pagination=pagination)


@router.post(
    "/work-centers",
    response_model=ApiResponse[WorkCenterRead],
    status_code=status.HTTP_201_CREATED,
    summary="Create a work centre",
    dependencies=[require_permission(Permission.WORK_CENTER_MANAGE)],
)
async def create_work_center(
    payload: WorkCenterCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[WorkCenterRead]:
    created = await WorkCenterService(session, tenant).create_work_center(payload)
    return ApiResponse[WorkCenterRead].ok(created, "Work centre created")


@router.get(
    "/work-centers/{work_center_id}",
    response_model=ApiResponse[WorkCenterRead],
    summary="Fetch one work centre",
    dependencies=[require_permission(Permission.WORK_CENTER_VIEW)],
)
async def get_work_center(
    work_center_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[WorkCenterRead]:
    return ApiResponse[WorkCenterRead].ok(
        await WorkCenterService(session, tenant).get_work_center(work_center_id)
    )


@router.patch(
    "/work-centers/{work_center_id}",
    response_model=ApiResponse[WorkCenterRead],
    summary="Update a work centre",
    description=(
        "A change to cost_per_hour moves the calculated cost of every approved routing "
        "that uses this centre - that is the intended standard-costing behaviour."
    ),
    dependencies=[require_permission(Permission.WORK_CENTER_MANAGE)],
)
async def update_work_center(
    work_center_id: uuid.UUID,
    payload: WorkCenterUpdate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[WorkCenterRead]:
    updated = await WorkCenterService(session, tenant).update_work_center(work_center_id, payload)
    return ApiResponse[WorkCenterRead].ok(updated, "Work centre updated")


@router.post(
    "/work-centers/{work_center_id}/activate",
    response_model=ApiResponse[WorkCenterRead],
    summary="Bring a work centre back into use",
    dependencies=[require_permission(Permission.WORK_CENTER_MANAGE)],
)
async def activate_work_center(
    work_center_id: uuid.UUID,
    payload: ReasonRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[WorkCenterRead]:
    updated = await WorkCenterService(session, tenant).set_active(
        work_center_id, active=True, reason=payload.reason
    )
    return ApiResponse[WorkCenterRead].ok(updated, "Work centre activated")


@router.post(
    "/work-centers/{work_center_id}/deactivate",
    response_model=ApiResponse[WorkCenterRead],
    summary="Stop planning new work at a work centre",
    dependencies=[require_permission(Permission.WORK_CENTER_MANAGE)],
)
async def deactivate_work_center(
    work_center_id: uuid.UUID,
    payload: ReasonRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[WorkCenterRead]:
    updated = await WorkCenterService(session, tenant).set_active(
        work_center_id, active=False, reason=payload.reason
    )
    return ApiResponse[WorkCenterRead].ok(updated, "Work centre deactivated")


@router.delete(
    "/work-centers/{work_center_id}",
    response_model=ApiResponse[None],
    summary="Retire a work centre",
    description=(
        "Refused while machines still sit in the centre or a draft or approved routing "
        "still plans work there."
    ),
    dependencies=[require_permission(Permission.WORK_CENTER_MANAGE)],
)
async def retire_work_center(
    work_center_id: uuid.UUID, session: DbSession, tenant: Tenant, reason: ReasonQuery
) -> ApiResponse[None]:
    await WorkCenterService(session, tenant).retire_work_center(work_center_id, reason=reason)
    return ApiResponse[None].ok(None, "Work centre retired")


# -----------------------------------------------------------------------------
# Machines
# -----------------------------------------------------------------------------


@router.get(
    "/machines",
    response_model=PaginatedResponse[MachineRead],
    summary="List machines",
    dependencies=[require_permission(Permission.MACHINE_VIEW)],
)
async def list_machines(
    session: DbSession,
    tenant: Tenant,
    filters: MachineFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[MachineRead]:
    rows, pagination = await MachineService(session, tenant).list_machines(filters, params, sort)
    return PaginatedResponse[MachineRead](data=rows, pagination=pagination)


@router.post(
    "/machines",
    response_model=ApiResponse[MachineRead],
    status_code=status.HTTP_201_CREATED,
    summary="Add a machine to a work centre",
    dependencies=[require_permission(Permission.MACHINE_MANAGE)],
)
async def create_machine(
    payload: MachineCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[MachineRead]:
    created = await MachineService(session, tenant).create_machine(payload)
    return ApiResponse[MachineRead].ok(created, "Machine created")


@router.get(
    "/machines/{machine_id}",
    response_model=ApiResponse[MachineRead],
    summary="Fetch one machine",
    dependencies=[require_permission(Permission.MACHINE_VIEW)],
)
async def get_machine(
    machine_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[MachineRead]:
    return ApiResponse[MachineRead].ok(
        await MachineService(session, tenant).get_machine(machine_id)
    )


@router.patch(
    "/machines/{machine_id}",
    response_model=ApiResponse[MachineRead],
    summary="Update a machine",
    dependencies=[require_permission(Permission.MACHINE_MANAGE)],
)
async def update_machine(
    machine_id: uuid.UUID, payload: MachineUpdate, session: DbSession, tenant: Tenant
) -> ApiResponse[MachineRead]:
    updated = await MachineService(session, tenant).update_machine(machine_id, payload)
    return ApiResponse[MachineRead].ok(updated, "Machine updated")


@router.post(
    "/machines/{machine_id}/status",
    response_model=ApiResponse[MachineRead],
    summary="Change a machine's availability",
    description=(
        "Moves the machine through its availability state machine. Completing "
        "maintenance stamps last_maintenance_date automatically."
    ),
    dependencies=[require_permission(Permission.MACHINE_MANAGE)],
)
async def change_machine_status(
    machine_id: uuid.UUID,
    payload: MachineStatusChange,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[MachineRead]:
    updated = await MachineService(session, tenant).change_status(machine_id, payload)
    return ApiResponse[MachineRead].ok(updated, f"Machine is now {updated.status}")


@router.delete(
    "/machines/{machine_id}",
    response_model=ApiResponse[None],
    summary="Retire a machine",
    description="Refused while a draft or approved routing operation still names it.",
    dependencies=[require_permission(Permission.MACHINE_MANAGE)],
)
async def retire_machine(
    machine_id: uuid.UUID, session: DbSession, tenant: Tenant, reason: ReasonQuery
) -> ApiResponse[None]:
    await MachineService(session, tenant).retire_machine(machine_id, reason=reason)
    return ApiResponse[None].ok(None, "Machine retired")


# -----------------------------------------------------------------------------
# Item-scoped reads
# -----------------------------------------------------------------------------


@router.get(
    "/items/{item_id}/versions",
    response_model=PaginatedResponse[RoutingSummary],
    summary="Every version of one item's routing",
    dependencies=[require_permission(Permission.ROUTING_VIEW)],
)
async def list_item_versions(
    item_id: uuid.UUID, session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[RoutingSummary]:
    rows, pagination = await RoutingService(session, tenant).list_versions(item_id, params)
    return PaginatedResponse[RoutingSummary](data=rows, pagination=pagination)


@router.get(
    "/items/{item_id}/lead-time",
    response_model=ApiResponse[LeadTimeRead],
    summary="Manufacture lead time for an item",
    description=(
        "Resolves the effective approved routing for the item and returns the whole "
        "days of shop time the quantity needs. Answers with routing_id=null when the "
        "item has no effective routing, so the caller can fall back to the item's own "
        "lead_time_days instead of assuming zero."
    ),
    dependencies=[require_permission(Permission.ROUTING_VIEW)],
)
async def item_lead_time(
    item_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    quantity: QuantityQuery = Decimal("1"),
    as_of: Annotated[
        date | None, Query(description="Effectivity date; defaults to today (UTC)")
    ] = None,
) -> ApiResponse[LeadTimeRead]:
    result = await RoutingService(session, tenant).item_lead_time(item_id, quantity, as_of=as_of)
    return ApiResponse[LeadTimeRead].ok(result)


# -----------------------------------------------------------------------------
# Routings
# -----------------------------------------------------------------------------


@router.get(
    "",
    response_model=PaginatedResponse[RoutingSummary],
    summary="List routings",
    dependencies=[require_permission(Permission.ROUTING_VIEW)],
)
async def list_routings(
    session: DbSession,
    tenant: Tenant,
    filters: RoutingFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[RoutingSummary]:
    rows, pagination = await RoutingService(session, tenant).list_routings(filters, params, sort)
    return PaginatedResponse[RoutingSummary](data=rows, pagination=pagination)


@router.post(
    "",
    response_model=ApiResponse[RoutingDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Create a draft routing",
    dependencies=[require_permission(Permission.ROUTING_MANAGE)],
)
async def create_routing(
    payload: RoutingCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[RoutingDetail]:
    created = await RoutingService(session, tenant).create_routing(payload)
    return ApiResponse[RoutingDetail].ok(created, "Routing created")


@router.get(
    "/{routing_id}",
    response_model=ApiResponse[RoutingDetail],
    summary="Fetch one routing with its operations",
    dependencies=[require_permission(Permission.ROUTING_VIEW)],
)
async def get_routing(
    routing_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[RoutingDetail]:
    return ApiResponse[RoutingDetail].ok(
        await RoutingService(session, tenant).get_routing_detail(routing_id)
    )


@router.patch(
    "/{routing_id}",
    response_model=ApiResponse[RoutingDetail],
    summary="Update a draft routing",
    description=(
        "Only a DRAFT can be edited. Supplying `operations` replaces the whole step "
        "set; omitting it leaves the steps untouched."
    ),
    dependencies=[require_permission(Permission.ROUTING_MANAGE)],
)
async def update_routing(
    routing_id: uuid.UUID, payload: RoutingUpdate, session: DbSession, tenant: Tenant
) -> ApiResponse[RoutingDetail]:
    updated = await RoutingService(session, tenant).update_routing(routing_id, payload)
    return ApiResponse[RoutingDetail].ok(updated, "Routing updated")


@router.delete(
    "/{routing_id}",
    response_model=ApiResponse[None],
    summary="Delete a draft routing",
    dependencies=[require_permission(Permission.ROUTING_MANAGE)],
)
async def delete_routing(
    routing_id: uuid.UUID, session: DbSession, tenant: Tenant, reason: ReasonQuery
) -> ApiResponse[None]:
    await RoutingService(session, tenant).delete_draft_routing(routing_id, reason=reason)
    return ApiResponse[None].ok(None, "Draft routing deleted")


@router.get(
    "/{routing_id}/operations",
    response_model=ApiResponse[list[RoutingOperationRead]],
    summary="The steps of one routing, in execution order",
    description=(
        "Returned whole rather than paginated: the set is bounded at write time and a "
        "process is only meaningful complete."
    ),
    dependencies=[require_permission(Permission.ROUTING_VIEW)],
)
async def list_routing_operations(
    routing_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[list[RoutingOperationRead]]:
    return ApiResponse[list[RoutingOperationRead]].ok(
        await RoutingService(session, tenant).list_operations(routing_id)
    )


@router.get(
    "/{routing_id}/cost",
    response_model=ApiResponse[RoutingCostRead],
    summary="Costed routing and the lead time it implies",
    description=(
        "Recomputed live and never written back, so opening a costing screen cannot "
        "mutate an approved routing. Queue and move time are reported as elapsed "
        "minutes but are not charged: nobody is working on the part while it waits."
    ),
    dependencies=[require_permission(Permission.ROUTING_VIEW)],
)
async def routing_cost(
    routing_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    quantity: QuantityQuery = Decimal("1"),
) -> ApiResponse[RoutingCostRead]:
    return ApiResponse[RoutingCostRead].ok(
        await RoutingService(session, tenant).routing_cost(routing_id, quantity)
    )


@router.post(
    "/{routing_id}/approve",
    response_model=ApiResponse[RoutingSummary],
    summary="Approve a routing",
    description=(
        "Releases the process to planning and production. The approver may not be the "
        "person who created it unless they hold SUPER_ADMIN."
    ),
    dependencies=[require_permission(Permission.ROUTING_MANAGE)],
)
async def approve_routing(
    routing_id: uuid.UUID,
    payload: RoutingStatusChange,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[RoutingSummary]:
    approved = await RoutingService(session, tenant).approve_routing(
        principal, routing_id, reason=payload.reason
    )
    return ApiResponse[RoutingSummary].ok(approved, "Routing approved")


@router.post(
    "/{routing_id}/obsolete",
    response_model=ApiResponse[RoutingSummary],
    summary="Retire a routing",
    dependencies=[require_permission(Permission.ROUTING_MANAGE)],
)
async def obsolete_routing(
    routing_id: uuid.UUID,
    payload: RoutingStatusChange,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[RoutingSummary]:
    retired = await RoutingService(session, tenant).obsolete_routing(
        routing_id, reason=payload.reason
    )
    return ApiResponse[RoutingSummary].ok(retired, "Routing retired")


@router.post(
    "/{routing_id}/copy-as-new-version",
    response_model=ApiResponse[RoutingDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Create the next draft version from this routing",
    dependencies=[require_permission(Permission.ROUTING_MANAGE)],
)
async def copy_as_new_version(
    routing_id: uuid.UUID,
    payload: CopyAsNewVersionRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[RoutingDetail]:
    copy = await RoutingService(session, tenant).copy_as_new_version(routing_id, payload)
    return ApiResponse[RoutingDetail].ok(copy, f"Created version {copy.version}")


__all__ = ["router"]
