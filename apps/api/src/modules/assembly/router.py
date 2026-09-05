"""HTTP surface for assembly orders.

Handlers bind parameters, call one service function and wrap the result in the
standard envelope. No rule, query or audit write lives here (master rules
section 74).

Route ordering note: ``/ready-for-packing`` is declared before ``/{order_id}``.
FastAPI matches in declaration order, and a literal segment declared after a
path parameter of the same shape would be captured by it and rejected as a
malformed UUID.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from src.core.deps import DbSession, Tenant, require_any_permission, require_permission
from src.core.pagination import PageParams, SortParams, page_params, sort_params
from src.core.permissions import Permission
from src.core.responses import COMMON_ERROR_RESPONSES, ApiResponse, PaginatedResponse
from src.modules.assembly import service
from src.modules.assembly.models import AssemblyOrderStatus
from src.modules.assembly.schemas import (
    AssemblyComponentRead,
    AssemblyOrderCreate,
    AssemblyOrderDetail,
    AssemblyOrderFilters,
    AssemblyOrderSummary,
    AssemblyOrderUpdate,
    CompleteRequest,
    CompletionResult,
    IssueComponentsRequest,
    PackingHandoverRequest,
    QcResultRequest,
    ReadyForPackingRow,
    ReasonRequest,
)

router = APIRouter(prefix="/assembly", tags=["Assembly"], responses=COMMON_ERROR_RESPONSES)

PageDep = Annotated[PageParams, Depends(page_params)]
SortDep = Annotated[SortParams, Depends(sort_params)]

# Deletion carries its reason in the query string: a DELETE body is legal but
# several HTTP clients and proxies drop it, and the reason is not optional
# (master rules section 83).
ReasonQuery = Annotated[
    str, Query(min_length=3, max_length=255, description="Why the draft is being deleted")
]


def order_filters(
    search: Annotated[str | None, Query(max_length=120, description="Order code or line")] = None,
    item_id: Annotated[uuid.UUID | None, Query()] = None,
    warehouse_id: Annotated[
        uuid.UUID | None, Query(description="Warehouse components are drawn from")
    ] = None,
    output_warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    order_status: Annotated[AssemblyOrderStatus | None, Query(alias="status")] = None,
    assembly_line: Annotated[str | None, Query(max_length=64)] = None,
    open_only: Annotated[
        bool, Query(description="Only orders that still owe finished stock")
    ] = False,
    requires_qc: Annotated[bool | None, Query()] = None,
    planned_from: Annotated[datetime | None, Query()] = None,
    planned_to: Annotated[datetime | None, Query()] = None,
) -> AssemblyOrderFilters:
    return AssemblyOrderFilters(
        search=search,
        item_id=item_id,
        warehouse_id=warehouse_id,
        output_warehouse_id=output_warehouse_id,
        status=order_status,
        assembly_line=assembly_line,
        open_only=open_only,
        requires_qc=requires_qc,
        planned_from=planned_from,
        planned_to=planned_to,
    )


OrderFilterDep = Annotated[AssemblyOrderFilters, Depends(order_filters)]


# -----------------------------------------------------------------------------
# Packing handover (declared first - see the module docstring)
# -----------------------------------------------------------------------------


@router.get(
    "/ready-for-packing",
    response_model=PaginatedResponse[ReadyForPackingRow],
    summary="Completed assemblies whose output is waiting to be packed",
    description=(
        "One statement: completed orders that no packing order has claimed, joined to the "
        "live availability of their output position. An assembly still on QC hold does not "
        "appear, and neither does one whose output has since been reserved elsewhere - "
        "availability is read from the balance table on every request, never cached."
    ),
    # Either side of the hand-over reads this feed: the packer who will claim the
    # output and the assembly planner who wants to see it leave. No preset role
    # holds both rights, so the gate is "any", as /job-work/charges is for
    # finance and job work.
    dependencies=[require_any_permission(Permission.PACKING_VIEW, Permission.ASSEMBLY_ORDER_VIEW)],
)
async def ready_for_packing(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    warehouse_id: Annotated[
        uuid.UUID | None, Query(description="Restrict to one output warehouse")
    ] = None,
) -> PaginatedResponse[ReadyForPackingRow]:
    rows, pagination = await service.list_ready_for_packing(
        session, tenant, warehouse_id=warehouse_id, params=params
    )
    return PaginatedResponse[ReadyForPackingRow](data=rows, pagination=pagination)


# -----------------------------------------------------------------------------
# CRUD
# -----------------------------------------------------------------------------


@router.get(
    "",
    response_model=PaginatedResponse[AssemblyOrderSummary],
    summary="List assembly orders",
    dependencies=[require_permission(Permission.ASSEMBLY_ORDER_VIEW)],
)
async def list_orders(
    session: DbSession,
    tenant: Tenant,
    filters: OrderFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[AssemblyOrderSummary]:
    rows, pagination = await service.list_orders(session, tenant, filters, params, sort)
    return PaginatedResponse[AssemblyOrderSummary](
        data=[AssemblyOrderSummary.model_validate(row) for row in rows], pagination=pagination
    )


@router.post(
    "",
    response_model=ApiResponse[AssemblyOrderDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Raise a draft assembly order",
    description=(
        "Explodes one level of the item's approved ASSEMBLY bill of materials, scales each "
        "component to the planned quantity with its scrap allowance and freezes the result "
        "onto the order. No stock is reserved or consumed by a draft."
    ),
    dependencies=[require_permission(Permission.ASSEMBLY_ORDER_MANAGE)],
)
async def create_order(
    payload: AssemblyOrderCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[AssemblyOrderDetail]:
    order, components = await service.create_order(session, tenant, payload)
    return ApiResponse[AssemblyOrderDetail].ok(
        service.order_detail(order, components), f"Assembly order {order.code} created"
    )


@router.get(
    "/{order_id}",
    response_model=ApiResponse[AssemblyOrderDetail],
    summary="Fetch one assembly order with its component set",
    dependencies=[require_permission(Permission.ASSEMBLY_ORDER_VIEW)],
)
async def get_order(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[AssemblyOrderDetail]:
    return ApiResponse[AssemblyOrderDetail].ok(
        await service.get_order_detail(session, tenant, order_id)
    )


@router.patch(
    "/{order_id}",
    response_model=ApiResponse[AssemblyOrderDetail],
    summary="Amend a draft assembly order",
    description=(
        "Only a DRAFT is editable. Changing the planned quantity re-explodes the component "
        "set from the same structure rather than rescaling the stored numbers."
    ),
    dependencies=[require_permission(Permission.ASSEMBLY_ORDER_MANAGE)],
)
async def update_order(
    order_id: uuid.UUID,
    payload: AssemblyOrderUpdate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[AssemblyOrderDetail]:
    order, components = await service.update_order(session, tenant, order_id, payload)
    return ApiResponse[AssemblyOrderDetail].ok(
        service.order_detail(order, components), f"Assembly order {order.code} updated"
    )


@router.delete(
    "/{order_id}",
    response_model=ApiResponse[None],
    summary="Delete a draft assembly order",
    description="Only a DRAFT may be deleted. Anything further along is cancelled instead.",
    dependencies=[require_permission(Permission.ASSEMBLY_ORDER_MANAGE)],
)
async def delete_order(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant, reason: ReasonQuery
) -> ApiResponse[None]:
    await service.delete_draft_order(session, tenant, order_id, reason=reason)
    return ApiResponse[None].ok(None, "Draft assembly order deleted")


@router.get(
    "/{order_id}/components",
    response_model=ApiResponse[list[AssemblyComponentRead]],
    summary="The kitting list: what this order consumes and what it has eaten",
    description=(
        "Returned whole rather than paginated: the set is bounded when the order is raised "
        "and a kitting list is only meaningful complete."
    ),
    dependencies=[require_permission(Permission.ASSEMBLY_ORDER_VIEW)],
)
async def list_components(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[list[AssemblyComponentRead]]:
    return ApiResponse[list[AssemblyComponentRead]].ok(
        await service.list_components(session, tenant, order_id)
    )


# -----------------------------------------------------------------------------
# Lifecycle
# -----------------------------------------------------------------------------


@router.post(
    "/{order_id}/approve",
    response_model=ApiResponse[AssemblyOrderSummary],
    summary="Approve a draft so it can be released",
    dependencies=[require_permission(Permission.ASSEMBLY_ORDER_MANAGE)],
)
async def approve_order(
    order_id: uuid.UUID, payload: ReasonRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[AssemblyOrderSummary]:
    order = await service.approve_order(session, tenant, order_id, reason=payload.reason)
    return ApiResponse[AssemblyOrderSummary].ok(
        AssemblyOrderSummary.model_validate(order), f"Assembly order {order.code} approved"
    )


@router.post(
    "/{order_id}/release",
    response_model=ApiResponse[AssemblyOrderDetail],
    summary="Release to the line and reserve every component",
    description=(
        "Takes a hard stock reservation for each component. A component that is short "
        "refuses the whole release: a half-kitted job blocks the bench and cannot be "
        "finished, so the shortage is surfaced before any parts are moved."
    ),
    dependencies=[require_permission(Permission.ASSEMBLY_ORDER_MANAGE)],
)
async def release_order(
    order_id: uuid.UUID, payload: ReasonRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[AssemblyOrderDetail]:
    order, components = await service.release_order(
        session, tenant, order_id, reason=payload.reason
    )
    return ApiResponse[AssemblyOrderDetail].ok(
        service.order_detail(order, components),
        f"Assembly order {order.code} released and components reserved",
    )


@router.post(
    "/{order_id}/issue-components",
    response_model=ApiResponse[AssemblyOrderDetail],
    summary="Record the hand-over of components to the bench",
    description=(
        "Records what was physically kitted. No stock moves here - the components are still "
        "owned, still in their warehouse position and still reserved to this order. The "
        "ledger is written at completion, when it is known what they were turned into."
    ),
    dependencies=[require_permission(Permission.ASSEMBLY_ORDER_MANAGE)],
)
async def issue_components(
    order_id: uuid.UUID,
    payload: IssueComponentsRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[AssemblyOrderDetail]:
    order, components = await service.issue_components(session, tenant, order_id, payload)
    return ApiResponse[AssemblyOrderDetail].ok(
        service.order_detail(order, components),
        f"Components issued to assembly order {order.code}",
    )


@router.post(
    "/{order_id}/start",
    response_model=ApiResponse[AssemblyOrderSummary],
    summary="Mark assembly as begun",
    description=(
        "Stamps ``actual_start``. Separate from issuing because kitting and building are "
        "separate events, often hours apart and performed by different people."
    ),
    dependencies=[require_permission(Permission.ASSEMBLY_ORDER_MANAGE)],
)
async def start_assembly(
    order_id: uuid.UUID, payload: ReasonRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[AssemblyOrderSummary]:
    order = await service.start_assembly(session, tenant, order_id, reason=payload.reason)
    return ApiResponse[AssemblyOrderSummary].ok(
        AssemblyOrderSummary.model_validate(order), f"Assembly order {order.code} started"
    )


@router.post(
    "/{order_id}/complete",
    response_model=ApiResponse[CompletionResult],
    summary="Consume the components and create the finished assembly stock",
    description=(
        "One transaction: an ASSEMBLY_CONSUME movement for every component - one per serial "
        "for serialised parts - and an ASSEMBLY_RECEIPT for the assembled output, posted as "
        "a single locked batch. When the order requires inspection the output is received "
        "onto QC hold instead of into the available pool and an inspection is raised."
    ),
    dependencies=[require_permission(Permission.ASSEMBLY_COMPLETE)],
)
async def complete_assembly(
    order_id: uuid.UUID,
    payload: CompleteRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[CompletionResult]:
    result = await service.complete_assembly(session, tenant, order_id, payload)
    message = (
        f"Assembled {result.output_quantity}; output held for inspection"
        if result.output_held_for_qc
        else f"Assembled {result.output_quantity} into available stock"
    )
    return ApiResponse[CompletionResult].ok(result, message)


@router.post(
    "/{order_id}/qc-result",
    response_model=ApiResponse[AssemblyOrderSummary],
    summary="Apply an inspection verdict to held assembly output",
    description=(
        "Accepted quantity is released into the available pool; rejected quantity is moved "
        "to the damaged pool. The two must account for everything on hold."
    ),
    dependencies=[require_permission(Permission.QC_DECIDE)],
)
async def record_qc_result(
    order_id: uuid.UUID,
    payload: QcResultRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[AssemblyOrderSummary]:
    order = await service.record_qc_result(session, tenant, order_id, payload)
    return ApiResponse[AssemblyOrderSummary].ok(
        AssemblyOrderSummary.model_validate(order),
        f"Inspection recorded for assembly order {order.code}",
    )


@router.post(
    "/{order_id}/close",
    response_model=ApiResponse[AssemblyOrderSummary],
    summary="Close a completed assembly order",
    dependencies=[require_permission(Permission.ASSEMBLY_ORDER_MANAGE)],
)
async def close_order(
    order_id: uuid.UUID, payload: ReasonRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[AssemblyOrderSummary]:
    order = await service.close_order(session, tenant, order_id, reason=payload.reason)
    return ApiResponse[AssemblyOrderSummary].ok(
        AssemblyOrderSummary.model_validate(order), f"Assembly order {order.code} closed"
    )


@router.post(
    "/{order_id}/cancel",
    response_model=ApiResponse[AssemblyOrderSummary],
    summary="Cancel an assembly order and release its component reservations",
    description=(
        "Only reachable before the components have been issued. Past that the parts are on "
        "the bench and the order is completed or closed instead, so the trail survives."
    ),
    dependencies=[require_permission(Permission.ASSEMBLY_ORDER_MANAGE)],
)
async def cancel_order(
    order_id: uuid.UUID, payload: ReasonRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[AssemblyOrderSummary]:
    order = await service.cancel_order(session, tenant, order_id, reason=payload.reason)
    return ApiResponse[AssemblyOrderSummary].ok(
        AssemblyOrderSummary.model_validate(order), f"Assembly order {order.code} cancelled"
    )


@router.post(
    "/{order_id}/pack",
    response_model=ApiResponse[AssemblyOrderSummary],
    summary="Claim a completed assembly's output for a packing order",
    description=(
        "The other half of ready-for-packing: once a packing order has taken an assembly it "
        "stops being offered, so two packers cannot plan around the same output."
    ),
    dependencies=[require_permission(Permission.PACKING_MANAGE)],
)
async def mark_packed(
    order_id: uuid.UUID,
    payload: PackingHandoverRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[AssemblyOrderSummary]:
    order = await service.mark_packed(
        session,
        tenant,
        order_id,
        packing_order_id=payload.packing_order_id,
        reason=payload.reason,
    )
    return ApiResponse[AssemblyOrderSummary].ok(
        AssemblyOrderSummary.model_validate(order),
        f"Assembly order {order.code} handed to packing",
    )


__all__ = ["router"]
