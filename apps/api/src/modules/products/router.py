"""HTTP surface for the item master.

Routes are thin by contract: they resolve dependencies, gate on a permission and
hand the payload to the service. No route builds a query, and no route contains
a business rule.

Two conventions are worth noting:

* Item reads declare ``response_model=None``. The service returns either
  ``ItemDetail`` or ``ItemDetailWithCost`` depending on the caller's
  ``item.cost_view`` permission, and a fixed ``response_model`` would re-widen
  the payload to the union - reintroducing the very field the service withheld.
* The item search is exposed twice: offset paging for the browsable grid, keyset
  paging for exports and infinite scroll, which must stay cheap at page 900
  (section 31).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from src.core.deps import (
    CurrentUser,
    DbSession,
    Tenant,
    require_any_permission,
    require_permission,
)
from src.core.pagination import (
    CursorParams,
    PageParams,
    SortParams,
    cursor_paginated,
    cursor_params,
    page_params,
    paginated,
    sort_params,
)
from src.core.permissions import Permission
from src.core.responses import (
    COMMON_ERROR_RESPONSES,
    ApiResponse,
    CursorPaginatedResponse,
    PaginatedResponse,
)
from src.modules.products import service
from src.modules.products.models import PriceType
from src.modules.products.repository import ItemRepository
from src.modules.products.schemas import (
    CategoryCreate,
    CategoryOut,
    CategoryUpdate,
    ConversionResult,
    ItemCreate,
    ItemFilters,
    ItemStatusChange,
    ItemSummary,
    ItemUpdate,
    PriceCreate,
    PriceOut,
    PriceUpdate,
    ResolvedPrice,
    UomConversionCreate,
    UomConversionOut,
    UomCreate,
    UomOut,
    UomUpdate,
    VariantCreate,
    VariantOut,
    VariantUpdate,
    VendorLinkCreate,
    VendorLinkOut,
    VendorLinkUpdate,
    item_filters,
)

router = APIRouter(prefix="/products", tags=["Products"], responses=COMMON_ERROR_RESPONSES)

PageDep = Annotated[PageParams, Depends(page_params)]
CursorDep = Annotated[CursorParams, Depends(cursor_params)]
SortDep = Annotated[SortParams, Depends(sort_params)]
FiltersDep = Annotated[ItemFilters, Depends(item_filters)]


# -----------------------------------------------------------------------------
# Categories
# -----------------------------------------------------------------------------


@router.get(
    "/categories",
    response_model=PaginatedResponse[CategoryOut],
    summary="List item categories",
    dependencies=[require_permission(Permission.CATEGORY_VIEW)],
)
async def list_categories(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    parent_id: Annotated[
        uuid.UUID | None, Query(description="Direct children of this node")
    ] = None,
    only_roots: Annotated[bool, Query(description="Top-level categories only")] = False,
    search: Annotated[str | None, Query(max_length=64)] = None,
    is_active: Annotated[bool | None, Query()] = None,
) -> PaginatedResponse[CategoryOut]:
    rows, pagination = await service.list_categories(
        session,
        tenant,
        params,
        parent_id=parent_id,
        only_roots=only_roots,
        search=search,
        is_active=is_active,
    )
    return paginated(rows, pagination)


@router.get(
    "/categories/{category_id}",
    response_model=ApiResponse[CategoryOut],
    summary="Get one item category",
    dependencies=[require_permission(Permission.CATEGORY_VIEW)],
)
async def get_category(
    session: DbSession, tenant: Tenant, category_id: uuid.UUID
) -> ApiResponse[CategoryOut]:
    return ApiResponse.ok(await service.get_category(session, tenant, category_id))


@router.post(
    "/categories",
    response_model=ApiResponse[CategoryOut],
    status_code=status.HTTP_201_CREATED,
    summary="Create an item category",
    dependencies=[require_permission(Permission.CATEGORY_MANAGE)],
)
async def create_category(
    session: DbSession, tenant: Tenant, payload: CategoryCreate
) -> ApiResponse[CategoryOut]:
    category = await service.create_category(session, tenant, payload)
    return ApiResponse.ok(category, "Category created")


@router.patch(
    "/categories/{category_id}",
    response_model=ApiResponse[CategoryOut],
    summary="Update or re-parent an item category",
    dependencies=[require_permission(Permission.CATEGORY_MANAGE)],
)
async def update_category(
    session: DbSession, tenant: Tenant, category_id: uuid.UUID, payload: CategoryUpdate
) -> ApiResponse[CategoryOut]:
    category = await service.update_category(session, tenant, category_id, payload)
    return ApiResponse.ok(category, "Category updated")


@router.delete(
    "/categories/{category_id}",
    response_model=ApiResponse[None],
    summary="Retire an item category",
    dependencies=[require_permission(Permission.CATEGORY_MANAGE)],
)
async def delete_category(
    session: DbSession, tenant: Tenant, category_id: uuid.UUID
) -> ApiResponse[None]:
    await service.delete_category(session, tenant, category_id)
    return ApiResponse.ok(None, "Category retired")


# -----------------------------------------------------------------------------
# Units of measure
# -----------------------------------------------------------------------------


@router.get(
    "/uoms",
    response_model=PaginatedResponse[UomOut],
    summary="List units of measure",
    dependencies=[require_permission(Permission.UOM_VIEW)],
)
async def list_uoms(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    search: Annotated[str | None, Query(max_length=64)] = None,
    is_active: Annotated[bool | None, Query()] = None,
) -> PaginatedResponse[UomOut]:
    rows, pagination = await service.list_uoms(
        session, tenant, params, search=search, is_active=is_active
    )
    return paginated(rows, pagination)


@router.post(
    "/uoms",
    response_model=ApiResponse[UomOut],
    status_code=status.HTTP_201_CREATED,
    summary="Create a unit of measure",
    dependencies=[require_permission(Permission.UOM_MANAGE)],
)
async def create_uom(session: DbSession, tenant: Tenant, payload: UomCreate) -> ApiResponse[UomOut]:
    return ApiResponse.ok(await service.create_uom(session, tenant, payload), "Unit created")


@router.patch(
    "/uoms/{uom_id}",
    response_model=ApiResponse[UomOut],
    summary="Update a unit of measure",
    dependencies=[require_permission(Permission.UOM_MANAGE)],
)
async def update_uom(
    session: DbSession, tenant: Tenant, uom_id: uuid.UUID, payload: UomUpdate
) -> ApiResponse[UomOut]:
    return ApiResponse.ok(
        await service.update_uom(session, tenant, uom_id, payload), "Unit updated"
    )


@router.get(
    "/uom-conversions",
    response_model=PaginatedResponse[UomConversionOut],
    summary="List unit conversions",
    dependencies=[require_permission(Permission.UOM_VIEW)],
)
async def list_conversions(
    session: DbSession, tenant: Tenant, params: PageDep
) -> PaginatedResponse[UomConversionOut]:
    rows, pagination = await service.list_conversions(session, tenant, params)
    return paginated(rows, pagination)


@router.post(
    "/uom-conversions",
    response_model=ApiResponse[UomConversionOut],
    status_code=status.HTTP_201_CREATED,
    summary="Define a unit conversion",
    dependencies=[require_permission(Permission.UOM_MANAGE)],
)
async def create_conversion(
    session: DbSession, tenant: Tenant, payload: UomConversionCreate
) -> ApiResponse[UomConversionOut]:
    conversion = await service.create_conversion(session, tenant, payload)
    return ApiResponse.ok(conversion, "Conversion created")


@router.delete(
    "/uom-conversions/{conversion_id}",
    response_model=ApiResponse[None],
    summary="Remove a unit conversion",
    dependencies=[require_permission(Permission.UOM_MANAGE)],
)
async def delete_conversion(
    session: DbSession, tenant: Tenant, conversion_id: uuid.UUID
) -> ApiResponse[None]:
    await service.delete_conversion(session, tenant, conversion_id)
    return ApiResponse.ok(None, "Conversion removed")


@router.get(
    "/uom-conversions/convert",
    response_model=ApiResponse[ConversionResult],
    summary="Convert a quantity between two units",
    dependencies=[require_permission(Permission.UOM_VIEW)],
)
async def convert_quantity(
    session: DbSession,
    tenant: Tenant,
    from_uom_id: Annotated[uuid.UUID, Query()],
    to_uom_id: Annotated[uuid.UUID, Query()],
    quantity: Annotated[Decimal, Query(gt=0, description="Quantity in from_uom")],
) -> ApiResponse[ConversionResult]:
    result = await service.convert_quantity(
        session, tenant, quantity=quantity, from_uom_id=from_uom_id, to_uom_id=to_uom_id
    )
    return ApiResponse.ok(result)


# -----------------------------------------------------------------------------
# Items
# -----------------------------------------------------------------------------


@router.get(
    "/items",
    response_model=None,
    summary="Search items (offset paging)",
    description=(
        "Server-side search over the item master: trigram term, category subtree, "
        "capability flags and active state. `standard_cost` is present in each row "
        "only for a principal holding `item.cost_view`."
    ),
    dependencies=[require_permission(Permission.ITEM_VIEW)],
)
async def search_items(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    filters: FiltersDep,
    params: PageDep,
    sort: SortDep,
) -> PaginatedResponse[ItemSummary]:
    order_by = sort.resolve(ItemRepository.sortable_fields, default=None)
    rows, pagination = await service.search_items(
        session, tenant, principal, filters, params, sort_by=order_by
    )
    return paginated(rows, pagination)


@router.get(
    "/items/stream",
    response_model=None,
    summary="Search items (keyset paging)",
    description=(
        "Same filters as `/items`, paged by opaque cursor. Constant cost at any "
        "depth - use this for exports and infinite scroll."
    ),
    dependencies=[require_permission(Permission.ITEM_VIEW)],
)
async def stream_items(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    filters: FiltersDep,
    params: CursorDep,
) -> CursorPaginatedResponse[ItemSummary]:
    rows, pagination = await service.search_items_keyset(
        session, tenant, principal, filters, params
    )
    return cursor_paginated(rows, pagination)


@router.get(
    "/items/{item_id}",
    response_model=None,
    summary="Get one item",
    dependencies=[require_permission(Permission.ITEM_VIEW)],
)
async def get_item(
    session: DbSession, tenant: Tenant, principal: CurrentUser, item_id: uuid.UUID
) -> ApiResponse:
    return ApiResponse.ok(await service.get_item(session, tenant, principal, item_id))


@router.post(
    "/items",
    response_model=None,
    status_code=status.HTTP_201_CREATED,
    summary="Create an item",
    dependencies=[require_permission(Permission.ITEM_CREATE)],
)
async def create_item(
    session: DbSession, tenant: Tenant, principal: CurrentUser, payload: ItemCreate
) -> ApiResponse:
    item = await service.create_item(session, tenant, payload)
    return ApiResponse.ok(
        service.item_response(item, include_cost=service.may_view_cost(principal)),
        "Item created",
    )


@router.patch(
    "/items/{item_id}",
    response_model=None,
    summary="Update an item",
    dependencies=[require_permission(Permission.ITEM_UPDATE)],
)
async def update_item(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    item_id: uuid.UUID,
    payload: ItemUpdate,
) -> ApiResponse:
    item = await service.update_item(session, tenant, item_id, payload)
    return ApiResponse.ok(
        service.item_response(item, include_cost=service.may_view_cost(principal)),
        "Item updated",
    )


@router.post(
    "/items/{item_id}/status",
    response_model=None,
    summary="Activate or deactivate an item",
    dependencies=[require_permission(Permission.ITEM_UPDATE)],
)
async def change_item_status(
    session: DbSession,
    tenant: Tenant,
    principal: CurrentUser,
    item_id: uuid.UUID,
    payload: ItemStatusChange,
) -> ApiResponse:
    item = await service.change_item_status(session, tenant, item_id, payload)
    return ApiResponse.ok(
        service.item_response(item, include_cost=service.may_view_cost(principal)),
        "Item status changed",
    )


@router.delete(
    "/items/{item_id}",
    response_model=ApiResponse[None],
    summary="Retire an item",
    dependencies=[require_permission(Permission.ITEM_DELETE)],
)
async def delete_item(session: DbSession, tenant: Tenant, item_id: uuid.UUID) -> ApiResponse[None]:
    await service.delete_item(session, tenant, item_id)
    return ApiResponse.ok(None, "Item retired")


# -----------------------------------------------------------------------------
# Variants
# -----------------------------------------------------------------------------


@router.get(
    "/items/{item_id}/variants",
    response_model=PaginatedResponse[VariantOut],
    summary="List an item's variants",
    dependencies=[require_permission(Permission.ITEM_VIEW)],
)
async def list_variants(
    session: DbSession,
    tenant: Tenant,
    item_id: uuid.UUID,
    params: PageDep,
    is_active: Annotated[bool | None, Query()] = None,
) -> PaginatedResponse[VariantOut]:
    rows, pagination = await service.list_variants(
        session, tenant, item_id, params, is_active=is_active
    )
    return paginated(rows, pagination)


@router.post(
    "/items/{item_id}/variants",
    response_model=ApiResponse[VariantOut],
    status_code=status.HTTP_201_CREATED,
    summary="Add a variant",
    dependencies=[require_permission(Permission.ITEM_CREATE)],
)
async def create_variant(
    session: DbSession, tenant: Tenant, item_id: uuid.UUID, payload: VariantCreate
) -> ApiResponse[VariantOut]:
    variant = await service.create_variant(session, tenant, item_id, payload)
    return ApiResponse.ok(variant, "Variant created")


@router.patch(
    "/variants/{variant_id}",
    response_model=ApiResponse[VariantOut],
    summary="Update a variant",
    dependencies=[require_permission(Permission.ITEM_UPDATE)],
)
async def update_variant(
    session: DbSession, tenant: Tenant, variant_id: uuid.UUID, payload: VariantUpdate
) -> ApiResponse[VariantOut]:
    variant = await service.update_variant(session, tenant, variant_id, payload)
    return ApiResponse.ok(variant, "Variant updated")


@router.delete(
    "/variants/{variant_id}",
    response_model=ApiResponse[None],
    summary="Retire a variant",
    dependencies=[require_permission(Permission.ITEM_DELETE)],
)
async def delete_variant(
    session: DbSession, tenant: Tenant, variant_id: uuid.UUID
) -> ApiResponse[None]:
    await service.delete_variant(session, tenant, variant_id)
    return ApiResponse.ok(None, "Variant retired")


# -----------------------------------------------------------------------------
# Prices
# -----------------------------------------------------------------------------


@router.get(
    "/items/{item_id}/prices",
    response_model=PaginatedResponse[PriceOut],
    summary="List an item's price list",
    dependencies=[require_permission(Permission.PRICE_VIEW)],
)
async def list_prices(
    session: DbSession,
    tenant: Tenant,
    item_id: uuid.UUID,
    params: PageDep,
    price_type: Annotated[PriceType | None, Query()] = None,
    variant_id: Annotated[uuid.UUID | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
) -> PaginatedResponse[PriceOut]:
    rows, pagination = await service.list_prices(
        session,
        tenant,
        item_id,
        params,
        price_type=price_type,
        variant_id=variant_id,
        is_active=is_active,
    )
    return paginated(rows, pagination)


@router.post(
    "/items/{item_id}/prices",
    response_model=ApiResponse[PriceOut],
    status_code=status.HTTP_201_CREATED,
    summary="Add a price-list entry",
    dependencies=[require_permission(Permission.PRICE_MANAGE)],
)
async def create_price(
    session: DbSession, tenant: Tenant, item_id: uuid.UUID, payload: PriceCreate
) -> ApiResponse[PriceOut]:
    price = await service.create_price(session, tenant, item_id, payload)
    return ApiResponse.ok(price, "Price created")


@router.patch(
    "/prices/{price_id}",
    response_model=ApiResponse[PriceOut],
    summary="Update a price-list entry",
    dependencies=[require_permission(Permission.PRICE_MANAGE)],
)
async def update_price(
    session: DbSession, tenant: Tenant, price_id: uuid.UUID, payload: PriceUpdate
) -> ApiResponse[PriceOut]:
    price = await service.update_price(session, tenant, price_id, payload)
    return ApiResponse.ok(price, "Price updated")


@router.delete(
    "/prices/{price_id}",
    response_model=ApiResponse[None],
    summary="Remove a price-list entry",
    dependencies=[require_permission(Permission.PRICE_MANAGE)],
)
async def delete_price(
    session: DbSession, tenant: Tenant, price_id: uuid.UUID
) -> ApiResponse[None]:
    await service.delete_price(session, tenant, price_id)
    return ApiResponse.ok(None, "Price removed")


@router.get(
    "/items/{item_id}/resolved-price",
    response_model=ApiResponse[ResolvedPrice],
    summary="Resolve the effective price for a channel and quantity",
    description=(
        "Applies the dated price list and its quantity slabs, falling back to the "
        "item master where no list entry matches. Order entry must use this rather "
        "than reading `sale_price` directly."
    ),
    dependencies=[require_permission(Permission.PRICE_VIEW)],
)
async def resolved_price(
    session: DbSession,
    tenant: Tenant,
    item_id: uuid.UUID,
    price_type: Annotated[PriceType, Query()],
    quantity: Annotated[Decimal, Query(gt=0)],
    variant_id: Annotated[uuid.UUID | None, Query()] = None,
    on_date: Annotated[date | None, Query(description="Defaults to today (UTC)")] = None,
) -> ApiResponse[ResolvedPrice]:
    result = await service.resolve_price(
        session,
        tenant,
        item_id=item_id,
        price_type=price_type,
        quantity=quantity,
        variant_id=variant_id,
        on_date=on_date,
    )
    return ApiResponse.ok(result)


# -----------------------------------------------------------------------------
# Vendor links
# -----------------------------------------------------------------------------


@router.get(
    "/items/{item_id}/vendors",
    response_model=PaginatedResponse[VendorLinkOut],
    summary="List the vendors that supply an item",
    # Procurement staff hold vendor.view; item maintainers hold item.view. Both
    # legitimately read this list, so either permission opens it.
    dependencies=[require_any_permission(Permission.ITEM_VIEW, Permission.VENDOR_VIEW)],
)
async def list_vendor_links(
    session: DbSession,
    tenant: Tenant,
    item_id: uuid.UUID,
    params: PageDep,
    is_active: Annotated[bool | None, Query()] = None,
) -> PaginatedResponse[VendorLinkOut]:
    rows, pagination = await service.list_vendor_links(
        session, tenant, item_id, params, is_active=is_active
    )
    return paginated(rows, pagination)


@router.post(
    "/items/{item_id}/vendors",
    response_model=ApiResponse[VendorLinkOut],
    status_code=status.HTTP_201_CREATED,
    summary="Link a vendor to an item",
    dependencies=[require_permission(Permission.ITEM_UPDATE)],
)
async def link_vendor(
    session: DbSession, tenant: Tenant, item_id: uuid.UUID, payload: VendorLinkCreate
) -> ApiResponse[VendorLinkOut]:
    link = await service.link_vendor(session, tenant, item_id, payload)
    return ApiResponse.ok(link, "Vendor linked")


@router.patch(
    "/item-vendors/{link_id}",
    response_model=ApiResponse[VendorLinkOut],
    summary="Update an item-vendor link",
    dependencies=[require_permission(Permission.ITEM_UPDATE)],
)
async def update_vendor_link(
    session: DbSession, tenant: Tenant, link_id: uuid.UUID, payload: VendorLinkUpdate
) -> ApiResponse[VendorLinkOut]:
    link = await service.update_vendor_link(session, tenant, link_id, payload)
    return ApiResponse.ok(link, "Vendor link updated")


@router.delete(
    "/item-vendors/{link_id}",
    response_model=ApiResponse[None],
    summary="Remove an item-vendor link",
    dependencies=[require_permission(Permission.ITEM_UPDATE)],
)
async def unlink_vendor(
    session: DbSession, tenant: Tenant, link_id: uuid.UUID
) -> ApiResponse[None]:
    await service.unlink_vendor(session, tenant, link_id)
    return ApiResponse.ok(None, "Vendor link removed")


__all__ = ["router"]
