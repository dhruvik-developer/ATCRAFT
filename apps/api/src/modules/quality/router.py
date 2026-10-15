"""HTTP surface for quality control.

Handlers bind parameters, call one service function and wrap the result in the
standard envelope. No rule, query or audit write lives here (master rules
section 74).

Route ordering note: ``/pending``, ``/templates`` and ``/reports/...`` are all
declared before ``/inspections/{inspection_id}``. FastAPI matches in declaration
order, and a literal segment declared after a path parameter of the same shape
would be captured by it and rejected as a malformed UUID.

Permission split (section 38): ``qc.view`` reads, ``qc.inspect`` records
measurements, ``qc.decide`` takes the decision that moves stock, and
``qc.template_manage`` writes specifications. They are separate because the
person who measures a part is usually not the person authorised to release a
consignment on the strength of it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from src.core.deps import DbSession, Tenant, require_permission
from src.core.pagination import PageParams, SortParams, page_params, sort_params
from src.core.permissions import Permission
from src.core.responses import COMMON_ERROR_RESPONSES, ApiResponse, PaginatedResponse
from src.modules.quality import service
from src.modules.quality.models import (
    InspectionResult,
    InspectionStatus,
    QcStage,
    ReworkStatus,
    SamplingPlan,
)
from src.modules.quality.schemas import (
    DEFAULT_REPORT_WINDOW_DAYS,
    MAX_REPORT_WINDOW_DAYS,
    CloseRequest,
    DecideRequest,
    DecisionResponse,
    InspectionCreate,
    InspectionDetail,
    InspectionFilters,
    InspectionSummary,
    ItemQualityHistory,
    RecordResultsRequest,
    RecordResultsResponse,
    ReworkCompleteRequest,
    ReworkCreate,
    ReworkFilters,
    ReworkRecordRead,
    ReworkStartRequest,
    TemplateCreate,
    TemplateDetail,
    TemplateFilters,
    TemplateSummary,
    TemplateUpdate,
    TemplateVersionRequest,
    VendorQualitySummary,
)

router = APIRouter(prefix="/quality", tags=["Quality"], responses=COMMON_ERROR_RESPONSES)

PageDep = Annotated[PageParams, Depends(page_params)]
SortDep = Annotated[SortParams, Depends(sort_params)]

# Retirement carries its reason in the query string: a DELETE body is legal but
# several HTTP clients and proxies drop it, and the reason is not optional
# (master rules section 83).
ReasonQuery = Annotated[
    str, Query(min_length=3, max_length=255, description="Why the template is being retired")
]

WindowQuery = Annotated[
    int,
    Query(
        ge=1,
        le=MAX_REPORT_WINDOW_DAYS,
        description="Reporting window in days, counted back from now",
    ),
]


def template_filters(
    search: Annotated[str | None, Query(max_length=120, description="Code or name")] = None,
    item_id: Annotated[uuid.UUID | None, Query()] = None,
    item_category_id: Annotated[uuid.UUID | None, Query()] = None,
    qc_stage: Annotated[QcStage | None, Query()] = None,
    sampling_plan: Annotated[SamplingPlan | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
) -> TemplateFilters:
    return TemplateFilters(
        search=search,
        item_id=item_id,
        item_category_id=item_category_id,
        qc_stage=qc_stage,
        sampling_plan=sampling_plan,
        is_active=is_active,
    )


def inspection_filters(
    search: Annotated[
        str | None, Query(max_length=120, description="Inspection code or source document code")
    ] = None,
    qc_stage: Annotated[QcStage | None, Query()] = None,
    inspection_status: Annotated[InspectionStatus | None, Query(alias="status")] = None,
    result: Annotated[InspectionResult | None, Query()] = None,
    item_id: Annotated[uuid.UUID | None, Query()] = None,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    vendor_id: Annotated[uuid.UUID | None, Query()] = None,
    reference_type: Annotated[
        str | None, Query(max_length=48, description="e.g. goods_receipt, assembly_order")
    ] = None,
    reference_id: Annotated[uuid.UUID | None, Query()] = None,
    pending_only: Annotated[
        bool, Query(description="Only inspections nobody has decided yet")
    ] = False,
    rework_only: Annotated[
        bool, Query(description="Only inspections holding quantity flagged for rework")
    ] = False,
    inspected_from: Annotated[datetime | None, Query()] = None,
    inspected_to: Annotated[datetime | None, Query()] = None,
) -> InspectionFilters:
    return InspectionFilters(
        search=search,
        qc_stage=qc_stage,
        status=inspection_status,
        result=result,
        item_id=item_id,
        warehouse_id=warehouse_id,
        vendor_id=vendor_id,
        reference_type=reference_type,
        reference_id=reference_id,
        pending_only=pending_only,
        rework_only=rework_only,
        inspected_from=inspected_from,
        inspected_to=inspected_to,
    )


TemplateFilterDep = Annotated[TemplateFilters, Depends(template_filters)]
InspectionFilterDep = Annotated[InspectionFilters, Depends(inspection_filters)]


def rework_filters(
    inspection_id: Annotated[uuid.UUID | None, Query()] = None,
    rework_status: Annotated[ReworkStatus | None, Query(alias="status")] = None,
    assigned_to: Annotated[uuid.UUID | None, Query()] = None,
    work_center_id: Annotated[uuid.UUID | None, Query()] = None,
) -> ReworkFilters:
    return ReworkFilters(
        inspection_id=inspection_id,
        status=rework_status,
        assigned_to=assigned_to,
        work_center_id=work_center_id,
    )


ReworkFilterDep = Annotated[ReworkFilters, Depends(rework_filters)]


# -----------------------------------------------------------------------------
# Templates
# -----------------------------------------------------------------------------


@router.get(
    "/templates",
    response_model=PaginatedResponse[TemplateSummary],
    summary="List inspection specifications",
    dependencies=[require_permission(Permission.QC_VIEW)],
)
async def list_templates(
    session: DbSession,
    tenant: Tenant,
    filters: TemplateFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[TemplateSummary]:
    rows, pagination = await service.list_templates(session, tenant, filters, params, sort)
    return PaginatedResponse[TemplateSummary](
        data=[TemplateSummary.model_validate(row) for row in rows], pagination=pagination
    )


@router.post(
    "/templates",
    response_model=ApiResponse[TemplateDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Create version 1 of an inspection specification",
    description=(
        "A template applies to one item, or to one item category, or to a whole stage. At most "
        "one active template may claim each of those scopes, so resolving the specification for "
        "an incoming lot has a single deterministic answer."
    ),
    dependencies=[require_permission(Permission.QC_TEMPLATE_MANAGE)],
)
async def create_template(
    payload: TemplateCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[TemplateDetail]:
    template, parameters = await service.create_template(session, tenant, payload)
    return ApiResponse[TemplateDetail].ok(
        service.template_detail(template, parameters), f"QC template {template.code} created"
    )


@router.get(
    "/templates/{template_id}",
    response_model=ApiResponse[TemplateDetail],
    summary="Fetch one specification with its check sheet",
    dependencies=[require_permission(Permission.QC_VIEW)],
)
async def get_template(
    template_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[TemplateDetail]:
    return ApiResponse[TemplateDetail].ok(
        await service.get_template_detail(session, tenant, template_id)
    )


@router.patch(
    "/templates/{template_id}",
    response_model=ApiResponse[TemplateDetail],
    summary="Amend a specification",
    description=(
        "Name, description and activation are always editable. Scope, sampling plan and the "
        "parameter set are editable only while no inspection has applied the template - "
        "afterwards the change has to fork a new version, so past pass and fail results stay "
        "reproducible against the specification they were judged by."
    ),
    dependencies=[require_permission(Permission.QC_TEMPLATE_MANAGE)],
)
async def update_template(
    template_id: uuid.UUID, payload: TemplateUpdate, session: DbSession, tenant: Tenant
) -> ApiResponse[TemplateDetail]:
    template, parameters = await service.update_template(session, tenant, template_id, payload)
    return ApiResponse[TemplateDetail].ok(
        service.template_detail(template, parameters), f"QC template {template.code} updated"
    )


@router.post(
    "/templates/{template_id}/versions",
    response_model=ApiResponse[TemplateDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Fork a used specification into a new version",
    description=(
        "Copies the current check sheet unless a replacement is supplied, and retires the "
        "previous version so only one specification is active for the scope."
    ),
    dependencies=[require_permission(Permission.QC_TEMPLATE_MANAGE)],
)
async def create_template_version(
    template_id: uuid.UUID,
    payload: TemplateVersionRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[TemplateDetail]:
    template, parameters = await service.create_template_version(
        session, tenant, template_id, payload
    )
    return ApiResponse[TemplateDetail].ok(
        service.template_detail(template, parameters),
        f"QC template {template.code} v{template.version} created",
    )


@router.delete(
    "/templates/{template_id}",
    response_model=ApiResponse[None],
    summary="Retire a specification",
    description=(
        "Soft deletion. Inspections name the template they followed, so the row is retired "
        "rather than removed - a specification that vanished would take the meaning of every "
        "past inspection with it."
    ),
    dependencies=[require_permission(Permission.QC_TEMPLATE_MANAGE)],
)
async def retire_template(
    template_id: uuid.UUID, session: DbSession, tenant: Tenant, reason: ReasonQuery
) -> ApiResponse[None]:
    await service.retire_template(session, tenant, template_id, reason=reason)
    return ApiResponse[None].ok(None, "QC template retired")


# -----------------------------------------------------------------------------
# The queue and the reports (declared before /inspections/{id} - see the docstring)
# -----------------------------------------------------------------------------


@router.get(
    "/pending",
    response_model=PaginatedResponse[InspectionSummary],
    summary="Inspections waiting for a verdict",
    description=(
        "Oldest first: the oldest hold is the one blocking a delivery. Served by the partial "
        "index on the open statuses, so the queue stays cheap as the inspection history grows."
    ),
    dependencies=[require_permission(Permission.QC_VIEW)],
)
async def list_pending(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    qc_stage: Annotated[QcStage | None, Query(description="Restrict to one stage")] = None,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
) -> PaginatedResponse[InspectionSummary]:
    rows, pagination = await service.list_pending(
        session, tenant, params, stage=qc_stage, warehouse_id=warehouse_id
    )
    return PaginatedResponse[InspectionSummary](
        data=[InspectionSummary.model_validate(row) for row in rows], pagination=pagination
    )


@router.get(
    "/reports/item/{item_id}",
    response_model=ApiResponse[ItemQualityHistory],
    summary="Rejection rate for one item over time",
    description=(
        "Totals for the window plus one row per calendar month, aggregated in the database. "
        "Only decided inspections count - a pending one has no verdict, and including it would "
        "drag the rate towards zero purely because nobody has looked at the goods yet."
    ),
    dependencies=[require_permission(Permission.QC_VIEW)],
)
async def item_quality_report(
    item_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    window_days: WindowQuery = DEFAULT_REPORT_WINDOW_DAYS,
) -> ApiResponse[ItemQualityHistory]:
    return ApiResponse[ItemQualityHistory].ok(
        await service.item_quality_history(session, tenant, item_id, window_days=window_days)
    )


@router.get(
    "/reports/vendor/{vendor_id}",
    response_model=ApiResponse[VendorQualitySummary],
    summary="Rejection rate for one vendor, broken down by item",
    description=(
        "The read the vendor rating service consumes alongside its own delivery figures. The "
        "item breakdown is ordered by inspected quantity and capped, so the items that dominate "
        "the rate survive the cut."
    ),
    dependencies=[require_permission(Permission.QC_VIEW)],
)
async def vendor_quality_report(
    vendor_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    window_days: WindowQuery = DEFAULT_REPORT_WINDOW_DAYS,
) -> ApiResponse[VendorQualitySummary]:
    return ApiResponse[VendorQualitySummary].ok(
        await service.vendor_quality_summary(session, tenant, vendor_id, window_days=window_days)
    )


# -----------------------------------------------------------------------------
# Inspections
# -----------------------------------------------------------------------------


@router.get(
    "/inspections",
    response_model=PaginatedResponse[InspectionSummary],
    summary="List inspections",
    dependencies=[require_permission(Permission.QC_VIEW)],
)
async def list_inspections(
    session: DbSession,
    tenant: Tenant,
    filters: InspectionFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[InspectionSummary]:
    rows, pagination = await service.list_inspections(session, tenant, filters, params, sort)
    return PaginatedResponse[InspectionSummary](
        data=[InspectionSummary.model_validate(row) for row in rows], pagination=pagination
    )


@router.post(
    "/inspections",
    response_model=ApiResponse[InspectionDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Raise an inspection by hand",
    description=(
        "Most inspections are raised automatically by the goods receipt, job work return or "
        "assembly completion that put the stock on QC hold. This endpoint is for a supervisor "
        "pulling a lot off the shelf: pass `hold_stock` to move the quantity from AVAILABLE into "
        "QC_HOLD as part of raising it. Leave it false when an upstream document has already "
        "held the stock, or the quantity would be taken out of availability twice."
    ),
    dependencies=[require_permission(Permission.QC_INSPECT)],
)
async def create_inspection(
    payload: InspectionCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[InspectionDetail]:
    inspection = await service.create_inspection(session, tenant, payload)
    return ApiResponse[InspectionDetail].ok(
        await service.get_inspection_detail(session, tenant, inspection.id),
        f"Inspection {inspection.code} raised",
    )


@router.get(
    "/inspections/{inspection_id}",
    response_model=ApiResponse[InspectionDetail],
    summary="Fetch one inspection with its check sheet and readings",
    dependencies=[require_permission(Permission.QC_VIEW)],
)
async def get_inspection(
    inspection_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[InspectionDetail]:
    return ApiResponse[InspectionDetail].ok(
        await service.get_inspection_detail(session, tenant, inspection_id)
    )


@router.post(
    "/inspections/{inspection_id}/results",
    response_model=ApiResponse[RecordResultsResponse],
    summary="Record parameter measurements",
    description=(
        "Each reading is judged against its parameter's specification on the server; the client "
        "never asserts its own pass or fail. A second reading for the same parameter replaces "
        "the first. The response carries the resulting recommendation, including whether a "
        "critical characteristic has already settled the verdict."
    ),
    dependencies=[require_permission(Permission.QC_INSPECT)],
)
async def record_results(
    inspection_id: uuid.UUID,
    payload: RecordResultsRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[RecordResultsResponse]:
    outcome = await service.record_results(session, tenant, inspection_id, payload)
    return ApiResponse[RecordResultsResponse].ok(
        outcome, f"{len(payload.measurements)} measurement(s) recorded"
    )


@router.post(
    "/inspections/{inspection_id}/decide",
    response_model=ApiResponse[DecisionResponse],
    summary="Decide an inspection and move the stock it decided",
    description=(
        "Accepted quantity is released from QC hold into the available pool, rejected quantity "
        "moves to the damaged pool and raises the vendor return, scrapped quantity is written "
        "off, and rework and hold quantities deliberately stay on QC hold to be decided again "
        "later. The five quantities must add up to the inspected quantity. A critical "
        "characteristic out of specification rejects the lot outright: none of it may be "
        "accepted or reworked."
    ),
    dependencies=[require_permission(Permission.QC_DECIDE)],
)
async def decide_inspection(
    inspection_id: uuid.UUID, payload: DecideRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[DecisionResponse]:
    outcome = await service.decide_inspection(session, tenant, inspection_id, payload)
    return ApiResponse[DecisionResponse].ok(
        outcome, f"Inspection {outcome.inspection.code} decided {outcome.inspection.result}"
    )


@router.post(
    "/inspections/{inspection_id}/close",
    response_model=ApiResponse[InspectionSummary],
    summary="Close a decided inspection",
    description=(
        "Only once nothing of the lot is still on QC hold: quantity left for rework or a further "
        "decision still owes somebody a verdict."
    ),
    dependencies=[require_permission(Permission.QC_DECIDE)],
)
async def close_inspection(
    inspection_id: uuid.UUID, payload: CloseRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[InspectionSummary]:
    inspection = await service.close_inspection(
        session, tenant, inspection_id, reason=payload.reason
    )
    return ApiResponse[InspectionSummary].ok(
        InspectionSummary.model_validate(inspection), f"Inspection {inspection.code} closed"
    )


# -----------------------------------------------------------------------------
# Rework cycles
# -----------------------------------------------------------------------------


@router.get(
    "/rework",
    response_model=PaginatedResponse[ReworkRecordRead],
    summary="List rework cycles",
    dependencies=[require_permission(Permission.QC_VIEW)],
)
async def list_rework(
    session: DbSession,
    tenant: Tenant,
    filters: ReworkFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[ReworkRecordRead]:
    rows, pagination = await service.list_rework(session, tenant, filters, params, sort)
    return PaginatedResponse[ReworkRecordRead](
        data=[ReworkRecordRead.model_validate(row) for row in rows], pagination=pagination
    )


@router.post(
    "/rework",
    response_model=ApiResponse[ReworkRecordRead],
    status_code=status.HTTP_201_CREATED,
    summary="Open a rework cycle",
    description=(
        "Send part of an inspection's rework quantity to the bench. The stock stays in QC hold "
        "for the whole cycle; completing the cycle re-opens the inspection so the quantity is "
        "re-decided."
    ),
    dependencies=[require_permission(Permission.QC_INSPECT)],
)
async def create_rework(
    payload: ReworkCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[ReworkRecordRead]:
    record = await service.create_rework(session, tenant, payload)
    return ApiResponse[ReworkRecordRead].ok(
        ReworkRecordRead.model_validate(record), f"Rework {record.code} opened"
    )


@router.post(
    "/rework/{record_id}/start",
    response_model=ApiResponse[ReworkRecordRead],
    summary="Start a rework cycle",
    dependencies=[require_permission(Permission.QC_INSPECT)],
)
async def start_rework(
    record_id: uuid.UUID, payload: ReworkStartRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[ReworkRecordRead]:
    record = await service.start_rework(session, tenant, record_id, reason=payload.reason)
    return ApiResponse[ReworkRecordRead].ok(
        ReworkRecordRead.model_validate(record), f"Rework {record.code} started"
    )


@router.post(
    "/rework/{record_id}/complete",
    response_model=ApiResponse[ReworkRecordRead],
    summary="Complete a rework cycle and re-open the inspection",
    description=(
        "The outcome is the bench's recommendation, not a stock movement: even a SCRAPPED "
        "outcome leaves the quantity in QC hold for the inspector to disposition on re-decision. "
        "The inspection is re-opened so that re-decision is owed."
    ),
    dependencies=[require_permission(Permission.QC_DECIDE)],
)
async def complete_rework(
    record_id: uuid.UUID, payload: ReworkCompleteRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[ReworkRecordRead]:
    record = await service.complete_rework(session, tenant, record_id, payload)
    return ApiResponse[ReworkRecordRead].ok(
        ReworkRecordRead.model_validate(record),
        f"Rework {record.code} completed {record.outcome}",
    )
