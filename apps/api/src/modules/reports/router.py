"""HTTP surface for reporting and dashboards.

Handlers bind parameters, call one service function and wrap the result. Every
rule that matters - the bounded period, the row ceiling, the cache policy, the
audit entry when a background job is raised - lives behind the service (section
74), and none of it is repeated per route.

Three shapes are deliberate:

* **A report answers 200 *or* 202, and the server decides which.** A result the
  caller can read comes back paginated; one past the inline row limit comes back
  as a job with a status URL. There is no query parameter to force either, so a
  client cannot hold a connection open while a hundred thousand rows are
  serialised (master rules sections 25, 35). Both shapes are declared in each
  route's ``responses`` map so the contract is visible in the OpenAPI document.

* **A period report will not run without a period.** ``date_from`` and
  ``date_to`` are validated as a pair by :func:`report_period` and bounded by
  :class:`~src.modules.reports.schemas.ReportPeriod`; the position reports take
  no dates at all, because they read a pre-aggregated position rather than a
  span of history and a date range on them would be a parameter that changes
  nothing.

* **Every catalogue report requires ``report.view`` and its definition's
  domain permission; the escalation to a file is gated on ``report.export``.**
  The second check is inside the service, because whether a request escalates
  is only known after the rows have been counted - a route-level dependency
  could only have refused everybody or nobody.

Route ordering: the literal ``/jobs`` and ``/catalogue`` segments are declared
before anything that could capture them.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.core.deps import (
    CurrentUser,
    DbSession,
    Tenant,
    forbid_dealer_users,
    forbid_vendor_users,
    require_permission,
)
from src.core.errors import ValidationError
from src.core.pagination import PageParams, bind_query_filters, page_params
from src.core.permissions import Permission
from src.core.responses import (
    COMMON_ERROR_RESPONSES,
    AcceptedJobResponse,
    ApiResponse,
    PaginatedResponse,
)
from src.modules.documents.service import DOWNLOAD_HEADERS, content_disposition, stream_file
from src.modules.reports import service
from src.modules.reports.models import ReportKey
from src.modules.reports.schemas import (
    ConsumptionByDepartmentRow,
    CostCentreExpenseRow,
    CourierTrackingRow,
    DashboardRead,
    DeadStockRow,
    DispatchRow,
    InventoryValuationRow,
    ItemMovementRow,
    JobWorkCostRow,
    JobWorkPendingRow,
    MachineUtilisationRow,
    MrpShortageRow,
    OrderDelayRow,
    ProductionCostRow,
    ProductionEfficiencyRow,
    ProductionPendingRow,
    ProductProfitabilityRow,
    PurchasePendingRow,
    QcRejectionRow,
    ReportDescriptorRead,
    ReportFilters,
    ReportJobRead,
    ReportPeriod,
    ReworkRateRow,
    SalesOrderStatusRow,
    ScrapRegisterRow,
    ShiftProductionRow,
    StockAvailabilityRow,
    VendorDispatchHistoryRow,
    VendorPerformanceRow,
)

router = APIRouter(prefix="/reports", tags=["Reports"], responses=COMMON_ERROR_RESPONSES)

PageDep = Annotated[PageParams, Depends(page_params)]


# -----------------------------------------------------------------------------
# Shared dependencies
# -----------------------------------------------------------------------------


def report_period(
    date_from: Annotated[
        date | None, Query(description="First day of the period, inclusive")
    ] = None,
    date_to: Annotated[date | None, Query(description="Last day of the period, inclusive")] = None,
) -> ReportPeriod | None:
    """Bind and validate the reporting period.

    Half a range is rejected rather than completed with a default. "From the 1st"
    with no end silently means "to whenever this query finishes reading", which
    is the unbounded scan section 25 exists to prevent - and a client that sent
    one end meant to send both.
    """
    if date_from is None and date_to is None:
        return None
    if date_from is None or date_to is None:
        raise ValidationError(
            "A report period needs both date_from and date_to.",
            details={"date_from": str(date_from), "date_to": str(date_to)},
        )
    return bind_query_filters(ReportPeriod, date_from=date_from, date_to=date_to)


def report_filters(
    item_id: Annotated[uuid.UUID | None, Query()] = None,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    vendor_id: Annotated[uuid.UUID | None, Query()] = None,
    customer_id: Annotated[uuid.UUID | None, Query()] = None,
    category_id: Annotated[uuid.UUID | None, Query()] = None,
    work_center_id: Annotated[uuid.UUID | None, Query()] = None,
    cost_centre_id: Annotated[uuid.UUID | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status", max_length=32)] = None,
    min_days: Annotated[
        int | None,
        Query(ge=0, le=3650, description="Age threshold in days, where the report uses one"),
    ] = None,
    search: Annotated[str | None, Query(max_length=120)] = None,
) -> ReportFilters:
    return bind_query_filters(
        ReportFilters,
        item_id=item_id,
        warehouse_id=warehouse_id,
        vendor_id=vendor_id,
        customer_id=customer_id,
        category_id=category_id,
        work_center_id=work_center_id,
        cost_centre_id=cost_centre_id,
        status=status_filter,
        min_days=min_days,
        search=search,
    )


PeriodDep = Annotated[ReportPeriod | None, Depends(report_period)]
FilterDep = Annotated[ReportFilters, Depends(report_filters)]


def _report_responses(model: type[BaseModel]) -> dict[int | str, dict[str, Any]]:
    """Document each report's inline rows and its background-job alternative.

    The models describe OpenAPI only: the service chooses the actual envelope,
    and a response model must not filter either runtime result into the other.
    """
    return {
        200: {"model": model, "description": "The report, paginated"},
        202: {
            "model": AcceptedJobResponse,
            "description": (
                "Too large to return inline. Generated in the background; poll the status URL."
            ),
        },
    }


async def _run(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    key: ReportKey,
    period: ReportPeriod | None,
    filters: ReportFilters,
    page: PageParams,
) -> Any:
    """Run one report and wrap whichever answer the service produced.

    The only shared code in this file. Envelope shaping is a route concern; the
    decision between the two envelopes was already taken by the service, and this
    function does not re-take it.
    """
    principal.require(service.definition_for(key).permission)
    result = await service.run_report(
        session,
        tenant,
        key=key,
        period=period,
        filters=filters,
        params=page,
        may_export=principal.has(Permission.REPORT_EXPORT),
    )

    if isinstance(result, service.QueuedReport):
        response.status_code = status.HTTP_202_ACCEPTED
        return AcceptedJobResponse.create(
            job_id=str(result.snapshot.id),
            status_url=service.job_status_url(result.snapshot.id),
            message=(
                f"'{result.definition.title}' exceeds "
                f"{service.REPORT_INLINE_ROW_LIMIT:,} rows and is being generated as a file."
            ),
        )

    return PaginatedResponse[Any](
        data=result.rows,
        pagination=result.pagination,
        message=f"{result.definition.title} as at {result.generated_at.isoformat()}",
    )


# -----------------------------------------------------------------------------
# Catalogue, dashboard and jobs - literal paths first
# -----------------------------------------------------------------------------


@router.get(
    "/catalogue",
    response_model=ApiResponse[list[ReportDescriptorRead]],
    summary="List the available reports",
    description=(
        "Every report, its columns and whether it needs a period. Drives a generic "
        "report picker without hard-coding the catalogue in the client."
    ),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def list_reports(principal: CurrentUser) -> ApiResponse[list[ReportDescriptorRead]]:
    return ApiResponse[list[ReportDescriptorRead]].ok(service.descriptors(principal))


@router.get(
    "/dashboard",
    response_model=ApiResponse[DashboardRead],
    summary="Management dashboard tiles",
    description=(
        "Open sales orders, stock value, pending purchase orders, production in "
        "progress, QC pending, consignments in transit and overdue deliveries. "
        "Assembled from summary tables in a single statement and cached briefly; "
        "the stock figure carries the time its summary was last refreshed."
    ),
    dependencies=[require_permission(Permission.DASHBOARD_VIEW)],
)
async def dashboard(session: DbSession, tenant: Tenant) -> ApiResponse[DashboardRead]:
    return ApiResponse[DashboardRead].ok(await service.dashboard(session, tenant))


@router.get(
    "/jobs/{job_id}",
    response_model=ApiResponse[ReportJobRead],
    summary="Background report status and download link",
    description=(
        "Status of a report handed to Celery. Once it completes, `download_url` "
        "points at the generated file in the document store."
    ),
    dependencies=[
        require_permission(
            Permission.REPORT_VIEW,
            Permission.INVENTORY_VALUATION_VIEW,
            Permission.JOB_WORK_VIEW,
            Permission.PURCHASE_RETURN_VIEW,
            mode="any",
        ),
        Depends(forbid_vendor_users),
        Depends(forbid_dealer_users),
    ],
)
async def get_job(
    job_id: uuid.UUID, session: DbSession, tenant: Tenant, principal: CurrentUser
) -> ApiResponse[ReportJobRead]:
    return ApiResponse[ReportJobRead].ok(
        await service.get_accessible_job(session, tenant, principal, job_id)
    )


@router.get(
    "/jobs/{job_id}/download",
    summary="Download the generated file authorized by its originating job",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
            "description": "File content; the Content-Type header identifies its actual media type",
        }
    },
    dependencies=[
        require_permission(
            Permission.REPORT_VIEW,
            Permission.INVENTORY_VALUATION_VIEW,
            Permission.JOB_WORK_VIEW,
            Permission.PURCHASE_RETURN_VIEW,
            mode="any",
        ),
        Depends(forbid_vendor_users),
        Depends(forbid_dealer_users),
    ],
)
async def download_job(
    job_id: uuid.UUID, session: DbSession, tenant: Tenant, principal: CurrentUser
) -> StreamingResponse:
    download = await service.open_job_download(session, tenant, principal, job_id)
    return StreamingResponse(
        stream_file(download.path),
        media_type=download.document.mime_type,
        headers={
            **DOWNLOAD_HEADERS,
            "Content-Disposition": content_disposition(download.document.file_name),
            "Content-Length": str(download.document.size_bytes),
        },
    )


# -----------------------------------------------------------------------------
# Order book
# -----------------------------------------------------------------------------


@router.get(
    "/sales-order-status",
    response_model=None,
    summary="Sales order status",
    responses=_report_responses(PaginatedResponse[SalesOrderStatusRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def sales_order_status(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    period: PeriodDep,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[SalesOrderStatusRow] | AcceptedJobResponse:
    return await _run(
        session, tenant, principal, response, ReportKey.SALES_ORDER_STATUS, period, filters, page
    )


@router.get(
    "/order-delay",
    response_model=None,
    summary="Order delay",
    responses=_report_responses(PaginatedResponse[OrderDelayRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def order_delay(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    period: PeriodDep,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[OrderDelayRow] | AcceptedJobResponse:
    return await _run(
        session, tenant, principal, response, ReportKey.ORDER_DELAY, period, filters, page
    )


@router.get(
    "/product-profitability",
    response_model=None,
    summary="Product profitability",
    responses=_report_responses(PaginatedResponse[ProductProfitabilityRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def product_profitability(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    period: PeriodDep,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[ProductProfitabilityRow] | AcceptedJobResponse:
    return await _run(
        session,
        tenant,
        principal,
        response,
        ReportKey.PRODUCT_PROFITABILITY,
        period,
        filters,
        page,
    )


# -----------------------------------------------------------------------------
# Stock
# -----------------------------------------------------------------------------


@router.get(
    "/stock-availability",
    response_model=None,
    summary="Stock availability",
    description=(
        "On-hand, reserved and available stock per item and warehouse, read from the "
        "refreshed summary. **Indicative only:** an allocation or a promise re-reads "
        "live stock under a row lock (master rules section 34), and this endpoint is "
        "never cached for the same reason."
    ),
    responses=_report_responses(PaginatedResponse[StockAvailabilityRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def stock_availability(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[StockAvailabilityRow] | AcceptedJobResponse:
    return await _run(
        session, tenant, principal, response, ReportKey.STOCK_AVAILABILITY, None, filters, page
    )


@router.get(
    "/inventory-valuation",
    response_model=None,
    summary="Inventory valuation",
    responses=_report_responses(PaginatedResponse[InventoryValuationRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def inventory_valuation(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[InventoryValuationRow] | AcceptedJobResponse:
    return await _run(
        session, tenant, principal, response, ReportKey.INVENTORY_VALUATION, None, filters, page
    )


@router.get(
    "/dead-stock",
    response_model=None,
    summary="Dead stock",
    description=(
        "Positions holding stock that has not moved for longer than the ageing "
        f"threshold. Defaults to {service.DEAD_STOCK_DAYS} days; override with `min_days`."
    ),
    responses=_report_responses(PaginatedResponse[DeadStockRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def dead_stock(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[DeadStockRow] | AcceptedJobResponse:
    return await _run(
        session, tenant, principal, response, ReportKey.DEAD_STOCK, None, filters, page
    )


@router.get(
    "/fast-moving",
    response_model=None,
    summary="Fast-moving items",
    responses=_report_responses(PaginatedResponse[ItemMovementRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def fast_moving(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    period: PeriodDep,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[ItemMovementRow] | AcceptedJobResponse:
    return await _run(
        session, tenant, principal, response, ReportKey.FAST_MOVING, period, filters, page
    )


@router.get(
    "/slow-moving",
    response_model=None,
    summary="Slow-moving items",
    responses=_report_responses(PaginatedResponse[ItemMovementRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def slow_moving(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    period: PeriodDep,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[ItemMovementRow] | AcceptedJobResponse:
    return await _run(
        session, tenant, principal, response, ReportKey.SLOW_MOVING, period, filters, page
    )


@router.get(
    "/mrp-shortage",
    response_model=None,
    summary="MRP shortage",
    responses=_report_responses(PaginatedResponse[MrpShortageRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def mrp_shortage(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    period: PeriodDep,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[MrpShortageRow] | AcceptedJobResponse:
    return await _run(
        session, tenant, principal, response, ReportKey.MRP_SHORTAGE, period, filters, page
    )


# -----------------------------------------------------------------------------
# Procurement, production and job work
# -----------------------------------------------------------------------------


@router.get(
    "/purchase-pending",
    response_model=None,
    summary="Purchase pending",
    responses=_report_responses(PaginatedResponse[PurchasePendingRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def purchase_pending(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    period: PeriodDep,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[PurchasePendingRow] | AcceptedJobResponse:
    return await _run(
        session, tenant, principal, response, ReportKey.PURCHASE_PENDING, period, filters, page
    )


@router.get(
    "/production-pending",
    response_model=None,
    summary="Production pending",
    responses=_report_responses(PaginatedResponse[ProductionPendingRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def production_pending(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    period: PeriodDep,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[ProductionPendingRow] | AcceptedJobResponse:
    return await _run(
        session, tenant, principal, response, ReportKey.PRODUCTION_PENDING, period, filters, page
    )


@router.get(
    "/job-work-pending",
    response_model=None,
    summary="Job-work pending",
    responses=_report_responses(PaginatedResponse[JobWorkPendingRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def job_work_pending(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    period: PeriodDep,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[JobWorkPendingRow] | AcceptedJobResponse:
    return await _run(
        session, tenant, principal, response, ReportKey.JOB_WORK_PENDING, period, filters, page
    )


@router.get(
    "/production-efficiency",
    response_model=None,
    summary="Production efficiency",
    responses=_report_responses(PaginatedResponse[ProductionEfficiencyRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def production_efficiency(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    period: PeriodDep,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[ProductionEfficiencyRow] | AcceptedJobResponse:
    return await _run(
        session,
        tenant,
        principal,
        response,
        ReportKey.PRODUCTION_EFFICIENCY,
        period,
        filters,
        page,
    )


@router.get(
    "/production-cost",
    response_model=None,
    summary="Production cost",
    description=(
        "Closed production orders costed in the selected period. Requires item-cost visibility."
    ),
    responses=_report_responses(PaginatedResponse[ProductionCostRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def production_cost(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    period: PeriodDep,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[ProductionCostRow] | AcceptedJobResponse:
    return await _run(
        session, tenant, principal, response, ReportKey.PRODUCTION_COST, period, filters, page
    )


@router.get(
    "/job-work-cost",
    response_model=None,
    summary="Job-work cost",
    description=(
        "Job-work order lines costed at the frozen challan issue cost plus accrued "
        "processing charges, with vendor debits and cost per unit. Requires "
        "item-cost visibility."
    ),
    responses=_report_responses(PaginatedResponse[JobWorkCostRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def job_work_cost(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    period: PeriodDep,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[JobWorkCostRow] | AcceptedJobResponse:
    return await _run(
        session, tenant, principal, response, ReportKey.JOB_WORK_COST, period, filters, page
    )


@router.get(
    "/machine-utilisation",
    response_model=None,
    summary="Machine utilisation",
    description=(
        "Busy minutes against available capacity per machine. Capacity is the work "
        "centre's working hours across the calendar days of the period - the system "
        "makes no weekend or holiday assumption. Separate rostered minutes and "
        "rostered utilisation use planned shift assignments less their breaks."
    ),
    responses=_report_responses(PaginatedResponse[MachineUtilisationRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def machine_utilisation(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    period: PeriodDep,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[MachineUtilisationRow] | AcceptedJobResponse:
    return await _run(
        session, tenant, principal, response, ReportKey.MACHINE_UTILISATION, period, filters, page
    )


@router.get(
    "/shift-production",
    response_model=None,
    summary="Daily production by shift",
    description=(
        "Bookings grouped by business date, shift code, work centre, machine and "
        "operator, with produced, rejected, downtime, the number of bookings and the "
        "yield. The shift is the code recorded on each booking, so bookings made before "
        "the shift master existed appear under the code that was typed."
    ),
    responses=_report_responses(PaginatedResponse[ShiftProductionRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def shift_production(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    period: PeriodDep,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[ShiftProductionRow] | AcceptedJobResponse:
    return await _run(
        session, tenant, principal, response, ReportKey.SHIFT_PRODUCTION, period, filters, page
    )


# -----------------------------------------------------------------------------
# Quality and vendors
# -----------------------------------------------------------------------------


@router.get(
    "/qc-rejection",
    response_model=None,
    summary="QC rejection",
    responses=_report_responses(PaginatedResponse[QcRejectionRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def qc_rejection(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    period: PeriodDep,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[QcRejectionRow] | AcceptedJobResponse:
    return await _run(
        session, tenant, principal, response, ReportKey.QC_REJECTION, period, filters, page
    )


@router.get(
    "/rework-rate",
    response_model=None,
    summary="Rework rate",
    description=(
        "Rework quantity and rate per item and vendor over the period, from decided "
        "inspections. A pending inspection has no verdict and is not counted."
    ),
    responses=_report_responses(PaginatedResponse[ReworkRateRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def rework_rate(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    period: PeriodDep,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[ReworkRateRow] | AcceptedJobResponse:
    return await _run(
        session, tenant, principal, response, ReportKey.REWORK_RATE, period, filters, page
    )


@router.get(
    "/vendor-performance",
    response_model=None,
    summary="Vendor performance",
    description=(
        "On-time delivery and rejection rate per vendor, scored equally. A receipt "
        "against an order with no promised delivery date is excluded from the "
        "on-time figure entirely rather than counted as either early or late."
    ),
    responses=_report_responses(PaginatedResponse[VendorPerformanceRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def vendor_performance(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    period: PeriodDep,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[VendorPerformanceRow] | AcceptedJobResponse:
    return await _run(
        session, tenant, principal, response, ReportKey.VENDOR_PERFORMANCE, period, filters, page
    )


# -----------------------------------------------------------------------------
# Dispatch
# -----------------------------------------------------------------------------


@router.get(
    "/dispatch",
    response_model=None,
    summary="Dispatch",
    responses=_report_responses(PaginatedResponse[DispatchRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def dispatch_report(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    period: PeriodDep,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[DispatchRow] | AcceptedJobResponse:
    return await _run(
        session, tenant, principal, response, ReportKey.DISPATCH, period, filters, page
    )


@router.get(
    "/courier-tracking",
    response_model=None,
    summary="Courier and transport tracking",
    responses=_report_responses(PaginatedResponse[CourierTrackingRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def courier_tracking(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    period: PeriodDep,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[CourierTrackingRow] | AcceptedJobResponse:
    return await _run(
        session, tenant, principal, response, ReportKey.COURIER_TRACKING, period, filters, page
    )


@router.get(
    "/vendor-dispatch-history",
    response_model=None,
    summary="Vendor dispatch history",
    responses=_report_responses(PaginatedResponse[VendorDispatchHistoryRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def vendor_dispatch_history(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    period: PeriodDep,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[VendorDispatchHistoryRow] | AcceptedJobResponse:
    return await _run(
        session,
        tenant,
        principal,
        response,
        ReportKey.VENDOR_DISPATCH_HISTORY,
        period,
        filters,
        page,
    )


# -----------------------------------------------------------------------------
# Finance
# -----------------------------------------------------------------------------


@router.get(
    "/cost-centre-expense",
    response_model=None,
    summary="Cost centre statement",
    description=(
        "Posted income and expense per cost centre and account over the period, read "
        "from the ledger. `search` matches the cost centre's code or name. Net is debit "
        "minus credit: positive is cost, negative is income. A retired cost centre still "
        "appears - posted history keeps it."
    ),
    responses=_report_responses(PaginatedResponse[CostCentreExpenseRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def cost_centre_expense(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    period: PeriodDep,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[CostCentreExpenseRow] | AcceptedJobResponse:
    return await _run(
        session, tenant, principal, response, ReportKey.COST_CENTRE_EXPENSE, period, filters, page
    )


@router.get(
    "/scrap-register",
    response_model=None,
    responses=_report_responses(PaginatedResponse[ScrapRegisterRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def scrap_register(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    period: PeriodDep,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[ScrapRegisterRow] | AcceptedJobResponse:
    return await _run(
        session, tenant, principal, response, ReportKey.SCRAP_REGISTER, period, filters, page
    )


@router.get(
    "/consumption-by-department",
    response_model=None,
    responses=_report_responses(PaginatedResponse[ConsumptionByDepartmentRow]),
    dependencies=[require_permission(Permission.REPORT_VIEW)],
)
async def consumption_by_department(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    response: Response,
    period: PeriodDep,
    filters: FilterDep,
    page: PageDep,
) -> PaginatedResponse[ConsumptionByDepartmentRow] | AcceptedJobResponse:
    return await _run(
        session,
        tenant,
        principal,
        response,
        ReportKey.CONSUMPTION_BY_DEPARTMENT,
        period,
        filters,
        page,
    )


__all__ = ["report_filters", "report_period", "router"]
