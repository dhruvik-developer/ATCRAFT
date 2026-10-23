"""The dealer portal: every route a dealer account may call, and only dealer accounts.

Thin by construction: bind, call one service function, wrap the envelope.
Every route names its portal right literally and the router demands a dealer
principal (``require_dealer_user``): an internal administrator holds every
permission string, and without that gate could act "as" a dealer by naming no
customer. The dealer is always the token's - no request carries a customer id -
and another dealer's order, consignment or statement is "not found", never
"forbidden".
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
    require_dealer_user,
    require_permission,
)
from src.core.messages import success_message
from src.core.pagination import PageParams, bind_query_filters, page_params
from src.core.permissions import Permission
from src.core.responses import COMMON_ERROR_RESPONSES, ApiResponse, PaginatedResponse
from src.modules.dealer_portal.schemas import (
    DealerBrandRead,
    DealerCancelRequest,
    DealerCartLineIn,
    DealerCartLineUpdate,
    DealerCartRead,
    DealerCatalogItem,
    DealerCatalogItemDetail,
    DealerCategoryNode,
    DealerCheckoutRequest,
    DealerDispatchSummary,
    DealerOrderDetail,
    DealerOrderFilters,
    DealerOrderSummary,
    DealerOutstanding,
    DealerPortalMe,
    DealerPriceQuote,
    DealerTracking,
    DealerWishlistIn,
    DealerWishlistRead,
)
from src.modules.dealer_portal.service import DealerPortalService
from src.modules.finance.schemas import LedgerStatement, StatementFilters
from src.modules.sales.models import SalesOrderStatus

router = APIRouter(
    prefix="/dealer-portal",
    tags=["Dealer Portal"],
    responses=COMMON_ERROR_RESPONSES,
    dependencies=[Depends(require_dealer_user)],
)
PageDep = Annotated[PageParams, Depends(page_params)]


def statement_filters(
    date_from: Annotated[date | None, Query(description="Statement period start")] = None,
    date_to: Annotated[date | None, Query(description="Statement period end")] = None,
) -> StatementFilters:
    return bind_query_filters(StatementFilters, date_from=date_from, date_to=date_to)


StatementDep = Annotated[StatementFilters, Depends(statement_filters)]


def order_filters(
    search: Annotated[str | None, Query(max_length=120, description="Order or customer PO")] = None,
    order_status: Annotated[SalesOrderStatus | None, Query(alias="status")] = None,
    open_only: Annotated[
        bool, Query(description="Only orders that still represent unmet demand")
    ] = False,
) -> DealerOrderFilters:
    return bind_query_filters(
        DealerOrderFilters, search=search, status=order_status, open_only=open_only
    )


OrderFilterDep = Annotated[DealerOrderFilters, Depends(order_filters)]


@router.get(
    "/me",
    response_model=ApiResponse[DealerPortalMe],
    summary="The signed-in dealer: account, channel, credit, addresses, contacts",
    dependencies=[require_permission(Permission.DEALER_PORTAL_BROWSE)],
)
async def me(session: DbSession, tenant: Tenant) -> ApiResponse[DealerPortalMe]:
    return ApiResponse[DealerPortalMe].ok(await DealerPortalService(session, tenant).me())


@router.get(
    "/catalog/categories",
    response_model=ApiResponse[list[DealerCategoryNode]],
    summary="Categories holding sellable active items",
    dependencies=[require_permission(Permission.DEALER_PORTAL_BROWSE)],
)
async def catalog_categories(
    session: DbSession, tenant: Tenant
) -> ApiResponse[list[DealerCategoryNode]]:
    return ApiResponse[list[DealerCategoryNode]].ok(
        await DealerPortalService(session, tenant).categories()
    )


@router.get(
    "/catalog/brands",
    response_model=ApiResponse[list[DealerBrandRead]],
    summary="Active brands holding sellable items",
    dependencies=[require_permission(Permission.DEALER_PORTAL_BROWSE)],
)
async def catalog_brands(session: DbSession, tenant: Tenant) -> ApiResponse[list[DealerBrandRead]]:
    return ApiResponse[list[DealerBrandRead]].ok(
        await DealerPortalService(session, tenant).brands()
    )


@router.get(
    "/catalog/items",
    response_model=PaginatedResponse[DealerCatalogItem],
    summary="Sellable items with the dealer channel price, offers and availability",
    description=(
        "category_id covers the whole subtree. Sorting by price is refused with "
        "422: a channel price is a slab ranking plus a fallback and cannot be "
        "ordered in the database."
    ),
    dependencies=[require_permission(Permission.DEALER_PORTAL_BROWSE)],
)
async def catalog_items(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    search: Annotated[str | None, Query(max_length=120)] = None,
    category_id: Annotated[uuid.UUID | None, Query()] = None,
    brand_id: Annotated[uuid.UUID | None, Query()] = None,
    in_stock_only: Annotated[bool, Query()] = False,
    on_offer_only: Annotated[bool, Query()] = False,
    sort_by: Annotated[str, Query(description="code or name")] = "code",
) -> PaginatedResponse[DealerCatalogItem]:
    rows, page = await DealerPortalService(session, tenant).catalog_items(
        params,
        search=search,
        category_id=category_id,
        brand_id=brand_id,
        in_stock_only=in_stock_only,
        on_offer_only=on_offer_only,
        sort_by=sort_by,
    )
    return PaginatedResponse(data=rows, pagination=page)


@router.get(
    "/catalog/items/{item_id}",
    response_model=ApiResponse[DealerCatalogItemDetail],
    summary="One sellable item: variants, slabs, offers, images",
    dependencies=[require_permission(Permission.DEALER_PORTAL_BROWSE)],
)
async def catalog_item(
    item_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerCatalogItemDetail]:
    return ApiResponse[DealerCatalogItemDetail].ok(
        await DealerPortalService(session, tenant).catalog_item(item_id)
    )


@router.get(
    "/catalog/items/{item_id}/price",
    response_model=ApiResponse[DealerPriceQuote],
    summary="Channel price plus the best offer for a quantity",
    dependencies=[require_permission(Permission.DEALER_PORTAL_BROWSE)],
)
async def catalog_item_price(
    item_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    quantity: Annotated[Decimal, Query(gt=0)] = Decimal("1"),
    variant_id: Annotated[uuid.UUID | None, Query()] = None,
) -> ApiResponse[DealerPriceQuote]:
    return ApiResponse[DealerPriceQuote].ok(
        await DealerPortalService(session, tenant).price_quote(item_id, quantity, variant_id)
    )


@router.get(
    "/cart",
    response_model=ApiResponse[DealerCartRead],
    summary="The priced cart preview: live prices, schemes, tax, credit, warnings",
    dependencies=[require_permission(Permission.DEALER_PORTAL_ORDER)],
)
async def get_cart(session: DbSession, tenant: Tenant) -> ApiResponse[DealerCartRead]:
    return ApiResponse[DealerCartRead].ok(await DealerPortalService(session, tenant).cart())


@router.post(
    "/cart/lines",
    response_model=ApiResponse[DealerCartRead],
    status_code=status.HTTP_201_CREATED,
    summary="Add a sellable item to the cart",
    dependencies=[require_permission(Permission.DEALER_PORTAL_ORDER)],
)
async def add_cart_line(
    payload: DealerCartLineIn, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerCartRead]:
    cart = await DealerPortalService(session, tenant).add_cart_line(
        payload.item_id, payload.item_variant_id, payload.quantity
    )
    return ApiResponse[DealerCartRead].ok(cart, "Added to the cart")


@router.patch(
    "/cart/lines/{line_id}",
    response_model=ApiResponse[DealerCartRead],
    summary="Change a cart line's quantity",
    dependencies=[require_permission(Permission.DEALER_PORTAL_ORDER)],
)
async def update_cart_line(
    line_id: uuid.UUID, payload: DealerCartLineUpdate, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerCartRead]:
    cart = await DealerPortalService(session, tenant).update_cart_line(line_id, payload.quantity)
    return ApiResponse[DealerCartRead].ok(cart, success_message("Cart", "updated"))


@router.delete(
    "/cart/lines/{line_id}",
    response_model=ApiResponse[DealerCartRead],
    summary="Remove one cart line",
    dependencies=[require_permission(Permission.DEALER_PORTAL_ORDER)],
)
async def remove_cart_line(
    line_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerCartRead]:
    cart = await DealerPortalService(session, tenant).remove_cart_line(line_id)
    return ApiResponse[DealerCartRead].ok(cart, "Removed from the cart")


@router.delete(
    "/cart",
    response_model=ApiResponse[DealerCartRead],
    summary="Empty the cart",
    dependencies=[require_permission(Permission.DEALER_PORTAL_ORDER)],
)
async def clear_cart(session: DbSession, tenant: Tenant) -> ApiResponse[DealerCartRead]:
    cart = await DealerPortalService(session, tenant).clear_cart()
    return ApiResponse[DealerCartRead].ok(cart, success_message("Cart", "cleared"))


@router.get(
    "/wishlist",
    response_model=ApiResponse[list[DealerWishlistRead]],
    summary="The dealer's saved items",
    dependencies=[require_permission(Permission.DEALER_PORTAL_BROWSE)],
)
async def list_wishlist(
    session: DbSession, tenant: Tenant
) -> ApiResponse[list[DealerWishlistRead]]:
    return ApiResponse[list[DealerWishlistRead]].ok(
        await DealerPortalService(session, tenant).wishlist()
    )


@router.post(
    "/wishlist",
    response_model=ApiResponse[DealerWishlistRead],
    status_code=status.HTTP_201_CREATED,
    summary="Save an item to the wishlist",
    dependencies=[require_permission(Permission.DEALER_PORTAL_BROWSE)],
)
async def add_wishlist_item(
    payload: DealerWishlistIn, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerWishlistRead]:
    saved = await DealerPortalService(session, tenant).add_wishlist_item(
        payload.item_id, payload.item_variant_id
    )
    return ApiResponse[DealerWishlistRead].ok(saved, "Saved to the wishlist")


@router.delete(
    "/wishlist/{entry_id}",
    response_model=ApiResponse[None],
    summary="Remove a saved item",
    dependencies=[require_permission(Permission.DEALER_PORTAL_BROWSE)],
)
async def remove_wishlist_item(
    entry_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[None]:
    await DealerPortalService(session, tenant).remove_wishlist_item(entry_id)
    return ApiResponse[None].ok(None, "Removed from the wishlist")


@router.post(
    "/wishlist/{entry_id}/move-to-cart",
    response_model=ApiResponse[DealerCartRead],
    summary="Move a saved item into the cart",
    dependencies=[require_permission(Permission.DEALER_PORTAL_ORDER)],
)
async def move_wishlist_to_cart(
    entry_id: uuid.UUID,
    session: DbSession,
    tenant: Tenant,
    quantity: Annotated[Decimal, Query(gt=0)] = Decimal("1"),
) -> ApiResponse[DealerCartRead]:
    cart = await DealerPortalService(session, tenant).move_wishlist_to_cart(entry_id, quantity)
    return ApiResponse[DealerCartRead].ok(cart, "Moved to the cart")


@router.post(
    "/checkout",
    response_model=ApiResponse[DealerOrderDetail],
    status_code=status.HTTP_201_CREATED,
    summary="Turn the cart into a DRAFT sales order for sales review",
    description=(
        "Locks the cart and checks its version, re-validates every line, "
        "fulfils from the default warehouse, and prices every line on the "
        "server with the best scheme. The order stays DRAFT for sales review."
    ),
    dependencies=[require_permission(Permission.DEALER_PORTAL_ORDER)],
)
async def checkout(
    payload: DealerCheckoutRequest, session: DbSession, tenant: Tenant, principal: CurrentUser
) -> ApiResponse[DealerOrderDetail]:
    order = await DealerPortalService(session, tenant).checkout(principal, payload)
    return ApiResponse[DealerOrderDetail].ok(order, f"Order {order.code} placed for review")


@router.get(
    "/orders",
    response_model=PaginatedResponse[DealerOrderSummary],
    summary="The dealer's own sales orders",
    dependencies=[require_permission(Permission.DEALER_PORTAL_ORDER)],
)
async def list_orders(
    session: DbSession, tenant: Tenant, params: PageDep, filters: OrderFilterDep
) -> PaginatedResponse[DealerOrderSummary]:
    rows, page = await DealerPortalService(session, tenant).orders(filters, params)
    return PaginatedResponse(data=rows, pagination=page)


@router.get(
    "/orders/{order_id}",
    response_model=ApiResponse[DealerOrderDetail],
    summary="One of the dealer's orders, with fulfilment progress",
    dependencies=[require_permission(Permission.DEALER_PORTAL_ORDER)],
)
async def get_order(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerOrderDetail]:
    return ApiResponse[DealerOrderDetail].ok(
        await DealerPortalService(session, tenant).order(order_id)
    )


@router.post(
    "/orders/{order_id}/cancel",
    response_model=ApiResponse[DealerOrderDetail],
    summary="Cancel the dealer's own DRAFT order, with a reason",
    dependencies=[require_permission(Permission.DEALER_PORTAL_ORDER)],
)
async def cancel_order(
    order_id: uuid.UUID, payload: DealerCancelRequest, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerOrderDetail]:
    order = await DealerPortalService(session, tenant).cancel_order(order_id, payload.reason)
    return ApiResponse[DealerOrderDetail].ok(
        order, success_message(f"Order {order.code}", "cancelled")
    )


@router.get(
    "/orders/{order_id}/dispatches",
    response_model=ApiResponse[list[DealerDispatchSummary]],
    summary="The consignments raised against one of the dealer's orders",
    dependencies=[require_permission(Permission.DEALER_PORTAL_ORDER)],
)
async def list_order_dispatches(
    order_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[list[DealerDispatchSummary]]:
    return ApiResponse[list[DealerDispatchSummary]].ok(
        await DealerPortalService(session, tenant).order_dispatches(order_id)
    )


@router.get(
    "/dispatches/{dispatch_id}/tracking",
    response_model=ApiResponse[DealerTracking],
    summary="Dealer-safe tracking: courier, tracking id, dates, events",
    description=("The driver's mobile number and the freight cost are never included."),
    dependencies=[require_permission(Permission.DEALER_PORTAL_ORDER)],
)
async def dispatch_tracking(
    dispatch_id: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerTracking]:
    return ApiResponse[DealerTracking].ok(
        await DealerPortalService(session, tenant).dispatch_tracking(dispatch_id)
    )


@router.get(
    "/statement",
    response_model=ApiResponse[LedgerStatement],
    summary="The dealer's own ledger statement",
    dependencies=[require_permission(Permission.DEALER_PORTAL_ACCOUNT)],
)
async def statement(
    session: DbSession, tenant: Tenant, params: PageDep, filters: StatementDep
) -> ApiResponse[LedgerStatement]:
    return ApiResponse[LedgerStatement].ok(
        await DealerPortalService(session, tenant).statement(filters, params)
    )


@router.get(
    "/outstanding",
    response_model=ApiResponse[DealerOutstanding],
    summary="The dealer's overdue position from the receivables ageing",
    dependencies=[require_permission(Permission.DEALER_PORTAL_ACCOUNT)],
)
async def outstanding(session: DbSession, tenant: Tenant) -> ApiResponse[DealerOutstanding]:
    return ApiResponse[DealerOutstanding].ok(
        await DealerPortalService(session, tenant).outstanding()
    )
