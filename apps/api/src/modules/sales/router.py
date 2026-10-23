"""HTTP surface for sales orders.

Handlers bind parameters, call one service function and wrap the result in the
standard envelope. No rule, query or audit write lives here (master rules
section 74).

Route ordering note: ``/demand-summary`` is declared before ``/{order_id}``.
FastAPI matches in declaration order, and a literal segment declared after a
path parameter of the same shape would be captured by it and rejected as a
malformed UUID.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from src.core.deps import CurrentUser, DbSession, Tenant, require_permission
from src.core.messages import success_message
from src.core.pagination import PageParams, SortParams, bind_query_filters, page_params, sort_params
from src.core.permissions import Permission
from src.core.responses import COMMON_ERROR_RESPONSES, ApiResponse, PaginatedResponse
from src.modules.sales import fulfilment as fulfilment_service
from src.modules.sales import service
from src.modules.sales.models import SalesOrderStatus
from src.modules.sales.schemas import (
    AllocateOrderRequest,
    ApproveRequest,
    CancelRequest,
    ConfirmRequest,
    ConfirmResult,
    DemandSummaryFilters,
    DemandSummaryRow,
    FulfilmentRead,
    OrderAllocationRead,
    SalesOrderCreate,
    SalesOrderDetail,
    SalesOrderFilters,
    SalesOrderSummary,
    SalesOrderUpdate,
    SalesSettingsRead,
    SalesSettingsUpdate,
    ShortfallPlanRead,
)

router = APIRouter(prefix="/sales", tags=["Sales Orders"], responses=COMMON_ERROR_RESPONSES)

PageDep = Annotated[PageParams, Depends(page_params)]
SortDep = Annotated[SortParams, Depends(sort_params)]

# Deletion carries its reason in the query string: a DELETE body is legal but
# several HTTP clients and proxies drop it, and the reason is not optional
# (master rules section 83).
ReasonQuery = Annotated[
    str, Query(min_length=3, max_length=255, description="Why the draft is being deleted")
]


def order_filters(
    search: Annotated[str | None, Query(max_length=120, description="Order or customer PO")] = None,
    customer_id: Annotated[uuid.UUID | None, Query()] = None,
    source: Annotated[str | None, Query(description="ERP or DEALER_PORTAL")] = None,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    branch_id: Annotated[uuid.UUID | None, Query()] = None,
    order_status: Annotated[SalesOrderStatus | None, Query(alias="status")] = None,
    open_only: Annotated[
        bool, Query(description="Only orders that still represent unmet demand")
    ] = False,
    is_urgent: Annotated[bool | None, Query()] = None,
    required_from: Annotated[date | None, Query()] = None,
    required_to: Annotated[date | None, Query()] = None,
) -> SalesOrderFilters:
    return bind_query_filters(
        SalesOrderFilters,
        search=search,
        customer_id=customer_id,
        source=source,
        warehouse_id=warehouse_id,
        branch_id=branch_id,
        status=order_status,
        open_only=open_only,
        is_urgent=is_urgent,
        required_from=required_from,
        required_to=required_to,
    )


def demand_filters(
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    item_id: Annotated[uuid.UUID | None, Query()] = None,
    required_before: Annotated[
        date | None, Query(description="Only demand needed on or before this date")
    ] = None,
    shortfall_only: Annotated[bool, Query(description="Only items with unreserved demand")] = False,
) -> DemandSummaryFilters:
    return bind_query_filters(
        DemandSummaryFilters,
        warehouse_id=warehouse_id,
        item_id=item_id,
        required_before=required_before,
        shortfall_only=shortfall_only,
    )


OrderFilterDep = Annotated[SalesOrderFilters, Depends(order_filters)]
DemandFilterDep = Annotated[DemandSummaryFilters, Depends(demand_filters)]


# -----------------------------------------------------------------------------
# Planning feed (declared first - see the module docstring)
# -----------------------------------------------------------------------------


@router.get(
    "/demand-summary",
    response_model=PaginatedResponse[DemandSummaryRow],
    summary="Open sales demand by item",
    description=(
        "Aggregates every open order line into one row per item: what is still owed, "
        "what is already reserved, and the shortfall MRP has to cover. One grouped "
        "query - this is read on every planning run and every dashboard load."
    ),
    dependencies=[require_permission(Permission.SALES_ORDER_VIEW)],
)
async def demand_summary(
    session: DbSession, tenant: Tenant, filters: DemandFilterDep, params: PageDep
) -> PaginatedResponse[DemandSummaryRow]:
    rows, pagination = await service.demand_summary(session, tenant, filters, params)
    return PaginatedResponse[DemandSummaryRow](data=rows, pagination=pagination)


# -----------------------------------------------------------------------------
# Settings (declared before /{order_id} - the path would otherwise shadow it)
# -----------------------------------------------------------------------------


@router.get(
    "/settings",
    response_model=ApiResponse[SalesSettingsRead],
    summary="Read the company's sales settings",
    description=(
        "The effective settings, with code defaults applied. A company that has never "
        "saved a setting reads the default and is told so - the row is sparse, not a "
        "hard-coded copy of today's defaults."
    ),
    dependencies=[require_permission(Permission.SETTINGS_VIEW)],
)
async def get_sales_settings(session: DbSession, tenant: Tenant) -> ApiResponse[SalesSettingsRead]:
    return ApiResponse[SalesSettingsRead].ok(await service.get_sales_settings(session, tenant))


@router.put(
    "/settings",
    response_model=ApiResponse[SalesSettingsRead],
    summary="Change the company's sales settings",
    description=(
        "Partial by design: omit a field to leave the stored decision alone, send an "
        "explicit null to restore the code default. Every change is audited."
    ),
    dependencies=[require_permission(Permission.SETTINGS_MANAGE)],
)
async def update_sales_settings(
    payload: SalesSettingsUpdate, session: DbSession, tenant: Tenant
) -> ApiResponse[SalesSettingsRead]:
    settings = await service.update_sales_settings(session, tenant, payload)
    return ApiResponse[SalesSettingsRead].ok(settings, success_message("Sales settings", "updated"))


# -----------------------------------------------------------------------------
# CRUD
# -----------------------------------------------------------------------------


@router.get(
    "",
    response_model=PaginatedResponse[SalesOrderSummary],
    summary="List sales orders",
    dependencies=[require_permission(Permission.SALES_ORDER_VIEW)],
)
async def list_orders(
    session: DbSession,
    tenant: Tenant,
    filters: OrderFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[SalesOrderSummary]:
    rows, pagination = await service.list_orders(session, tenant, filters, params, sort)
    return PaginatedResponse[SalesOrderSummary](data=rows, pagination=pagination)


@router.post(
    "",
    response_model=ApiResponse[SalesOrderDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Create a draft sales order",
    description=(
        "Prices every line from the customer's price list unless an authorised override "
        "is supplied, applies the GST treatment for the place of supply, and stores the "
        "result. No stock is reserved until the order is confirmed."
    ),
    dependencies=[require_permission(Permission.SALES_ORDER_CREATE)],
)
async def create_order(
    payload: SalesOrderCreate, session: DbSession, tenant: Tenant, principal: CurrentUser
) -> ApiResponse[SalesOrderDetail]:
    order, lines = await service.create_order(session, tenant, principal, payload)
    return ApiResponse[SalesOrderDetail].ok(
        service.order_detail(order, lines), success_message(f"Sales order {order.code}", "created")
    )


@router.get(
    "/{order_id}",
    response_model=ApiResponse[SalesOrderDetail],
    summary="Fetch one sales order with its lines",
    dependencies=[require_permission(Permission.SALES_ORDER_VIEW)],
)
async def get_order(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[SalesOrderDetail]:
    return ApiResponse[SalesOrderDetail].ok(
        await service.get_order_detail(session, tenant, order_id)
    )


@router.patch(
    "/{order_id}",
    response_model=ApiResponse[SalesOrderDetail],
    summary="Update a sales order",
    description=(
        "A draft is re-priced from scratch. A confirmed order accepts quantity and "
        "scheduling changes only, and each quantity change is pushed through to the "
        "stock reservation the line holds."
    ),
    dependencies=[require_permission(Permission.SALES_ORDER_UPDATE)],
)
async def update_order(
    order_id: uuid.UUID,
    payload: SalesOrderUpdate,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[SalesOrderDetail]:
    order, lines = await service.update_order(session, tenant, principal, order_id, payload)
    return ApiResponse[SalesOrderDetail].ok(
        service.order_detail(order, lines), success_message(f"Sales order {order.code}", "updated")
    )


@router.delete(
    "/{order_id}",
    response_model=ApiResponse[None],
    summary="Delete a draft sales order",
    description="Only a DRAFT may be deleted. Anything further along is cancelled instead.",
    dependencies=[require_permission(Permission.SALES_ORDER_CANCEL)],
)
async def delete_order(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant, reason: ReasonQuery
) -> ApiResponse[None]:
    await service.delete_draft_order(session, tenant, order_id, reason=reason)
    return ApiResponse[None].ok(None, success_message("Draft sales order", "deleted"))


# -----------------------------------------------------------------------------
# Lifecycle
# -----------------------------------------------------------------------------


@router.post(
    "/{order_id}/confirm",
    response_model=ApiResponse[ConfirmResult],
    summary="Confirm an order and reserve stock",
    description=(
        "Runs the credit check, then reserves what is actually available for each line. "
        "Reserving less than was ordered is the expected outcome when stock is short: "
        "the response reports the per-line shortfall so it is visible immediately, and "
        "MRP plans for it."
    ),
    dependencies=[require_permission(Permission.SALES_ORDER_UPDATE)],
)
async def confirm_order(
    order_id: uuid.UUID,
    payload: ConfirmRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[ConfirmResult]:
    result = await service.confirm_order(
        session, tenant, principal, order_id, reason=payload.reason
    )
    message = (
        "Order confirmed and fully allocated"
        if result.allocation.fully_allocated
        else f"Order confirmed; {result.allocation.total_shortfall_lines} line(s) short of stock"
    )
    return ApiResponse[ConfirmResult].ok(result, message)


@router.post(
    "/{order_id}/cancel",
    response_model=ApiResponse[SalesOrderSummary],
    summary="Cancel an order and release its reservations",
    dependencies=[require_permission(Permission.SALES_ORDER_CANCEL)],
)
async def cancel_order(
    order_id: uuid.UUID, payload: CancelRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[SalesOrderSummary]:
    order = await service.cancel_order(session, tenant, order_id, reason=payload.reason)
    return ApiResponse[SalesOrderSummary].ok(
        SalesOrderSummary.model_validate(order),
        success_message(f"Sales order {order.code}", "cancelled"),
    )


@router.post(
    "/{order_id}/approve",
    response_model=ApiResponse[SalesOrderSummary],
    summary="Approve a below-cost order so it can be confirmed",
    description=(
        "Required when a line was priced below the item's cost. The approver may not be "
        "the person who raised the order unless they hold SUPER_ADMIN."
    ),
    dependencies=[require_permission(Permission.SALES_ORDER_APPROVE)],
)
async def approve_order(
    order_id: uuid.UUID,
    payload: ApproveRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[SalesOrderSummary]:
    order = await service.approve_order(session, tenant, principal, order_id, reason=payload.reason)
    return ApiResponse[SalesOrderSummary].ok(
        SalesOrderSummary.model_validate(order),
        success_message(f"Sales order {order.code}", "approved"),
    )


@router.post(
    "/{order_id}/allocate",
    response_model=ApiResponse[OrderAllocationRead],
    summary="Allocate newly available stock to a short order",
    description=(
        "The allocation top-up. Reserves stock that has become free at the order's "
        "warehouse for each line still short, in line order, and moves a CONFIRMED order "
        "to ALLOCATED once every open line is covered. The scheduled sweep "
        "`maintenance.allocate_short_orders` runs the same service for every short order. "
        "Allowed on CONFIRMED and PARTIALLY_DISPATCHED orders."
    ),
    dependencies=[require_permission(Permission.SALES_ORDER_UPDATE)],
)
async def allocate_order(
    order_id: uuid.UUID, payload: AllocateOrderRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[OrderAllocationRead]:
    view = await service.allocate_order(session, tenant, order_id, reason=payload.reason)
    message = (
        "Every line is now fully reserved"
        if view.fully_allocated
        else f"{view.total_shortfall_lines} line(s) still short of stock"
    )
    return ApiResponse[OrderAllocationRead].ok(view, message)


@router.get(
    "/{order_id}/allocation-status",
    response_model=ApiResponse[OrderAllocationRead],
    summary="Per-line reserved, dispatchable-now and shortfall",
    description=(
        "What each line's reservation still holds live, how much a new consignment may "
        "carry for it right now (the live reservation, less what other open consignments "
        "already carry), what it is still short of, and how much free stock there is at "
        "the order's warehouse."
    ),
    dependencies=[require_permission(Permission.SALES_ORDER_VIEW)],
)
async def allocation_status(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[OrderAllocationRead]:
    return ApiResponse[OrderAllocationRead].ok(
        await service.allocation_status(session, tenant, order_id)
    )


@router.post(
    "/{order_id}/plan-shortfall",
    response_model=ApiResponse[ShortfallPlanRead],
    summary="Plan the order's shortfall into draft supply",
    description=(
        "The on-demand form of the automatic plan: raises or resizes DRAFT production "
        "orders and the order's draft purchase requisition so they cover what is still "
        "unreserved. Never releases, approves or converts anything. Works whether or not "
        "the company has automatic planning switched on."
    ),
    dependencies=[require_permission(Permission.SALES_ORDER_UPDATE)],
)
async def plan_shortfall(
    order_id: uuid.UUID,
    payload: ConfirmRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[ShortfallPlanRead]:
    plan = await service.plan_shortfall(session, tenant, order_id, reason=payload.reason)
    documents = len({(document.kind, document.id) for document in plan.documents})
    if documents:
        message = f"{documents} draft document(s) now cover the shortfall"
    elif plan.needs_job_work:
        message = "The shortfall needs job work; no draft was raised"
    else:
        message = "Nothing is short at the order's warehouse"
    return ApiResponse[ShortfallPlanRead].ok(plan, message)


@router.get(
    "/{order_id}/fulfilment",
    response_model=ApiResponse[FulfilmentRead],
    summary="The order's fulfilment trail",
    description=(
        "Everything raised or recorded against the order, one batched read per document "
        "family: reservations, MRP suggestions, production and job-work orders, "
        "requisitions and purchase orders, inspections, packing, dispatches, transfers, "
        "invoices, credit notes and returns."
    ),
    dependencies=[require_permission(Permission.SALES_ORDER_VIEW)],
)
async def fulfilment(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[FulfilmentRead]:
    return ApiResponse[FulfilmentRead].ok(
        await fulfilment_service.order_fulfilment(session, tenant, order_id)
    )


__all__ = ["router"]


@router.post(
    "/{order_id}/close",
    response_model=ApiResponse[SalesOrderSummary],
    summary="Close an invoiced sales order",
    dependencies=[require_permission(Permission.SALES_ORDER_UPDATE)],
)
async def close_order(
    order_id: uuid.UUID, payload: ConfirmRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[SalesOrderSummary]:
    order = await service.close_order(session, tenant, order_id, reason=payload.reason)
    return ApiResponse[SalesOrderSummary].ok(
        SalesOrderSummary.model_validate(order), success_message("Sales order", "closed")
    )
