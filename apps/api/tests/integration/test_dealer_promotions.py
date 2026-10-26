"""Promotion lifecycle, audience and document ownership use real tenant rows."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from src.core.clock import today
from src.core.errors import BusinessRuleError, NotFoundError, ValidationError
from src.core.pagination import PageParams
from src.modules.audit.models import AuditLog
from src.modules.dealer_portal.models import DealerBannerStatus
from src.modules.dealer_portal.promotion_schemas import (
    DealerBannerCreate,
    DealerBannerUpdate,
    DealerOfferCreate,
    DealerOfferUpdate,
)
from src.modules.dealer_portal.promotion_service import PromotionService
from src.modules.documents.models import Document, DocumentCategory
from src.modules.products import service as products
from src.modules.products.models import Item, PriceType
from tests.api.dealer_portal_support import dealer_customer
from tests.seed_support import seed_item, seed_uom

pytestmark = pytest.mark.integration


async def image_document(session, tenant, *, item_id=None, mime="image/png"):
    document = Document(
        company_id=tenant.company_id,
        file_name="cover.png",
        storage_key=f"test/{uuid.uuid4()}.png",
        mime_type=mime,
        size_bytes=10,
        checksum_sha256="0" * 64,
        category=DocumentCategory.ATTACHMENT,
        uploaded_by_id=tenant.user_id,
        entity_type="item" if item_id else None,
        entity_id=item_id,
    )
    session.add(document)
    await session.flush()
    return document


@pytest.fixture
async def promotions(session, tenant, company):
    return PromotionService(session, tenant)


async def test_banner_lifecycle_requires_reasoned_actions_and_writes_audit(
    session, tenant, promotions
):
    document = await image_document(session, tenant)
    banner = await promotions.create(
        "banners", DealerBannerCreate(code="HOME", title="New range", image_document_id=document.id)
    )
    assert banner.status == DealerBannerStatus.INACTIVE
    with pytest.raises(BusinessRuleError, match="reason"):
        await promotions.update("banners", banner.id, DealerBannerUpdate(status="ACTIVE"))
    active = await promotions.set_status(
        "banners", banner.id, DealerBannerStatus.ACTIVE, "Launch approved"
    )
    assert active.status == DealerBannerStatus.ACTIVE
    retired = await promotions.retire("banners", banner.id, "Campaign finished")
    assert retired.status == DealerBannerStatus.ARCHIVED
    with pytest.raises(NotFoundError):
        await promotions.update("banners", banner.id, DealerBannerUpdate(title="Resurrected"))
    logs = list(
        (await session.scalars(select(AuditLog).where(AuditLog.entity_id == banner.id))).all()
    )
    assert len(logs) == 3
    assert logs[-1].context["reason"] == "Campaign finished"


async def test_dealer_sees_only_active_current_eligible_banners(session, tenant, promotions):
    customer = await dealer_customer(session, tenant, "Dealer")
    document = await image_document(session, tenant)
    for code, extras in [
        ("VISIBLE", {}),
        ("FUTURE", {"valid_from": today() + timedelta(days=1)}),
        ("EXPIRED", {"valid_to": today() - timedelta(days=1)}),
        ("OTHER", {"audience_price_type": PriceType.SALE}),
    ]:
        await promotions.create(
            "banners",
            DealerBannerCreate(
                code=code, title=code, image_document_id=document.id, status="ACTIVE", **extras
            ),
        )
    await promotions.create(
        "banners",
        DealerBannerCreate(code="INACTIVE", title="Hidden", image_document_id=document.id),
    )
    portal = PromotionService(session, replace(tenant, customer_id=customer.id))
    rows, page = await portal.dealer_banners(PageParams())
    assert [row.code for row in rows] == ["VISIBLE"] and page.total == 1


async def test_banner_rejects_non_image_and_foreign_or_missing_document(
    session, tenant, promotions
):
    document = await image_document(session, tenant, mime="application/pdf")
    with pytest.raises(ValidationError, match="image"):
        await promotions.create(
            "banners", DealerBannerCreate(code="BAD", title="Bad", image_document_id=document.id)
        )
    with pytest.raises(NotFoundError):
        await promotions.create(
            "banners",
            DealerBannerCreate(code="MISSING", title="Bad", image_document_id=uuid.uuid4()),
        )


async def test_offer_targets_dates_and_customer_audience_remain_consistent(
    session, tenant, promotions
):
    ours = await dealer_customer(session, tenant, "Ours")
    other = await dealer_customer(session, tenant, "Other")
    for code, customer in [("ALL", None), ("OURS", ours.id), ("OTHER", other.id)]:
        await promotions.create(
            "offers",
            DealerOfferCreate(
                code=code,
                title=code,
                scope_kind="ALL",
                discount_percentage="10",
                customer_id=customer,
                is_active=True,
            ),
        )
    portal = PromotionService(session, replace(tenant, customer_id=ours.id))
    rows, page = await portal.dealer_offers(PageParams())
    assert {row.code for row in rows} == {"ALL", "OURS"} and page.total == 2
    with pytest.raises(NotFoundError):
        await promotions.create(
            "offers",
            DealerOfferCreate(
                code="FOREIGN",
                title="Foreign item",
                scope_kind="ITEM",
                item_id=uuid.uuid4(),
                discount_percentage="10",
            ),
        )
    with pytest.raises(BusinessRuleError, match="reason"):
        await promotions.update("offers", rows[0].id, DealerOfferUpdate(is_active=False))


async def test_primary_image_must_belong_to_the_same_item_and_can_be_cleared(
    session, tenant, company
):
    uom = await seed_uom(session, company_id=tenant.company_id)
    item = await seed_item(
        session, company_id=tenant.company_id, code="IMAGE-ITEM", base_uom_id=uom.id
    )
    image = await image_document(session, tenant, item_id=item.id)
    other = await image_document(session, tenant, item_id=uuid.uuid4())
    assert await products.set_item_image(session, tenant, item.id, image.id) == image.id
    with pytest.raises(ValidationError, match="this item"):
        await products.set_item_image(session, tenant, item.id, other.id)
    assert await products.set_item_image(session, tenant, item.id, None) is None
    assert (await session.get(Item, item.id)).image_document_id is None


async def test_dealer_image_route_refuses_unpublished_documents(session, tenant, promotions):
    customer = await dealer_customer(session, tenant, "Dealer")
    image = await image_document(session, tenant)
    portal = PromotionService(session, replace(tenant, customer_id=customer.id))
    with pytest.raises(NotFoundError):
        await portal.image(image.id)


async def test_offer_preview_counts_only_sellable_live_matching_items(session, tenant, promotions):
    uom = await seed_uom(session, company_id=tenant.company_id)
    await seed_item(
        session, company_id=tenant.company_id, code="SELL", base_uom_id=uom.id, can_sell=True
    )
    await seed_item(
        session, company_id=tenant.company_id, code="NOSALE", base_uom_id=uom.id, can_sell=False
    )
    await seed_item(
        session,
        company_id=tenant.company_id,
        code="INACTIVE",
        base_uom_id=uom.id,
        can_sell=True,
        is_active=False,
    )
    offer = await promotions.create(
        "offers",
        DealerOfferCreate(
            code="PREVIEW", title="Preview", scope_kind="ALL", discount_percentage="10"
        ),
    )
    assert await promotions.preview(offer.id) == 1
