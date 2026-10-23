"""HTTP surface for warehouse and storage-location master data.

Route handlers here do three things and nothing else: bind query parameters,
call one service method, wrap the result in the standard envelope. Every rule,
every query and every audit write lives behind the service (section 74).

Route ordering note: the literal ``/locations`` routes are declared *before*
``/{warehouse_id}``. FastAPI matches in declaration order, so with the reverse
order a request for ``/locations/{id}`` would be captured by the warehouse route
and rejected as a malformed UUID instead of reaching the location handler.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from src.core.deps import DbSession, Tenant, require_permission
from src.core.messages import success_message
from src.core.pagination import PageParams, SortParams, bind_query_filters, page_params, sort_params
from src.core.permissions import Permission
from src.core.responses import (
    COMMON_ERROR_RESPONSES,
    ApiResponse,
    PaginatedResponse,
)
from src.modules.warehouses.models import StorageLocationType, WarehouseType
from src.modules.warehouses.schemas import (
    StatusChangeRequest,
    StorageLocationCreate,
    StorageLocationFilters,
    StorageLocationRead,
    StorageLocationUpdate,
    WarehouseCreate,
    WarehouseFilters,
    WarehouseOption,
    WarehouseRead,
    WarehouseUpdate,
)
from src.modules.warehouses.service import StorageLocationService, WarehouseService

router = APIRouter(prefix="/warehouses", tags=["Warehouses"], responses=COMMON_ERROR_RESPONSES)

PageDep = Annotated[PageParams, Depends(page_params)]
SortDep = Annotated[SortParams, Depends(sort_params)]

# Retirement carries its reason in the query string rather than a request body:
# a DELETE body is legal but several HTTP clients and proxies drop it, and the
# reason is not optional (section 83).
ReasonQuery = Annotated[
    str, Query(min_length=3, max_length=255, description="Why the record is being retired")
]


def warehouse_filters(
    search: Annotated[str | None, Query(max_length=120, description="Code, name or city")] = None,
    warehouse_type: Annotated[WarehouseType | None, Query()] = None,
    branch_id: Annotated[uuid.UUID | None, Query()] = None,
    plant_id: Annotated[uuid.UUID | None, Query()] = None,
    parent_warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
    allow_negative_stock: Annotated[bool | None, Query()] = None,
    include_deleted: Annotated[bool, Query(description="Include retired warehouses")] = False,
) -> WarehouseFilters:
    return bind_query_filters(
        WarehouseFilters,
        search=search,
        warehouse_type=warehouse_type,
        branch_id=branch_id,
        plant_id=plant_id,
        parent_warehouse_id=parent_warehouse_id,
        is_active=is_active,
        allow_negative_stock=allow_negative_stock,
        include_deleted=include_deleted,
    )


def location_filters(
    search: Annotated[
        str | None, Query(max_length=120, description="Code, name or barcode")
    ] = None,
    warehouse_id: Annotated[uuid.UUID | None, Query()] = None,
    location_type: Annotated[StorageLocationType | None, Query()] = None,
    parent_location_id: Annotated[uuid.UUID | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
    include_deleted: Annotated[bool, Query(description="Include retired locations")] = False,
) -> StorageLocationFilters:
    return bind_query_filters(
        StorageLocationFilters,
        search=search,
        warehouse_id=warehouse_id,
        location_type=location_type,
        parent_location_id=parent_location_id,
        is_active=is_active,
        include_deleted=include_deleted,
    )


def nested_location_filters(
    search: Annotated[
        str | None, Query(max_length=120, description="Code, name or barcode")
    ] = None,
    location_type: Annotated[StorageLocationType | None, Query()] = None,
    parent_location_id: Annotated[uuid.UUID | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
    include_deleted: Annotated[bool, Query(description="Include retired locations")] = False,
) -> StorageLocationFilters:
    """Filters for the warehouse-nested location list.

    Deliberately has no ``warehouse_id``: on ``/{warehouse_id}/locations`` that
    name is a path parameter, and a dependency offering it as a query parameter
    too would both confuse the OpenAPI document and let a caller try to widen
    the scope the URL already fixed.
    """
    return location_filters(
        search=search,
        warehouse_id=None,
        location_type=location_type,
        parent_location_id=parent_location_id,
        is_active=is_active,
        include_deleted=include_deleted,
    )


WarehouseFilterDep = Annotated[WarehouseFilters, Depends(warehouse_filters)]
LocationFilterDep = Annotated[StorageLocationFilters, Depends(location_filters)]
NestedLocationFilterDep = Annotated[StorageLocationFilters, Depends(nested_location_filters)]


# -----------------------------------------------------------------------------
# Storage locations (declared first - see the module docstring)
# -----------------------------------------------------------------------------


@router.get(
    "/locations",
    response_model=PaginatedResponse[StorageLocationRead],
    summary="List storage locations across warehouses",
    dependencies=[require_permission(Permission.LOCATION_VIEW)],
)
async def list_locations(
    session: DbSession,
    tenant: Tenant,
    filters: LocationFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[StorageLocationRead]:
    items, pagination = await StorageLocationService(session, tenant).list_locations(
        filters, params, sort
    )
    return PaginatedResponse[StorageLocationRead](data=items, pagination=pagination)


@router.get(
    "/locations/{location_id}",
    response_model=ApiResponse[StorageLocationRead],
    summary="Fetch one storage location",
    dependencies=[require_permission(Permission.LOCATION_VIEW)],
)
async def get_location(
    location_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[StorageLocationRead]:
    location = await StorageLocationService(session, tenant).get_location(location_id)
    return ApiResponse[StorageLocationRead].ok(location)


@router.patch(
    "/locations/{location_id}",
    response_model=ApiResponse[StorageLocationRead],
    summary="Update a storage location",
    dependencies=[require_permission(Permission.LOCATION_MANAGE)],
)
async def update_location(
    location_id: uuid.UUID,
    payload: StorageLocationUpdate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[StorageLocationRead]:
    location = await StorageLocationService(session, tenant).update_location(location_id, payload)
    return ApiResponse[StorageLocationRead].ok(
        location, success_message("Storage location", "updated")
    )


@router.post(
    "/locations/{location_id}/activate",
    response_model=ApiResponse[StorageLocationRead],
    summary="Activate a storage location",
    dependencies=[require_permission(Permission.LOCATION_MANAGE)],
)
async def activate_location(
    location_id: uuid.UUID,
    payload: StatusChangeRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[StorageLocationRead]:
    location = await StorageLocationService(session, tenant).set_active(
        location_id, active=True, reason=payload.reason
    )
    return ApiResponse[StorageLocationRead].ok(
        location, success_message("Storage location", "activated")
    )


@router.post(
    "/locations/{location_id}/deactivate",
    response_model=ApiResponse[StorageLocationRead],
    summary="Deactivate a storage location",
    dependencies=[require_permission(Permission.LOCATION_MANAGE)],
)
async def deactivate_location(
    location_id: uuid.UUID,
    payload: StatusChangeRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[StorageLocationRead]:
    location = await StorageLocationService(session, tenant).set_active(
        location_id, active=False, reason=payload.reason
    )
    return ApiResponse[StorageLocationRead].ok(
        location, success_message("Storage location", "deactivated")
    )


@router.delete(
    "/locations/{location_id}",
    response_model=ApiResponse[None],
    summary="Retire a storage location",
    dependencies=[require_permission(Permission.LOCATION_MANAGE)],
)
async def delete_location(
    location_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    reason: ReasonQuery,
) -> ApiResponse[None]:
    await StorageLocationService(session, tenant).delete_location(location_id, reason=reason)
    return ApiResponse[None].ok(None, success_message("Storage location", "retired"))


# -----------------------------------------------------------------------------
# Warehouses
# -----------------------------------------------------------------------------


@router.get(
    "",
    response_model=PaginatedResponse[WarehouseRead],
    summary="List warehouses",
    dependencies=[require_permission(Permission.WAREHOUSE_VIEW)],
)
async def list_warehouses(
    session: DbSession,
    tenant: Tenant,
    filters: WarehouseFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[WarehouseRead]:
    items, pagination = await WarehouseService(session, tenant).list_warehouses(
        filters, params, sort
    )
    return PaginatedResponse[WarehouseRead](data=items, pagination=pagination)


@router.get(
    "/options",
    response_model=ApiResponse[list[WarehouseOption]],
    summary="Active warehouses for a picker",
    dependencies=[require_permission(Permission.WAREHOUSE_VIEW)],
)
async def list_warehouse_options(
    session: DbSession,
    tenant: Tenant,
    warehouse_type: Annotated[WarehouseType | None, Query()] = None,
) -> ApiResponse[list[WarehouseOption]]:
    options = await WarehouseService(session, tenant).list_options(warehouse_type)
    return ApiResponse[list[WarehouseOption]].ok(options)


@router.post(
    "",
    response_model=ApiResponse[WarehouseRead],
    status_code=status.HTTP_201_CREATED,
    summary="Create a warehouse",
    dependencies=[require_permission(Permission.WAREHOUSE_MANAGE)],
)
async def create_warehouse(
    payload: WarehouseCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[WarehouseRead]:
    warehouse = await WarehouseService(session, tenant).create_warehouse(payload)
    return ApiResponse[WarehouseRead].ok(warehouse, success_message("Warehouse", "created"))


@router.get(
    "/{warehouse_id}",
    response_model=ApiResponse[WarehouseRead],
    summary="Fetch one warehouse",
    dependencies=[require_permission(Permission.WAREHOUSE_VIEW)],
)
async def get_warehouse(
    warehouse_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[WarehouseRead]:
    warehouse = await WarehouseService(session, tenant).get_warehouse(warehouse_id)
    return ApiResponse[WarehouseRead].ok(warehouse)


@router.patch(
    "/{warehouse_id}",
    response_model=ApiResponse[WarehouseRead],
    summary="Update a warehouse",
    dependencies=[require_permission(Permission.WAREHOUSE_MANAGE)],
)
async def update_warehouse(
    warehouse_id: uuid.UUID,
    payload: WarehouseUpdate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[WarehouseRead]:
    warehouse = await WarehouseService(session, tenant).update_warehouse(warehouse_id, payload)
    return ApiResponse[WarehouseRead].ok(warehouse, success_message("Warehouse", "updated"))


@router.post(
    "/{warehouse_id}/set-default",
    response_model=ApiResponse[WarehouseRead],
    summary="Make this the company's default warehouse",
    dependencies=[require_permission(Permission.WAREHOUSE_MANAGE)],
)
async def set_default_warehouse(
    warehouse_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[WarehouseRead]:
    warehouse = await WarehouseService(session, tenant).set_default(warehouse_id)
    return ApiResponse[WarehouseRead].ok(warehouse, success_message("Default warehouse", "updated"))


@router.post(
    "/{warehouse_id}/activate",
    response_model=ApiResponse[WarehouseRead],
    summary="Activate a warehouse",
    dependencies=[require_permission(Permission.WAREHOUSE_MANAGE)],
)
async def activate_warehouse(
    warehouse_id: uuid.UUID,
    payload: StatusChangeRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[WarehouseRead]:
    warehouse = await WarehouseService(session, tenant).set_active(
        warehouse_id, active=True, reason=payload.reason
    )
    return ApiResponse[WarehouseRead].ok(warehouse, success_message("Warehouse", "activated"))


@router.post(
    "/{warehouse_id}/deactivate",
    response_model=ApiResponse[WarehouseRead],
    summary="Deactivate a warehouse",
    dependencies=[require_permission(Permission.WAREHOUSE_MANAGE)],
)
async def deactivate_warehouse(
    warehouse_id: uuid.UUID,
    payload: StatusChangeRequest,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[WarehouseRead]:
    warehouse = await WarehouseService(session, tenant).set_active(
        warehouse_id, active=False, reason=payload.reason
    )
    return ApiResponse[WarehouseRead].ok(warehouse, success_message("Warehouse", "deactivated"))


@router.post(
    "/{warehouse_id}/restore",
    response_model=ApiResponse[WarehouseRead],
    summary="Restore a retired warehouse (returns it inactive)",
    dependencies=[require_permission(Permission.WAREHOUSE_MANAGE)],
)
async def restore_warehouse(
    warehouse_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[WarehouseRead]:
    warehouse = await WarehouseService(session, tenant).restore_warehouse(warehouse_id)
    return ApiResponse[WarehouseRead].ok(warehouse, "Warehouse restored, inactive")


@router.delete(
    "/{warehouse_id}",
    response_model=ApiResponse[None],
    summary="Retire a warehouse",
    dependencies=[require_permission(Permission.WAREHOUSE_MANAGE)],
)
async def delete_warehouse(
    warehouse_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    reason: ReasonQuery,
) -> ApiResponse[None]:
    await WarehouseService(session, tenant).delete_warehouse(warehouse_id, reason=reason)
    return ApiResponse[None].ok(None, success_message("Warehouse", "retired"))


# --- locations nested under a warehouse --------------------------------------


@router.get(
    "/{warehouse_id}/locations",
    response_model=PaginatedResponse[StorageLocationRead],
    summary="List the storage locations of one warehouse",
    dependencies=[require_permission(Permission.LOCATION_VIEW)],
)
async def list_warehouse_locations(
    warehouse_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    filters: NestedLocationFilterDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[StorageLocationRead]:
    # The path wins over a query parameter of the same name: the URL is the
    # authoritative scope.
    scoped = filters.model_copy(update={"warehouse_id": warehouse_id})
    items, pagination = await StorageLocationService(session, tenant).list_locations(
        scoped, params, sort
    )
    return PaginatedResponse[StorageLocationRead](data=items, pagination=pagination)


@router.post(
    "/{warehouse_id}/locations",
    response_model=ApiResponse[StorageLocationRead],
    status_code=status.HTTP_201_CREATED,
    summary="Create a storage location in a warehouse",
    dependencies=[require_permission(Permission.LOCATION_MANAGE)],
)
async def create_location(
    warehouse_id: uuid.UUID,
    payload: StorageLocationCreate,
    session: DbSession,
    tenant: Tenant,
) -> ApiResponse[StorageLocationRead]:
    location = await StorageLocationService(session, tenant).create_location(warehouse_id, payload)
    return ApiResponse[StorageLocationRead].ok(
        location, success_message("Storage location", "created")
    )


__all__ = ["router"]
