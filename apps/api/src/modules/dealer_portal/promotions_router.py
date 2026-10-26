"""Staff promotion management. Portal accounts cannot enter this surface."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from src.core.deps import (
    DbSession,
    Tenant,
    forbid_dealer_users,
    forbid_vendor_users,
    require_permission,
)
from src.core.pagination import PageParams, page_params
from src.core.permissions import Permission
from src.core.responses import COMMON_ERROR_RESPONSES, ApiResponse, PaginatedResponse
from src.modules.dealer_portal.models import DealerBannerStatus
from src.modules.dealer_portal.promotion_schemas import (
    DealerBannerCreate,
    DealerBannerRead,
    DealerBannerUpdate,
    DealerOfferCreate,
    DealerOfferPreview,
    DealerOfferRead,
    DealerOfferUpdate,
    PromotionRetire,
)
from src.modules.dealer_portal.promotion_service import PromotionService

router = APIRouter(
    prefix="/promotions",
    tags=["Promotions"],
    responses=COMMON_ERROR_RESPONSES,
    dependencies=[Depends(forbid_vendor_users), Depends(forbid_dealer_users)],
)
PageDep = Annotated[PageParams, Depends(page_params)]


@router.post(
    "/banners/{identity}/activate",
    response_model=ApiResponse[DealerBannerRead],
    dependencies=[require_permission(Permission.PROMOTION_MANAGE)],
)
async def activate_banner(
    identity: uuid.UUID, payload: PromotionRetire, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerBannerRead]:
    row = await PromotionService(session, tenant).set_status(
        "banners", identity, DealerBannerStatus.ACTIVE, payload.reason
    )
    return ApiResponse[DealerBannerRead].ok(
        DealerBannerRead.model_validate(row.model_dump()), "Promotion activated successfully"
    )


@router.post(
    "/banners/{identity}/deactivate",
    response_model=ApiResponse[DealerBannerRead],
    dependencies=[require_permission(Permission.PROMOTION_MANAGE)],
)
async def deactivate_banner(
    identity: uuid.UUID, payload: PromotionRetire, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerBannerRead]:
    row = await PromotionService(session, tenant).set_status(
        "banners", identity, DealerBannerStatus.INACTIVE, payload.reason
    )
    return ApiResponse[DealerBannerRead].ok(
        DealerBannerRead.model_validate(row.model_dump()), "Promotion deactivated successfully"
    )


@router.post(
    "/banners/{identity}/retire",
    response_model=ApiResponse[DealerBannerRead],
    dependencies=[require_permission(Permission.PROMOTION_MANAGE)],
)
async def retire_banner_with_reason(
    identity: uuid.UUID, payload: PromotionRetire, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerBannerRead]:
    row = await PromotionService(session, tenant).retire("banners", identity, payload.reason)
    return ApiResponse[DealerBannerRead].ok(
        DealerBannerRead.model_validate(row.model_dump()), "Promotion retired successfully"
    )


@router.post(
    "/offers/{identity}/activate",
    response_model=ApiResponse[DealerOfferRead],
    dependencies=[require_permission(Permission.PROMOTION_MANAGE)],
)
async def activate_offer(
    identity: uuid.UUID, payload: PromotionRetire, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerOfferRead]:
    row = await PromotionService(session, tenant).set_status(
        "offers", identity, DealerBannerStatus.ACTIVE, payload.reason
    )
    return ApiResponse[DealerOfferRead].ok(
        DealerOfferRead.model_validate(row.model_dump()), "Promotion activated successfully"
    )


@router.post(
    "/offers/{identity}/deactivate",
    response_model=ApiResponse[DealerOfferRead],
    dependencies=[require_permission(Permission.PROMOTION_MANAGE)],
)
async def deactivate_offer(
    identity: uuid.UUID, payload: PromotionRetire, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerOfferRead]:
    row = await PromotionService(session, tenant).set_status(
        "offers", identity, DealerBannerStatus.INACTIVE, payload.reason
    )
    return ApiResponse[DealerOfferRead].ok(
        DealerOfferRead.model_validate(row.model_dump()), "Promotion deactivated successfully"
    )


@router.post(
    "/offers/{identity}/retire",
    response_model=ApiResponse[DealerOfferRead],
    dependencies=[require_permission(Permission.PROMOTION_MANAGE)],
)
async def retire_offer_with_reason(
    identity: uuid.UUID, payload: PromotionRetire, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerOfferRead]:
    row = await PromotionService(session, tenant).retire("offers", identity, payload.reason)
    return ApiResponse[DealerOfferRead].ok(
        DealerOfferRead.model_validate(row.model_dump()), "Promotion retired successfully"
    )


@router.get(
    "/offers/{identity}/preview",
    response_model=ApiResponse[DealerOfferPreview],
    dependencies=[require_permission(Permission.PROMOTION_VIEW)],
)
async def preview_offer(
    identity: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerOfferPreview]:
    count = await PromotionService(session, tenant).preview(identity)
    return ApiResponse[DealerOfferPreview].ok(DealerOfferPreview(item_count=count))


@router.get(
    "/banners",
    response_model=PaginatedResponse[DealerBannerRead],
    dependencies=[require_permission(Permission.PROMOTION_VIEW)],
)
async def list_banners(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    search: Annotated[str | None, Query(max_length=120)] = None,
) -> PaginatedResponse[DealerBannerRead]:
    rows, page = await PromotionService(session, tenant).list_promotions(
        "banners", params, search=search
    )
    return PaginatedResponse(
        data=[DealerBannerRead.model_validate(row.model_dump()) for row in rows], pagination=page
    )


@router.get(
    "/banners/{identity}",
    response_model=ApiResponse[DealerBannerRead],
    dependencies=[require_permission(Permission.PROMOTION_VIEW)],
)
async def get_banner(
    identity: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerBannerRead]:
    row = await PromotionService(session, tenant).get("banners", identity)
    return ApiResponse[DealerBannerRead].ok(DealerBannerRead.model_validate(row.model_dump()))


@router.post(
    "/banners",
    response_model=ApiResponse[DealerBannerRead],
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_permission(Permission.PROMOTION_MANAGE)],
)
async def create_banner(
    payload: DealerBannerCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerBannerRead]:
    row = await PromotionService(session, tenant).create("banners", payload)
    return ApiResponse[DealerBannerRead].ok(
        DealerBannerRead.model_validate(row.model_dump()), "Promotion created successfully"
    )


@router.patch(
    "/banners/{identity}",
    response_model=ApiResponse[DealerBannerRead],
    dependencies=[require_permission(Permission.PROMOTION_MANAGE)],
)
async def update_banner(
    identity: uuid.UUID, payload: DealerBannerUpdate, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerBannerRead]:
    row = await PromotionService(session, tenant).update("banners", identity, payload)
    return ApiResponse[DealerBannerRead].ok(
        DealerBannerRead.model_validate(row.model_dump()), "Promotion updated successfully"
    )


@router.delete(
    "/banners/{identity}",
    response_model=ApiResponse[DealerBannerRead],
    dependencies=[require_permission(Permission.PROMOTION_MANAGE)],
)
async def retire_banner(
    identity: uuid.UUID, payload: PromotionRetire, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerBannerRead]:
    row = await PromotionService(session, tenant).retire("banners", identity, payload.reason)
    return ApiResponse[DealerBannerRead].ok(
        DealerBannerRead.model_validate(row.model_dump()), "Promotion retired successfully"
    )


@router.get(
    "/offers",
    response_model=PaginatedResponse[DealerOfferRead],
    dependencies=[require_permission(Permission.PROMOTION_VIEW)],
)
async def list_offers(
    session: DbSession,
    tenant: Tenant,
    params: PageDep,
    search: Annotated[str | None, Query(max_length=120)] = None,
) -> PaginatedResponse[DealerOfferRead]:
    rows, page = await PromotionService(session, tenant).list_promotions(
        "offers", params, search=search
    )
    return PaginatedResponse(
        data=[DealerOfferRead.model_validate(row.model_dump()) for row in rows], pagination=page
    )


@router.get(
    "/offers/{identity}",
    response_model=ApiResponse[DealerOfferRead],
    dependencies=[require_permission(Permission.PROMOTION_VIEW)],
)
async def get_offer(
    identity: uuid.UUID, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerOfferRead]:
    row = await PromotionService(session, tenant).get("offers", identity)
    return ApiResponse[DealerOfferRead].ok(DealerOfferRead.model_validate(row.model_dump()))


@router.post(
    "/offers",
    response_model=ApiResponse[DealerOfferRead],
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_permission(Permission.PROMOTION_MANAGE)],
)
async def create_offer(
    payload: DealerOfferCreate, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerOfferRead]:
    row = await PromotionService(session, tenant).create("offers", payload)
    return ApiResponse[DealerOfferRead].ok(
        DealerOfferRead.model_validate(row.model_dump()), "Promotion created successfully"
    )


@router.patch(
    "/offers/{identity}",
    response_model=ApiResponse[DealerOfferRead],
    dependencies=[require_permission(Permission.PROMOTION_MANAGE)],
)
async def update_offer(
    identity: uuid.UUID, payload: DealerOfferUpdate, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerOfferRead]:
    row = await PromotionService(session, tenant).update("offers", identity, payload)
    return ApiResponse[DealerOfferRead].ok(
        DealerOfferRead.model_validate(row.model_dump()), "Promotion updated successfully"
    )


@router.delete(
    "/offers/{identity}",
    response_model=ApiResponse[DealerOfferRead],
    dependencies=[require_permission(Permission.PROMOTION_MANAGE)],
)
async def retire_offer(
    identity: uuid.UUID, payload: PromotionRetire, session: DbSession, tenant: Tenant
) -> ApiResponse[DealerOfferRead]:
    row = await PromotionService(session, tenant).retire("offers", identity, payload.reason)
    return ApiResponse[DealerOfferRead].ok(
        DealerOfferRead.model_validate(row.model_dump()), "Promotion retired successfully"
    )
