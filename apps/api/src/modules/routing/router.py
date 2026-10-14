"""HTTP surface for routings, work centres, machines, shifts and the shift roster.

Handlers bind parameters, call one service method and wrap the result in the
standard envelope. No rule, query or audit write lives here (master rules
section 74).

Route ordering note: every literal-prefixed route - ``/work-centers``,
``/machines``, ``/shifts``, ``/roster/...``, ``/items/...`` - is declared
**before** ``/{routing_id}``.
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

from src.core.deps import (
    CurrentUser,
    DbSession,
    Tenant,
    require_any_permission,
    require_permission,
)
from src.core.pagination import (
    PageParams,
    SortParams,
    bind_query_filters,
    page_params,
    sort_params,
)
from src.core.permissions import Permission
from src.core.responses import COMMON_ERROR_RESPONSES, ApiResponse, PaginatedResponse
from src.modules.routing.models import MachineStatus, RoutingStatus, ShiftAssignmentStatus
from src.modules.routing.schemas import (
    MAX_COSTING_QUANTITY,
    MAX_ROSTER_BULK,
    MAX_ROSTER_RANGE_DAYS,
    CopyAsNewVersionRequest,
    LeadTimeRead,
    MachineCreate,
    MachineFilters,
    MachineRead,
    MachineStatusChange,
    MachineUpdate,
    ReasonRequest,
    RosterOperatorRead,
    RoutingCostRead,
    RoutingCreate,
    RoutingDetail,
    RoutingFilters,
    RoutingOperationRead,
    RoutingStatusChange,
    RoutingSummary,
    RoutingUpdate,
    ShiftAssignmentBulkCreate,
    ShiftAssignmentCreate,
    ShiftAssignmentRead,
    ShiftAssignmentUpdate,
    ShiftCreate,
    ShiftFilters,
    ShiftRead,
    ShiftRosterCopyRequest,
    ShiftRosterFilters,
    ShiftUpdate,
    WorkCenterCreate,
    WorkCenterFilters,
    WorkCenterRead,
    WorkCenterUpdate,
)
from src.modules.routing.service import (
    MachineService,
    RoutingService,
    ShiftRosterService,
    ShiftService,
    WorkCenterService,
)

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


def shift_filters(
    search: Annotated[str | None, Query(max_length=120, description="Code or name")] = None,
    is_active: Annotated[bool | None, Query()] = None,
) -> ShiftFilters:
    return ShiftFilters(search=search, is_active=is_active)


def roster_filters(
    date_from: Annotated[date, Query(description="First roster date, inclusive")],
    date_to: Annotated[
        date,
        Query(
            description=(
                f"Last roster date, inclusive - at most {MAX_ROSTER_RANGE_DAYS} days "
                "counting both ends"
            )
        ),
    ],
    shift_id: Annotated[uuid.UUID | None, Query()] = None,
    work_center_id: Annotated[uuid.UUID | None, Query()] = None,
    machine_id: Annotated[uuid.UUID | None, Query()] = None,
    operator_user_id: Annotated[uuid.UUID | None, Query()] = None,
    assignment_status: Annotated[ShiftAssignmentStatus | None, Query(alias="status")] = None,
) -> ShiftRosterFilters:
    # A range the wrong way round, or too long, is answered 422 against the
    # query parameter rather than as a 500 from inside the dependency.
    return bind_query_filters(
        ShiftRosterFilters,
        date_from=date_from,
        date_to=date_to,
        shift_id=shift_id,
        work_center_id=work_center_id,
        machine_id=machine_id,
        operator_user_id=operator_user_id,
        status=assignment_status,
    )


WorkCenterFilterDep = Annotated[WorkCenterFilters, Depends(work_center_filters)]
MachineFilterDep = Annotated[MachineFilters, Depends(machine_filters)]
RoutingFilterDep = Annotated[RoutingFilters, Depends(routing_filters)]
ShiftFilterDep = Annotated[ShiftFilters, Depends(shift_filters)]
RosterFilterDep = Annotated[ShiftRosterFilters, Depends(roster_filters)]


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
# Shifts (declared before /{routing_id} - see the module docstring)
# -----------------------------------------------------------------------------


@router.get(
    "/shifts",
    response_model=PaginatedResponse[ShiftRead],
    summary="List shifts",
    description=(
        "Readable with any right that fills a shift picker - the shift register's own, "
        "the roster's, a work-order reader's or a booking clerk's - so production roles "
        "need no new grant to choose a shift."
    ),
    dependencies=[
        require_any_permission(
            Permission.SHIFT_VIEW,
            Permission.SHIFT_ROSTER_VIEW,
            Permission.WORK_ORDER_VIEW,
            Permission.PRODUCTION_ENTRY_CREATE,
        )
    ],
)
async def list_shifts(
    session: DbSession,
    tenant: Tenant,
    filters: ShiftFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[ShiftRead]:
    rows, pagination = await ShiftService(session, tenant).list_shifts(filters, params, sort)
    return PaginatedResponse[ShiftRead](data=rows, pagination=pagination)


@router.post(
    "/shifts",
    response_model=ApiResponse[ShiftRead],
    status_code=status.HTTP_201_CREATED,
    summary="Create a shift",
    description=(
        "Times are wall-clock times in the company timezone. Whether the shift crosses "
        "midnight is derived from them: an end at or before the start ends the next day."
    ),
    dependencies=[require_permission(Permission.SHIFT_MANAGE)],
)
async def create_shift(
    payload: ShiftCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[ShiftRead]:
    created = await ShiftService(session, tenant).create_shift(payload)
    return ApiResponse[ShiftRead].ok(created, "Shift created")


@router.get(
    "/shifts/{shift_id}",
    response_model=ApiResponse[ShiftRead],
    summary="Fetch one shift",
    dependencies=[
        require_any_permission(
            Permission.SHIFT_VIEW,
            Permission.SHIFT_ROSTER_VIEW,
            Permission.WORK_ORDER_VIEW,
            Permission.PRODUCTION_ENTRY_CREATE,
        )
    ],
)
async def get_shift(
    shift_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[ShiftRead]:
    return ApiResponse[ShiftRead].ok(await ShiftService(session, tenant).get_shift(shift_id))


@router.patch(
    "/shifts/{shift_id}",
    response_model=ApiResponse[ShiftRead],
    summary="Update a shift",
    description=(
        "The code is fixed once created. New times or a new break are refused while "
        "planned roster assignments from today onwards still use the shift."
    ),
    dependencies=[require_permission(Permission.SHIFT_MANAGE)],
)
async def update_shift(
    shift_id: uuid.UUID, payload: ShiftUpdate, session: DbSession, tenant: Tenant
) -> ApiResponse[ShiftRead]:
    updated = await ShiftService(session, tenant).update_shift(shift_id, payload)
    return ApiResponse[ShiftRead].ok(updated, "Shift updated")


@router.post(
    "/shifts/{shift_id}/activate",
    response_model=ApiResponse[ShiftRead],
    summary="Offer a shift on pickers again",
    dependencies=[require_permission(Permission.SHIFT_MANAGE)],
)
async def activate_shift(
    shift_id: uuid.UUID, payload: ReasonRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[ShiftRead]:
    updated = await ShiftService(session, tenant).set_active(
        shift_id, active=True, reason=payload.reason
    )
    return ApiResponse[ShiftRead].ok(updated, "Shift activated")


@router.post(
    "/shifts/{shift_id}/deactivate",
    response_model=ApiResponse[ShiftRead],
    summary="Stop offering a shift",
    description="Refused while planned roster assignments from today onwards use it.",
    dependencies=[require_permission(Permission.SHIFT_MANAGE)],
)
async def deactivate_shift(
    shift_id: uuid.UUID, payload: ReasonRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[ShiftRead]:
    updated = await ShiftService(session, tenant).set_active(
        shift_id, active=False, reason=payload.reason
    )
    return ApiResponse[ShiftRead].ok(updated, "Shift deactivated")


@router.delete(
    "/shifts/{shift_id}",
    response_model=ApiResponse[None],
    summary="Retire a shift",
    description=(
        "Refused while planned roster assignments from today onwards use it. Work orders "
        "and bookings keep the shift's code."
    ),
    dependencies=[require_permission(Permission.SHIFT_MANAGE)],
)
async def retire_shift(
    shift_id: uuid.UUID, session: DbSession, tenant: Tenant, reason: ReasonQuery
) -> ApiResponse[None]:
    await ShiftService(session, tenant).retire_shift(shift_id, reason=reason)
    return ApiResponse[None].ok(None, "Shift retired")


# -----------------------------------------------------------------------------
# The shift roster
# -----------------------------------------------------------------------------


@router.get(
    "/roster/assignments",
    response_model=PaginatedResponse[ShiftAssignmentRead],
    summary="List roster assignments",
    description=(
        f"A date range is required, at most {MAX_ROSTER_RANGE_DAYS} days with both ends "
        "included. Rows carry the shift, work centre, machine and operator labels, so "
        "the roster is readable without the user or machine registers."
    ),
    dependencies=[require_permission(Permission.SHIFT_ROSTER_VIEW)],
)
async def list_roster_assignments(
    session: DbSession,
    tenant: Tenant,
    filters: RosterFilterDep,
    params: PageDep,
) -> PaginatedResponse[ShiftAssignmentRead]:
    rows, pagination = await ShiftRosterService(session, tenant).list_assignments(filters, params)
    return PaginatedResponse[ShiftAssignmentRead](data=rows, pagination=pagination)


@router.post(
    "/roster/assignments",
    response_model=ApiResponse[ShiftAssignmentRead],
    status_code=status.HTTP_201_CREATED,
    summary="Plan an operator on a shift",
    description=(
        "Refused when the operator, or the named machine, already has a planned window "
        "that overlaps this one; every clash is listed in details.conflicts. A machine "
        "under maintenance or broken down is planned with a warning in the message."
    ),
    dependencies=[require_permission(Permission.SHIFT_ROSTER_MANAGE)],
)
async def create_roster_assignment(
    payload: ShiftAssignmentCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[ShiftAssignmentRead]:
    rows, message = await ShiftRosterService(session, tenant).plan([payload])
    return ApiResponse[ShiftAssignmentRead].ok(rows[0], message)


@router.post(
    "/roster/assignments/bulk",
    response_model=ApiResponse[list[ShiftAssignmentRead]],
    status_code=status.HTTP_201_CREATED,
    summary="Plan several assignments at once",
    description=(
        f"All or nothing, at most {MAX_ROSTER_BULK} rows. Overlaps inside the request "
        "are refused as well as overlaps with what is already planned."
    ),
    dependencies=[require_permission(Permission.SHIFT_ROSTER_MANAGE)],
)
async def bulk_create_roster_assignments(
    payload: ShiftAssignmentBulkCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[list[ShiftAssignmentRead]]:
    rows, message = await ShiftRosterService(session, tenant).plan(payload.assignments)
    return ApiResponse[list[ShiftAssignmentRead]].ok(rows, message)


@router.post(
    "/roster/assignments/copy",
    response_model=ApiResponse[list[ShiftAssignmentRead]],
    status_code=status.HTTP_201_CREATED,
    summary="Copy one day's plan onto a range of dates",
    description=(
        "Plans every PLANNED row of the source day again on each target date, all or "
        f"nothing and at most {MAX_ROSTER_BULK} rows; narrow by work centre to split a "
        "larger copy."
    ),
    dependencies=[require_permission(Permission.SHIFT_ROSTER_MANAGE)],
)
async def copy_roster_day(
    payload: ShiftRosterCopyRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[list[ShiftAssignmentRead]]:
    rows, message = await ShiftRosterService(session, tenant).copy_day(payload)
    return ApiResponse[list[ShiftAssignmentRead]].ok(rows, message)


@router.patch(
    "/roster/assignments/{assignment_id}",
    response_model=ApiResponse[ShiftAssignmentRead],
    summary="Re-staff a planned assignment",
    description=(
        "Another operator, work centre or machine, or new notes. The date and the shift "
        "are fixed - cancel and plan again to move the window. Re-validated like a new "
        "row; the row never conflicts with itself."
    ),
    dependencies=[require_permission(Permission.SHIFT_ROSTER_MANAGE)],
)
async def update_roster_assignment(
    assignment_id: uuid.UUID,
    payload: ShiftAssignmentUpdate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[ShiftAssignmentRead]:
    row, message = await ShiftRosterService(session, tenant).restaff(assignment_id, payload)
    return ApiResponse[ShiftAssignmentRead].ok(row, message)


@router.post(
    "/roster/assignments/{assignment_id}/cancel",
    response_model=ApiResponse[ShiftAssignmentRead],
    summary="Cancel a planned assignment",
    description="Irreversible. The row is kept, CANCELLED, with the reason in the audit trail.",
    dependencies=[require_permission(Permission.SHIFT_ROSTER_MANAGE)],
)
async def cancel_roster_assignment(
    assignment_id: uuid.UUID, payload: ReasonRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[ShiftAssignmentRead]:
    cancelled = await ShiftRosterService(session, tenant).cancel(
        assignment_id, reason=payload.reason
    )
    return ApiResponse[ShiftAssignmentRead].ok(cancelled, "Assignment cancelled")


@router.get(
    "/roster/operators",
    response_model=PaginatedResponse[RosterOperatorRead],
    summary="List the people who can be planned or booked",
    description=(
        "Active internal accounts only - no vendor-portal users - with the name and "
        "employee code a picker shows. Readable by roster readers, work-order managers "
        "and booking clerks, none of whom hold user.view."
    ),
    dependencies=[
        require_any_permission(
            Permission.SHIFT_ROSTER_VIEW,
            Permission.WORK_ORDER_MANAGE,
            Permission.PRODUCTION_ENTRY_CREATE,
        )
    ],
)
async def list_roster_operators(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    search: Annotated[
        str | None, Query(max_length=120, description="Name or employee code")
    ] = None,
) -> PaginatedResponse[RosterOperatorRead]:
    rows, pagination = await ShiftRosterService(session, tenant).list_operators(search, params)
    return PaginatedResponse[RosterOperatorRead](data=rows, pagination=pagination)


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
