"""HTTP surface for production.

Handlers bind parameters, call one service function and wrap the result in the
standard envelope. Every rule - the state machine, the explosion, the
reservations, the stock movements, the audit writes - lives behind the service
(master rules section 74).

Four deliberate shapes:

* **Release answers 200 with a shortage list, not 422.** A job whose material is
  short is still released; the response says which components could not be held.
  Failing the request would leave the planner with no job, no reservations and no
  list of what to chase.

* **There is no endpoint that marks an order MATERIAL_ISSUED.** That status is
  reached by posting the issue that completes the kit, or by an explicit
  ``allow_partial_issue`` on the posting - which needs the approval right and
  records the shortfall it waved through. A general-purpose status endpoint
  would be a way around both.

* **A posted issue cannot be edited or deleted.** Stock has moved; the correction
  is a material return, which posts the offsetting movement. Only a draft can be
  cancelled.

* **Work in progress is two endpoints, not one.** "Which jobs are running?" and
  "how loaded is each cell?" have different row shapes and different page sizes,
  and one paginated response cannot honestly carry both.

Route ordering: literal segments are declared before any ``/{id}`` route.
FastAPI matches in declaration order, so the reverse would capture ``wip`` as an
order id and fail on the UUID parse.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from src.core.deps import (
    CurrentUser,
    DbSession,
    Tenant,
    require_any_permission,
    require_permission,
)
from src.core.messages import success_message
from src.core.pagination import PageParams, SortParams, bind_query_filters, page_params, sort_params
from src.core.permissions import Permission
from src.core.responses import COMMON_ERROR_RESPONSES, ApiResponse, PaginatedResponse
from src.modules.production import service
from src.modules.production.models import (
    MaterialIssueStatus,
    MaterialIssueType,
    ProductionOrderStatus,
    ProductionSourceType,
    WorkOrderStatus,
)
from src.modules.production.schemas import (
    ApproveRequest,
    CancelRequest,
    CapacityFilters,
    CloseRequest,
    CloseResult,
    CompleteRequest,
    CompleteResult,
    ComponentRead,
    MaterialIssueCreate,
    MaterialIssueDetail,
    MaterialIssueFilters,
    MaterialIssuePostRequest,
    MaterialIssuePostResult,
    MaterialIssueRead,
    ProductionCapacityDay,
    ProductionCostRead,
    ProductionEntryCreate,
    ProductionEntryFilters,
    ProductionEntryRead,
    ProductionEntryResult,
    ProductionOrderCreate,
    ProductionOrderDetail,
    ProductionOrderFilters,
    ProductionOrderRead,
    ProductionOrderSummary,
    ProductionOrderUpdate,
    ReleaseRequest,
    ReleaseResult,
    StartRequest,
    WipFilters,
    WipOrderRow,
    WipWorkCentreRow,
    WorkOrderFilters,
    WorkOrderRead,
    WorkOrderTransitionRequest,
    WorkOrderUpdate,
)

router = APIRouter(prefix="/production", tags=["Production"], responses=COMMON_ERROR_RESPONSES)

PageDep = Annotated[PageParams, Depends(page_params)]
SortDep = Annotated[SortParams, Depends(sort_params)]

# Deletion carries its reason in the query string: a DELETE body is legal but
# several HTTP clients and proxies drop it, and the reason is not optional
# (master rules section 83).
ReasonQuery = Annotated[
    str, Query(min_length=3, max_length=255, description="Why the draft is being deleted")
]


# -----------------------------------------------------------------------------
# Filter dependencies
# -----------------------------------------------------------------------------


def order_filters(
    status_filter: Annotated[ProductionOrderStatus | None, Query(alias="status")] = None,
    open_only: Annotated[
        bool, Query(description="Only jobs released, issued or in process")
    ] = False,
    item_id: Annotated[uuid.UUID | None, Query()] = None,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    plant_id: Annotated[uuid.UUID | None, Query()] = None,
    source_type: Annotated[ProductionSourceType | None, Query()] = None,
    source_id: Annotated[uuid.UUID | None, Query()] = None,
    planned_from: Annotated[date | None, Query()] = None,
    planned_to: Annotated[date | None, Query()] = None,
    max_priority: Annotated[
        int | None, Query(ge=0, le=9999, description="Lower is more urgent")
    ] = None,
    search: Annotated[str | None, Query(max_length=64, description="Order code")] = None,
) -> ProductionOrderFilters:
    return bind_query_filters(
        ProductionOrderFilters,
        status=status_filter,
        open_only=open_only,
        item_id=item_id,
        warehouse_id=warehouse_id,
        plant_id=plant_id,
        source_type=source_type,
        source_id=source_id,
        planned_from=planned_from,
        planned_to=planned_to,
        max_priority=max_priority,
        search=search,
    )


def issue_filters(
    production_order_id: Annotated[uuid.UUID | None, Query()] = None,
    issue_type: Annotated[MaterialIssueType | None, Query()] = None,
    status_filter: Annotated[MaterialIssueStatus | None, Query(alias="status")] = None,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    issued_from: Annotated[date | None, Query()] = None,
    issued_to: Annotated[date | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=64, description="Document code")] = None,
) -> MaterialIssueFilters:
    return bind_query_filters(
        MaterialIssueFilters,
        production_order_id=production_order_id,
        issue_type=issue_type,
        status=status_filter,
        warehouse_id=warehouse_id,
        issued_from=issued_from,
        issued_to=issued_to,
        search=search,
    )


def entry_filters(
    production_order_id: Annotated[uuid.UUID | None, Query()] = None,
    work_order_id: Annotated[uuid.UUID | None, Query()] = None,
    operator_user_id: Annotated[uuid.UUID | None, Query()] = None,
    machine_id: Annotated[uuid.UUID | None, Query()] = None,
    shift: Annotated[str | None, Query(max_length=16)] = None,
    shift_id: Annotated[uuid.UUID | None, Query()] = None,
    entry_from: Annotated[date | None, Query()] = None,
    entry_to: Annotated[date | None, Query()] = None,
    output_only: Annotated[
        bool, Query(description="Only bookings that received finished goods")
    ] = False,
) -> ProductionEntryFilters:
    return bind_query_filters(
        ProductionEntryFilters,
        production_order_id=production_order_id,
        work_order_id=work_order_id,
        operator_user_id=operator_user_id,
        machine_id=machine_id,
        shift=shift,
        shift_id=shift_id,
        entry_from=entry_from,
        entry_to=entry_to,
        output_only=output_only,
    )


def work_order_filters(
    production_order_id: Annotated[uuid.UUID | None, Query()] = None,
    status_filter: Annotated[WorkOrderStatus | None, Query(alias="status")] = None,
    work_center_id: Annotated[uuid.UUID | None, Query()] = None,
    machine_id: Annotated[uuid.UUID | None, Query()] = None,
    operator_user_id: Annotated[uuid.UUID | None, Query()] = None,
    shift_id: Annotated[uuid.UUID | None, Query()] = None,
    shift: Annotated[str | None, Query(max_length=16)] = None,
    open_only: Annotated[bool, Query()] = False,
    planned_from: Annotated[
        date | None, Query(description="Only operations planned on or after this date")
    ] = None,
    planned_to: Annotated[
        date | None, Query(description="Only operations planned on or before this date")
    ] = None,
) -> WorkOrderFilters:
    return bind_query_filters(
        WorkOrderFilters,
        production_order_id=production_order_id,
        status=status_filter,
        work_center_id=work_center_id,
        machine_id=machine_id,
        operator_user_id=operator_user_id,
        shift_id=shift_id,
        shift=shift,
        open_only=open_only,
        planned_from=planned_from,
        planned_to=planned_to,
    )


def wip_filters(
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    plant_id: Annotated[uuid.UUID | None, Query()] = None,
    item_id: Annotated[uuid.UUID | None, Query()] = None,
    work_center_id: Annotated[uuid.UUID | None, Query()] = None,
    late_only: Annotated[
        bool, Query(description="Only jobs whose planned finish has passed")
    ] = False,
) -> WipFilters:
    return bind_query_filters(
        WipFilters,
        warehouse_id=warehouse_id,
        plant_id=plant_id,
        item_id=item_id,
        work_center_id=work_center_id,
        late_only=late_only,
    )


def capacity_filters(
    date_from: Annotated[date | None, Query(description="First date on the board")] = None,
    date_to: Annotated[date | None, Query(description="Last date on the board")] = None,
    work_center_id: Annotated[uuid.UUID | None, Query()] = None,
) -> CapacityFilters:
    return bind_query_filters(
        CapacityFilters,
        date_from=date_from,
        date_to=date_to,
        work_center_id=work_center_id,
    )


OrderFilterDep = Annotated[ProductionOrderFilters, Depends(order_filters)]
IssueFilterDep = Annotated[MaterialIssueFilters, Depends(issue_filters)]
EntryFilterDep = Annotated[ProductionEntryFilters, Depends(entry_filters)]
WorkOrderFilterDep = Annotated[WorkOrderFilters, Depends(work_order_filters)]
WipFilterDep = Annotated[WipFilters, Depends(wip_filters)]
CapacityFilterDep = Annotated[CapacityFilters, Depends(capacity_filters)]


# -----------------------------------------------------------------------------
# Work in progress - declared before /{order_id} so "wip" is not read as an id
# -----------------------------------------------------------------------------


@router.get(
    "/wip",
    response_model=PaginatedResponse[WipOrderRow],
    summary="Work in progress, by order",
    description=(
        "Every job that is released, issued or in process, most urgent first, with "
        "the balance still to make and how many of its operations are still open. "
        "WIP value is posted net material issue cost plus operation cost to date. "
        "`late_only` narrows it to jobs whose planned finish has already passed. "
        "Requires both production-order view and production-cost view permissions."
    ),
    dependencies=[
        require_permission(Permission.PRODUCTION_ORDER_VIEW),
        require_permission(Permission.PRODUCTION_COST_VIEW),
    ],
)
async def get_wip_by_order(
    session: DbSession, tenant: Tenant, filters: WipFilterDep, params: PageDep
) -> PaginatedResponse[WipOrderRow]:
    items, pagination = await service.wip_orders(session, tenant, filters, params)
    return PaginatedResponse[WipOrderRow](data=items, pagination=pagination)


@router.get(
    "/wip/work-centres",
    response_model=PaginatedResponse[WipWorkCentreRow],
    summary="Work in progress, by work centre",
    description=(
        "The same live work grouped by where it physically sits: how many operations "
        "are queued and running in each cell, and the chargeable minutes still to run "
        "at the planned rate. Busiest cell first, because the board is read to find "
        "the bottleneck."
    ),
    dependencies=[require_permission(Permission.WORK_ORDER_VIEW)],
)
async def get_wip_by_work_centre(
    session: DbSession, tenant: Tenant, filters: WipFilterDep, params: PageDep
) -> PaginatedResponse[WipWorkCentreRow]:
    items, pagination = await service.wip_work_centres(session, tenant, filters, params)
    return PaginatedResponse[WipWorkCentreRow](data=items, pagination=pagination)


@router.get(
    "/capacity",
    response_model=PaginatedResponse[ProductionCapacityDay],
    summary="Remaining planned load per work centre per day, against capacity",
    description=(
        "The planning board's heat strip: every work centre crossed with every "
        "date in the window, carrying the open operations' planned minutes "
        "sliced across the days they touch and the day's capacity beside it. "
        "Capacity is the rostered minutes when the roster plans that centre that "
        "day, otherwise the centre's standard working hours. Days the plan does "
        "not fit are flagged `over_capacity`. Defaults to four weeks from today; "
        "at most 62 days in one request."
    ),
    dependencies=[require_permission(Permission.PRODUCTION_ORDER_VIEW)],
)
async def get_production_capacity(
    session: DbSession, tenant: Tenant, filters: CapacityFilterDep, params: PageDep
) -> PaginatedResponse[ProductionCapacityDay]:
    items, pagination = await service.production_capacity(session, tenant, filters, params)
    return PaginatedResponse[ProductionCapacityDay](data=items, pagination=pagination)


# -----------------------------------------------------------------------------
# Material issues and returns - literal prefix, declared before /{order_id}
# -----------------------------------------------------------------------------


@router.get(
    "/material-issues",
    response_model=PaginatedResponse[MaterialIssueRead],
    summary="List material issues and returns",
    dependencies=[require_permission(Permission.PRODUCTION_ORDER_VIEW)],
)
async def list_material_issues(
    session: DbSession,
    tenant: Tenant,
    filters: IssueFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[MaterialIssueRead]:
    items, pagination = await service.list_material_issues(session, tenant, filters, params, sort)
    return PaginatedResponse[MaterialIssueRead](data=items, pagination=pagination)


@router.get(
    "/material-issues/{issue_id}",
    response_model=ApiResponse[MaterialIssueDetail],
    summary="One issue or return with its lines",
    dependencies=[require_permission(Permission.PRODUCTION_ORDER_VIEW)],
)
async def get_material_issue(
    issue_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[MaterialIssueDetail]:
    return ApiResponse[MaterialIssueDetail].ok(
        await service.get_material_issue_detail(session, tenant, issue_id)
    )


@router.post(
    "/material-issues/{issue_id}/post",
    response_model=ApiResponse[MaterialIssuePostResult],
    summary="Post an issue or return",
    description=(
        "Moves the stock. An issue draws down the job's reservations first and takes "
        "the balance from free stock; a return puts material back at the cost it left "
        "the store at. When the posting completes every mandatory component, the order "
        "advances to `MATERIAL_ISSUED` on its own. `allow_partial_issue` advances it "
        "anyway and requires `production_order.approve`.\n\n"
        "One endpoint posts both directions, so it admits either right here and the "
        "service then requires the one matching the document: `material_issue.create` "
        "to post an issue, `material_return.create` to post a return."
    ),
    dependencies=[
        require_any_permission(Permission.MATERIAL_ISSUE_CREATE, Permission.MATERIAL_RETURN_CREATE)
    ],
)
async def post_material_issue(
    issue_id: uuid.UUID,
    payload: MaterialIssuePostRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[MaterialIssuePostResult]:
    result = await service.post_material_issue(session, tenant, principal, issue_id, payload)
    return ApiResponse[MaterialIssuePostResult].ok(result, result.message)


@router.post(
    "/material-issues/{issue_id}/cancel",
    response_model=ApiResponse[MaterialIssueRead],
    summary="Cancel a draft issue or return",
    description=(
        "Only a draft may be cancelled. A posted document has moved stock and is "
        "corrected with an offsetting return, never by deletion."
    ),
    dependencies=[require_permission(Permission.MATERIAL_ISSUE_CREATE)],
)
async def cancel_material_issue(
    issue_id: uuid.UUID,
    payload: CancelRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[MaterialIssueRead]:
    issue = await service.cancel_material_issue(session, tenant, issue_id, payload.reason)
    return ApiResponse[MaterialIssueRead].ok(
        MaterialIssueRead.model_validate(issue), success_message(f"{issue.code}", "cancelled")
    )


# -----------------------------------------------------------------------------
# Production entries, company-wide - literal prefix, declared before /{order_id}
# -----------------------------------------------------------------------------


@router.get(
    "/entries",
    response_model=PaginatedResponse[ProductionEntryRead],
    summary="Bookings across all jobs",
    description=(
        "Every shop-floor booking in the company, newest first - the daily board "
        "behind the date-and-shift view. The same filters as the per-order "
        "listing; narrow by entry date and shift for one day's work."
    ),
    dependencies=[require_permission(Permission.PRODUCTION_ORDER_VIEW)],
)
async def list_all_production_entries(
    session: DbSession,
    tenant: Tenant,
    filters: EntryFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[ProductionEntryRead]:
    items, pagination = await service.list_production_entries(
        session, tenant, filters, params, sort
    )
    return PaginatedResponse[ProductionEntryRead](data=items, pagination=pagination)


# -----------------------------------------------------------------------------
# Work orders - literal prefix, declared before /{order_id}
# -----------------------------------------------------------------------------


@router.get(
    "/work-orders",
    response_model=PaginatedResponse[WorkOrderRead],
    summary="List work orders",
    description=(
        "The shop-floor queue. Filter by work centre, machine or operator to get one "
        "cell's or one person's list."
    ),
    dependencies=[require_permission(Permission.WORK_ORDER_VIEW)],
)
async def list_work_orders(
    session: DbSession,
    tenant: Tenant,
    filters: WorkOrderFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[WorkOrderRead]:
    items, pagination = await service.list_work_orders(session, tenant, filters, params, sort)
    return PaginatedResponse[WorkOrderRead](data=items, pagination=pagination)


@router.patch(
    "/work-orders/{work_order_id}",
    response_model=ApiResponse[WorkOrderRead],
    summary="Assign or reschedule an operation",
    description=(
        "Operator, machine, shift and planned times. Quantities are never set here - "
        "they only ever move through a production booking. The shift is chosen from the "
        "shift master by shift_id; its code is kept on the row as a snapshot. An operator "
        "the shift roster does not plan on that shift is accepted with a warning in the "
        "message - the roster is advisory. The proposed machine and window are "
        "re-validated too: a retired machine is refused, a machine in maintenance "
        "or breakdown warns, and a window overlapping another open operation on the "
        "same machine warns in the message - or is refused when machine-overlap "
        "blocking is configured."
    ),
    dependencies=[require_permission(Permission.WORK_ORDER_MANAGE)],
)
async def update_work_order(
    work_order_id: uuid.UUID,
    payload: WorkOrderUpdate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[WorkOrderRead]:
    work_order, message = await service.assign_work_order(session, tenant, work_order_id, payload)
    return ApiResponse[WorkOrderRead].ok(WorkOrderRead.model_validate(work_order), message)


@router.post(
    "/work-orders/{work_order_id}/status",
    response_model=ApiResponse[WorkOrderRead],
    summary="Start, complete or cancel an operation",
    dependencies=[require_permission(Permission.WORK_ORDER_MANAGE)],
)
async def transition_work_order(
    work_order_id: uuid.UUID,
    payload: WorkOrderTransitionRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[WorkOrderRead]:
    work_order = await service.transition_work_order(session, tenant, work_order_id, payload)
    return ApiResponse[WorkOrderRead].ok(
        WorkOrderRead.model_validate(work_order),
        f"{work_order.code} is now {work_order.status}",
    )


# -----------------------------------------------------------------------------
# Production orders
# -----------------------------------------------------------------------------


@router.post(
    "",
    response_model=ApiResponse[ProductionOrderRead],
    status_code=status.HTTP_201_CREATED,
    summary="Raise a production order",
    description=(
        "Creates a DRAFT job. Nothing is planned against stock or capacity until the "
        "order is approved and released. Leave `bom_id` and `routing_id` unset to take "
        "the item's default approved structure and process at release."
    ),
    dependencies=[require_permission(Permission.PRODUCTION_ORDER_CREATE)],
)
async def create_order(
    payload: ProductionOrderCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[ProductionOrderRead]:
    order = await service.create_order(session, tenant, payload)
    return ApiResponse[ProductionOrderRead].ok(
        ProductionOrderRead.model_validate(order),
        success_message(f"Production order {order.code}", "created"),
    )


@router.get(
    "",
    response_model=PaginatedResponse[ProductionOrderSummary],
    summary="List production orders",
    dependencies=[require_permission(Permission.PRODUCTION_ORDER_VIEW)],
)
async def list_orders(
    session: DbSession,
    tenant: Tenant,
    filters: OrderFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[ProductionOrderSummary]:
    items, pagination = await service.list_orders(session, tenant, filters, params, sort)
    return PaginatedResponse[ProductionOrderSummary](data=items, pagination=pagination)


@router.get(
    "/{order_id}",
    response_model=ApiResponse[ProductionOrderDetail],
    summary="One production order with its operations",
    description=(
        "Components are not inlined: a multi-level explosion is not bounded the way a "
        "routing is, so they have their own paginated endpoint."
    ),
    dependencies=[require_permission(Permission.PRODUCTION_ORDER_VIEW)],
)
async def get_order(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[ProductionOrderDetail]:
    return ApiResponse[ProductionOrderDetail].ok(
        await service.get_order_detail(session, tenant, order_id)
    )


@router.patch(
    "/{order_id}",
    response_model=ApiResponse[ProductionOrderRead],
    summary="Edit a draft production order",
    dependencies=[require_permission(Permission.PRODUCTION_ORDER_CREATE)],
)
async def update_order(
    order_id: uuid.UUID,
    payload: ProductionOrderUpdate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[ProductionOrderRead]:
    order = await service.update_order(session, tenant, order_id, payload)
    return ApiResponse[ProductionOrderRead].ok(
        ProductionOrderRead.model_validate(order), success_message(f"{order.code}", "updated")
    )


@router.delete(
    "/{order_id}",
    response_model=ApiResponse[None],
    summary="Delete a draft production order",
    description=(
        "Only a draft can be deleted, and only because nothing points at it yet. "
        "An approved or released job is cancelled, which leaves its history intact."
    ),
    dependencies=[require_permission(Permission.PRODUCTION_ORDER_CANCEL)],
)
async def delete_order(
    order_id: uuid.UUID, reason: ReasonQuery, session: DbSession, tenant: Tenant
) -> ApiResponse[None]:
    await service.delete_draft_order(session, tenant, order_id, reason=reason)
    return ApiResponse[None].ok(None, success_message("Draft production order", "deleted"))


# -----------------------------------------------------------------------------
# Lifecycle
# -----------------------------------------------------------------------------


@router.post(
    "/{order_id}/approve",
    response_model=ApiResponse[ProductionOrderRead],
    summary="Approve a production order",
    description=(
        "Approval is evidence of a second person's judgement, so the approver may not "
        "be the person who raised the order (`403 APPROVAL_REQUIRED`)."
    ),
    dependencies=[require_permission(Permission.PRODUCTION_ORDER_APPROVE)],
)
async def approve_order(
    order_id: uuid.UUID,
    payload: ApproveRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[ProductionOrderRead]:
    order = await service.approve_order(session, tenant, principal, order_id, payload)
    return ApiResponse[ProductionOrderRead].ok(
        ProductionOrderRead.model_validate(order), success_message(f"{order.code}", "approved")
    )


@router.post(
    "/{order_id}/release",
    response_model=ApiResponse[ReleaseResult],
    summary="Release a production order to the floor",
    description=(
        "Explodes the approved bill of materials, **freezes the result** on the order "
        "as its component snapshot, issues one work order per routing operation and "
        "reserves the material. A component that cannot be fully held is reported in "
        "`shortages` rather than failing the release: the planner needs the whole list "
        "of what is missing, and the stock that *is* there is held while they chase it."
    ),
    dependencies=[require_permission(Permission.PRODUCTION_ORDER_RELEASE)],
)
async def release_order(
    order_id: uuid.UUID,
    payload: ReleaseRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[ReleaseResult]:
    result = await service.release_order(session, tenant, order_id, payload)
    return ApiResponse[ReleaseResult].ok(result, result.message)


@router.post(
    "/{order_id}/start",
    response_model=ApiResponse[ProductionOrderRead],
    summary="Start production",
    description="Requires the material to have been issued. From here the job cannot be cancelled.",
    dependencies=[require_permission(Permission.WORK_ORDER_MANAGE)],
)
async def start_order(
    order_id: uuid.UUID,
    payload: StartRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[ProductionOrderRead]:
    order = await service.start_order(session, tenant, order_id, payload)
    return ApiResponse[ProductionOrderRead].ok(
        ProductionOrderRead.model_validate(order), success_message(f"{order.code}", "started")
    )


@router.post(
    "/{order_id}/complete",
    response_model=ApiResponse[CompleteResult],
    summary="Complete production",
    description=(
        "Books the final output when a quantity is given - which posts the "
        "`PRODUCTION_RECEIPT` into the output warehouse - backflushes any automatic "
        "components in proportion to what was actually made, and accounts the issued "
        "material as consumed. Consumption posts nothing: the stock left the store "
        "when it was issued."
    ),
    dependencies=[require_permission(Permission.PRODUCTION_ENTRY_CREATE)],
)
async def complete_order(
    order_id: uuid.UUID,
    payload: CompleteRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[CompleteResult]:
    result = await service.complete_order(session, tenant, order_id, payload)
    return ApiResponse[CompleteResult].ok(result, result.message)


@router.post(
    "/{order_id}/close",
    response_model=ApiResponse[CloseResult],
    summary="Close and cost a production order",
    description=(
        "Computes the actual cost - material at the value its movements carried, "
        "operations at the time they actually took - and **stores** it, so a later "
        "work-centre rate revision cannot move a closed job's cost. Anything the job "
        "was still holding is released back to free stock."
    ),
    dependencies=[require_permission(Permission.PRODUCTION_ORDER_CLOSE)],
)
async def close_order(
    order_id: uuid.UUID,
    payload: CloseRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[CloseResult]:
    result = await service.close_order(session, tenant, order_id, payload)
    return ApiResponse[CloseResult].ok(result, result.message)


@router.post(
    "/{order_id}/cancel",
    response_model=ApiResponse[ProductionOrderRead],
    summary="Cancel a production order",
    description=(
        "Possible only before the job is physically under way, and **refused while any "
        "material is still out with it**: that stock is on the floor, and cancelling "
        "around it would leave the ledger saying it was consumed by an order that no "
        "longer exists. Post a material return first."
    ),
    dependencies=[require_permission(Permission.PRODUCTION_ORDER_CANCEL)],
)
async def cancel_order(
    order_id: uuid.UUID,
    payload: CancelRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[ProductionOrderRead]:
    order = await service.cancel_order(session, tenant, order_id, payload)
    return ApiResponse[ProductionOrderRead].ok(
        ProductionOrderRead.model_validate(order), success_message(f"{order.code}", "cancelled")
    )


# -----------------------------------------------------------------------------
# Components
# -----------------------------------------------------------------------------


@router.get(
    "/{order_id}/components",
    response_model=PaginatedResponse[ComponentRead],
    summary="Component snapshot with live shortage",
    description=(
        "The exploded structure **as it stood when the job was released** - a bill of "
        "materials revised since then does not change what this order needs - together "
        "with what is issued, what is reserved for it and what is free right now. "
        "`shortage_only` narrows it to the parts that still have to be found."
    ),
    dependencies=[require_permission(Permission.PRODUCTION_ORDER_VIEW)],
)
async def list_components(
    order_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    sort: SortDep,
    search: Annotated[str | None, Query(max_length=64, description="Component item code")] = None,
    shortage_only: Annotated[bool, Query()] = False,
) -> PaginatedResponse[ComponentRead]:
    items, pagination = await service.list_components(
        session, tenant, order_id, params, sort, search=search, shortage_only=shortage_only
    )
    return PaginatedResponse[ComponentRead](data=items, pagination=pagination)


# -----------------------------------------------------------------------------
# Material issue and return, per order
# -----------------------------------------------------------------------------


@router.post(
    "/{order_id}/material-issues",
    response_model=ApiResponse[MaterialIssueDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Draft a material issue",
    description=(
        "Creates the picking document. Nothing moves until it is posted, which is what "
        "lets a storekeeper review the kit before the stock leaves the store."
    ),
    dependencies=[require_permission(Permission.MATERIAL_ISSUE_CREATE)],
)
async def create_material_issue(
    order_id: uuid.UUID,
    payload: MaterialIssueCreate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[MaterialIssueDetail]:
    issue, lines = await service.create_material_issue(
        session, tenant, order_id, payload, issue_type=MaterialIssueType.ISSUE
    )
    detail = service.issue_detail(issue, lines)
    return ApiResponse[MaterialIssueDetail].ok(
        detail, success_message(f"Material issue {issue.code}", "drafted")
    )


@router.post(
    "/{order_id}/material-returns",
    response_model=ApiResponse[MaterialIssueDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Draft a material return",
    description=(
        "Puts unused material back on the shelf when it is posted. A job cannot be "
        "cancelled while it is still holding material, so this is the document that "
        "unwinds a released order."
    ),
    dependencies=[require_permission(Permission.MATERIAL_RETURN_CREATE)],
)
async def create_material_return(
    order_id: uuid.UUID,
    payload: MaterialIssueCreate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[MaterialIssueDetail]:
    issue, lines = await service.create_material_issue(
        session, tenant, order_id, payload, issue_type=MaterialIssueType.RETURN
    )
    detail = service.issue_detail(issue, lines)
    return ApiResponse[MaterialIssueDetail].ok(
        detail, success_message(f"Material return {issue.code}", "drafted")
    )


# -----------------------------------------------------------------------------
# Production entries
# -----------------------------------------------------------------------------


@router.post(
    "/{order_id}/entries",
    response_model=ApiResponse[ProductionEntryResult],
    status_code=status.HTTP_201_CREATED,
    summary="Book a shift's production",
    description=(
        "A booking against the **last** operation of the routing - or against a job "
        "with no routing at all - posts the `PRODUCTION_RECEIPT` into the output "
        "warehouse. A booking at an intermediate operation moves that operation's "
        "completed quantity and receives no stock. Rejected output is counted but never "
        "received: it was never good stock."
    ),
    dependencies=[require_permission(Permission.PRODUCTION_ENTRY_CREATE)],
)
async def create_production_entry(
    order_id: uuid.UUID,
    payload: ProductionEntryCreate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[ProductionEntryResult]:
    result = await service.create_production_entry(session, tenant, order_id, payload)
    return ApiResponse[ProductionEntryResult].ok(result, result.message)


@router.get(
    "/{order_id}/entries",
    response_model=PaginatedResponse[ProductionEntryRead],
    summary="Bookings against one job",
    dependencies=[require_permission(Permission.PRODUCTION_ORDER_VIEW)],
)
async def list_production_entries(
    order_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    filters: EntryFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[ProductionEntryRead]:
    # The path segment is the authority on which job is being read; a query
    # parameter naming a different one would be ambiguous, so it is overwritten.
    scoped = filters.model_copy(update={"production_order_id": order_id})
    items, pagination = await service.list_production_entries(session, tenant, scoped, params, sort)
    return PaginatedResponse[ProductionEntryRead](data=items, pagination=pagination)


@router.get(
    "/{order_id}/work-orders",
    response_model=PaginatedResponse[WorkOrderRead],
    summary="Operations of one job",
    dependencies=[require_permission(Permission.WORK_ORDER_VIEW)],
)
async def list_order_work_orders(
    order_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    filters: WorkOrderFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[WorkOrderRead]:
    scoped = filters.model_copy(update={"production_order_id": order_id})
    items, pagination = await service.list_work_orders(session, tenant, scoped, params, sort)
    return PaginatedResponse[WorkOrderRead](data=items, pagination=pagination)


# -----------------------------------------------------------------------------
# Costing
# -----------------------------------------------------------------------------


@router.get(
    "/{order_id}/cost",
    response_model=ApiResponse[ProductionCostRead],
    summary="What this job has cost",
    description=(
        "Material at the value the movements actually carried, plus each operation at "
        "the time it actually took. A closed job is answered from the figures written "
        "at closure (`is_final`); anything else is computed live and will move as more "
        "material is issued and more time is booked."
    ),
    dependencies=[require_permission(Permission.PRODUCTION_COST_VIEW)],
)
async def get_production_cost(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[ProductionCostRead]:
    return ApiResponse[ProductionCostRead].ok(
        await service.production_cost(session, tenant, order_id)
    )


__all__ = ["router"]
