"""Tenant-scoped promotion rows and safe image associations."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, literal, or_, select, union_all

from src.common.repository import BaseRepository
from src.core.deps import TenantContext
from src.core.pagination import PageParams
from src.core.responses import OffsetPagination
from src.modules.customers.models import Customer
from src.modules.dealer_portal.models import DealerBanner, DealerBannerStatus, DealerOffer
from src.modules.dealer_portal.repository import IMAGE_MIME_TYPES, DealerOfferRepository
from src.modules.documents.models import Document
from src.modules.documents.repository import document_not_expired
from src.modules.products.models import Brand, Item, ItemCategory


class DealerBannerRepository(BaseRepository[DealerBanner]):
    model = DealerBanner
    entity_label = "Dealer banner"
    searchable_fields = ("code", "title")

    async def active(
        self, price_type: str, on_date: date, params: PageParams, *, now: datetime
    ) -> tuple[list[DealerBanner], OffsetPagination]:
        """The banners a dealer on ``price_type`` is shown on ``on_date``.

        Only a banner whose image ``/dealer-portal/images/{id}`` will serve - a
        JPEG, PNG or WebP of this company's, not past its retention date. That
        route reads the image as a live document and answers 404 for one that
        lapsed, or that the purge sweep has since removed, so listing the banner
        showed every dealer a broken image.

        ASSUMPTION: such a banner is withdrawn from dealers rather than shown
        without its image. A banner is its image, and unlike an item's there is
        no next one to take its place; staff still see it, ACTIVE, and choose a
        new image to bring it back. docs/assumptions.md §PROMO-1.
        """
        image_live = (
            select(Document.id)
            .where(
                Document.company_id == self.tenant.company_id,
                Document.id == DealerBanner.image_document_id,
                Document.mime_type.in_(IMAGE_MIME_TYPES),
                document_not_expired(now),
            )
            .exists()
        )
        stmt = self._scoped().where(
            DealerBanner.status == DealerBannerStatus.ACTIVE,
            or_(DealerBanner.valid_from.is_(None), DealerBanner.valid_from <= on_date),
            or_(DealerBanner.valid_to.is_(None), DealerBanner.valid_to >= on_date),
            or_(
                DealerBanner.audience_price_type.is_(None),
                DealerBanner.audience_price_type == price_type,
            ),
            image_live,
        )
        return await self.list_page(params, stmt=stmt, order_by=DealerBanner.sort_order.asc())


class PromotionRepository:
    def __init__(self, session: Any, tenant: TenantContext) -> None:
        self.session, self.tenant = session, tenant
        self.banners = DealerBannerRepository(session, tenant)
        self.offers = DealerOfferRepository(session, tenant)

    async def offer_item_count(self, offer: DealerOffer) -> int:
        stmt = (
            select(func.count())
            .select_from(Item)
            .where(
                Item.company_id == self.tenant.company_id,
                Item.deleted_at.is_(None),
                Item.is_active.is_(True),
                Item.can_sell.is_(True),
            )
        )
        if offer.scope_kind == "ITEM":
            stmt = stmt.where(Item.id == offer.item_id)
        elif offer.scope_kind == "BRAND":
            stmt = stmt.where(Item.brand_id == offer.brand_id)
        elif offer.scope_kind == "CATEGORY":
            if offer.category_id is None:
                return 0
            category = await self.target(ItemCategory, offer.category_id)
            if category is None:
                return 0
            stmt = stmt.where(
                Item.category_id.in_(
                    select(ItemCategory.id).where(
                        ItemCategory.company_id == self.tenant.company_id,
                        ItemCategory.deleted_at.is_(None),
                        ItemCategory.path.like(f"{category.path}%"),
                    )
                )
            )
        return int(await self.session.scalar(stmt) or 0)

    async def list_promotions(
        self, resource: str, params: PageParams, search: str | None
    ) -> tuple[list[Any], OffsetPagination]:
        repo = self.banners if resource == "banners" else self.offers
        model = DealerBanner if resource == "banners" else DealerOffer
        stmt = repo._scoped()
        if search:
            pattern = f"%{search.strip()}%"
            stmt = stmt.where(or_(model.code.ilike(pattern), model.title.ilike(pattern)))
        return await repo.list_page(params, stmt=stmt, order_by=model.code.asc())

    async def missing_offer_targets(self, payload: Any) -> list[str]:
        queries = []
        expected = []
        for name, model in (
            ("item_id", Item),
            ("category_id", ItemCategory),
            ("brand_id", Brand),
            ("customer_id", Customer),
        ):
            identity = getattr(payload, name)
            if identity is not None:
                expected.append(name)
                queries.append(
                    select(literal(name)).where(
                        model.company_id == self.tenant.company_id,
                        model.id == identity,
                        model.deleted_at.is_(None),
                    )
                )
        if not queries:
            return []
        found = set((await self.session.scalars(union_all(*queries))).all())
        return [name for name in expected if name not in found]

    async def active_offers(
        self, customer_id: uuid.UUID, price_type: str, on_date: date, params: PageParams
    ) -> tuple[list[DealerOffer], OffsetPagination]:
        stmt = self.offers._scoped().where(
            DealerOffer.is_active.is_(True),
            or_(DealerOffer.valid_from.is_(None), DealerOffer.valid_from <= on_date),
            or_(DealerOffer.valid_to.is_(None), DealerOffer.valid_to >= on_date),
            or_(
                DealerOffer.audience_price_type.is_(None),
                DealerOffer.audience_price_type == price_type,
            ),
            or_(DealerOffer.customer_id.is_(None), DealerOffer.customer_id == customer_id),
        )
        return await self.offers.list_page(params, stmt=stmt, order_by=DealerOffer.sort_order.asc())

    async def visible_banner_image(
        self, document_id: uuid.UUID, price_type: str, on_date: date
    ) -> bool:
        return bool(
            await self.session.scalar(
                select(DealerBanner.id)
                .where(
                    DealerBanner.company_id == self.tenant.company_id,
                    DealerBanner.deleted_at.is_(None),
                    DealerBanner.status == DealerBannerStatus.ACTIVE,
                    DealerBanner.image_document_id == document_id,
                    or_(DealerBanner.valid_from.is_(None), DealerBanner.valid_from <= on_date),
                    or_(DealerBanner.valid_to.is_(None), DealerBanner.valid_to >= on_date),
                    or_(
                        DealerBanner.audience_price_type.is_(None),
                        DealerBanner.audience_price_type == price_type,
                    ),
                )
                .limit(1)
            )
        )

    async def target(self, model: Any, identity: uuid.UUID) -> Any:
        stmt = select(model).where(model.company_id == self.tenant.company_id, model.id == identity)
        if hasattr(model, "deleted_at"):
            stmt = stmt.where(model.deleted_at.is_(None))
        return await self.session.scalar(stmt)

    async def lock(self, model: Any, identity: uuid.UUID) -> Any:
        return await self.session.scalar(
            select(model)
            .where(
                model.company_id == self.tenant.company_id,
                model.id == identity,
                model.deleted_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    async def sellable_image_item(self, identity: uuid.UUID) -> Item | None:
        return await self.session.scalar(
            select(Item).where(
                Item.company_id == self.tenant.company_id,
                Item.id == identity,
                Item.is_active.is_(True),
                Item.can_sell.is_(True),
                Item.deleted_at.is_(None),
            )
        )
