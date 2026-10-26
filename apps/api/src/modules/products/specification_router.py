"""Thin permission boundaries for descriptive product data."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from src.core.deps import DbSession, Tenant, require_any_permission, require_permission
from src.core.messages import success_message
from src.core.pagination import PageParams, SortParams, page_params, paginated, sort_params
from src.core.permissions import Permission
from src.core.responses import ApiResponse, PaginatedResponse
from src.modules.products.specification_schemas import (
    CategorySpecificationsRead,
    CategorySpecificationsReplace,
    ItemAccessoryCreate,
    ItemAccessoryRead,
    ItemAccessoryUpdate,
    ItemSpecificationsRead,
    ItemSpecificationsReplace,
    SpecificationAttributeCreate,
    SpecificationAttributeRead,
    SpecificationAttributeUpdate,
    SpecificationCategoryOption,
)
from src.modules.products.specification_service import SpecificationService

router = APIRouter()
PageDep = Annotated[PageParams, Depends(page_params)]
SortDep = Annotated[SortParams, Depends(sort_params)]
ReasonQuery = Annotated[str, Query(min_length=3, max_length=255, pattern=r"\S.{1,253}\S|\S{3}")]


@router.get(
    "/specification-attributes",
    response_model=PaginatedResponse[SpecificationAttributeRead],
    dependencies=[require_permission(Permission.ITEM_ATTRIBUTE_VIEW)],
)
async def list_attributes(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    sort: SortDep,
    search: Annotated[str | None, Query(max_length=64)] = None,
    is_active: bool | None = None,
    filterable_only: bool = False,
) -> PaginatedResponse[SpecificationAttributeRead]:
    rows, pagination = await SpecificationService(session, tenant).list_attributes(
        params, sort, search=search, is_active=is_active, filterable_only=filterable_only
    )
    return paginated(rows, pagination)


@router.post(
    "/specification-attributes",
    response_model=ApiResponse[SpecificationAttributeRead],
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_permission(Permission.ITEM_ATTRIBUTE_MANAGE)],
)
async def create_attribute(
    session: DbSession, tenant: Tenant, payload: SpecificationAttributeCreate
) -> ApiResponse[SpecificationAttributeRead]:
    return ApiResponse.ok(
        await SpecificationService(session, tenant).create_attribute(payload),
        success_message("Specification attribute", "created"),
    )


@router.get(
    "/specification-attributes/{attribute_id}",
    response_model=ApiResponse[SpecificationAttributeRead],
    dependencies=[require_permission(Permission.ITEM_ATTRIBUTE_VIEW)],
)
async def get_attribute(
    session: DbSession, tenant: Tenant, attribute_id: uuid.UUID
) -> ApiResponse[SpecificationAttributeRead]:
    return ApiResponse.ok(await SpecificationService(session, tenant).get_attribute(attribute_id))


@router.patch(
    "/specification-attributes/{attribute_id}",
    response_model=ApiResponse[SpecificationAttributeRead],
    dependencies=[require_permission(Permission.ITEM_ATTRIBUTE_MANAGE)],
)
async def update_attribute(
    session: DbSession,
    tenant: Tenant,
    attribute_id: uuid.UUID,
    payload: SpecificationAttributeUpdate,
) -> ApiResponse[SpecificationAttributeRead]:
    return ApiResponse.ok(
        await SpecificationService(session, tenant).update_attribute(attribute_id, payload),
        success_message("Specification attribute", "updated"),
    )


@router.delete(
    "/specification-attributes/{attribute_id}",
    response_model=ApiResponse[None],
    dependencies=[require_permission(Permission.ITEM_ATTRIBUTE_MANAGE)],
)
async def retire_attribute(
    session: DbSession, tenant: Tenant, attribute_id: uuid.UUID, reason: ReasonQuery
) -> ApiResponse[None]:
    await SpecificationService(session, tenant).retire_attribute(attribute_id, reason=reason)
    return ApiResponse.ok(None, success_message("Specification attribute", "retired"))


@router.get(
    "/specification-categories",
    response_model=PaginatedResponse[SpecificationCategoryOption],
    dependencies=[
        require_any_permission(Permission.ITEM_ATTRIBUTE_VIEW, Permission.ITEM_ATTRIBUTE_MANAGE)
    ],
)
async def specification_categories(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    search: Annotated[str | None, Query(max_length=64)] = None,
) -> PaginatedResponse[SpecificationCategoryOption]:
    rows, pagination = await SpecificationService(session, tenant).list_categories(params, search)
    return paginated(rows, pagination)


@router.get(
    "/categories/{category_id}/specifications",
    response_model=ApiResponse[CategorySpecificationsRead],
    dependencies=[
        require_any_permission(Permission.ITEM_ATTRIBUTE_VIEW, Permission.ITEM_ATTRIBUTE_MANAGE)
    ],
)
async def category_specifications(
    session: DbSession, tenant: Tenant, category_id: uuid.UUID
) -> ApiResponse[CategorySpecificationsRead]:
    return ApiResponse.ok(
        await SpecificationService(session, tenant).get_category_specifications(category_id)
    )


@router.put(
    "/categories/{category_id}/specifications",
    response_model=ApiResponse[CategorySpecificationsRead],
    dependencies=[require_permission(Permission.ITEM_ATTRIBUTE_MANAGE)],
)
async def replace_category_specifications(
    session: DbSession,
    tenant: Tenant,
    category_id: uuid.UUID,
    payload: CategorySpecificationsReplace,
) -> ApiResponse[CategorySpecificationsRead]:
    return ApiResponse.ok(
        await SpecificationService(session, tenant).replace_category_specifications(
            category_id, payload
        ),
        success_message("Category specifications", "updated"),
    )


@router.get(
    "/items/{item_id}/specification-attributes",
    response_model=PaginatedResponse[SpecificationAttributeRead],
    dependencies=[require_any_permission(Permission.ITEM_VIEW, Permission.ITEM_UPDATE)],
)
async def item_attribute_options(
    session: DbSession,
    tenant: Tenant,
    item_id: uuid.UUID,
    params: PageDep,
    search: Annotated[str | None, Query(max_length=64)] = None,
) -> PaginatedResponse[SpecificationAttributeRead]:
    rows, pagination = await SpecificationService(session, tenant).item_attribute_options(
        item_id, params, search=search
    )
    return paginated(rows, pagination)


@router.get(
    "/items/{item_id}/specifications",
    response_model=ApiResponse[ItemSpecificationsRead],
    dependencies=[require_permission(Permission.ITEM_VIEW)],
)
async def item_specifications(
    session: DbSession, tenant: Tenant, item_id: uuid.UUID
) -> ApiResponse[ItemSpecificationsRead]:
    return ApiResponse.ok(
        await SpecificationService(session, tenant).get_item_specifications(item_id)
    )


@router.put(
    "/items/{item_id}/specifications",
    response_model=ApiResponse[ItemSpecificationsRead],
    dependencies=[require_permission(Permission.ITEM_UPDATE)],
)
async def replace_item_specifications(
    session: DbSession, tenant: Tenant, item_id: uuid.UUID, payload: ItemSpecificationsReplace
) -> ApiResponse[ItemSpecificationsRead]:
    return ApiResponse.ok(
        await SpecificationService(session, tenant).replace_item_specifications(item_id, payload),
        success_message("Item specifications", "updated"),
    )


@router.get(
    "/items/{item_id}/accessories",
    response_model=PaginatedResponse[ItemAccessoryRead],
    dependencies=[require_permission(Permission.ITEM_VIEW)],
)
async def item_accessories(
    session: DbSession, tenant: Tenant, item_id: uuid.UUID, params: PageDep
) -> PaginatedResponse[ItemAccessoryRead]:
    rows, pagination = await SpecificationService(session, tenant).list_accessories(item_id, params)
    return paginated(rows, pagination)


@router.post(
    "/items/{item_id}/accessories",
    response_model=ApiResponse[ItemAccessoryRead],
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_permission(Permission.ITEM_UPDATE)],
)
async def create_accessory(
    session: DbSession, tenant: Tenant, item_id: uuid.UUID, payload: ItemAccessoryCreate
) -> ApiResponse[ItemAccessoryRead]:
    return ApiResponse.ok(
        await SpecificationService(session, tenant).create_accessory(item_id, payload),
        success_message("Item accessory", "created"),
    )


@router.patch(
    "/item-accessories/{accessory_id}",
    response_model=ApiResponse[ItemAccessoryRead],
    dependencies=[require_permission(Permission.ITEM_UPDATE)],
)
async def update_accessory(
    session: DbSession, tenant: Tenant, accessory_id: uuid.UUID, payload: ItemAccessoryUpdate
) -> ApiResponse[ItemAccessoryRead]:
    return ApiResponse.ok(
        await SpecificationService(session, tenant).update_accessory(accessory_id, payload),
        success_message("Item accessory", "updated"),
    )


@router.delete(
    "/item-accessories/{accessory_id}",
    response_model=ApiResponse[None],
    dependencies=[require_permission(Permission.ITEM_UPDATE)],
)
async def delete_accessory(
    session: DbSession, tenant: Tenant, accessory_id: uuid.UUID, reason: ReasonQuery
) -> ApiResponse[None]:
    await SpecificationService(session, tenant).delete_accessory(accessory_id, reason=reason)
    return ApiResponse.ok(None, success_message("Item accessory", "removed"))
