"""HTTP surface for packing orders, cartons and labels.

Handlers bind parameters, call one service function and wrap the result in the
standard envelope. No rule, query or audit write lives here (master rules
section 74).

Route ordering note: ``/ready-for-dispatch``, ``/packing-materials``,
``/from-assembly`` and ``/trace/serial/{serial_id}`` are declared before
``/{order_id}``. FastAPI
matches in declaration order, and a literal segment declared after a path
parameter of the same shape would be captured by it and rejected as a malformed
UUID.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from src.core.deps import DbSession, Tenant, require_permission
from src.core.pagination import PageParams, SortParams, page_params, sort_params
from src.core.permissions import Permission
from src.core.responses import (
    COMMON_ERROR_RESPONSES,
    AcceptedJobResponse,
    ApiResponse,
    PaginatedResponse,
)
from src.modules.packing import service
from src.modules.packing.models import PackingOrderStatus
from src.modules.packing.schemas import (
    CartonContentsReplace,
    CartonCreate,
    CartonDetail,
    CartonLabel,
    CartonScanResult,
    CompleteRequest,
    DispatchHandoverRequest,
    PackingCompletionResult,
    PackingFromAssemblyCreate,
    PackingMaterialRow,
    PackingOrderCreate,
    PackingOrderDetail,
    PackingOrderFilters,
    PackingOrderLineRead,
    PackingOrderSummary,
    PackingOrderUpdate,
    ReadyForDispatchRow,
    ReasonRequest,
    SealCartonRequest,
    SealResult,
)
from src.modules.reports import service as reports_service

router = APIRouter(prefix="/packing", tags=["Packing"], responses=COMMON_ERROR_RESPONSES)

PageDep = Annotated[PageParams, Depends(page_params)]
SortDep = Annotated[SortParams, Depends(sort_params)]

# Deletion carries its reason in the query string: a DELETE body is legal but
# several HTTP clients and proxies drop it, and the reason is not optional
# (master rules section 83).
ReasonQuery = Annotated[
    str, Query(min_length=3, max_length=255, description="Why this is being removed")
]


def order_filters(
    search: Annotated[str | None, Query(max_length=120, description="Order code or note")] = None,
    order_status: Annotated[PackingOrderStatus | None, Query(alias="status")] = None,
    customer_id: Annotated[uuid.UUID | None, Query()] = None,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    sales_order_id: Annotated[uuid.UUID | None, Query()] = None,
    open_only: Annotated[
        bool, Query(description="Only orders that still owe packed goods")
    ] = False,
    awaiting_dispatch: Annotated[
        bool, Query(description="Only packed orders no dispatch has claimed")
    ] = False,
    planned_from: Annotated[date | None, Query()] = None,
    planned_to: Annotated[date | None, Query()] = None,
) -> PackingOrderFilters:
    return PackingOrderFilters(
        search=search,
        status=order_status,
        customer_id=customer_id,
        warehouse_id=warehouse_id,
        sales_order_id=sales_order_id,
        open_only=open_only,
        awaiting_dispatch=awaiting_dispatch,
        planned_from=planned_from,
        planned_to=planned_to,
    )


OrderFilterDep = Annotated[PackingOrderFilters, Depends(order_filters)]


# -----------------------------------------------------------------------------
# Literal routes (declared first - see the module docstring)
# -----------------------------------------------------------------------------


@router.get(
    "/ready-for-dispatch",
    response_model=PaginatedResponse[ReadyForDispatchRow],
    summary="Packed orders waiting for a truck",
    description=(
        "Orders that are PACKED and that no dispatch has claimed, oldest first, with the "
        "carton count counted from the carton rows rather than read from the header. "
        "Completing a packing order does not dispatch it - it puts it on this list."
    ),
    dependencies=[
        require_permission(Permission.PACKING_VIEW, Permission.DISPATCH_VIEW, mode="any")
    ],
)
async def ready_for_dispatch(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    warehouse_id: Annotated[
        uuid.UUID | None, Query(description="Restrict to one warehouse")
    ] = None,
) -> PaginatedResponse[ReadyForDispatchRow]:
    rows, pagination = await service.list_ready_for_dispatch(
        session, tenant, warehouse_id=warehouse_id, params=params
    )
    return PaginatedResponse[ReadyForDispatchRow](data=rows, pagination=pagination)


@router.get(
    "/packing-materials",
    response_model=PaginatedResponse[PackingMaterialRow],
    summary="Items offered as packing material, with live availability",
    description=(
        "The carton form's pick-list. Availability is read from the balance table on every "
        "request and is never cached (section 34)."
    ),
    dependencies=[require_permission(Permission.PACKING_VIEW)],
)
async def packing_materials(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    warehouse_id: Annotated[uuid.UUID, Query(description="Warehouse to read stock at")],
    search: Annotated[str | None, Query(max_length=120)] = None,
) -> PaginatedResponse[PackingMaterialRow]:
    rows, pagination = await service.list_packing_materials(
        session, tenant, warehouse_id=warehouse_id, params=params, search=search
    )
    return PaginatedResponse[PackingMaterialRow](data=rows, pagination=pagination)


@router.get(
    "/trace/serial/{serial_id}",
    response_model=ApiResponse[list[CartonDetail]],
    summary="Which carton contains this serial",
    description=(
        "The traceability question carton tracking exists to answer. Returns a list: a "
        "correct data set yields at most one carton, and an answer of two is an incident "
        "somebody must be able to see rather than one the API hides."
    ),
    dependencies=[require_permission(Permission.PACKING_VIEW)],
)
async def trace_serial(
    session: DbSession, tenant: Tenant, serial_id: uuid.UUID
) -> ApiResponse[list[CartonDetail]]:
    cartons = await service.find_cartons_holding_serial(session, tenant, serial_id)
    return ApiResponse[list[CartonDetail]].ok(cartons)


@router.get(
    "/scan",
    response_model=ApiResponse[CartonScanResult],
    summary="Find a carton by scanning its barcode",
    description=(
        "Resolves a carton label within the authenticated company and returns the recorded "
        "order, seal state, weights and contents."
    ),
    dependencies=[require_permission(Permission.PACKING_VIEW)],
)
async def scan_carton(
    session: DbSession,
    tenant: Tenant,
    barcode: Annotated[str, Query(min_length=8, max_length=64, pattern=r"^[0-9A-Za-z-]+$")],
) -> ApiResponse[CartonScanResult]:
    result = await service.find_carton_by_barcode(session, tenant, barcode)
    return ApiResponse[CartonScanResult].ok(result)


# -----------------------------------------------------------------------------
# CRUD
# -----------------------------------------------------------------------------


@router.get(
    "",
    response_model=PaginatedResponse[PackingOrderSummary],
    summary="List packing orders",
    dependencies=[require_permission(Permission.PACKING_VIEW)],
)
async def list_orders(
    session: DbSession,
    tenant: Tenant,
    filters: OrderFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[PackingOrderSummary]:
    rows, pagination = await service.list_orders(session, tenant, filters, params, sort)
    return PaginatedResponse[PackingOrderSummary](data=rows, pagination=pagination)


@router.post(
    "",
    response_model=ApiResponse[PackingOrderDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Raise a draft packing order",
    dependencies=[require_permission(Permission.PACKING_MANAGE)],
)
async def create_order(
    session: DbSession, tenant: Tenant, payload: PackingOrderCreate
) -> ApiResponse[PackingOrderDetail]:
    order, lines = await service.create_order(session, tenant, payload)
    return ApiResponse[PackingOrderDetail].ok(
        service.order_detail(order, lines), message=f"Packing order {order.code} created"
    )


@router.post(
    "/from-assembly",
    response_model=ApiResponse[PackingOrderDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Raise a packing order for a completed assembly",
    description=(
        "The assembly hand-over: one transaction that locks the completed assembly, raises "
        "a draft packing order for its output - the assembled item, from the warehouse it "
        "was received into, for the assembled quantity unless a smaller one is named - and "
        "marks the assembly packed against it, so it leaves the ready-for-packing list. The "
        "customer and sales order come from the assembly's source when it was raised for a "
        "sales order; otherwise the customer must be named."
    ),
    dependencies=[require_permission(Permission.PACKING_MANAGE)],
)
async def create_order_from_assembly(
    session: DbSession, tenant: Tenant, payload: PackingFromAssemblyCreate
) -> ApiResponse[PackingOrderDetail]:
    order, lines = await service.create_order_from_assembly(session, tenant, payload)
    return ApiResponse[PackingOrderDetail].ok(
        service.order_detail(order, lines),
        message=f"Packing order {order.code} created from the assembly",
    )


@router.get(
    "/{order_id}",
    response_model=ApiResponse[PackingOrderDetail],
    summary="One packing order with its lines",
    dependencies=[require_permission(Permission.PACKING_VIEW)],
)
async def get_order(
    session: DbSession, tenant: Tenant, order_id: uuid.UUID
) -> ApiResponse[PackingOrderDetail]:
    return ApiResponse[PackingOrderDetail].ok(
        await service.get_order_detail(session, tenant, order_id)
    )


@router.patch(
    "/{order_id}",
    response_model=ApiResponse[PackingOrderDetail],
    summary="Amend a draft packing order",
    dependencies=[require_permission(Permission.PACKING_MANAGE)],
)
async def update_order(
    session: DbSession, tenant: Tenant, order_id: uuid.UUID, payload: PackingOrderUpdate
) -> ApiResponse[PackingOrderDetail]:
    order, lines = await service.update_order(session, tenant, order_id, payload)
    return ApiResponse[PackingOrderDetail].ok(
        service.order_detail(order, lines), message=f"Packing order {order.code} updated"
    )


@router.delete(
    "/{order_id}",
    response_model=ApiResponse[None],
    summary="Delete a draft packing order",
    dependencies=[require_permission(Permission.PACKING_MANAGE)],
)
async def delete_order(
    session: DbSession, tenant: Tenant, order_id: uuid.UUID, reason: ReasonQuery
) -> ApiResponse[None]:
    await service.delete_draft_order(session, tenant, order_id, reason=reason)
    return ApiResponse[None].ok(message="Draft packing order deleted")


# -----------------------------------------------------------------------------
# Lifecycle
# -----------------------------------------------------------------------------


@router.post(
    "/{order_id}/start",
    response_model=ApiResponse[PackingOrderDetail],
    summary="Put a draft on the packing bench",
    dependencies=[require_permission(Permission.PACKING_MANAGE)],
)
async def start_packing(
    session: DbSession, tenant: Tenant, order_id: uuid.UUID, payload: ReasonRequest
) -> ApiResponse[PackingOrderDetail]:
    order, lines = await service.start_packing(session, tenant, order_id, reason=payload.reason)
    return ApiResponse[PackingOrderDetail].ok(
        service.order_detail(order, lines), message=f"{order.code} is now being packed"
    )


@router.post(
    "/{order_id}/complete",
    response_model=ApiResponse[PackingCompletionResult],
    summary="Finish packing and publish the order to dispatch",
    description=(
        "Freezes the carton totals and moves the order to PACKED. No stock moves: the goods "
        "leave the building when dispatch posts its own movements."
    ),
    dependencies=[require_permission(Permission.PACKING_MANAGE)],
)
async def complete_packing(
    session: DbSession, tenant: Tenant, order_id: uuid.UUID, payload: CompleteRequest
) -> ApiResponse[PackingCompletionResult]:
    result = await service.complete_packing(session, tenant, order_id, payload)
    return ApiResponse[PackingCompletionResult].ok(
        result, message=f"Packed into {result.total_cartons} carton(s)"
    )


@router.post(
    "/{order_id}/hand-over",
    response_model=ApiResponse[PackingOrderSummary],
    summary="Claim a packed order for a dispatch",
    dependencies=[require_permission(Permission.DISPATCH_CREATE)],
)
async def hand_over(
    session: DbSession, tenant: Tenant, order_id: uuid.UUID, payload: DispatchHandoverRequest
) -> ApiResponse[PackingOrderSummary]:
    order = await service.hand_over_to_dispatch(
        session, tenant, order_id, dispatch_id=payload.dispatch_id, reason=payload.reason
    )
    return ApiResponse[PackingOrderSummary].ok(
        PackingOrderSummary.model_validate(order),
        message=f"{order.code} handed over to dispatch",
    )


@router.post(
    "/{order_id}/lines/{line_id}/reinspect",
    response_model=ApiResponse[PackingOrderLineRead],
    summary="Raise a fresh FINAL inspection for a rejected line",
    description=(
        "Only once the line's FINAL inspection is REJECTED: the rejected stock has moved to "
        "DAMAGED, so the operator repacks replacement goods and this raises the inspection "
        "that covers them."
    ),
    dependencies=[require_permission(Permission.PACKING_MANAGE)],
)
async def reinspect_line(
    session: DbSession,
    tenant: Tenant,
    order_id: uuid.UUID,
    line_id: uuid.UUID,
    payload: ReasonRequest,
) -> ApiResponse[PackingOrderLineRead]:
    line = await service.reinspect_line(session, tenant, order_id, line_id, reason=payload.reason)
    return ApiResponse[PackingOrderLineRead].ok(
        PackingOrderLineRead.model_validate(line),
        message=f"Line {line.line_number} re-inspected",
    )


@router.post(
    "/{order_id}/close",
    response_model=ApiResponse[PackingOrderSummary],
    summary="Close a handed-over packing order",
    dependencies=[require_permission(Permission.PACKING_MANAGE)],
)
async def close_order(
    session: DbSession, tenant: Tenant, order_id: uuid.UUID, payload: ReasonRequest
) -> ApiResponse[PackingOrderSummary]:
    order = await service.close_order(session, tenant, order_id, reason=payload.reason)
    return ApiResponse[PackingOrderSummary].ok(
        PackingOrderSummary.model_validate(order), message=f"{order.code} closed"
    )


@router.post(
    "/{order_id}/cancel",
    response_model=ApiResponse[PackingOrderSummary],
    summary="Cancel a packing order that has not been packed",
    dependencies=[require_permission(Permission.PACKING_MANAGE)],
)
async def cancel_order(
    session: DbSession, tenant: Tenant, order_id: uuid.UUID, payload: ReasonRequest
) -> ApiResponse[PackingOrderSummary]:
    order = await service.cancel_order(session, tenant, order_id, reason=payload.reason)
    return ApiResponse[PackingOrderSummary].ok(
        PackingOrderSummary.model_validate(order), message=f"{order.code} cancelled"
    )


# -----------------------------------------------------------------------------
# Cartons
# -----------------------------------------------------------------------------


@router.get(
    "/{order_id}/cartons",
    response_model=PaginatedResponse[CartonDetail],
    summary="Cartons of one packing order, with their contents",
    dependencies=[require_permission(Permission.PACKING_VIEW)],
)
async def list_cartons(
    session: DbSession,
    tenant: Tenant,
    order_id: uuid.UUID,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[CartonDetail]:
    rows, pagination = await service.list_cartons(session, tenant, order_id, params, sort)
    return PaginatedResponse[CartonDetail](data=rows, pagination=pagination)


@router.post(
    "/{order_id}/cartons",
    response_model=ApiResponse[CartonDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Open a new carton",
    description=(
        "The carton number and its barcode are assigned by the server. No packing material "
        "is issued yet - a carton is paid for when it is sealed."
    ),
    dependencies=[require_permission(Permission.PACKING_MANAGE)],
)
async def add_carton(
    session: DbSession, tenant: Tenant, order_id: uuid.UUID, payload: CartonCreate
) -> ApiResponse[CartonDetail]:
    carton = await service.add_carton(session, tenant, order_id, payload)
    return ApiResponse[CartonDetail].ok(carton, message=f"Carton {carton.barcode} opened")


@router.get(
    "/{order_id}/cartons/{carton_id}",
    response_model=ApiResponse[CartonDetail],
    summary="One carton and everything in it",
    dependencies=[require_permission(Permission.PACKING_VIEW)],
)
async def get_carton(
    session: DbSession, tenant: Tenant, order_id: uuid.UUID, carton_id: uuid.UUID
) -> ApiResponse[CartonDetail]:
    return ApiResponse[CartonDetail].ok(
        await service.get_carton_detail(session, tenant, order_id, carton_id)
    )


@router.put(
    "/{order_id}/cartons/{carton_id}/contents",
    response_model=ApiResponse[CartonDetail],
    summary="Set what is inside an open carton",
    description=(
        "Replaces the whole content set and rolls the difference onto the order's lines. "
        "Refused once the carton is sealed."
    ),
    dependencies=[require_permission(Permission.PACKING_MANAGE)],
)
async def replace_contents(
    session: DbSession,
    tenant: Tenant,
    order_id: uuid.UUID,
    carton_id: uuid.UUID,
    payload: CartonContentsReplace,
) -> ApiResponse[CartonDetail]:
    carton = await service.replace_carton_contents(session, tenant, order_id, carton_id, payload)
    return ApiResponse[CartonDetail].ok(carton, message="Carton contents updated")


@router.post(
    "/{order_id}/cartons/{carton_id}/seal",
    response_model=ApiResponse[SealResult],
    summary="Seal a carton",
    description=(
        "Freezes the contents, computes the gross weight from the contents plus the empty "
        "box, and issues the packing material from stock as a MATERIAL_ISSUE. One-way."
    ),
    dependencies=[require_permission(Permission.PACKING_MANAGE)],
)
async def seal_carton(
    session: DbSession,
    tenant: Tenant,
    order_id: uuid.UUID,
    carton_id: uuid.UUID,
    payload: SealCartonRequest,
) -> ApiResponse[SealResult]:
    result = await service.seal_carton(session, tenant, order_id, carton_id, payload)
    return ApiResponse[SealResult].ok(result, message=f"Carton {result.carton.barcode} sealed")


@router.delete(
    "/{order_id}/cartons/{carton_id}",
    response_model=ApiResponse[None],
    summary="Remove an unsealed carton",
    dependencies=[require_permission(Permission.PACKING_MANAGE)],
)
async def remove_carton(
    session: DbSession,
    tenant: Tenant,
    order_id: uuid.UUID,
    carton_id: uuid.UUID,
    reason: ReasonQuery,
) -> ApiResponse[None]:
    await service.remove_carton(session, tenant, order_id, carton_id, reason=reason)
    return ApiResponse[None].ok(message="Carton removed")


@router.get(
    "/{order_id}/cartons/{carton_id}/label",
    response_model=ApiResponse[CartonLabel],
    summary="The printable label payload for one carton",
    description=(
        "The barcode value, its symbology, the addressee, the weights and the content "
        "block. Rendering it is the printer's job - no artwork is generated here."
    ),
    dependencies=[require_permission(Permission.PACKING_VIEW)],
)
async def carton_label(
    session: DbSession, tenant: Tenant, order_id: uuid.UUID, carton_id: uuid.UUID
) -> ApiResponse[CartonLabel]:
    return ApiResponse[CartonLabel].ok(
        await service.carton_label(session, tenant, order_id, carton_id)
    )


# -----------------------------------------------------------------------------
# Packing list - queued, never rendered in the request (section 22)
# -----------------------------------------------------------------------------


@router.get(
    "/{order_id}/packing-list",
    response_model=AcceptedJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue the packing-list PDF",
    description=(
        "Hands the render to the reports queue and returns a job id immediately. A "
        "fifty-carton packing list is not something an HTTP worker holds a connection "
        "open for (section 22, section 35)."
    ),
    dependencies=[require_permission(Permission.PACKING_VIEW)],
)
async def packing_list(
    session: DbSession, tenant: Tenant, order_id: uuid.UUID
) -> AcceptedJobResponse:
    job_id = await service.queue_packing_list(session, tenant, order_id)
    return AcceptedJobResponse.create(
        job_id=job_id,
        status_url=reports_service.job_status_url(job_id),
        message="The packing list is being generated",
    )


__all__ = ["router"]
