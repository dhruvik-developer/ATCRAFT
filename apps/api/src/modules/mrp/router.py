"""HTTP surface for material requirements planning.

Handlers bind parameters, call one service function and wrap the result in the
standard envelope. Every rule - the execution mode, the concurrency lock, the
netting, the audit writes - lives behind the service (section 74).

Three deliberate shapes:

* **Starting a run answers 200 *or* 202, and the server decides which.** A small
  run is calculated inline and comes back complete; a large one is queued and
  comes back as a job with a status URL. The request cannot ask for either, so
  there is no way for a client to hold an HTTP connection open through a
  four-minute calculation (master rules section 14). Both shapes are declared in
  the ``responses`` map so the contract is visible in the OpenAPI document.

* **There is no endpoint that edits a suggestion.** A suggestion is converted or
  ignored, each with its own control, its own permission and its own audit entry.
  A general-purpose PATCH would be a way around all three.

* **There is no "re-open" endpoint.** ``CONVERTED`` and ``IGNORED`` are terminal:
  re-opening a converted suggestion invites a second order for a shortage that
  has already been covered. The next run raises a fresh suggestion if the
  shortage is still real.

Route ordering: literal segments are declared before any ``/{id}`` route.
FastAPI matches in declaration order, so the reverse would capture
``bulk-ignore`` as a suggestion id and fail on the UUID parse.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status

from src.core.deps import DbSession, Tenant, require_permission
from src.core.messages import success_message
from src.core.pagination import (
    PageParams,
    SortParams,
    bind_query_filters,
    page_params,
    sort_params,
)
from src.core.permissions import Permission
from src.core.responses import (
    COMMON_ERROR_RESPONSES,
    AcceptedJobResponse,
    ApiResponse,
    PaginatedResponse,
)
from src.modules.mrp import service
from src.modules.mrp.models import (
    MrpRunStatus,
    MrpSuggestionStatus,
    MrpSuggestionType,
)
from src.modules.mrp.schemas import (
    BulkIgnoreRequest,
    BulkIgnoreResult,
    MrpDemandRead,
    MrpRequirementRead,
    MrpRunFilters,
    MrpRunRead,
    MrpRunRequest,
    MrpRunSummary,
    MrpSuggestionRead,
    SalesOrderAvailabilityRead,
    ShortageReportFilters,
    SuggestionConversionResult,
    SuggestionConvertRequest,
    SuggestionFilters,
)

router = APIRouter(prefix="/mrp", tags=["MRP"], responses=COMMON_ERROR_RESPONSES)

PageDep = Annotated[PageParams, Depends(page_params)]
SortDep = Annotated[SortParams, Depends(sort_params)]


# -----------------------------------------------------------------------------
# Filter dependencies
# -----------------------------------------------------------------------------


def run_filters(
    status_filter: Annotated[MrpRunStatus | None, Query(alias="status")] = None,
    created_from: Annotated[date | None, Query()] = None,
    created_to: Annotated[date | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=120, description="Run code")] = None,
) -> MrpRunFilters:
    return bind_query_filters(
        MrpRunFilters,
        status=status_filter,
        created_from=created_from,
        created_to=created_to,
        search=search,
    )


def suggestion_filters(
    suggestion_type: Annotated[MrpSuggestionType | None, Query()] = None,
    status_filter: Annotated[MrpSuggestionStatus | None, Query(alias="status")] = None,
    item_id: Annotated[uuid.UUID | None, Query()] = None,
    late_only: Annotated[
        bool, Query(description="Only suggestions whose order date has already passed")
    ] = False,
    search: Annotated[str | None, Query(max_length=120, description="Item code")] = None,
) -> SuggestionFilters:
    return bind_query_filters(
        SuggestionFilters,
        suggestion_type=suggestion_type,
        status=status_filter,
        item_id=item_id,
        late_only=late_only,
        search=search,
    )


def shortage_filters(
    run_id: Annotated[
        uuid.UUID | None, Query(description="Defaults to the latest completed run")
    ] = None,
    item_id: Annotated[uuid.UUID | None, Query()] = None,
    shortage_only: Annotated[bool, Query()] = True,
    search: Annotated[str | None, Query(max_length=120, description="Item code")] = None,
) -> ShortageReportFilters:
    return bind_query_filters(
        ShortageReportFilters,
        run_id=run_id,
        item_id=item_id,
        shortage_only=shortage_only,
        search=search,
    )


RunFilterDep = Annotated[MrpRunFilters, Depends(run_filters)]
SuggestionFilterDep = Annotated[SuggestionFilters, Depends(suggestion_filters)]
ShortageFilterDep = Annotated[ShortageReportFilters, Depends(shortage_filters)]


# -----------------------------------------------------------------------------
# Runs
# -----------------------------------------------------------------------------


@router.post(
    "/runs",
    response_model=None,
    summary="Start a planning run",
    description=(
        "Captures the open order book inside the horizon, plus any manual demand, "
        "and plans against that snapshot. **The server decides how it executes:** a "
        "run whose demand exceeds the configured threshold is queued and answered "
        "with `202` and a status URL; anything smaller is calculated inline and "
        "answered with `200` and the completed run. Only one run may be in flight "
        "per company - a second returns `409 MRP_RUN_IN_PROGRESS`. Starts are also "
        "budgeted per company, because the lock only stops runs overlapping and "
        "not a client restarting one the instant the last finished; over the "
        "budget the answer is `429` with a `Retry-After`."
    ),
    responses={
        200: {"model": ApiResponse[MrpRunRead], "description": "Run completed inline"},
        202: {
            "model": AcceptedJobResponse,
            "description": "Run queued; poll the status URL for progress",
        },
        409: {
            **COMMON_ERROR_RESPONSES[409],
            "description": "A planning run is already in progress for this company",
        },
        429: {
            **COMMON_ERROR_RESPONSES[429],
            "description": "This company has started too many runs; see Retry-After",
        },
    },
    dependencies=[require_permission(Permission.MRP_RUN)],
)
async def start_run(
    payload: MrpRunRequest,
    session: DbSession,
    tenant: Tenant,
    response: Response,
) -> Any:
    run, dispatched = await service.start_run(session, tenant, payload)
    if dispatched:
        response.status_code = status.HTTP_202_ACCEPTED
        return AcceptedJobResponse.create(
            job_id=str(run.id),
            status_url=service.status_url(run.id),
            message=(f"Planning run {run.code} queued with {run.demand_line_count} demand lines."),
        )
    return ApiResponse[MrpRunRead].ok(run, success_message(f"Planning run {run.code}", "completed"))


@router.get(
    "/runs",
    response_model=PaginatedResponse[MrpRunSummary],
    summary="List planning runs",
    dependencies=[require_permission(Permission.MRP_VIEW)],
)
async def list_runs(
    session: DbSession,
    tenant: Tenant,
    filters: RunFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[MrpRunSummary]:
    items, pagination = await service.list_runs(session, tenant, filters, params, sort)
    return PaginatedResponse[MrpRunSummary](data=items, pagination=pagination)


@router.get(
    "/shortage-report",
    response_model=PaginatedResponse[MrpRequirementRead],
    summary="Items short against a plan",
    description=(
        "The netted requirement for every short item, with the figures it was "
        "netted against - gross demand, free stock, what is already on order and "
        "what is in production, assembly or job work - so a number can be reviewed rather than "
        "just believed. Omit `run_id` to report against the latest completed run."
        "\n\n"
        "It also carries the supply the plan deliberately did *not* net, each "
        "quantity with its own arrival date: `late_incoming_quantity` lands on "
        "`first_late_incoming_date`, `late_in_production_quantity` on "
        "`first_late_in_production_date`, and `late_in_assembly_quantity` on "
        "`first_late_in_assembly_date`. `first_late_supply_date` is the "
        "earliest across all feeds - the soonest anything lands - and is not the date "
        "for either quantity on its own. A shortage with an open order behind "
        "it that arrives too late is the row a planner has to act on "
        "differently - expedite the order rather than raise another - and these "
        "fields are what let a table say so per row instead of leaving it in "
        "the run's warning text."
    ),
    dependencies=[require_permission(Permission.MRP_VIEW)],
)
async def get_shortage_report(
    session: DbSession,
    tenant: Tenant,
    filters: ShortageFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[MrpRequirementRead]:
    items, pagination = await service.shortage_report(session, tenant, filters, params, sort)
    return PaginatedResponse[MrpRequirementRead](data=items, pagination=pagination)


@router.get(
    "/runs/{run_id}",
    response_model=ApiResponse[MrpRunRead],
    summary="Run status and summary",
    description=(
        "The polling endpoint for a queued run. `warnings` carries any netting "
        "stage the run could not perform - read it before acting on the "
        "suggestions, because a plan that could not see incoming purchase orders "
        "reports an upper bound rather than a shortage."
    ),
    dependencies=[require_permission(Permission.MRP_VIEW)],
)
async def get_run(run_id: uuid.UUID, session: DbSession, tenant: Tenant) -> ApiResponse[MrpRunRead]:
    return ApiResponse[MrpRunRead].ok(await service.get_run(session, tenant, run_id))


@router.get(
    "/runs/{run_id}/demand",
    response_model=PaginatedResponse[MrpDemandRead],
    summary="The demand snapshot a run planned against",
    description=(
        "What the order book looked like when the run started, not what it looks "
        "like now. This is what makes a plan defensible after the orders behind it "
        "have been amended."
    ),
    dependencies=[require_permission(Permission.MRP_VIEW)],
)
async def list_run_demand(
    run_id: uuid.UUID, session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[MrpDemandRead]:
    items, pagination = await service.list_run_demand(session, tenant, run_id, params)
    return PaginatedResponse[MrpDemandRead](data=items, pagination=pagination)


@router.get(
    "/sales-orders/{order_id}/expected-availability",
    response_model=ApiResponse[SalesOrderAvailabilityRead],
    summary="When the latest plan expects a sales order to be covered",
    description=(
        "The newest completed run whose demand snapshot contained this order, "
        "with the date each line is expected to be covered by stock, dated supply "
        "or the order the plan raises. `run_id` is null when no completed run has "
        "ever seen the order; that is a normal state, not a 404. Dates come from "
        "the plan's stamped demand rows, so they are evidence of what was known "
        "when it ran rather than a fresh calculation."
    ),
    dependencies=[require_permission(Permission.MRP_VIEW)],
)
async def sales_order_availability(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[SalesOrderAvailabilityRead]:
    return ApiResponse[SalesOrderAvailabilityRead].ok(
        await service.sales_order_availability(session, tenant, order_id)
    )


@router.get(
    "/runs/{run_id}/suggestions",
    response_model=PaginatedResponse[MrpSuggestionRead],
    summary="What one run proposes",
    dependencies=[require_permission(Permission.MRP_VIEW)],
)
async def list_run_suggestions(
    run_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    filters: SuggestionFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[MrpSuggestionRead]:
    # The path segment is the authority on which run is being read; a query
    # parameter naming a different one would be ambiguous, so it is overwritten.
    scoped = filters.model_copy(update={"run_id": run_id})
    items, pagination = await service.list_suggestions(session, tenant, scoped, params, sort)
    return PaginatedResponse[MrpSuggestionRead](data=items, pagination=pagination)


# -----------------------------------------------------------------------------
# Suggestions
# -----------------------------------------------------------------------------


@router.get(
    "/suggestions",
    response_model=PaginatedResponse[MrpSuggestionRead],
    summary="Suggestions across runs",
    description=(
        "The buyer's and planner's working queue. Filter by type and status, or "
        "by `late_only` for the orders that should already have been placed."
    ),
    dependencies=[require_permission(Permission.MRP_VIEW)],
)
async def list_suggestions(
    session: DbSession,
    tenant: Tenant,
    filters: SuggestionFilterDep,
    params: PageDep,
    sort: SortDep,
    run_id: Annotated[uuid.UUID | None, Query()] = None,
) -> PaginatedResponse[MrpSuggestionRead]:
    scoped = filters.model_copy(update={"run_id": run_id})
    items, pagination = await service.list_suggestions(session, tenant, scoped, params, sort)
    return PaginatedResponse[MrpSuggestionRead](data=items, pagination=pagination)


@router.post(
    "/suggestions/bulk-ignore",
    response_model=ApiResponse[BulkIgnoreResult],
    summary="Dismiss several suggestions",
    description=(
        "Suggestions that are no longer open are reported as skipped rather than "
        "failing the whole call. The reason is mandatory and is written to the "
        "audit log against every dismissed row."
    ),
    dependencies=[require_permission(Permission.MRP_CONVERT_SUGGESTION)],
)
async def bulk_ignore_suggestions(
    payload: BulkIgnoreRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[BulkIgnoreResult]:
    result = await service.bulk_ignore_suggestions(session, tenant, payload)
    return ApiResponse[BulkIgnoreResult].ok(result, result.message)


@router.get(
    "/suggestions/{suggestion_id}",
    response_model=ApiResponse[MrpSuggestionRead],
    summary="Read one suggestion",
    dependencies=[require_permission(Permission.MRP_VIEW)],
)
async def get_suggestion(
    suggestion_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[MrpSuggestionRead]:
    found = await service.get_suggestion(session, tenant, suggestion_id)
    return ApiResponse[MrpSuggestionRead].ok(found)


@router.post(
    "/suggestions/{suggestion_id}/convert",
    response_model=ApiResponse[SuggestionConversionResult],
    summary="Convert a suggestion into a document",
    description=(
        "Raises a purchase requisition, production order or job-work order from the suggestion "
        "and closes it, in one transaction. Omit `target` to use the default for "
        "the suggestion type; naming a different one is allowed and is recorded in "
        "the audit entry. If the owning module is not deployed, no document is "
        "created: `handled` is false and the typed conversion request is returned "
        "for the caller to action. Purchase conversion requires complete, nonempty demand "
        "links with at most fifty distinct roots. Incomplete historical links return 422 "
        "without closing the suggestion or creating a requisition; rerun the plan first."
    ),
    dependencies=[require_permission(Permission.MRP_CONVERT_SUGGESTION)],
)
async def convert_suggestion(
    suggestion_id: uuid.UUID,
    payload: SuggestionConvertRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[SuggestionConversionResult]:
    result = await service.convert_suggestion(session, tenant, suggestion_id, payload)
    return ApiResponse[SuggestionConversionResult].ok(result, result.message)


__all__ = ["router"]
