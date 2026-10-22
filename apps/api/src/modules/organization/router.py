"""HTTP surface for the organisation masters.

Route handlers do three things and nothing else: bind parameters, call one
service method, wrap the result in the standard envelope. Every rule, every
query and every audit write lives behind the service (section 74).

Two shapes are deliberate:

* **``/company`` is singular and has no ``{id}``.** The company is resolved from
  the tenant context, so there is no list endpoint and no path that could name
  another tenant's company. That absence *is* the tenant boundary - a route
  taking a company id would be one refactor away from leaking one.
* **``/financial-years/{id}/close`` and ``/reopen`` are commands, not PATCHes.**
  Closing a period is a state transition with its own authority and its own
  audit entry; exposing it as a writable ``is_closed`` field would make it
  indistinguishable from editing a note.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from src.core.deps import DbSession, Tenant, require_permission, require_superuser
from src.core.messages import success_message
from src.core.pagination import PageParams, SortParams, bind_query_filters, page_params, sort_params
from src.core.permissions import Permission
from src.core.responses import COMMON_ERROR_RESPONSES, ApiResponse, PaginatedResponse
from src.modules.organization.models import CostCentreType
from src.modules.organization.schemas import (
    BranchCreate,
    BranchFilters,
    BranchRead,
    BranchUpdate,
    CompanyRead,
    CompanyUpdate,
    CostCentreCreate,
    CostCentreFilters,
    CostCentreRead,
    CostCentreUpdate,
    DepartmentCreate,
    DepartmentFilters,
    DepartmentRead,
    DepartmentUpdate,
    FinancialYearCreate,
    FinancialYearFilters,
    FinancialYearRead,
    FinancialYearUpdate,
    PlantCreate,
    PlantFilters,
    PlantRead,
    PlantUpdate,
    ReasonRequest,
)
from src.modules.organization.service import (
    BranchService,
    CompanyService,
    CostCentreService,
    DepartmentService,
    FinancialYearService,
    PlantService,
)

router = APIRouter(prefix="/organization", tags=["Organisation"], responses=COMMON_ERROR_RESPONSES)

PageDep = Annotated[PageParams, Depends(page_params)]
SortDep = Annotated[SortParams, Depends(sort_params)]

# Retirement carries its reason in the query string rather than a request body:
# a DELETE body is legal but several HTTP clients and proxies drop it, and the
# reason is not optional (section 83).
ReasonQuery = Annotated[
    str, Query(min_length=3, max_length=255, description="Why the record is being retired")
]

# ASSUMPTION: financial years are gated on SETTINGS_VIEW / SETTINGS_MANAGE.
# The Permission enum has no financial-year member, and the accounting calendar
# is company configuration rather than a partner or product master - inventing a
# new permission string here would silently expand the API contract.
FY_VIEW = Permission.SETTINGS_VIEW
FY_MANAGE = Permission.SETTINGS_MANAGE


def branch_filters(
    search: Annotated[str | None, Query(max_length=120, description="Code, name or city")] = None,
    is_active: Annotated[bool | None, Query()] = None,
    is_head_office: Annotated[bool | None, Query()] = None,
    include_deleted: Annotated[bool, Query(description="Include retired branches")] = False,
) -> BranchFilters:
    return bind_query_filters(
        BranchFilters,
        search=search,
        is_active=is_active,
        is_head_office=is_head_office,
        include_deleted=include_deleted,
    )


def plant_filters(
    search: Annotated[str | None, Query(max_length=120, description="Code, name or city")] = None,
    branch_id: Annotated[uuid.UUID | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
    include_deleted: Annotated[bool, Query(description="Include retired plants")] = False,
) -> PlantFilters:
    return bind_query_filters(
        PlantFilters,
        search=search,
        branch_id=branch_id,
        is_active=is_active,
        include_deleted=include_deleted,
    )


def department_filters(
    search: Annotated[str | None, Query(max_length=120, description="Code or name")] = None,
    plant_id: Annotated[uuid.UUID | None, Query()] = None,
    parent_id: Annotated[uuid.UUID | None, Query()] = None,
    head_user_id: Annotated[uuid.UUID | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
    include_deleted: Annotated[bool, Query(description="Include retired departments")] = False,
) -> DepartmentFilters:
    return bind_query_filters(
        DepartmentFilters,
        search=search,
        plant_id=plant_id,
        parent_id=parent_id,
        head_user_id=head_user_id,
        is_active=is_active,
        include_deleted=include_deleted,
    )


def cost_centre_filters(
    search: Annotated[str | None, Query(max_length=120, description="Code or name")] = None,
    parent_id: Annotated[uuid.UUID | None, Query(description="Direct children of")] = None,
    plant_id: Annotated[uuid.UUID | None, Query()] = None,
    cost_centre_type: Annotated[CostCentreType | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
    include_deleted: Annotated[bool, Query(description="Include retired cost centres")] = False,
) -> CostCentreFilters:
    return bind_query_filters(
        CostCentreFilters,
        search=search,
        parent_id=parent_id,
        plant_id=plant_id,
        cost_centre_type=cost_centre_type,
        is_active=is_active,
        include_deleted=include_deleted,
    )


def financial_year_filters(
    search: Annotated[str | None, Query(max_length=32, description="Year code")] = None,
    is_active: Annotated[bool | None, Query()] = None,
    is_closed: Annotated[bool | None, Query()] = None,
    contains_date: Annotated[
        date | None, Query(description="Return the year whose range contains this date")
    ] = None,
    include_deleted: Annotated[bool, Query(description="Include retired years")] = False,
) -> FinancialYearFilters:
    return bind_query_filters(
        FinancialYearFilters,
        search=search,
        is_active=is_active,
        is_closed=is_closed,
        contains_date=contains_date,
        include_deleted=include_deleted,
    )


BranchFilterDep = Annotated[BranchFilters, Depends(branch_filters)]
PlantFilterDep = Annotated[PlantFilters, Depends(plant_filters)]
DepartmentFilterDep = Annotated[DepartmentFilters, Depends(department_filters)]
CostCentreFilterDep = Annotated[CostCentreFilters, Depends(cost_centre_filters)]
FinancialYearFilterDep = Annotated[FinancialYearFilters, Depends(financial_year_filters)]


# -----------------------------------------------------------------------------
# Company
# -----------------------------------------------------------------------------


@router.get(
    "/company",
    response_model=ApiResponse[CompanyRead],
    summary="Fetch the caller's own company",
    dependencies=[require_permission(Permission.COMPANY_VIEW)],
)
async def get_company(session: DbSession, tenant: Tenant) -> ApiResponse[CompanyRead]:
    company = await CompanyService(session, tenant).get_company()
    return ApiResponse[CompanyRead].ok(company)


@router.patch(
    "/company",
    response_model=ApiResponse[CompanyRead],
    summary="Update the caller's own company profile",
    dependencies=[require_permission(Permission.COMPANY_MANAGE)],
)
async def update_company(
    payload: CompanyUpdate, session: DbSession, tenant: Tenant
) -> ApiResponse[CompanyRead]:
    company = await CompanyService(session, tenant).update_company(payload)
    return ApiResponse[CompanyRead].ok(company, success_message("Company profile", "updated"))


# -----------------------------------------------------------------------------
# Branches
# -----------------------------------------------------------------------------


@router.get(
    "/branches",
    response_model=PaginatedResponse[BranchRead],
    summary="List branches",
    dependencies=[require_permission(Permission.BRANCH_VIEW)],
)
async def list_branches(
    session: DbSession,
    tenant: Tenant,
    filters: BranchFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[BranchRead]:
    items, pagination = await BranchService(session, tenant).list_branches(filters, params, sort)
    return PaginatedResponse[BranchRead](data=items, pagination=pagination)


@router.post(
    "/branches",
    response_model=ApiResponse[BranchRead],
    status_code=status.HTTP_201_CREATED,
    summary="Create a branch",
    dependencies=[require_permission(Permission.BRANCH_MANAGE)],
)
async def create_branch(
    payload: BranchCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[BranchRead]:
    branch = await BranchService(session, tenant).create_branch(payload)
    return ApiResponse[BranchRead].ok(branch, success_message("Branch", "created"))


@router.get(
    "/branches/{branch_id}",
    response_model=ApiResponse[BranchRead],
    summary="Fetch one branch",
    dependencies=[require_permission(Permission.BRANCH_VIEW)],
)
async def get_branch(
    branch_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[BranchRead]:
    branch = await BranchService(session, tenant).get_branch(branch_id)
    return ApiResponse[BranchRead].ok(branch)


@router.patch(
    "/branches/{branch_id}",
    response_model=ApiResponse[BranchRead],
    summary="Update a branch",
    dependencies=[require_permission(Permission.BRANCH_MANAGE)],
)
async def update_branch(
    branch_id: uuid.UUID,
    payload: BranchUpdate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[BranchRead]:
    branch = await BranchService(session, tenant).update_branch(branch_id, payload)
    return ApiResponse[BranchRead].ok(branch, success_message("Branch", "updated"))


@router.delete(
    "/branches/{branch_id}",
    response_model=ApiResponse[None],
    summary="Retire a branch",
    dependencies=[require_permission(Permission.BRANCH_MANAGE)],
)
async def delete_branch(
    branch_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    reason: ReasonQuery,
) -> ApiResponse[None]:
    await BranchService(session, tenant).delete_branch(branch_id, reason=reason)
    return ApiResponse[None].ok(None, success_message("Branch", "retired"))


# -----------------------------------------------------------------------------
# Plants
# -----------------------------------------------------------------------------


@router.get(
    "/plants",
    response_model=PaginatedResponse[PlantRead],
    summary="List plants",
    dependencies=[require_permission(Permission.PLANT_VIEW)],
)
async def list_plants(
    session: DbSession,
    tenant: Tenant,
    filters: PlantFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[PlantRead]:
    items, pagination = await PlantService(session, tenant).list_plants(filters, params, sort)
    return PaginatedResponse[PlantRead](data=items, pagination=pagination)


@router.post(
    "/plants",
    response_model=ApiResponse[PlantRead],
    status_code=status.HTTP_201_CREATED,
    summary="Create a plant",
    dependencies=[require_permission(Permission.PLANT_MANAGE)],
)
async def create_plant(
    payload: PlantCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[PlantRead]:
    plant = await PlantService(session, tenant).create_plant(payload)
    return ApiResponse[PlantRead].ok(plant, success_message("Plant", "created"))


@router.get(
    "/plants/{plant_id}",
    response_model=ApiResponse[PlantRead],
    summary="Fetch one plant",
    dependencies=[require_permission(Permission.PLANT_VIEW)],
)
async def get_plant(
    plant_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[PlantRead]:
    plant = await PlantService(session, tenant).get_plant(plant_id)
    return ApiResponse[PlantRead].ok(plant)


@router.patch(
    "/plants/{plant_id}",
    response_model=ApiResponse[PlantRead],
    summary="Update a plant",
    dependencies=[require_permission(Permission.PLANT_MANAGE)],
)
async def update_plant(
    plant_id: uuid.UUID,
    payload: PlantUpdate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[PlantRead]:
    plant = await PlantService(session, tenant).update_plant(plant_id, payload)
    return ApiResponse[PlantRead].ok(plant, success_message("Plant", "updated"))


@router.delete(
    "/plants/{plant_id}",
    response_model=ApiResponse[None],
    summary="Retire a plant",
    dependencies=[require_permission(Permission.PLANT_MANAGE)],
)
async def delete_plant(
    plant_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    reason: ReasonQuery,
) -> ApiResponse[None]:
    await PlantService(session, tenant).delete_plant(plant_id, reason=reason)
    return ApiResponse[None].ok(None, success_message("Plant", "retired"))


# -----------------------------------------------------------------------------
# Departments
# -----------------------------------------------------------------------------


@router.get(
    "/departments",
    response_model=PaginatedResponse[DepartmentRead],
    summary="List departments",
    dependencies=[require_permission(Permission.DEPARTMENT_VIEW)],
)
async def list_departments(
    session: DbSession,
    tenant: Tenant,
    filters: DepartmentFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[DepartmentRead]:
    items, pagination = await DepartmentService(session, tenant).list_departments(
        filters, params, sort
    )
    return PaginatedResponse[DepartmentRead](data=items, pagination=pagination)


@router.post(
    "/departments",
    response_model=ApiResponse[DepartmentRead],
    status_code=status.HTTP_201_CREATED,
    summary="Create a department",
    dependencies=[require_permission(Permission.DEPARTMENT_MANAGE)],
)
async def create_department(
    payload: DepartmentCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[DepartmentRead]:
    department = await DepartmentService(session, tenant).create_department(payload)
    return ApiResponse[DepartmentRead].ok(department, success_message("Department", "created"))


@router.get(
    "/departments/{department_id}",
    response_model=ApiResponse[DepartmentRead],
    summary="Fetch one department",
    dependencies=[require_permission(Permission.DEPARTMENT_VIEW)],
)
async def get_department(
    department_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[DepartmentRead]:
    department = await DepartmentService(session, tenant).get_department(department_id)
    return ApiResponse[DepartmentRead].ok(department)


@router.patch(
    "/departments/{department_id}",
    response_model=ApiResponse[DepartmentRead],
    summary="Update a department",
    dependencies=[require_permission(Permission.DEPARTMENT_MANAGE)],
)
async def update_department(
    department_id: uuid.UUID,
    payload: DepartmentUpdate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[DepartmentRead]:
    department = await DepartmentService(session, tenant).update_department(department_id, payload)
    return ApiResponse[DepartmentRead].ok(department, success_message("Department", "updated"))


@router.delete(
    "/departments/{department_id}",
    response_model=ApiResponse[None],
    summary="Retire a department",
    dependencies=[require_permission(Permission.DEPARTMENT_MANAGE)],
)
async def delete_department(
    department_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    reason: ReasonQuery,
) -> ApiResponse[None]:
    await DepartmentService(session, tenant).delete_department(department_id, reason=reason)
    return ApiResponse[None].ok(None, success_message("Department", "retired"))


# -----------------------------------------------------------------------------
# Cost centres
# -----------------------------------------------------------------------------


@router.get(
    "/cost-centres",
    response_model=PaginatedResponse[CostCentreRead],
    summary="List cost centres",
    dependencies=[require_permission(Permission.COST_CENTRE_VIEW)],
)
async def list_cost_centres(
    session: DbSession,
    tenant: Tenant,
    filters: CostCentreFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[CostCentreRead]:
    items, pagination = await CostCentreService(session, tenant).list_cost_centres(
        filters, params, sort
    )
    return PaginatedResponse[CostCentreRead](data=items, pagination=pagination)


@router.post(
    "/cost-centres",
    response_model=ApiResponse[CostCentreRead],
    status_code=status.HTTP_201_CREATED,
    summary="Create a cost centre",
    dependencies=[require_permission(Permission.COST_CENTRE_MANAGE)],
)
async def create_cost_centre(
    payload: CostCentreCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[CostCentreRead]:
    centre = await CostCentreService(session, tenant).create_cost_centre(payload)
    return ApiResponse[CostCentreRead].ok(centre, success_message("Cost centre", "created"))


@router.get(
    "/cost-centres/{cost_centre_id}",
    response_model=ApiResponse[CostCentreRead],
    summary="Fetch one cost centre",
    dependencies=[require_permission(Permission.COST_CENTRE_VIEW)],
)
async def get_cost_centre(
    cost_centre_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[CostCentreRead]:
    centre = await CostCentreService(session, tenant).get_cost_centre(cost_centre_id)
    return ApiResponse[CostCentreRead].ok(centre)


@router.patch(
    "/cost-centres/{cost_centre_id}",
    response_model=ApiResponse[CostCentreRead],
    summary="Update a cost centre",
    description=(
        "Deactivating (`is_active: false`) is refused while an active sub-centre, a "
        "department default or a work centre still hands the centre out; documents "
        "keep their reference."
    ),
    dependencies=[require_permission(Permission.COST_CENTRE_MANAGE)],
)
async def update_cost_centre(
    cost_centre_id: uuid.UUID,
    payload: CostCentreUpdate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[CostCentreRead]:
    centre = await CostCentreService(session, tenant).update_cost_centre(cost_centre_id, payload)
    return ApiResponse[CostCentreRead].ok(centre, success_message("Cost centre", "updated"))


@router.delete(
    "/cost-centres/{cost_centre_id}",
    response_model=ApiResponse[None],
    summary="Retire a cost centre",
    description=(
        "Refused while a sub-centre, a department, a work centre or any document still "
        "names the centre. Posted ledger history keeps it."
    ),
    dependencies=[require_permission(Permission.COST_CENTRE_MANAGE)],
)
async def delete_cost_centre(
    cost_centre_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    reason: ReasonQuery,
) -> ApiResponse[None]:
    await CostCentreService(session, tenant).delete_cost_centre(cost_centre_id, reason=reason)
    return ApiResponse[None].ok(None, success_message("Cost centre", "retired"))


# -----------------------------------------------------------------------------
# Financial years
# -----------------------------------------------------------------------------


@router.get(
    "/financial-years",
    response_model=PaginatedResponse[FinancialYearRead],
    summary="List financial years",
    dependencies=[require_permission(FY_VIEW)],
)
async def list_financial_years(
    session: DbSession,
    tenant: Tenant,
    filters: FinancialYearFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[FinancialYearRead]:
    items, pagination = await FinancialYearService(session, tenant).list_years(
        filters, params, sort
    )
    return PaginatedResponse[FinancialYearRead](data=items, pagination=pagination)


@router.post(
    "/financial-years",
    response_model=ApiResponse[FinancialYearRead],
    status_code=status.HTTP_201_CREATED,
    summary="Open a financial year",
    dependencies=[require_permission(FY_MANAGE)],
)
async def create_financial_year(
    payload: FinancialYearCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[FinancialYearRead]:
    year = await FinancialYearService(session, tenant).create_year(payload)
    return ApiResponse[FinancialYearRead].ok(year, success_message("Financial year", "opened"))


@router.get(
    "/financial-years/{year_id}",
    response_model=ApiResponse[FinancialYearRead],
    summary="Fetch one financial year",
    dependencies=[require_permission(FY_VIEW)],
)
async def get_financial_year(
    year_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[FinancialYearRead]:
    year = await FinancialYearService(session, tenant).get_year(year_id)
    return ApiResponse[FinancialYearRead].ok(year)


@router.patch(
    "/financial-years/{year_id}",
    response_model=ApiResponse[FinancialYearRead],
    summary="Update an open financial year",
    dependencies=[require_permission(FY_MANAGE)],
)
async def update_financial_year(
    year_id: uuid.UUID,
    payload: FinancialYearUpdate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[FinancialYearRead]:
    year = await FinancialYearService(session, tenant).update_year(year_id, payload)
    return ApiResponse[FinancialYearRead].ok(year, success_message("Financial year", "updated"))


@router.post(
    "/financial-years/{year_id}/close",
    response_model=ApiResponse[FinancialYearRead],
    summary="Close a financial year (one-way for non-administrators)",
    dependencies=[require_permission(FY_MANAGE)],
)
async def close_financial_year(
    year_id: uuid.UUID,
    payload: ReasonRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[FinancialYearRead]:
    year = await FinancialYearService(session, tenant).close_year(year_id, reason=payload.reason)
    return ApiResponse[FinancialYearRead].ok(year, success_message("Financial year", "closed"))


@router.post(
    "/financial-years/{year_id}/reopen",
    response_model=ApiResponse[FinancialYearRead],
    summary="Reopen a closed financial year (platform administrators only)",
    # Not a permission: reopening a filed period must not be grantable through
    # the ordinary role editor. The service repeats the check so the rule holds
    # for any future caller that does not come through this route.
    dependencies=[Depends(require_superuser)],
)
async def reopen_financial_year(
    year_id: uuid.UUID,
    payload: ReasonRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[FinancialYearRead]:
    year = await FinancialYearService(session, tenant).reopen_year(year_id, reason=payload.reason)
    return ApiResponse[FinancialYearRead].ok(year, success_message("Financial year", "reopened"))


@router.delete(
    "/financial-years/{year_id}",
    response_model=ApiResponse[None],
    summary="Retire a financial year created in error",
    dependencies=[require_permission(FY_MANAGE)],
)
async def delete_financial_year(
    year_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    reason: ReasonQuery,
) -> ApiResponse[None]:
    await FinancialYearService(session, tenant).delete_year(year_id, reason=reason)
    return ApiResponse[None].ok(None, success_message("Financial year", "retired"))


__all__ = ["router"]
