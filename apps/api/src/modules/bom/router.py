"""HTTP surface for bills of materials.

Handlers bind parameters, call one service function and wrap the result in the
standard envelope. No rule, query or audit write lives here (master rules
section 74).

Route ordering note: ``/where-used/{item_id}`` and ``/items/{item_id}/versions``
are declared before ``/{bom_id}``. FastAPI matches in declaration order, and
while the segment counts happen to differ today, keeping the literal-prefixed
routes first means adding ``/{bom_id}`` variants later cannot silently capture
them and reject a perfectly good request as a malformed UUID.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from src.core.deps import CurrentUser, DbSession, Tenant, require_permission
from src.core.messages import success_message
from src.core.pagination import PageParams, SortParams, bind_query_filters, page_params, sort_params
from src.core.permissions import Permission
from src.core.responses import COMMON_ERROR_RESPONSES, ApiResponse, PaginatedResponse
from src.modules.bom import service
from src.modules.bom.engine import MAX_WHERE_USED_LEVELS
from src.modules.bom.models import DEFAULT_MAX_EXPLOSION_DEPTH, BomStatus, BomType
from src.modules.bom.schemas import (
    BomCostRead,
    BomCreate,
    BomDetail,
    BomExplosionRead,
    BomFilters,
    BomStatusChange,
    BomSummary,
    BomUpdate,
    CopyAsNewVersionRequest,
    WhereUsedRead,
)

router = APIRouter(prefix="/bom", tags=["BOM"], responses=COMMON_ERROR_RESPONSES)

PageDep = Annotated[PageParams, Depends(page_params)]
SortDep = Annotated[SortParams, Depends(sort_params)]

# Deletion carries its reason in the query string: a DELETE body is legal but
# several HTTP clients and proxies drop it, and the reason is not optional
# (master rules section 83).
ReasonQuery = Annotated[
    str, Query(min_length=3, max_length=255, description="Why the draft is being deleted")
]


def bom_filters(
    search: Annotated[str | None, Query(max_length=120, description="Code or revision")] = None,
    item_id: Annotated[uuid.UUID | None, Query(description="Structures for this item")] = None,
    bom_status: Annotated[BomStatus | None, Query(alias="status")] = None,
    bom_type: Annotated[BomType | None, Query()] = None,
    is_default: Annotated[bool | None, Query()] = None,
    effective_on: Annotated[
        date | None, Query(description="Only structures effective on this date")
    ] = None,
) -> BomFilters:
    return bind_query_filters(
        BomFilters,
        search=search,
        item_id=item_id,
        status=bom_status,
        bom_type=bom_type,
        is_default=is_default,
        effective_on=effective_on,
    )


BomFilterDep = Annotated[BomFilters, Depends(bom_filters)]


# -----------------------------------------------------------------------------
# Analysis endpoints (declared first - see the module docstring)
# -----------------------------------------------------------------------------


@router.get(
    "/where-used/{item_id}",
    response_model=PaginatedResponse[WhereUsedRead],
    summary="Which structures use this component",
    dependencies=[require_permission(Permission.BOM_VIEW)],
)
async def where_used(
    item_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    levels: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_WHERE_USED_LEVELS,
            description="1 lists direct parents; higher values walk up the structure",
        ),
    ] = 1,
) -> PaginatedResponse[WhereUsedRead]:
    rows, pagination = await service.where_used_page(
        session, tenant, item_id, levels=levels, params=params
    )
    return PaginatedResponse[WhereUsedRead](data=rows, pagination=pagination)


@router.get(
    "/items/{item_id}/versions",
    response_model=PaginatedResponse[BomSummary],
    summary="Every version of one item's structure",
    dependencies=[require_permission(Permission.BOM_VIEW)],
)
async def list_versions(
    item_id: uuid.UUID, session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[BomSummary]:
    rows, pagination = await service.list_versions(session, tenant, item_id, params)
    return PaginatedResponse[BomSummary](data=rows, pagination=pagination)


# -----------------------------------------------------------------------------
# CRUD
# -----------------------------------------------------------------------------


@router.get(
    "",
    response_model=PaginatedResponse[BomSummary],
    summary="List bills of materials",
    dependencies=[require_permission(Permission.BOM_VIEW)],
)
async def list_boms(
    session: DbSession,
    tenant: Tenant,
    filters: BomFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[BomSummary]:
    rows, pagination = await service.list_boms(session, tenant, filters, params, sort)
    return PaginatedResponse[BomSummary](data=rows, pagination=pagination)


@router.post(
    "",
    response_model=ApiResponse[BomDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Create a draft bill of materials",
    dependencies=[require_permission(Permission.BOM_CREATE)],
)
async def create_bom(
    payload: BomCreate, session: DbSession, tenant: Tenant, principal: CurrentUser
) -> ApiResponse[BomDetail]:
    bom, lines, operations = await service.create_bom(session, tenant, payload)
    detail = service.bom_detail_response(bom, lines, operations, principal)
    return ApiResponse[BomDetail].ok(detail, success_message("Bill of materials", "created"))


@router.get(
    "/{bom_id}",
    response_model=ApiResponse[BomDetail],
    summary="Fetch one bill of materials with its lines and operations",
    dependencies=[require_permission(Permission.BOM_VIEW)],
)
async def get_bom(
    bom_id: uuid.UUID, session: DbSession, tenant: Tenant, principal: CurrentUser
) -> ApiResponse[BomDetail]:
    detail = await service.get_bom_detail(session, tenant, principal, bom_id)
    return ApiResponse[BomDetail].ok(detail)


@router.patch(
    "/{bom_id}",
    response_model=ApiResponse[BomDetail],
    summary="Update a draft bill of materials",
    dependencies=[require_permission(Permission.BOM_UPDATE)],
)
async def update_bom(
    bom_id: uuid.UUID,
    payload: BomUpdate,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[BomDetail]:
    bom, lines, operations = await service.update_bom(session, tenant, bom_id, payload)
    detail = service.bom_detail_response(bom, lines, operations, principal)
    return ApiResponse[BomDetail].ok(detail, success_message("Bill of materials", "updated"))


@router.delete(
    "/{bom_id}",
    response_model=ApiResponse[None],
    summary="Delete a draft bill of materials",
    dependencies=[require_permission(Permission.BOM_OBSOLETE)],
)
async def delete_bom(
    bom_id: uuid.UUID, session: DbSession, tenant: Tenant, reason: ReasonQuery
) -> ApiResponse[None]:
    await service.delete_draft_bom(session, tenant, bom_id, reason=reason)
    return ApiResponse[None].ok(None, success_message("Draft bill of materials", "deleted"))


# -----------------------------------------------------------------------------
# Lifecycle
# -----------------------------------------------------------------------------


@router.post(
    "/{bom_id}/submit",
    response_model=ApiResponse[BomSummary],
    summary="Submit a draft for approval",
    dependencies=[require_permission(Permission.BOM_UPDATE)],
)
async def submit_bom(
    bom_id: uuid.UUID, payload: BomStatusChange, session: DbSession, tenant: Tenant
) -> ApiResponse[BomSummary]:
    bom = await service.submit_bom(session, tenant, bom_id, reason=payload.reason)
    return ApiResponse[BomSummary].ok(
        BomSummary.model_validate(bom), success_message("Bill of materials", "submitted")
    )


@router.post(
    "/{bom_id}/approve",
    response_model=ApiResponse[BomSummary],
    summary="Approve a bill of materials",
    description=(
        "Releases the structure to planning and production. The approver may not be "
        "the person who created it unless they hold SUPER_ADMIN."
    ),
    dependencies=[require_permission(Permission.BOM_APPROVE)],
)
async def approve_bom(
    bom_id: uuid.UUID,
    payload: BomStatusChange,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[BomSummary]:
    bom = await service.approve_bom(session, tenant, principal, bom_id, reason=payload.reason)
    return ApiResponse[BomSummary].ok(
        BomSummary.model_validate(bom), success_message("Bill of materials", "approved")
    )


@router.post(
    "/{bom_id}/reject",
    response_model=ApiResponse[BomSummary],
    summary="Return a submitted bill of materials to draft",
    dependencies=[require_permission(Permission.BOM_APPROVE)],
)
async def reject_bom(
    bom_id: uuid.UUID, payload: BomStatusChange, session: DbSession, tenant: Tenant
) -> ApiResponse[BomSummary]:
    bom = await service.reject_bom(session, tenant, bom_id, reason=payload.reason)
    return ApiResponse[BomSummary].ok(
        BomSummary.model_validate(bom), "Bill of materials returned to draft"
    )


@router.post(
    "/{bom_id}/obsolete",
    response_model=ApiResponse[BomSummary],
    summary="Retire a bill of materials",
    dependencies=[require_permission(Permission.BOM_OBSOLETE)],
)
async def obsolete_bom(
    bom_id: uuid.UUID, payload: BomStatusChange, session: DbSession, tenant: Tenant
) -> ApiResponse[BomSummary]:
    bom = await service.obsolete_bom(session, tenant, bom_id, reason=payload.reason)
    return ApiResponse[BomSummary].ok(
        BomSummary.model_validate(bom), success_message("Bill of materials", "retired")
    )


@router.post(
    "/{bom_id}/copy-as-new-version",
    response_model=ApiResponse[BomDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Create the next draft version from this structure",
    dependencies=[require_permission(Permission.BOM_CREATE)],
)
async def copy_as_new_version(
    bom_id: uuid.UUID,
    payload: CopyAsNewVersionRequest,
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
) -> ApiResponse[BomDetail]:
    bom, lines, operations = await service.copy_as_new_version(session, tenant, bom_id, payload)
    detail = service.bom_detail_response(bom, lines, operations, principal)
    return ApiResponse[BomDetail].ok(detail, f"Created version {bom.version}")


# -----------------------------------------------------------------------------
# Engine-backed reads
# -----------------------------------------------------------------------------


@router.get(
    "/{bom_id}/explode",
    response_model=ApiResponse[BomExplosionRead],
    summary="Explode a structure into a consolidated component list",
    description=(
        "Walks every level, consolidating a component that appears on several branches "
        "into one line with the summed quantity and the paths it came from. Levels below "
        "the root resolve only approved structures effective on the as-of date."
    ),
    dependencies=[require_permission(Permission.BOM_VIEW)],
)
async def explode_bom(
    bom_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    quantity: Annotated[
        Decimal, Query(gt=0, max_digits=18, decimal_places=4, description="Units to explode for")
    ] = Decimal("1"),
    as_of: Annotated[
        date | None, Query(description="Effectivity date; defaults to today (UTC)")
    ] = None,
    max_depth: Annotated[
        int, Query(ge=1, le=DEFAULT_MAX_EXPLOSION_DEPTH, description="Levels to descend")
    ] = DEFAULT_MAX_EXPLOSION_DEPTH,
) -> ApiResponse[BomExplosionRead]:
    result = await service.explode_bom(
        session, tenant, bom_id, quantity=quantity, as_of=as_of, max_depth=max_depth
    )
    return ApiResponse[BomExplosionRead].ok(result)


@router.get(
    "/{bom_id}/cost",
    response_model=ApiResponse[BomCostRead],
    summary="Multi-level costed roll-up",
    description=(
        "Recomputed live and never written back: the stored totals are refreshed when "
        "the structure is approved, so opening a costing screen cannot rewrite an "
        "approved document."
    ),
    dependencies=[require_permission(Permission.BOM_COST_VIEW)],
)
async def bom_cost(
    bom_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    as_of: Annotated[
        date | None, Query(description="Effectivity date; defaults to today (UTC)")
    ] = None,
) -> ApiResponse[BomCostRead]:
    result = await service.bom_cost(session, tenant, bom_id, as_of=as_of)
    return ApiResponse[BomCostRead].ok(result)


__all__ = ["router"]
