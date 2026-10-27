"""HTTP surface for the procurement lifecycle.

Handlers bind parameters, call one service function and wrap the result in the
standard envelope. No rule, query or audit write lives here (master rules
section 74).

Route ordering note: every literal path - ``/pending-approvals``,
``/purchase-orders/check-duplicates`` - is declared before the ``/{id}`` route
of the same shape. FastAPI matches in declaration order, and a literal segment
declared after a path parameter would be captured by it and rejected as a
malformed UUID.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status

from src.core.deps import (
    CurrentUser,
    DbSession,
    Tenant,
    forbid_vendor_users,
    require_any_permission,
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
    AcceptedJobResponse,
    ApiResponse,
    CursorPaginatedResponse,
    PaginatedResponse,
)
from src.modules.procurement import inbound_service, purchase_order_changes, service
from src.modules.procurement.inbound_schemas import (
    DocumentKind,
    InboundDeliveredRequest,
    InboundDocumentRead,
    InboundShipmentCreate,
    InboundShipmentRead,
    InboundTrackingFilters,
    InboundTrackingRead,
)
from src.modules.procurement.models import (
    GoodsReceiptStatus,
    PurchaseOrderStatus,
    PurchaseReturnStatus,
    PurchaseSourceType,
    QuotationStatus,
    RequisitionStatus,
    RfqStatus,
)
from src.modules.procurement.purchase_order_pdf import queue_purchase_order_pdf
from src.modules.procurement.purchase_return_debit_note_pdf_service import (
    queue_purchase_return_debit_note_pdf,
)
from src.modules.procurement.schemas import (
    AcknowledgeRequest,
    ApproveRequest,
    DuplicateCheckRequest,
    DuplicateCheckResult,
    GoodsReceiptCreate,
    GoodsReceiptDetail,
    GoodsReceiptFilters,
    GoodsReceiptPostResult,
    GoodsReceiptSummary,
    PendingApproval,
    PendingApprovalFilters,
    PurchaseOrderAmendmentRead,
    PurchaseOrderAmendRequest,
    PurchaseOrderApprovalRequest,
    PurchaseOrderCreate,
    PurchaseOrderDetail,
    PurchaseOrderDuplicateResolutionRequest,
    PurchaseOrderDuplicateResolutionResult,
    PurchaseOrderFilters,
    PurchaseOrderSummary,
    PurchaseOrderTracking,
    PurchaseOrderUpdate,
    PurchaseOrderVendorAssignee,
    PurchaseReturnCreate,
    PurchaseReturnDetail,
    PurchaseReturnFilters,
    PurchaseReturnPostResult,
    PurchaseReturnSummary,
    QuotationComparison,
    QuotationCreate,
    QuotationDecisionRequest,
    QuotationDetail,
    QuotationFilters,
    QuotationSummary,
    QuotationUpdate,
    ReasonRequest,
    RequisitionBatchConversionRequest,
    RequisitionConversionRequest,
    RequisitionConversionResult,
    RequisitionCreate,
    RequisitionDetail,
    RequisitionFilters,
    RequisitionListRow,
    RequisitionSummary,
    RequisitionUpdate,
    RfqCreate,
    RfqDetail,
    RfqFilters,
    RfqSummary,
    RfqUpdate,
    SendRequest,
    ShipmentUpdate,
)
from src.modules.reports import service as reports_service

router = APIRouter(prefix="/procurement", tags=["Procurement"], responses=COMMON_ERROR_RESPONSES)

PageDep = Annotated[PageParams, Depends(page_params)]
SortDep = Annotated[SortParams, Depends(sort_params)]

# Deletion carries its reason in the query string: a DELETE body is legal but
# several HTTP clients and proxies drop it, and the reason is not optional
# (master rules section 83).
ReasonQuery = Annotated[
    str, Query(min_length=3, max_length=255, description="Why this document is being removed")
]


# -----------------------------------------------------------------------------
# Filter dependencies
# -----------------------------------------------------------------------------


def requisition_filters(
    search: Annotated[str | None, Query(max_length=120)] = None,
    requisition_status: Annotated[RequisitionStatus | None, Query(alias="status")] = None,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    department_id: Annotated[uuid.UUID | None, Query()] = None,
    item_id: Annotated[uuid.UUID | None, Query()] = None,
    open_only: Annotated[bool, Query(description="Only unmet approved demand")] = False,
    convertible_only: Annotated[
        bool,
        Query(description="Only approved or converted requisitions with a line still to order"),
    ] = False,
    required_from: Annotated[date | None, Query()] = None,
    required_to: Annotated[date | None, Query()] = None,
) -> RequisitionFilters:
    return bind_query_filters(
        RequisitionFilters,
        search=search,
        status=requisition_status,
        warehouse_id=warehouse_id,
        department_id=department_id,
        item_id=item_id,
        open_only=open_only,
        convertible_only=convertible_only,
        required_from=required_from,
        required_to=required_to,
    )


def rfq_filters(
    search: Annotated[str | None, Query(max_length=120)] = None,
    rfq_status: Annotated[RfqStatus | None, Query(alias="status")] = None,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    vendor_id: Annotated[uuid.UUID | None, Query()] = None,
    item_id: Annotated[uuid.UUID | None, Query()] = None,
    due_before: Annotated[date | None, Query()] = None,
) -> RfqFilters:
    return bind_query_filters(
        RfqFilters,
        search=search,
        status=rfq_status,
        warehouse_id=warehouse_id,
        vendor_id=vendor_id,
        item_id=item_id,
        due_before=due_before,
    )


def quotation_filters(
    search: Annotated[str | None, Query(max_length=120)] = None,
    rfq_id: Annotated[uuid.UUID | None, Query()] = None,
    vendor_id: Annotated[uuid.UUID | None, Query()] = None,
    quotation_status: Annotated[QuotationStatus | None, Query(alias="status")] = None,
    valid_on: Annotated[
        date | None, Query(description="Only offers still valid on this date")
    ] = None,
) -> QuotationFilters:
    return bind_query_filters(
        QuotationFilters,
        search=search,
        rfq_id=rfq_id,
        vendor_id=vendor_id,
        status=quotation_status,
        valid_on=valid_on,
    )


def order_filters(
    search: Annotated[str | None, Query(max_length=120)] = None,
    order_status: Annotated[PurchaseOrderStatus | None, Query(alias="status")] = None,
    vendor_id: Annotated[uuid.UUID | None, Query()] = None,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    item_id: Annotated[uuid.UUID | None, Query()] = None,
    item_category_id: Annotated[
        uuid.UUID | None,
        Query(description="Orders raised for this item category or any category beneath it"),
    ] = None,
    open_only: Annotated[bool, Query(description="Only orders still expecting delivery")] = False,
    overdue_only: Annotated[
        bool, Query(description="Only orders past their promised date")
    ] = False,
    source_type: Annotated[PurchaseSourceType | None, Query()] = None,
    source_id: Annotated[uuid.UUID | None, Query()] = None,
    expected_from: Annotated[date | None, Query()] = None,
    expected_to: Annotated[date | None, Query()] = None,
) -> PurchaseOrderFilters:
    return bind_query_filters(
        PurchaseOrderFilters,
        search=search,
        status=order_status,
        vendor_id=vendor_id,
        warehouse_id=warehouse_id,
        item_id=item_id,
        item_category_id=item_category_id,
        open_only=open_only,
        overdue_only=overdue_only,
        source_type=source_type,
        source_id=source_id,
        expected_from=expected_from,
        expected_to=expected_to,
    )


def receipt_filters(
    search: Annotated[str | None, Query(max_length=120)] = None,
    receipt_status: Annotated[GoodsReceiptStatus | None, Query(alias="status")] = None,
    purchase_order_id: Annotated[uuid.UUID | None, Query()] = None,
    vendor_id: Annotated[uuid.UUID | None, Query()] = None,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    item_id: Annotated[uuid.UUID | None, Query()] = None,
    qc_pending_only: Annotated[
        bool, Query(description="Only receipts with quantity awaiting inspection")
    ] = False,
    received_from: Annotated[date | None, Query()] = None,
    received_to: Annotated[date | None, Query()] = None,
) -> GoodsReceiptFilters:
    return bind_query_filters(
        GoodsReceiptFilters,
        search=search,
        status=receipt_status,
        purchase_order_id=purchase_order_id,
        vendor_id=vendor_id,
        warehouse_id=warehouse_id,
        item_id=item_id,
        qc_pending_only=qc_pending_only,
        received_from=received_from,
        received_to=received_to,
    )


def return_filters(
    search: Annotated[str | None, Query(max_length=120)] = None,
    return_status: Annotated[PurchaseReturnStatus | None, Query(alias="status")] = None,
    vendor_id: Annotated[uuid.UUID | None, Query()] = None,
    goods_receipt_id: Annotated[uuid.UUID | None, Query()] = None,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    return_from: Annotated[date | None, Query()] = None,
    return_to: Annotated[date | None, Query()] = None,
) -> PurchaseReturnFilters:
    return bind_query_filters(
        PurchaseReturnFilters,
        search=search,
        status=return_status,
        vendor_id=vendor_id,
        goods_receipt_id=goods_receipt_id,
        warehouse_id=warehouse_id,
        return_from=return_from,
        return_to=return_to,
    )


def approval_filters(
    document_type: Annotated[
        str | None, Query(pattern=r"^(purchase_requisition|purchase_order)$")
    ] = None,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    vendor_id: Annotated[uuid.UUID | None, Query()] = None,
) -> PendingApprovalFilters:
    return bind_query_filters(
        PendingApprovalFilters,
        document_type=document_type,
        warehouse_id=warehouse_id,
        vendor_id=vendor_id,
    )


RequisitionFilterDep = Annotated[RequisitionFilters, Depends(requisition_filters)]
RfqFilterDep = Annotated[RfqFilters, Depends(rfq_filters)]
QuotationFilterDep = Annotated[QuotationFilters, Depends(quotation_filters)]
OrderFilterDep = Annotated[PurchaseOrderFilters, Depends(order_filters)]
ReceiptFilterDep = Annotated[GoodsReceiptFilters, Depends(receipt_filters)]
ReturnFilterDep = Annotated[PurchaseReturnFilters, Depends(return_filters)]
ApprovalFilterDep = Annotated[PendingApprovalFilters, Depends(approval_filters)]


# -----------------------------------------------------------------------------
# Approvals queue (declared first - see the module docstring)
# -----------------------------------------------------------------------------


@router.get(
    "/pending-approvals",
    response_model=PaginatedResponse[PendingApproval],
    summary="Requisitions and purchase orders awaiting this user's approval",
    description=(
        "One merged, paginated queue across both documents, filtered to what the caller "
        "can actually approve. Most urgent first, then oldest."
    ),
    dependencies=[
        require_permission(
            Permission.PURCHASE_REQUISITION_APPROVE,
            Permission.PURCHASE_ORDER_APPROVE,
            mode="any",
        )
    ],
)
async def list_pending_approvals(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    filters: ApprovalFilterDep,
    params: PageDep,
) -> PaginatedResponse[PendingApproval]:
    rows, pagination = await service.pending_approvals(session, tenant, principal, filters, params)
    return PaginatedResponse[PendingApproval](data=rows, pagination=pagination)


# =============================================================================
# Purchase requisitions
# =============================================================================


@router.get(
    "/purchase-requisitions",
    response_model=PaginatedResponse[RequisitionListRow],
    summary="List purchase requisitions",
    description="Each row carries open_line_count: how many of its lines are still to order.",
    dependencies=[require_permission(Permission.PURCHASE_REQUISITION_VIEW)],
)
async def list_requisitions(
    session: DbSession,
    tenant: Tenant,
    filters: RequisitionFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[RequisitionListRow]:
    rows, pagination = await service.list_requisitions(session, tenant, filters, params, sort)
    return PaginatedResponse[RequisitionListRow](data=rows, pagination=pagination)


# --- the batch conversion: literal paths, declared before /{requisition_id} ---


@router.post(
    "/purchase-requisitions/conversion-preview",
    response_model=ApiResponse[RequisitionConversionResult],
    summary="Preview converting several approved requisitions into purchase orders",
    description=(
        "Read-only. One proposed order per (vendor, procurement category, warehouse), each with "
        "its lines, prices and their sources, the vendor and its source, the vendor user it "
        "would be assigned to and why, and the open orders it would duplicate; plus every line "
        "that cannot be converted and why. MRP raises one requisition per suggestion, which is "
        "what the batch form is for."
    ),
    dependencies=[
        require_permission(Permission.PURCHASE_ORDER_CREATE),
        Depends(forbid_vendor_users),
    ],
)
async def preview_batch_conversion(
    payload: RequisitionBatchConversionRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[RequisitionConversionResult]:
    result = await service.preview_requisition_conversion(
        session, tenant, payload.requisition_ids, payload
    )
    return ApiResponse[RequisitionConversionResult].ok(result, _preview_message(result))


@router.post(
    "/purchase-requisitions/convert-to-orders",
    response_model=ApiResponse[RequisitionConversionResult],
    status_code=status.HTTP_201_CREATED,
    summary="Convert several approved requisitions into category-wise purchase orders",
    description=(
        "Raises one DRAFT purchase order per (vendor, procurement category, warehouse) in one "
        "transaction. Any unresolved line refuses the whole conversion. A duplicate is refused "
        "with DUPLICATE_DOCUMENT unless acknowledge_duplicates is set with a reason by a caller "
        "holding purchase_order.approve."
    ),
    dependencies=[
        require_permission(Permission.PURCHASE_ORDER_CREATE),
        Depends(forbid_vendor_users),
    ],
)
async def convert_batch_to_orders(
    payload: RequisitionBatchConversionRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[RequisitionConversionResult]:
    result = await service.convert_requisitions_to_orders(
        session, tenant, principal, payload.requisition_ids, payload
    )
    return ApiResponse[RequisitionConversionResult].ok(result, _converted_message(result))


def _preview_message(result: RequisitionConversionResult) -> str:
    if result.unresolved_lines:
        return f"{len(result.unresolved_lines)} line(s) cannot be converted yet"
    return f"{len(result.groups)} purchase order(s) would be raised"


def _converted_message(result: RequisitionConversionResult) -> str:
    codes = ", ".join(group.purchase_order_code or "" for group in result.groups)
    return f"Raised {len(result.groups)} purchase order(s): {codes}"


@router.post(
    "/purchase-requisitions",
    response_model=ApiResponse[RequisitionDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Raise a draft purchase requisition",
    dependencies=[require_permission(Permission.PURCHASE_REQUISITION_CREATE)],
)
async def create_requisition(
    payload: RequisitionCreate, session: DbSession, tenant: Tenant, principal: CurrentUser
) -> ApiResponse[RequisitionDetail]:
    requisition, lines = await service.create_requisition(session, tenant, principal, payload)
    return ApiResponse[RequisitionDetail].ok(
        service.requisition_detail(requisition, lines),
        success_message(f"Requisition {requisition.code}", "raised"),
    )


@router.get(
    "/purchase-requisitions/{requisition_id}",
    response_model=ApiResponse[RequisitionDetail],
    summary="Fetch one requisition with its lines",
    dependencies=[require_permission(Permission.PURCHASE_REQUISITION_VIEW)],
)
async def get_requisition(
    requisition_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[RequisitionDetail]:
    return ApiResponse[RequisitionDetail].ok(
        await service.get_requisition_detail(session, tenant, requisition_id)
    )


@router.patch(
    "/purchase-requisitions/{requisition_id}",
    response_model=ApiResponse[RequisitionDetail],
    summary="Update a draft requisition",
    dependencies=[require_permission(Permission.PURCHASE_REQUISITION_CREATE)],
)
async def update_requisition(
    requisition_id: uuid.UUID,
    payload: RequisitionUpdate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[RequisitionDetail]:
    requisition, lines = await service.update_requisition(session, tenant, requisition_id, payload)
    return ApiResponse[RequisitionDetail].ok(
        service.requisition_detail(requisition, lines),
        success_message(f"Requisition {requisition.code}", "updated"),
    )


@router.delete(
    "/purchase-requisitions/{requisition_id}",
    response_model=ApiResponse[None],
    summary="Delete a draft requisition",
    description="Only a DRAFT may be deleted. Anything further along is cancelled instead.",
    dependencies=[require_permission(Permission.PURCHASE_REQUISITION_CREATE)],
)
async def delete_requisition(
    requisition_id: uuid.UUID, session: DbSession, tenant: Tenant, reason: ReasonQuery
) -> ApiResponse[None]:
    await service.delete_draft_requisition(session, tenant, requisition_id, reason=reason)
    return ApiResponse[None].ok(None, success_message("Draft requisition", "deleted"))


@router.post(
    "/purchase-requisitions/{requisition_id}/submit",
    response_model=ApiResponse[RequisitionSummary],
    summary="Submit a requisition for approval",
    dependencies=[require_permission(Permission.PURCHASE_REQUISITION_CREATE)],
)
async def submit_requisition(
    requisition_id: uuid.UUID,
    payload: ApproveRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[RequisitionSummary]:
    requisition = await service.transition_requisition(
        session,
        tenant,
        principal,
        requisition_id,
        RequisitionStatus.PENDING_APPROVAL,
        reason=payload.reason or "Submitted for approval",
    )
    return ApiResponse[RequisitionSummary].ok(
        RequisitionSummary.model_validate(requisition),
        f"Requisition {requisition.code} submitted for approval",
    )


@router.post(
    "/purchase-requisitions/{requisition_id}/approve",
    response_model=ApiResponse[RequisitionSummary],
    summary="Approve a requisition",
    description="The approver may not be the person who raised it unless they hold SUPER_ADMIN.",
    dependencies=[require_permission(Permission.PURCHASE_REQUISITION_APPROVE)],
)
async def approve_requisition(
    requisition_id: uuid.UUID,
    payload: ApproveRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[RequisitionSummary]:
    requisition = await service.transition_requisition(
        session,
        tenant,
        principal,
        requisition_id,
        RequisitionStatus.APPROVED,
        reason=payload.reason or "Approved",
    )
    return ApiResponse[RequisitionSummary].ok(
        RequisitionSummary.model_validate(requisition),
        success_message(f"Requisition {requisition.code}", "approved"),
    )


@router.post(
    "/purchase-requisitions/{requisition_id}/reject",
    response_model=ApiResponse[RequisitionSummary],
    summary="Reject a requisition",
    dependencies=[require_permission(Permission.PURCHASE_REQUISITION_APPROVE)],
)
async def reject_requisition(
    requisition_id: uuid.UUID,
    payload: ReasonRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[RequisitionSummary]:
    requisition = await service.transition_requisition(
        session,
        tenant,
        principal,
        requisition_id,
        RequisitionStatus.REJECTED,
        reason=payload.reason,
    )
    return ApiResponse[RequisitionSummary].ok(
        RequisitionSummary.model_validate(requisition),
        success_message(f"Requisition {requisition.code}", "rejected"),
    )


@router.post(
    "/purchase-requisitions/{requisition_id}/cancel",
    response_model=ApiResponse[RequisitionSummary],
    summary="Cancel a requisition",
    dependencies=[require_permission(Permission.PURCHASE_REQUISITION_CREATE)],
)
async def cancel_requisition(
    requisition_id: uuid.UUID,
    payload: ReasonRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[RequisitionSummary]:
    requisition = await service.transition_requisition(
        session,
        tenant,
        principal,
        requisition_id,
        RequisitionStatus.CANCELLED,
        reason=payload.reason,
    )
    return ApiResponse[RequisitionSummary].ok(
        RequisitionSummary.model_validate(requisition),
        success_message(f"Requisition {requisition.code}", "cancelled"),
    )


@router.post(
    "/purchase-requisitions/{requisition_id}/close",
    response_model=ApiResponse[RequisitionSummary],
    summary="Close an approved or converted requisition",
    description=(
        "Stops the requisition counting as open demand: whatever is still unordered on it will "
        "not be ordered. Reaches CLOSED from APPROVED or CONVERTED; the reason is recorded."
    ),
    # The right the state machine declares on both CLOSED edges.
    dependencies=[require_permission(Permission.PURCHASE_REQUISITION_APPROVE)],
)
async def close_requisition(
    requisition_id: uuid.UUID,
    payload: ReasonRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[RequisitionSummary]:
    requisition = await service.transition_requisition(
        session,
        tenant,
        principal,
        requisition_id,
        RequisitionStatus.CLOSED,
        reason=payload.reason,
    )
    return ApiResponse[RequisitionSummary].ok(
        RequisitionSummary.model_validate(requisition),
        success_message(f"Requisition {requisition.code}", "closed"),
    )


@router.post(
    "/purchase-requisitions/{requisition_id}/conversion-preview",
    response_model=ApiResponse[RequisitionConversionResult],
    summary="Preview converting an approved requisition into purchase orders",
    description=(
        "Read-only. One proposed order per (vendor, procurement category), with every line's "
        "price and vendor and where each came from, the vendor user each order would be "
        "assigned to, duplicate warnings, and every line that cannot be converted and why."
    ),
    dependencies=[
        require_permission(Permission.PURCHASE_ORDER_CREATE),
        Depends(forbid_vendor_users),
    ],
)
async def preview_requisition_conversion(
    requisition_id: uuid.UUID,
    payload: RequisitionConversionRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[RequisitionConversionResult]:
    result = await service.preview_requisition_conversion(
        session, tenant, [requisition_id], payload
    )
    return ApiResponse[RequisitionConversionResult].ok(result, _preview_message(result))


@router.post(
    "/purchase-requisitions/{requisition_id}/convert-to-orders",
    response_model=ApiResponse[RequisitionConversionResult],
    status_code=status.HTTP_201_CREATED,
    summary="Convert an approved requisition into category-wise purchase orders",
    description=(
        "Raises one DRAFT purchase order per (vendor, procurement category) in one transaction "
        "and moves the requisition to CONVERTED. Any unresolved line refuses the whole "
        "conversion. A duplicate is refused with DUPLICATE_DOCUMENT unless acknowledge_duplicates "
        "is set with a reason by a caller holding purchase_order.approve."
    ),
    dependencies=[
        require_permission(Permission.PURCHASE_ORDER_CREATE),
        Depends(forbid_vendor_users),
    ],
)
async def convert_requisition_to_orders(
    requisition_id: uuid.UUID,
    payload: RequisitionConversionRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[RequisitionConversionResult]:
    result = await service.convert_requisitions_to_orders(
        session, tenant, principal, [requisition_id], payload
    )
    return ApiResponse[RequisitionConversionResult].ok(result, _converted_message(result))


# =============================================================================
# RFQs
# =============================================================================


@router.get(
    "/rfqs",
    response_model=PaginatedResponse[RfqSummary],
    summary="List requests for quotation",
    dependencies=[require_permission(Permission.RFQ_VIEW)],
)
async def list_rfqs(
    session: DbSession, tenant: Tenant, filters: RfqFilterDep, params: PageDep, sort: SortDep
) -> PaginatedResponse[RfqSummary]:
    rows, pagination = await service.list_rfqs(session, tenant, filters, params, sort)
    return PaginatedResponse[RfqSummary](data=rows, pagination=pagination)


@router.post(
    "/rfqs",
    response_model=ApiResponse[RfqDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Raise an RFQ, optionally from an approved requisition",
    description=(
        "Given a requisition and no explicit lines, every requisition line that has not "
        "already been ordered becomes an RFQ line."
    ),
    dependencies=[require_permission(Permission.RFQ_MANAGE)],
)
async def create_rfq(
    payload: RfqCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[RfqDetail]:
    rfq, lines = await service.create_rfq(session, tenant, payload)
    return ApiResponse[RfqDetail].ok(
        service.rfq_detail(rfq, lines), success_message(f"RFQ {rfq.code}", "raised")
    )


@router.get(
    "/rfqs/{rfq_id}",
    response_model=ApiResponse[RfqDetail],
    summary="Fetch one RFQ with its lines",
    dependencies=[require_permission(Permission.RFQ_VIEW)],
)
async def get_rfq(rfq_id: uuid.UUID, session: DbSession, tenant: Tenant) -> ApiResponse[RfqDetail]:
    return ApiResponse[RfqDetail].ok(await service.get_rfq_detail(session, tenant, rfq_id))


@router.get(
    "/rfqs/{rfq_id}/comparison",
    response_model=ApiResponse[QuotationComparison],
    summary="Compare every quotation received for an RFQ",
    description=(
        "Side by side, one row per item and one column per vendor, with price, lead time and "
        "vendor rating. Each row names a recommended vendor and states the reason; the overall "
        "recommendation is the vendor that wins the most lines. No recommendation is made when "
        "the quotations are in different currencies."
    ),
    dependencies=[require_permission(Permission.QUOTATION_VIEW)],
)
async def compare_quotations(
    rfq_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[QuotationComparison]:
    return ApiResponse[QuotationComparison].ok(
        await service.compare_quotations(session, tenant, rfq_id)
    )


@router.patch(
    "/rfqs/{rfq_id}",
    response_model=ApiResponse[RfqDetail],
    summary="Update a draft RFQ",
    dependencies=[require_permission(Permission.RFQ_MANAGE)],
)
async def update_rfq(
    rfq_id: uuid.UUID, payload: RfqUpdate, session: DbSession, tenant: Tenant
) -> ApiResponse[RfqDetail]:
    rfq, lines = await service.update_rfq(session, tenant, rfq_id, payload)
    return ApiResponse[RfqDetail].ok(
        service.rfq_detail(rfq, lines), success_message(f"RFQ {rfq.code}", "updated")
    )


@router.post(
    "/rfqs/{rfq_id}/send",
    response_model=ApiResponse[RfqSummary],
    summary="Send an RFQ to its invited vendors",
    dependencies=[require_permission(Permission.RFQ_MANAGE)],
)
async def send_rfq(
    rfq_id: uuid.UUID, payload: ApproveRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[RfqSummary]:
    rfq = await service.transition_rfq(
        session, tenant, rfq_id, RfqStatus.SENT, reason=payload.reason or "Sent to vendors"
    )
    return ApiResponse[RfqSummary].ok(
        RfqSummary.model_validate(rfq), f"RFQ {rfq.code} sent to {len(rfq.vendor_ids)} vendor(s)"
    )


@router.post(
    "/rfqs/{rfq_id}/close",
    response_model=ApiResponse[RfqSummary],
    summary="Close an RFQ once a vendor has been chosen",
    dependencies=[require_permission(Permission.RFQ_MANAGE)],
)
async def close_rfq(
    rfq_id: uuid.UUID, payload: ApproveRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[RfqSummary]:
    rfq = await service.transition_rfq(
        session, tenant, rfq_id, RfqStatus.CLOSED, reason=payload.reason or "Closed"
    )
    return ApiResponse[RfqSummary].ok(
        RfqSummary.model_validate(rfq), success_message(f"RFQ {rfq.code}", "closed")
    )


@router.post(
    "/rfqs/{rfq_id}/cancel",
    response_model=ApiResponse[RfqSummary],
    summary="Cancel an RFQ",
    dependencies=[require_permission(Permission.RFQ_MANAGE)],
)
async def cancel_rfq(
    rfq_id: uuid.UUID, payload: ReasonRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[RfqSummary]:
    rfq = await service.transition_rfq(
        session, tenant, rfq_id, RfqStatus.CANCELLED, reason=payload.reason
    )
    return ApiResponse[RfqSummary].ok(
        RfqSummary.model_validate(rfq), success_message(f"RFQ {rfq.code}", "cancelled")
    )


# =============================================================================
# Vendor quotations
# =============================================================================


@router.get(
    "/quotations",
    response_model=PaginatedResponse[QuotationSummary],
    summary="List vendor quotations",
    dependencies=[require_permission(Permission.QUOTATION_VIEW)],
)
async def list_quotations(
    session: DbSession, tenant: Tenant, filters: QuotationFilterDep, params: PageDep, sort: SortDep
) -> PaginatedResponse[QuotationSummary]:
    rows, pagination = await service.list_quotations(session, tenant, filters, params, sort)
    return PaginatedResponse[QuotationSummary](data=rows, pagination=pagination)


@router.post(
    "/quotations",
    response_model=ApiResponse[QuotationDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Record a vendor's quotation against an RFQ",
    description=(
        "One live quotation per vendor per RFQ. A revised offer updates that quotation rather "
        "than creating a second one, so the comparison shows one column per vendor."
    ),
    dependencies=[require_permission(Permission.QUOTATION_MANAGE)],
)
async def create_quotation(
    payload: QuotationCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[QuotationDetail]:
    quotation, lines = await service.create_quotation(session, tenant, payload)
    return ApiResponse[QuotationDetail].ok(
        service.quotation_detail(quotation, lines),
        success_message(f"Quotation {quotation.vendor_reference}", "recorded"),
    )


@router.get(
    "/quotations/{quotation_id}",
    response_model=ApiResponse[QuotationDetail],
    summary="Fetch one quotation with its lines",
    dependencies=[require_permission(Permission.QUOTATION_VIEW)],
)
async def get_quotation(
    quotation_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[QuotationDetail]:
    return ApiResponse[QuotationDetail].ok(
        await service.get_quotation_detail(session, tenant, quotation_id)
    )


@router.patch(
    "/quotations/{quotation_id}",
    response_model=ApiResponse[QuotationDetail],
    summary="Record a revised offer from the same vendor",
    dependencies=[require_permission(Permission.QUOTATION_MANAGE)],
)
async def update_quotation(
    quotation_id: uuid.UUID, payload: QuotationUpdate, session: DbSession, tenant: Tenant
) -> ApiResponse[QuotationDetail]:
    quotation, lines = await service.update_quotation(session, tenant, quotation_id, payload)
    return ApiResponse[QuotationDetail].ok(
        service.quotation_detail(quotation, lines),
        f"Quotation {quotation.vendor_reference} revised",
    )


@router.post(
    "/quotations/{quotation_id}/select",
    response_model=ApiResponse[QuotationSummary],
    summary="Select a quotation to order against",
    description=(
        "The reason is required even when following the recommendation: the buyer's judgement "
        "is the record, and the comparison's score is only its input."
    ),
    dependencies=[require_permission(Permission.QUOTATION_MANAGE)],
)
async def select_quotation(
    quotation_id: uuid.UUID,
    payload: QuotationDecisionRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[QuotationSummary]:
    quotation = await service.decide_quotation(
        session, tenant, principal, quotation_id, QuotationStatus.SELECTED, reason=payload.reason
    )
    return ApiResponse[QuotationSummary].ok(
        QuotationSummary.model_validate(quotation),
        f"Quotation {quotation.vendor_reference} selected",
    )


@router.post(
    "/quotations/{quotation_id}/reject",
    response_model=ApiResponse[QuotationSummary],
    summary="Reject a quotation",
    dependencies=[require_permission(Permission.QUOTATION_MANAGE)],
)
async def reject_quotation(
    quotation_id: uuid.UUID,
    payload: QuotationDecisionRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[QuotationSummary]:
    quotation = await service.decide_quotation(
        session, tenant, principal, quotation_id, QuotationStatus.REJECTED, reason=payload.reason
    )
    return ApiResponse[QuotationSummary].ok(
        QuotationSummary.model_validate(quotation),
        success_message(f"Quotation {quotation.vendor_reference}", "rejected"),
    )


# =============================================================================
# Purchase orders
# =============================================================================


@router.get(
    "/purchase-orders",
    response_model=PaginatedResponse[PurchaseOrderSummary],
    summary="List purchase orders",
    dependencies=[require_permission(Permission.PURCHASE_ORDER_VIEW)],
)
async def list_purchase_orders(
    session: DbSession, tenant: Tenant, filters: OrderFilterDep, params: PageDep, sort: SortDep
) -> PaginatedResponse[PurchaseOrderSummary]:
    rows, pagination = await service.list_purchase_orders(session, tenant, filters, params, sort)
    return PaginatedResponse[PurchaseOrderSummary](data=rows, pagination=pagination)


@router.post(
    "/purchase-orders/check-duplicates",
    response_model=ApiResponse[DuplicateCheckResult],
    summary="Check a proposed order against open ones before raising it",
    description=(
        "Master rules section 16. Matches on vendor, item, warehouse, an overlapping required "
        "date window, the source demand reference and the pending requisition line. Each "
        "candidate carries the possible actions - merge, increase the existing quantity, create "
        "a new order with approval, or cancel the duplicate - and the recommended one."
    ),
    # Buyer-side: the answer names other vendors' open orders, their quantities
    # and dates, and the vendor in the payload is the caller's choice - so a
    # vendor account must not be able to ask it at all.
    dependencies=[
        require_permission(Permission.PURCHASE_ORDER_VIEW),
        Depends(forbid_vendor_users),
    ],
)
async def check_duplicates(
    payload: DuplicateCheckRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[DuplicateCheckResult]:
    result = await service.check_duplicates(session, tenant, payload)
    message = (
        f"{len(result.candidates)} possible duplicate(s) found"
        if result.candidates
        else "No duplicate purchase orders found"
    )
    return ApiResponse[DuplicateCheckResult].ok(result, message)


@router.post(
    "/purchase-orders/duplicate-resolution",
    response_model=ApiResponse[PurchaseOrderDuplicateResolutionResult],
    summary="Resolve duplicate demand atomically",
    description=(
        "Merge or increase a current candidate, request a separate order with approval, "
        "or cancel outstanding demand. Permissions depend on the selected action."
    ),
    dependencies=[
        require_any_permission(
            Permission.PURCHASE_ORDER_UPDATE,
            Permission.PURCHASE_ORDER_CREATE,
            Permission.PURCHASE_REQUISITION_CREATE,
        ),
        Depends(forbid_vendor_users),
    ],
)
async def resolve_purchase_order_duplicate(
    payload: PurchaseOrderDuplicateResolutionRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[PurchaseOrderDuplicateResolutionResult]:
    result = await purchase_order_changes.resolve_duplicate(session, tenant, principal, payload)
    codes = ", ".join(order.code for order in result.purchase_orders)
    message = {
        "MERGE": f"Demand merged into {codes}",
        "INCREASE": f"Purchase quantity increased on {codes}",
        "CREATE_WITH_APPROVAL": f"Separate purchase order {codes} raised for approval",
        "CANCEL_REQUEST": "Duplicate request discarded"
        if result.discarded
        else "Outstanding duplicate demand cancelled",
    }[result.action]
    return ApiResponse[PurchaseOrderDuplicateResolutionResult].ok(result, message)


@router.post(
    "/purchase-orders",
    response_model=ApiResponse[PurchaseOrderDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Raise a draft purchase order",
    description=(
        "Runs the duplicate check inside the creating transaction. An order with candidates is "
        "refused with DUPLICATE_DOCUMENT unless acknowledge_duplicates is set with a reason by "
        "a caller holding purchase_order.approve."
    ),
    dependencies=[require_permission(Permission.PURCHASE_ORDER_CREATE)],
)
async def create_purchase_order(
    payload: PurchaseOrderCreate, session: DbSession, tenant: Tenant, principal: CurrentUser
) -> ApiResponse[PurchaseOrderDetail]:
    order, lines, candidates = await service.create_purchase_order(
        session, tenant, principal, payload
    )
    message = f"Purchase order {order.code} raised"
    if candidates:
        message += f" over {len(candidates)} acknowledged duplicate(s)"
    return ApiResponse[PurchaseOrderDetail].ok(
        await service.describe_purchase_order(session, tenant, order, lines), message
    )


@router.get(
    "/purchase-orders/{order_id}",
    response_model=ApiResponse[PurchaseOrderDetail],
    summary="Fetch one purchase order with its lines",
    dependencies=[require_permission(Permission.PURCHASE_ORDER_VIEW)],
)
async def get_purchase_order(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[PurchaseOrderDetail]:
    return ApiResponse[PurchaseOrderDetail].ok(
        await service.get_purchase_order_detail(session, tenant, order_id)
    )


@router.get(
    "/purchase-orders/{order_id}/tracking",
    response_model=ApiResponse[PurchaseOrderTracking],
    summary="Inbound dispatch tracking for one order",
    description=(
        "Master rules section 23: courier and tracking reference, the promised date and whether "
        "the order is late, what is still to be received, and whether quantity is sitting on QC "
        "hold awaiting inspection."
    ),
    dependencies=[require_permission(Permission.PURCHASE_ORDER_VIEW)],
)
async def purchase_order_tracking(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[PurchaseOrderTracking]:
    return ApiResponse[PurchaseOrderTracking].ok(
        await service.order_tracking(session, tenant, order_id)
    )


@router.get(
    "/purchase-orders/{order_id}/vendor-assignees",
    response_model=ApiResponse[list[PurchaseOrderVendorAssignee]],
    summary="Vendor users this order can be sent to",
    description=(
        "The Send dialog's picker: active users of the order's vendor who may act on purchase "
        "orders and whose category scope covers the order's category (or is unrestricted), "
        "with the one the assignment rule would choose marked is_suggested. Served here because "
        "the buyer sending the order holds purchase_order.send, not user.view."
    ),
    dependencies=[
        require_permission(Permission.PURCHASE_ORDER_SEND),
        Depends(forbid_vendor_users),
    ],
)
async def list_vendor_assignees(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[list[PurchaseOrderVendorAssignee]]:
    return ApiResponse[list[PurchaseOrderVendorAssignee]].ok(
        await service.list_vendor_assignees(session, tenant, order_id)
    )


@router.patch(
    "/purchase-orders/{order_id}",
    response_model=ApiResponse[PurchaseOrderDetail],
    summary="Update a purchase order that is not yet approved",
    # Buyer-side: rewriting a draft's lines and prices is the buyer's document,
    # even for the vendor it names. Vendor principals hold the permission for
    # acknowledge/shipment, which stay reachable below.
    dependencies=[
        require_permission(Permission.PURCHASE_ORDER_UPDATE),
        Depends(forbid_vendor_users),
    ],
)
async def update_purchase_order(
    order_id: uuid.UUID,
    payload: PurchaseOrderUpdate,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[PurchaseOrderDetail]:
    order, lines = await service.update_purchase_order(
        session, tenant, principal, order_id, payload
    )
    return ApiResponse[PurchaseOrderDetail].ok(
        await service.describe_purchase_order(session, tenant, order, lines),
        success_message(f"Purchase order {order.code}", "updated"),
    )


@router.post(
    "/purchase-orders/{order_id}/amend",
    response_model=ApiResponse[PurchaseOrderDetail],
    summary="Revise an approved order and require reapproval",
    description=(
        "Requires the current version and no receipt history, including draft or cancelled "
        "GRNs. Retains existing line identities and explicit requisition allocations."
    ),
    dependencies=[
        require_permission(Permission.PURCHASE_ORDER_UPDATE),
        Depends(forbid_vendor_users),
    ],
)
async def amend_purchase_order(
    order_id: uuid.UUID,
    payload: PurchaseOrderAmendRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[PurchaseOrderDetail]:
    order, lines = await purchase_order_changes.amend_purchase_order(
        session, tenant, order_id, payload
    )
    return ApiResponse[PurchaseOrderDetail].ok(
        await service.describe_purchase_order(session, tenant, order, lines),
        f"{order.code} revision {order.amendment_number} submitted for approval",
    )


@router.get(
    "/purchase-orders/{order_id}/amendments",
    response_model=CursorPaginatedResponse[PurchaseOrderAmendmentRead],
    summary="Read the purchase order's revision history",
    dependencies=[require_permission(Permission.PURCHASE_ORDER_VIEW), Depends(forbid_vendor_users)],
)
async def list_purchase_order_amendments(
    order_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    params: Annotated[CursorParams, Depends(cursor_params)],
) -> CursorPaginatedResponse[PurchaseOrderAmendmentRead]:
    rows, pagination = await purchase_order_changes.list_amendments(
        session, tenant, order_id, params
    )
    return CursorPaginatedResponse[PurchaseOrderAmendmentRead](data=rows, pagination=pagination)


@router.post(
    "/purchase-orders/{order_id}/submit",
    response_model=ApiResponse[PurchaseOrderSummary],
    summary="Submit a purchase order for approval",
    # Buyer-side, like the edit above: submitting for approval is the buyer's act.
    dependencies=[
        require_permission(Permission.PURCHASE_ORDER_UPDATE),
        Depends(forbid_vendor_users),
    ],
)
async def submit_purchase_order(
    order_id: uuid.UUID,
    payload: ApproveRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[PurchaseOrderSummary]:
    order = await service.transition_purchase_order(
        session,
        tenant,
        principal,
        order_id,
        PurchaseOrderStatus.PENDING_APPROVAL,
        reason=payload.reason or "Submitted for approval",
    )
    return ApiResponse[PurchaseOrderSummary].ok(
        PurchaseOrderSummary.model_validate(order),
        f"Purchase order {order.code} submitted for approval",
    )


@router.post(
    "/purchase-orders/{order_id}/approve",
    response_model=ApiResponse[PurchaseOrderSummary],
    summary="Approve a purchase order",
    description="The approver may not be the person who raised it unless they hold SUPER_ADMIN.",
    dependencies=[require_permission(Permission.PURCHASE_ORDER_APPROVE)],
)
async def approve_purchase_order(
    order_id: uuid.UUID,
    payload: PurchaseOrderApprovalRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[PurchaseOrderSummary]:
    order = await service.transition_purchase_order(
        session,
        tenant,
        principal,
        order_id,
        PurchaseOrderStatus.APPROVED,
        reason=payload.reason or "Approved",
        confirm_duplicate=getattr(payload, "confirm_duplicate", False),
    )
    return ApiResponse[PurchaseOrderSummary].ok(
        PurchaseOrderSummary.model_validate(order),
        success_message(f"Purchase order {order.code}", "approved"),
    )


@router.post(
    "/purchase-orders/{order_id}/reject",
    response_model=ApiResponse[PurchaseOrderSummary],
    summary="Reject a purchase order",
    dependencies=[require_permission(Permission.PURCHASE_ORDER_REJECT)],
)
async def reject_purchase_order(
    order_id: uuid.UUID,
    payload: ReasonRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[PurchaseOrderSummary]:
    order = await service.transition_purchase_order(
        session, tenant, principal, order_id, PurchaseOrderStatus.REJECTED, reason=payload.reason
    )
    return ApiResponse[PurchaseOrderSummary].ok(
        PurchaseOrderSummary.model_validate(order),
        success_message(f"Purchase order {order.code}", "rejected"),
    )


@router.post(
    "/purchase-orders/{order_id}/send",
    response_model=ApiResponse[PurchaseOrderSummary],
    summary="Send an approved order to the vendor",
    dependencies=[require_permission(Permission.PURCHASE_ORDER_SEND)],
)
async def send_purchase_order(
    order_id: uuid.UUID,
    payload: SendRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[PurchaseOrderSummary]:
    order = await service.transition_purchase_order(
        session,
        tenant,
        principal,
        order_id,
        PurchaseOrderStatus.SENT,
        reason=payload.reason or "Sent to vendor",
        assigned_vendor_user_id=payload.assigned_vendor_user_id,
    )
    return ApiResponse[PurchaseOrderSummary].ok(
        PurchaseOrderSummary.model_validate(order), f"Purchase order {order.code} sent to vendor"
    )


@router.post(
    "/purchase-orders/{order_id}/acknowledge",
    response_model=ApiResponse[PurchaseOrderSummary],
    summary="Record the vendor's acknowledgement on its behalf",
    description=(
        "A buyer recording an acceptance the vendor gave by phone or e-mail; the vendor itself "
        "accepts through /vendor-portal. Here the buyer may move the planning date."
    ),
    # Vendor accounts reach orders only through /vendor-portal (the route
    # allow-list refuses this path first); kept as a second line of defence.
    dependencies=[
        require_permission(Permission.PURCHASE_ORDER_UPDATE),
        Depends(forbid_vendor_users),
    ],
)
async def acknowledge_purchase_order(
    order_id: uuid.UUID,
    payload: AcknowledgeRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[PurchaseOrderSummary]:
    order = await service.transition_purchase_order(
        session,
        tenant,
        principal,
        order_id,
        PurchaseOrderStatus.ACKNOWLEDGED,
        reason=payload.reason or "Acknowledged by vendor",
        expected_delivery_date=payload.expected_delivery_date,
        vendor_reference=payload.vendor_reference,
    )
    return ApiResponse[PurchaseOrderSummary].ok(
        PurchaseOrderSummary.model_validate(order),
        f"Purchase order {order.code} acknowledged by the vendor",
    )


@router.post(
    "/purchase-orders/{order_id}/shipment",
    response_model=ApiResponse[PurchaseOrderSummary],
    summary="Record inbound courier and tracking details on the order header",
    description=(
        "The single-shipment header fields (master rules section 23). Numbered consignments "
        "are recorded with POST /purchase-orders/{order_id}/shipments, which also keeps these "
        "fields in step with the latest consignment."
    ),
    dependencies=[
        require_permission(Permission.PURCHASE_ORDER_UPDATE),
        Depends(forbid_vendor_users),
    ],
)
async def record_shipment(
    order_id: uuid.UUID, payload: ShipmentUpdate, session: DbSession, tenant: Tenant
) -> ApiResponse[PurchaseOrderSummary]:
    order = await service.record_shipment(
        session,
        tenant,
        order_id,
        courier_name=payload.courier_name,
        tracking_reference=payload.tracking_reference,
        shipped_at=payload.shipped_at,
        expected_delivery_date=payload.expected_delivery_date,
    )
    return ApiResponse[PurchaseOrderSummary].ok(
        PurchaseOrderSummary.model_validate(order), f"Shipment details recorded on {order.code}"
    )


@router.post(
    "/purchase-orders/{order_id}/cancel",
    response_model=ApiResponse[PurchaseOrderSummary],
    summary="Cancel a purchase order before anything is received",
    dependencies=[require_permission(Permission.PURCHASE_ORDER_CANCEL)],
)
async def cancel_purchase_order(
    order_id: uuid.UUID,
    payload: ReasonRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[PurchaseOrderSummary]:
    order = await service.transition_purchase_order(
        session, tenant, principal, order_id, PurchaseOrderStatus.CANCELLED, reason=payload.reason
    )
    return ApiResponse[PurchaseOrderSummary].ok(
        PurchaseOrderSummary.model_validate(order),
        success_message(f"Purchase order {order.code}", "cancelled"),
    )


@router.post(
    "/purchase-orders/{order_id}/close",
    response_model=ApiResponse[PurchaseOrderSummary],
    summary="Close a purchase order",
    description=(
        "From PARTIALLY_RECEIVED this is a short close: the undelivered balance is no longer "
        "expected and its lines stop holding the order open."
    ),
    dependencies=[require_permission(Permission.PURCHASE_ORDER_CANCEL)],
)
async def close_purchase_order(
    order_id: uuid.UUID,
    payload: ReasonRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[PurchaseOrderSummary]:
    order = await service.transition_purchase_order(
        session, tenant, principal, order_id, PurchaseOrderStatus.CLOSED, reason=payload.reason
    )
    return ApiResponse[PurchaseOrderSummary].ok(
        PurchaseOrderSummary.model_validate(order),
        success_message(f"Purchase order {order.code}", "closed"),
    )


# =============================================================================
# Inbound consignments and tracking (WP C1a) - the buyer's side
# =============================================================================
#
# The vendor records the same consignments through /vendor-portal; these routes
# let a buyer do it on the vendor's behalf (a vendor that phones the details in)
# and give the buying office its inbound board. Vendor accounts are refused by
# the route allow-list before they get here; ``forbid_vendor_users`` is the
# second line, as on every buyer-only route.


@router.get(
    "/inbound-tracking",
    response_model=PaginatedResponse[InboundTrackingRead],
    summary="Inbound tracking board",
    description=(
        "Open purchase orders with their latest consignment and the derived tracking status "
        "(NOT_DISPATCHED to GRN_COMPLETED). Completed, closed and cancelled orders appear only "
        "with include_completed. overdue uses the shared PO-late rule: receivable, goods still "
        "due, planning date passed."
    ),
    dependencies=[
        require_permission(Permission.PURCHASE_ORDER_VIEW),
        Depends(forbid_vendor_users),
    ],
)
async def inbound_tracking(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    overdue: bool = False,
    expected_today: bool = False,
    in_transit: bool = False,
    not_acknowledged: bool = False,
    include_completed: bool = False,
    vendor_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
    search: Annotated[str | None, Query(max_length=120)] = None,
) -> PaginatedResponse[InboundTrackingRead]:
    rows, page = await inbound_service.tracking_board(
        session,
        tenant,
        bind_query_filters(
            InboundTrackingFilters,
            overdue=overdue,
            expected_today=expected_today,
            in_transit=in_transit,
            not_acknowledged=not_acknowledged,
            include_completed=include_completed,
            vendor_id=vendor_id,
            category_id=category_id,
            search=search,
        ),
        params,
    )
    return PaginatedResponse(data=rows, pagination=page)


@router.get(
    "/purchase-orders/{order_id}/shipments",
    response_model=PaginatedResponse[InboundShipmentRead],
    summary="The order's numbered consignments, newest first",
    dependencies=[
        require_permission(Permission.PURCHASE_ORDER_VIEW),
        Depends(forbid_vendor_users),
    ],
)
async def list_purchase_order_shipments(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[InboundShipmentRead]:
    rows, page = await inbound_service.list_shipments(session, tenant, order_id, params)
    return PaginatedResponse(data=rows, pagination=page)


@router.post(
    "/purchase-orders/{order_id}/shipments",
    response_model=ApiResponse[InboundShipmentRead],
    status_code=status.HTTP_201_CREATED,
    summary="Record a consignment on the vendor's behalf",
    description=(
        "Same body and rules as the vendor portal: only on an accepted order with goods still "
        "due. Notifies the order's creator, its approvers and administrators."
    ),
    dependencies=[
        require_permission(Permission.PURCHASE_ORDER_UPDATE),
        Depends(forbid_vendor_users),
    ],
)
async def create_purchase_order_shipment(
    order_id: uuid.UUID, payload: InboundShipmentCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[InboundShipmentRead]:
    shipment = await inbound_service.record_inbound_shipment(session, tenant, order_id, payload)
    return ApiResponse[InboundShipmentRead].ok(
        shipment, success_message(f"Shipment {shipment.shipment_number}", "recorded")
    )


@router.patch(
    "/purchase-orders/{order_id}/shipments/{shipment_id}",
    response_model=ApiResponse[InboundShipmentRead],
    summary="Correct a consignment",
    description="Replaces the consignment's details; the same validation as recording it.",
    dependencies=[
        require_permission(Permission.PURCHASE_ORDER_UPDATE),
        Depends(forbid_vendor_users),
    ],
)
async def update_purchase_order_shipment(
    order_id: uuid.UUID,
    shipment_id: uuid.UUID,
    payload: InboundShipmentCreate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[InboundShipmentRead]:
    shipment = await inbound_service.record_inbound_shipment(
        session, tenant, order_id, payload, shipment_id=shipment_id
    )
    return ApiResponse[InboundShipmentRead].ok(
        shipment, success_message(f"Shipment {shipment.shipment_number}", "updated")
    )


@router.post(
    "/purchase-orders/{order_id}/shipments/{shipment_id}/delivered",
    response_model=ApiResponse[InboundShipmentRead],
    summary="Mark a consignment delivered at the gate",
    dependencies=[
        require_permission(Permission.GRN_CREATE),
        Depends(forbid_vendor_users),
    ],
)
async def mark_purchase_order_shipment_delivered(
    order_id: uuid.UUID,
    shipment_id: uuid.UUID,
    payload: InboundDeliveredRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[InboundShipmentRead]:
    shipment = await inbound_service.mark_delivered(
        session, tenant, order_id, shipment_id, payload.delivered_at
    )
    return ApiResponse[InboundShipmentRead].ok(
        shipment, f"Shipment {shipment.shipment_number} marked delivered"
    )


@router.post(
    "/purchase-orders/{order_id}/documents",
    response_model=ApiResponse[InboundDocumentRead],
    status_code=status.HTTP_201_CREATED,
    summary="File a typed document against the order or one of its consignments",
    description=(
        "Multipart: file, kind (INVOICE, DISPATCH_PHOTO, LR_COPY, DELIVERY_CHALLAN, OTHER) and "
        "an optional shipment_id. Files filed here are shared with the order's vendor."
    ),
    dependencies=[
        require_permission(Permission.DOCUMENT_UPLOAD),
        Depends(forbid_vendor_users),
    ],
)
async def upload_purchase_order_document(
    order_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    file: Annotated[UploadFile, File()],
    kind: Annotated[DocumentKind, Form()],
    shipment_id: Annotated[uuid.UUID | None, Form()] = None,
) -> ApiResponse[InboundDocumentRead]:
    document = await inbound_service.upload_document(
        session, tenant, order_id, file, kind, shipment_id
    )
    return ApiResponse[InboundDocumentRead].ok(document, f"{document.file_name} attached")


@router.get(
    "/purchase-orders/{order_id}/documents",
    response_model=PaginatedResponse[InboundDocumentRead],
    summary="Every file filed against the order, typed where it was uploaded with a kind",
    description="Download through GET /documents/{document_id}/download.",
    dependencies=[
        require_permission(Permission.PURCHASE_ORDER_VIEW),
        Depends(forbid_vendor_users),
    ],
)
async def list_purchase_order_documents(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[InboundDocumentRead]:
    rows, page = await inbound_service.list_documents(session, tenant, order_id, params)
    return PaginatedResponse(data=rows, pagination=page)


@router.post(
    "/purchase-orders/{order_id}/accept-vendor-date",
    response_model=ApiResponse[PurchaseOrderSummary],
    summary="Adopt the vendor's committed date as the planning date",
    description=(
        "Copies vendor_committed_date into expected_delivery_date (which MRP nets on). The two "
        "are kept apart until a buyer does this; a no-op when they already agree."
    ),
    dependencies=[
        require_permission(Permission.PURCHASE_ORDER_UPDATE),
        Depends(forbid_vendor_users),
    ],
)
async def accept_vendor_date(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[PurchaseOrderSummary]:
    order = await inbound_service.accept_vendor_date(session, tenant, order_id)
    return ApiResponse[PurchaseOrderSummary].ok(
        order, f"Planning date of {order.code} set to the vendor's commitment"
    )


@router.post(
    "/purchase-orders/{order_id}/pdf",
    response_model=AcceptedJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Generate the purchase order PDF",
    description="Queued; poll the returned status URL, then download the document.",
    dependencies=[
        require_permission(Permission.PURCHASE_ORDER_VIEW),
        Depends(forbid_vendor_users),
    ],
)
async def purchase_order_pdf(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> AcceptedJobResponse:
    return await queue_purchase_order_pdf(session, tenant, order_id)


# =============================================================================
# Goods receipts
# =============================================================================


@router.get(
    "/goods-receipts",
    response_model=PaginatedResponse[GoodsReceiptSummary],
    summary="List goods receipts",
    dependencies=[require_permission(Permission.GRN_VIEW)],
)
async def list_goods_receipts(
    session: DbSession, tenant: Tenant, filters: ReceiptFilterDep, params: PageDep, sort: SortDep
) -> PaginatedResponse[GoodsReceiptSummary]:
    rows, pagination = await service.list_goods_receipts(session, tenant, filters, params, sort)
    return PaginatedResponse[GoodsReceiptSummary](data=rows, pagination=pagination)


@router.post(
    "/goods-receipts",
    response_model=ApiResponse[GoodsReceiptDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Record a delivery as a draft receipt",
    description=(
        "Validates each line against the order and the over-receipt tolerance at the gate. "
        "No stock moves until the receipt is posted."
    ),
    dependencies=[require_permission(Permission.GRN_CREATE)],
)
async def create_goods_receipt(
    payload: GoodsReceiptCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[GoodsReceiptDetail]:
    receipt, lines = await service.create_goods_receipt(session, tenant, payload)
    return ApiResponse[GoodsReceiptDetail].ok(
        service.receipt_detail(receipt, lines),
        success_message(f"Goods receipt {receipt.code}", "recorded"),
    )


@router.get(
    "/goods-receipts/{receipt_id}",
    response_model=ApiResponse[GoodsReceiptDetail],
    summary="Fetch one goods receipt with its lines",
    dependencies=[require_permission(Permission.GRN_VIEW)],
)
async def get_goods_receipt(
    receipt_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[GoodsReceiptDetail]:
    return ApiResponse[GoodsReceiptDetail].ok(
        await service.get_goods_receipt_detail(session, tenant, receipt_id)
    )


@router.post(
    "/goods-receipts/{receipt_id}/post",
    response_model=ApiResponse[GoodsReceiptPostResult],
    summary="Post a goods receipt: move the stock and advance the order",
    description=(
        "One transaction: the tolerance is re-checked against locked quantities, accepted "
        "quantity is posted as PURCHASE_RECEIPT into AVAILABLE or QC_HOLD, incoming "
        "inspections are raised, the purchase order advances through its state machine, and "
        "every audit row is written. A short receipt leaves the order PARTIALLY_RECEIVED."
    ),
    dependencies=[require_permission(Permission.GRN_POST)],
)
async def post_goods_receipt(
    receipt_id: uuid.UUID, session: DbSession, tenant: Tenant, principal: CurrentUser
) -> ApiResponse[GoodsReceiptPostResult]:
    result = await service.post_goods_receipt(session, tenant, principal, receipt_id)
    message = (
        f"Goods receipt {result.receipt.code} posted; "
        f"purchase order is now {result.purchase_order_status}"
    )
    if result.qc_hold_line_count:
        message += f" ({result.qc_hold_line_count} line(s) held for inspection)"
    return ApiResponse[GoodsReceiptPostResult].ok(result, message)


@router.post(
    "/goods-receipts/{receipt_id}/cancel",
    response_model=ApiResponse[GoodsReceiptSummary],
    summary="Cancel a draft goods receipt",
    description="A posted receipt has moved stock; reverse it with a purchase return instead.",
    dependencies=[require_permission(Permission.GRN_CREATE)],
)
async def cancel_goods_receipt(
    receipt_id: uuid.UUID, payload: ReasonRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[GoodsReceiptSummary]:
    receipt = await service.cancel_goods_receipt(session, tenant, receipt_id, reason=payload.reason)
    return ApiResponse[GoodsReceiptSummary].ok(
        GoodsReceiptSummary.model_validate(receipt),
        success_message(f"Goods receipt {receipt.code}", "cancelled"),
    )


# =============================================================================
# Purchase returns
# =============================================================================


@router.get(
    "/purchase-returns",
    response_model=PaginatedResponse[PurchaseReturnSummary],
    summary="List purchase returns",
    dependencies=[require_permission(Permission.PURCHASE_RETURN_VIEW)],
)
async def list_purchase_returns(
    session: DbSession, tenant: Tenant, filters: ReturnFilterDep, params: PageDep, sort: SortDep
) -> PaginatedResponse[PurchaseReturnSummary]:
    rows, pagination = await service.list_purchase_returns(session, tenant, filters, params, sort)
    return PaginatedResponse[PurchaseReturnSummary](data=rows, pagination=pagination)


@router.post(
    "/purchase-returns",
    response_model=ApiResponse[PurchaseReturnDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Raise a draft return against a posted receipt",
    description=(
        "Each line names the goods receipt line it reverses and is bounded by what that line "
        "actually accepted."
    ),
    dependencies=[require_permission(Permission.PURCHASE_RETURN_CREATE)],
)
async def create_purchase_return(
    payload: PurchaseReturnCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[PurchaseReturnDetail]:
    purchase_return, lines = await service.create_purchase_return(session, tenant, payload)
    return ApiResponse[PurchaseReturnDetail].ok(
        service.return_detail(purchase_return, lines),
        success_message(f"Purchase return {purchase_return.code}", "raised"),
    )


@router.get(
    "/purchase-returns/{return_id}",
    response_model=ApiResponse[PurchaseReturnDetail],
    summary="Fetch one purchase return with its lines",
    dependencies=[require_permission(Permission.PURCHASE_RETURN_VIEW)],
)
async def get_purchase_return(
    return_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[PurchaseReturnDetail]:
    return ApiResponse[PurchaseReturnDetail].ok(
        await service.get_purchase_return_detail(session, tenant, return_id)
    )


@router.post(
    "/purchase-returns/{return_id}/debit-note-pdf",
    response_model=AcceptedJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue the vendor debit-note PDF for a posted purchase return",
    dependencies=[
        require_permission(Permission.PURCHASE_RETURN_VIEW),
        Depends(forbid_vendor_users),
    ],
)
async def purchase_return_debit_note_pdf(
    return_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> AcceptedJobResponse:
    job_id = await queue_purchase_return_debit_note_pdf(session, tenant, return_id)
    return AcceptedJobResponse.create(
        job_id=job_id,
        status_url=reports_service.job_status_url(job_id),
        message="Vendor debit note PDF queued",
    )


@router.post(
    "/purchase-returns/{return_id}/post",
    response_model=ApiResponse[PurchaseReturnPostResult],
    summary="Post a purchase return: move the stock back out",
    description=(
        "Writes one PURCHASE_RETURN movement per line against the same batch the goods came in "
        "on, updates the receipt and order lines it reverses, and posts the vendor debit and "
        "input-tax reversal in the same transaction."
    ),
    dependencies=[require_permission(Permission.PURCHASE_RETURN_CREATE)],
)
async def post_purchase_return(
    return_id: uuid.UUID, session: DbSession, tenant: Tenant, principal: CurrentUser
) -> ApiResponse[PurchaseReturnPostResult]:
    result = await service.post_purchase_return(session, tenant, principal, return_id)
    return ApiResponse[PurchaseReturnPostResult].ok(
        result, success_message(f"Purchase return {result.purchase_return.code}", "posted")
    )


@router.post(
    "/purchase-returns/{return_id}/cancel",
    response_model=ApiResponse[PurchaseReturnSummary],
    summary="Cancel a draft purchase return",
    dependencies=[require_permission(Permission.PURCHASE_RETURN_CREATE)],
)
async def cancel_purchase_return(
    return_id: uuid.UUID, payload: ReasonRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[PurchaseReturnSummary]:
    purchase_return = await service.cancel_purchase_return(
        session, tenant, return_id, reason=payload.reason
    )
    return ApiResponse[PurchaseReturnSummary].ok(
        PurchaseReturnSummary.model_validate(purchase_return),
        success_message(f"Purchase return {purchase_return.code}", "cancelled"),
    )


__all__ = ["router"]
