"""Audited promotion lifecycles and image authorization."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import ValidationError as SchemaValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, NotFoundError, ValidationError
from src.core.pagination import PageParams
from src.core.responses import OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit
from src.modules.dealer_portal.models import DealerBanner, DealerBannerStatus, DealerOffer
from src.modules.dealer_portal.promotion_repository import PromotionRepository
from src.modules.dealer_portal.promotion_schemas import (
    DealerBannerCreate,
    DealerBannerRead,
    DealerBannerUpdate,
    DealerOfferCreate,
    DealerOfferRead,
    DealerOfferUpdate,
)
from src.modules.dealer_portal.repository import IMAGE_MIME_TYPES
from src.modules.dealer_portal.service import DealerPortalService
from src.modules.documents.service import DocumentDownload, DocumentService
from src.modules.products.models import Brand, Item, ItemCategory

Resource = Literal["banners", "offers"]
PromotionRead = DealerBannerRead | DealerOfferRead
PromotionCreate = DealerBannerCreate | DealerOfferCreate
PromotionUpdate = DealerBannerUpdate | DealerOfferUpdate


def promotion_lifecycle(resource: Resource) -> StateMachine[DealerBannerStatus]:
    return StateMachine(
        entity="Dealer banner" if resource == "banners" else "Dealer offer",
        initial=DealerBannerStatus.INACTIVE,
        transitions=(
            Transition(DealerBannerStatus.INACTIVE, DealerBannerStatus.ACTIVE, label="Activate"),
            Transition(DealerBannerStatus.ACTIVE, DealerBannerStatus.INACTIVE, label="Deactivate"),
            Transition(
                DealerBannerStatus.ACTIVE,
                DealerBannerStatus.ARCHIVED,
                label="Retire",
                is_irreversible=True,
            ),
            Transition(
                DealerBannerStatus.INACTIVE,
                DealerBannerStatus.ARCHIVED,
                label="Retire",
                is_irreversible=True,
            ),
        ),
        terminal=frozenset({DealerBannerStatus.ARCHIVED}),
    )


def promotion_status(row: DealerBanner | DealerOffer) -> DealerBannerStatus:
    if row.deleted_at is not None:
        return DealerBannerStatus.ARCHIVED
    if isinstance(row, DealerBanner):
        return DealerBannerStatus(row.status)
    return DealerBannerStatus.ACTIVE if row.is_active else DealerBannerStatus.INACTIVE


def promotion_read(row: DealerBanner | DealerOffer) -> PromotionRead:
    if isinstance(row, DealerBanner):
        return DealerBannerRead.model_validate(row)
    payload = {name: getattr(row, name) for name in DealerOfferCreate.model_fields}
    return DealerOfferRead(**payload, id=row.id, status=promotion_status(row))


class PromotionService:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session, self.tenant = session, tenant
        self.repo = PromotionRepository(session, tenant)

    async def _validate(self, payload: PromotionCreate) -> None:
        if isinstance(payload, DealerBannerCreate):
            document = await DocumentService(self.session, self.tenant).get_document(
                payload.image_document_id
            )
            if document.mime_type not in IMAGE_MIME_TYPES:
                raise ValidationError("Choose a JPEG, PNG or WebP banner image.")
            targets = {"ITEM": Item, "CATEGORY": ItemCategory, "BRAND": Brand, "OFFER": DealerOffer}
            if (
                payload.link_id is not None
                and await self.repo.target(targets[payload.link_kind], payload.link_id) is None
            ):
                raise NotFoundError("Banner target", payload.link_id)
        else:
            missing = await self.repo.missing_offer_targets(payload)
            if missing:
                raise NotFoundError("Offer target", getattr(payload, missing[0]))

    def _repository(self, resource: Resource) -> Any:
        return self.repo.banners if resource == "banners" else self.repo.offers

    async def list_promotions(
        self, resource: Resource, params: PageParams, *, search: str | None = None
    ) -> tuple[list[PromotionRead], OffsetPagination]:
        rows, page = await self.repo.list_promotions(resource, params, search)
        return [promotion_read(row) for row in rows], page

    async def get(self, resource: Resource, identity: uuid.UUID) -> PromotionRead:
        row = await self._repository(resource).get_or_404(identity)
        return promotion_read(row)

    async def create(self, resource: Resource, payload: PromotionCreate) -> PromotionRead:
        async with transaction(self.session):
            await self._validate(payload)
            model = DealerBanner if resource == "banners" else DealerOffer
            row = model(**payload.model_dump())
            target = (
                DealerBannerStatus(payload.status)
                if isinstance(payload, DealerBannerCreate)
                else (
                    DealerBannerStatus.ACTIVE if payload.is_active else DealerBannerStatus.INACTIVE
                )
            )
            if target != DealerBannerStatus.INACTIVE:
                promotion_lifecycle(resource).assert_transition(DealerBannerStatus.INACTIVE, target)
            self._repository(resource).add(row)
            await self.session.flush()
            await self._audit(row, AuditAction.CREATE, "created")
        return promotion_read(row)

    async def update(
        self, resource: Resource, identity: uuid.UUID, patch: PromotionUpdate
    ) -> PromotionRead:
        async with transaction(self.session):
            model = DealerBanner if resource == "banners" else DealerOffer
            row = await self.repo.lock(model, identity)
            if row is None:
                raise NotFoundError("Promotion", identity)
            schema = DealerBannerCreate if resource == "banners" else DealerOfferCreate
            current = {name: getattr(row, name) for name in schema.model_fields}
            try:
                payload = schema.model_validate({**current, **patch.model_dump(exclude_unset=True)})
            except SchemaValidationError as exc:
                raise ValidationError("Check the promotion fields and validity window.") from exc
            await self._validate(payload)
            previous = promotion_status(row)
            target = (
                payload.status
                if isinstance(payload, DealerBannerCreate)
                else (
                    DealerBannerStatus.ACTIVE if payload.is_active else DealerBannerStatus.INACTIVE
                )
            )
            if previous != target:
                raise BusinessRuleError(
                    "Use the activate or deactivate action with a reason "
                    "to change promotion status."
                )
            for name, value in payload.model_dump(exclude={"code"}).items():
                setattr(row, name, value)
            row.updated_by_id = self.tenant.user_id
            await self.session.flush()
            await self._audit(row, AuditAction.UPDATE, "updated")
        return promotion_read(row)

    async def retire(self, resource: Resource, identity: uuid.UUID, reason: str) -> PromotionRead:
        async with transaction(self.session):
            row = await self.repo.lock(
                DealerBanner if resource == "banners" else DealerOffer, identity
            )
            if row is None:
                raise NotFoundError("Promotion", identity)
            promotion_lifecycle(resource).assert_transition(
                promotion_status(row), DealerBannerStatus.ARCHIVED
            )
            row.deleted_at = utcnow()
            row.updated_by_id = self.tenant.user_id
            if isinstance(row, DealerBanner):
                row.status = DealerBannerStatus.ARCHIVED
            else:
                row.is_active = False
            await self.session.flush()
            await self._audit(row, AuditAction.DELETE, "retired", reason=reason)
        return promotion_read(row)

    async def set_status(
        self, resource: Resource, identity: uuid.UUID, target: DealerBannerStatus, reason: str
    ) -> PromotionRead:
        async with transaction(self.session):
            row = await self.repo.lock(
                DealerBanner if resource == "banners" else DealerOffer, identity
            )
            if row is None:
                raise NotFoundError("Promotion", identity)
            promotion_lifecycle(resource).assert_transition(promotion_status(row), target)
            if target == DealerBannerStatus.ACTIVE:
                schema = DealerBannerCreate if resource == "banners" else DealerOfferCreate
                payload = schema.model_validate(
                    {name: getattr(row, name) for name in schema.model_fields}
                )
                await self._validate(payload)
            if isinstance(row, DealerBanner):
                row.status = target
            else:
                row.is_active = target == DealerBannerStatus.ACTIVE
            row.updated_by_id = self.tenant.user_id
            await self.session.flush()
            await self._audit(row, AuditAction.UPDATE, target.lower(), reason=reason)
        return promotion_read(row)

    async def preview(self, identity: uuid.UUID) -> int:
        row = await self.repo.offers.get_or_404(identity)
        return await self.repo.offer_item_count(row)

    async def _audit(
        self,
        row: DealerBanner | DealerOffer,
        action: AuditAction,
        verb: str,
        *,
        reason: str | None = None,
    ) -> None:
        await record_audit(
            self.session,
            company_id=self.tenant.company_id,
            user_id=self.tenant.user_id,
            action=action,
            entity_type="dealer_banner" if isinstance(row, DealerBanner) else "dealer_offer",
            entity_id=row.id,
            entity_code=row.code,
            summary=f"Promotion {row.code} {verb}",
            new_values=promotion_read(row).model_dump(mode="json"),
            context={"reason": reason} if reason else None,
        )

    async def dealer_banners(
        self, params: PageParams
    ) -> tuple[list[DealerBannerRead], OffsetPagination]:
        customer = await DealerPortalService(self.session, self.tenant)._customer()
        rows, page = await self.repo.banners.active(customer.price_list_type, today(), params)
        return [DealerBannerRead.model_validate(row) for row in rows], page

    async def dealer_offers(
        self, params: PageParams
    ) -> tuple[list[DealerOfferRead], OffsetPagination]:
        customer = await DealerPortalService(self.session, self.tenant)._customer()
        rows, page = await self.repo.active_offers(
            customer.id, customer.price_list_type, today(), params
        )
        return [
            DealerOfferRead.model_validate(promotion_read(row).model_dump()) for row in rows
        ], page

    async def image(self, identity: uuid.UUID) -> DocumentDownload:
        customer = await DealerPortalService(self.session, self.tenant)._customer()
        documents = DocumentService(self.session, self.tenant)
        document = await documents.get_document(identity)
        if document.mime_type not in IMAGE_MIME_TYPES:
            raise NotFoundError("Image", identity)
        item = None
        if document.entity_type == "item" and document.entity_id is not None:
            item = await self.repo.sellable_image_item(document.entity_id)
        if item is None and not await self.repo.visible_banner_image(
            identity, customer.price_list_type, today()
        ):
            raise NotFoundError("Image", identity)
        return await documents.open_download(identity)
