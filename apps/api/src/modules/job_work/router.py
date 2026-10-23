"""HTTP surface for job work.

Handlers bind parameters, call one service function and wrap the result in the
standard envelope. No rule, query or audit write lives here (master rules
section 74).

Route ordering note: ``/pending-at-vendor`` and ``/charges`` are declared before
``/{order_id}``. FastAPI matches in declaration order, and a literal segment
declared after a path parameter of the same shape would be captured by it and
rejected as a malformed UUID.

Permission note: the vocabulary has no ``JOB_WORK_APPROVE``. Approving an order
and completing one are gated on ``JOB_WORK_CLOSE``, the module's senior right -
held by the plant manager and not by the planner who raises and issues orders -
so raising and approving stay separate duties. See the service's Assumptions.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
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
from src.core.responses import (
    COMMON_ERROR_RESPONSES,
    AcceptedJobResponse,
    ApiResponse,
    PaginatedResponse,
)
from src.modules.job_work import service
from src.modules.job_work.models import JobWorkOrderStatus, JobWorkProcess
from src.modules.job_work.schemas import (
    BillableLine,
    ChallanFilters,
    ChallanRead,
    IssueRequest,
    IssueResult,
    JobWorkOrderCreate,
    JobWorkOrderDetail,
    JobWorkOrderFilters,
    JobWorkOrderSummary,
    JobWorkOrderUpdate,
    PendingAtVendorRow,
    QcResultRequest,
    ReasonRequest,
    ReceiptRead,
    ReceiptResult,
    ReceiveRequest,
    ReconciliationReport,
    VendorChargeRow,
    VendorLedgerReport,
)
from src.modules.reports import service as reports_service

router = APIRouter(prefix="/job-work", tags=["Job Work"], responses=COMMON_ERROR_RESPONSES)

PageDep = Annotated[PageParams, Depends(page_params)]
SortDep = Annotated[SortParams, Depends(sort_params)]

# Deletion carries its reason in the query string: a DELETE body is legal but
# several HTTP clients and proxies drop it, and the reason is not optional
# (master rules section 83).
ReasonQuery = Annotated[
    str, Query(min_length=3, max_length=255, description="Why the draft is being deleted")
]


def order_filters(
    search: Annotated[
        str | None, Query(max_length=120, description="Order code or process description")
    ] = None,
    vendor_id: Annotated[uuid.UUID | None, Query()] = None,
    item_id: Annotated[uuid.UUID | None, Query(description="Input material on any line")] = None,
    warehouse_id: Annotated[
        uuid.UUID | None, Query(description="Warehouse the material is drawn from")
    ] = None,
    order_status: Annotated[JobWorkOrderStatus | None, Query(alias="status")] = None,
    process_type: Annotated[JobWorkProcess | None, Query()] = None,
    at_vendor_only: Annotated[
        bool, Query(description="Only orders with company material at a vendor")
    ] = False,
    open_only: Annotated[
        bool, Query(description="Only orders that still owe processed stock")
    ] = False,
    due_before: Annotated[date | None, Query(description="Due back on or before this date")] = None,
    created_from: Annotated[datetime | None, Query()] = None,
    created_to: Annotated[datetime | None, Query()] = None,
) -> JobWorkOrderFilters:
    return bind_query_filters(
        JobWorkOrderFilters,
        search=search,
        vendor_id=vendor_id,
        item_id=item_id,
        warehouse_id=warehouse_id,
        status=order_status,
        process_type=process_type,
        at_vendor_only=at_vendor_only,
        open_only=open_only,
        due_before=due_before,
        created_from=created_from,
        created_to=created_to,
    )


OrderFilterDep = Annotated[JobWorkOrderFilters, Depends(order_filters)]


# -----------------------------------------------------------------------------
# Feeds (declared first - see the module docstring)
# -----------------------------------------------------------------------------


@router.get(
    "/pending-at-vendor",
    response_model=PaginatedResponse[PendingAtVendorRow],
    summary="Company material currently at a vendor, by vendor and item, with ageing",
    description=(
        "Every row is stock the company still owns and still values - it is at somebody "
        "else's premises and held in the JOB_WORK state, so it is counted in the valuation "
        "and in stock reports but cannot be promised to anyone. Aggregated from what was "
        "issued against what has been accounted back, with the ageing measured from the "
        "despatch rather than from the promised return date."
    ),
    dependencies=[require_permission(Permission.JOB_WORK_VIEW)],
)
async def pending_at_vendor(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    vendor_id: Annotated[uuid.UUID | None, Query(description="Restrict to one vendor")] = None,
    item_id: Annotated[uuid.UUID | None, Query(description="Restrict to one item")] = None,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    overdue_only: Annotated[
        bool, Query(description="Only material past its promised return date")
    ] = False,
) -> PaginatedResponse[PendingAtVendorRow]:
    rows, pagination = await service.list_pending_at_vendor(
        session,
        tenant,
        vendor_id=vendor_id,
        item_id=item_id,
        warehouse_id=warehouse_id,
        overdue_only=overdue_only,
        params=params,
    )
    return PaginatedResponse[PendingAtVendorRow](data=rows, pagination=pagination)


@router.get(
    "/charges",
    response_model=PaginatedResponse[VendorChargeRow],
    summary="Per-vendor job work charges for a period",
    description=(
        "The figures finance posts to the vendor ledger. Charges accrue on receipt, so the "
        "period is applied to the receipt date: a vendor earns the fee when the processed "
        "goods come back. Net charges are the gross processing fee less the value of company "
        "material the vendor lost beyond the agreed allowance. One vendor per row, largest "
        "spend first, paged - `has_next` says when there is more to post."
    ),
    dependencies=[require_any_permission(Permission.FINANCE_VIEW, Permission.JOB_WORK_VIEW)],
)
async def vendor_charges(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    date_from: Annotated[date, Query(description="First receipt date in the period")],
    date_to: Annotated[date, Query(description="Last receipt date in the period")],
    vendor_id: Annotated[
        list[uuid.UUID] | None, Query(description="Restrict to these vendors")
    ] = None,
) -> PaginatedResponse[VendorChargeRow]:
    rows, pagination = await service.list_charges_page(
        session,
        tenant,
        date_from=date_from,
        date_to=date_to,
        vendor_ids=vendor_id,
        params=params,
    )
    return PaginatedResponse[VendorChargeRow](data=rows, pagination=pagination)


def challan_filters(
    search: Annotated[
        str | None, Query(max_length=120, description="Challan code or vehicle number")
    ] = None,
    vendor_id: Annotated[uuid.UUID | None, Query()] = None,
    job_work_order_id: Annotated[
        uuid.UUID | None, Query(description="Only this order's challans")
    ] = None,
    date_from: Annotated[date | None, Query(description="Despatched on or after this date")] = None,
    date_to: Annotated[date | None, Query(description="Despatched on or before this date")] = None,
) -> ChallanFilters:
    return bind_query_filters(
        ChallanFilters,
        search=search,
        vendor_id=vendor_id,
        job_work_order_id=job_work_order_id,
        date_from=date_from,
        date_to=date_to,
    )


ChallanFilterDep = Annotated[ChallanFilters, Depends(challan_filters)]


@router.get(
    "/challans",
    response_model=PaginatedResponse[ChallanRead],
    summary="Company-wide delivery-challan register",
    description=(
        "Every job-work delivery challan, newest despatch first, with vendor, order "
        "and date filters. The register the GST delivery challan is filed from: "
        "each row names the consignment, and the full document with its lines is "
        "one per-order call away."
    ),
    dependencies=[require_permission(Permission.JOB_WORK_VIEW)],
)
async def challan_register(
    session: DbSession,
    tenant: Tenant,
    filters: ChallanFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[ChallanRead]:
    rows, pagination = await service.list_challan_register(session, tenant, filters, params, sort)
    return PaginatedResponse[ChallanRead](data=rows, pagination=pagination)


@router.get(
    "/vendor-ledger",
    response_model=ApiResponse[VendorLedgerReport],
    summary="One job-work vendor's material account for a period",
    description=(
        "Per input item: what was already at the vendor when the period opened, "
        "what the challans sent, what the receipts accounted back - good output, "
        "unworked returns, process loss - and what is still out, beside the "
        "processing charges accrued, billed and debited. Returned whole rather "
        "than paginated: the rows are aggregated in the database and a vendor's "
        "item set is small."
    ),
    dependencies=[require_permission(Permission.JOB_WORK_VIEW)],
)
async def vendor_ledger(
    session: DbSession,
    tenant: Tenant,
    vendor_id: Annotated[uuid.UUID, Query(description="The job worker")],
    date_from: Annotated[date, Query(description="First date in the period")],
    date_to: Annotated[date, Query(description="Last date in the period")],
) -> ApiResponse[VendorLedgerReport]:
    return ApiResponse[VendorLedgerReport].ok(
        await service.vendor_ledger(
            session, tenant, vendor_id=vendor_id, date_from=date_from, date_to=date_to
        )
    )


@router.post(
    "/challans/{challan_id}/pdf",
    response_model=AcceptedJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue the delivery-challan PDF",
    description=(
        "Hands the GST Rule 55 render to the reports queue and returns a job id "
        "immediately. Poll GET /reports/jobs/{id}; once it completes, "
        "`download_url` points at the PDF in the document store. Rendering never "
        "happens on an HTTP worker (section 22)."
    ),
    dependencies=[require_permission(Permission.JOB_WORK_VIEW)],
)
async def challan_pdf(
    challan_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> AcceptedJobResponse:
    job_id = await service.queue_job_work_challan(session, tenant, challan_id)
    return AcceptedJobResponse.create(
        job_id=job_id,
        status_url=reports_service.job_status_url(job_id),
        message="The delivery challan is being generated",
    )


# -----------------------------------------------------------------------------
# CRUD
# -----------------------------------------------------------------------------


@router.get(
    "",
    response_model=PaginatedResponse[JobWorkOrderSummary],
    summary="List job work orders",
    dependencies=[require_permission(Permission.JOB_WORK_VIEW)],
)
async def list_orders(
    session: DbSession,
    tenant: Tenant,
    filters: OrderFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[JobWorkOrderSummary]:
    rows, pagination = await service.list_orders(session, tenant, filters, params, sort)
    return PaginatedResponse[JobWorkOrderSummary](
        data=[JobWorkOrderSummary.model_validate(row) for row in rows], pagination=pagination
    )


@router.post(
    "",
    response_model=ApiResponse[JobWorkOrderDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Raise a draft job work order",
    description=(
        "Validates the vendor, the warehouses and every line against the item master, and "
        "freezes the yield ratio and the estimated charge onto the order. No stock is "
        "reserved and none moves: a draft is a plan."
    ),
    dependencies=[require_permission(Permission.JOB_WORK_CREATE)],
)
async def create_order(
    payload: JobWorkOrderCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[JobWorkOrderDetail]:
    order, lines = await service.create_order(session, tenant, payload)
    return ApiResponse[JobWorkOrderDetail].ok(
        service.order_detail(order, lines),
        success_message(f"Job work order {order.code}", "created"),
    )


@router.get(
    "/{order_id}",
    response_model=ApiResponse[JobWorkOrderDetail],
    summary="Fetch one job work order with its lines",
    dependencies=[require_permission(Permission.JOB_WORK_VIEW)],
)
async def get_order(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[JobWorkOrderDetail]:
    return ApiResponse[JobWorkOrderDetail].ok(
        await service.get_order_detail(session, tenant, order_id)
    )


@router.patch(
    "/{order_id}",
    response_model=ApiResponse[JobWorkOrderDetail],
    summary="Amend a draft job work order",
    description=(
        "Only a DRAFT is editable. From APPROVED the order holds hard stock reservations, "
        "and from ISSUED it holds company material at somebody else's premises. Supplying "
        "`lines` replaces the whole set."
    ),
    dependencies=[require_permission(Permission.JOB_WORK_CREATE)],
)
async def update_order(
    order_id: uuid.UUID, payload: JobWorkOrderUpdate, session: DbSession, tenant: Tenant
) -> ApiResponse[JobWorkOrderDetail]:
    order, lines = await service.update_order(session, tenant, order_id, payload)
    return ApiResponse[JobWorkOrderDetail].ok(
        service.order_detail(order, lines),
        success_message(f"Job work order {order.code}", "updated"),
    )


@router.delete(
    "/{order_id}",
    response_model=ApiResponse[None],
    summary="Delete a draft job work order",
    description="Only a DRAFT may be deleted. Anything further along is cancelled instead.",
    dependencies=[require_permission(Permission.JOB_WORK_CREATE)],
)
async def delete_order(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant, reason: ReasonQuery
) -> ApiResponse[None]:
    await service.delete_draft_order(session, tenant, order_id, reason=reason)
    return ApiResponse[None].ok(None, success_message("Draft job work order", "deleted"))


# -----------------------------------------------------------------------------
# Documents and reports for one order
# -----------------------------------------------------------------------------


@router.get(
    "/{order_id}/billable-lines",
    response_model=ApiResponse[list[BillableLine]],
    summary="Receipt lines of one order a job-work invoice may still bill",
    description=(
        "Only lines with unbilled charges are listed, in receipt and line order, "
        "with the order's rate, basis, tax rate and SAC alongside - the dialog "
        "prefills from them and the match compares against them. Returned whole "
        "rather than paginated: an order carries a handful of receipts and a "
        "billable line is only meaningful with its siblings."
    ),
    dependencies=[
        require_any_permission(Permission.VENDOR_INVOICE_CREATE, Permission.JOB_WORK_VIEW)
    ],
)
async def billable_lines(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[list[BillableLine]]:
    return ApiResponse[list[BillableLine]].ok(
        await service.billable_lines(session, tenant, order_id)
    )


@router.get(
    "/{order_id}/challans",
    response_model=ApiResponse[list[ChallanRead]],
    summary="The delivery challans the material travelled under",
    description=(
        "Statutory documents under GST rules 45 and 55. Returned whole rather than "
        "paginated: an order carries one challan per despatch and a challan is only "
        "meaningful complete."
    ),
    dependencies=[require_permission(Permission.JOB_WORK_VIEW)],
)
async def list_challans(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[list[ChallanRead]]:
    return ApiResponse[list[ChallanRead]].ok(await service.list_challans(session, tenant, order_id))


@router.get(
    "/{order_id}/receipts",
    response_model=ApiResponse[list[ReceiptRead]],
    summary="Every lot received back against this order",
    dependencies=[require_permission(Permission.JOB_WORK_VIEW)],
)
async def list_receipts(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[list[ReceiptRead]]:
    return ApiResponse[list[ReceiptRead]].ok(await service.list_receipts(session, tenant, order_id))


@router.get(
    "/{order_id}/reconciliation",
    response_model=ApiResponse[ReconciliationReport],
    summary="Issued vs received vs loss for one order",
    description=(
        "What went out, what came back, what was lost and what is still at the vendor, line "
        "by line. The module's own outstanding figure is shown beside the stock ledger's "
        "live JOB_WORK pool for the same items and warehouse: a divergence between them "
        "means a movement was posted outside this module or a receipt was recorded without "
        "one, and it is visible no other way."
    ),
    dependencies=[require_permission(Permission.JOB_WORK_VIEW)],
)
async def reconciliation(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[ReconciliationReport]:
    return ApiResponse[ReconciliationReport].ok(
        await service.reconciliation(session, tenant, order_id)
    )


# -----------------------------------------------------------------------------
# Lifecycle
# -----------------------------------------------------------------------------


@router.post(
    "/{order_id}/approve",
    response_model=ApiResponse[JobWorkOrderDetail],
    summary="Approve a draft and reserve the material it will send out",
    description=(
        "Takes a hard stock reservation for every line, so material earmarked for a vendor "
        "cannot also be promised to a sales order. A line that is short refuses the whole "
        "approval rather than reserving what it can."
    ),
    dependencies=[require_permission(Permission.JOB_WORK_CLOSE)],
)
async def approve_order(
    order_id: uuid.UUID, payload: ReasonRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[JobWorkOrderDetail]:
    order, lines = await service.approve_order(session, tenant, order_id, reason=payload.reason)
    return ApiResponse[JobWorkOrderDetail].ok(
        service.order_detail(order, lines),
        success_message(f"Job work order {order.code} approved and material", "reserved"),
    )


@router.post(
    "/{order_id}/cancel",
    response_model=ApiResponse[JobWorkOrderSummary],
    summary="Cancel an order before its material has left",
    description=(
        "Releases every reservation in the same transaction. Only reachable from DRAFT or "
        "APPROVED: once material is at a vendor the order is the only record of where it "
        "went, and it is closed out by a final receipt instead."
    ),
    dependencies=[require_permission(Permission.JOB_WORK_CREATE)],
)
async def cancel_order(
    order_id: uuid.UUID, payload: ReasonRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[JobWorkOrderSummary]:
    order = await service.cancel_order(session, tenant, order_id, reason=payload.reason)
    return ApiResponse[JobWorkOrderSummary].ok(
        JobWorkOrderSummary.model_validate(order),
        success_message(f"Job work order {order.code}", "cancelled"),
    )


@router.post(
    "/{order_id}/issue",
    response_model=ApiResponse[IssueResult],
    summary="Send the material out: raise the challan and post the stock",
    description=(
        "Posts JOB_WORK_ISSUE for every line, which moves the quantity out of the available "
        "pool and into inventory_balances.job_work_quantity. The stock is still company "
        "property at the vendor's premises: it stays on the books, stays in the valuation "
        "and stays traceable to this order and challan - it simply cannot be promised to "
        "anybody. The statutory delivery challan is raised in the same transaction, valued "
        "from the ledger rows the issue actually wrote."
    ),
    dependencies=[require_permission(Permission.JOB_WORK_ISSUE)],
)
async def issue_order(
    order_id: uuid.UUID, payload: IssueRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[IssueResult]:
    result = await service.issue_order(session, tenant, order_id, payload)
    return ApiResponse[IssueResult].ok(
        result, f"Job work order {result.order.code} issued under challan {result.challan.code}"
    )


@router.post(
    "/{order_id}/receive",
    response_model=ApiResponse[ReceiptResult],
    summary="Record one lot returning from the vendor",
    description=(
        "Posts the processed output as its own item, draws the input it consumed out of the "
        "job-work pool, returns anything handed back untouched, and writes process loss off "
        "as scrap with a reason - all in one transaction. Loss beyond the line's agreed "
        "allowance is refused until `approve_excess_loss` is set, and its value is then "
        "debited to the vendor at the material's own moving-average cost. Approving the "
        "excess requires `job_work.close`. Receiving more output than the order expected "
        "is refused rather than clamped."
    ),
    dependencies=[require_permission(Permission.JOB_WORK_RECEIVE)],
)
async def receive_lot(
    order_id: uuid.UUID,
    payload: ReceiveRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[ReceiptResult]:
    result = await service.receive(session, tenant, principal, order_id, payload)
    return ApiResponse[ReceiptResult].ok(
        result, f"Received {result.receipt.code} against {result.order.code}"
    )


@router.post(
    "/{order_id}/qc-result",
    response_model=ApiResponse[JobWorkOrderSummary],
    summary="Record the inspection verdict on held output",
    description=(
        "Accepted output moves QC_HOLD -> AVAILABLE and becomes promisable; rejected output "
        "moves QC_HOLD -> DAMAGED and stays on the books but out of the available pool. The "
        "verdict must account for every held unit of every held item."
    ),
    dependencies=[require_permission(Permission.QC_DECIDE)],
)
async def record_qc_result(
    order_id: uuid.UUID, payload: QcResultRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[JobWorkOrderSummary]:
    order = await service.record_qc_result(session, tenant, order_id, payload)
    return ApiResponse[JobWorkOrderSummary].ok(
        JobWorkOrderSummary.model_validate(order),
        f"Inspection verdict recorded for {order.code}",
    )


@router.post(
    "/{order_id}/close",
    response_model=ApiResponse[JobWorkOrderSummary],
    summary="Close a finished job work order",
    description=(
        "Terminal: nothing may be posted against the order afterwards. An order with "
        "material still at the vendor cannot be closed - record a final receipt, which "
        "writes the shortfall off as scrap with a reason, so the loss is stated rather than "
        "left sitting in the job-work pool forever."
    ),
    dependencies=[require_permission(Permission.JOB_WORK_CLOSE)],
)
async def close_order(
    order_id: uuid.UUID, payload: ReasonRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[JobWorkOrderSummary]:
    order = await service.close_order(session, tenant, order_id, reason=payload.reason)
    return ApiResponse[JobWorkOrderSummary].ok(
        JobWorkOrderSummary.model_validate(order),
        success_message(f"Job work order {order.code}", "closed"),
    )


__all__ = ["router"]
