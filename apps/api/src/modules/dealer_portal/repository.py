"""Tenant-scoped reads and writes for the dealer portal.

One query per concern per page. An empty id list is a false predicate, never
the absence of a filter.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import InstrumentedAttribute

from src.common.repository import BaseRepository
from src.modules.customers.models import Customer, CustomerAddress, CustomerContact
from src.modules.dealer_portal.models import (
    SELLABLE_WAREHOUSE_TYPES,
    DealerCart,
    DealerCartLine,
    DealerOffer,
    DealerWishlistItem,
)
from src.modules.documents.models import Document
from src.modules.inventory.models import InventoryBalance
from src.modules.products.models import Brand, Item, ItemCategory, ItemVariant
from src.modules.users.models import User
from src.modules.warehouses.models import Warehouse

IMAGE_MIME_TYPES = ("image/jpeg", "image/png", "image/webp")
CATALOGUE_ENTITY = "item"


class DealerOfferRepository(BaseRepository[DealerOffer]):
    model = DealerOffer
    entity_label = "Dealer offer"
    sortable_fields: dict[str, InstrumentedAttribute[object]] = {  # noqa: RUF012
        "code": DealerOffer.code,
        "sort_order": DealerOffer.sort_order,
        "created_at": DealerOffer.created_at,
    }

    async def active_for_channel(
        self, *, price_type: str, customer_id: uuid.UUID, on_date: object
    ) -> list[DealerOffer]:
        stmt = self._scoped().where(
            DealerOffer.is_active.is_(True),
            or_(DealerOffer.valid_from.is_(None), DealerOffer.valid_from <= on_date),
            or_(DealerOffer.valid_to.is_(None), DealerOffer.valid_to >= on_date),
            or_(
                DealerOffer.audience_price_type.is_(None),
                DealerOffer.audience_price_type == price_type,
            ),
            or_(DealerOffer.customer_id.is_(None), DealerOffer.customer_id == customer_id),
        )
        return list((await self.session.execute(stmt)).scalars().all())


class DealerCartRepository(BaseRepository[DealerCart]):
    model = DealerCart
    entity_label = "Dealer cart"

    async def for_user(self) -> DealerCart | None:
        stmt = self._scoped().where(DealerCart.user_id == self.tenant.user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock_for_user(self) -> DealerCart | None:
        stmt = self._scoped().where(DealerCart.user_id == self.tenant.user_id).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()


class DealerCartLineRepository(BaseRepository[DealerCartLine]):
    model = DealerCartLine
    entity_label = "Dealer cart line"

    async def for_cart(self, cart_id: uuid.UUID) -> list[DealerCartLine]:
        stmt = (
            self._scoped()
            .where(DealerCartLine.cart_id == cart_id)
            .order_by(DealerCartLine.created_at, DealerCartLine.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def quantities_for_user(self) -> dict[tuple[uuid.UUID, uuid.UUID | None], Decimal]:
        """Item and variant to the quantity sitting in this user's cart. One query."""
        stmt = (
            select(DealerCartLine.item_id, DealerCartLine.item_variant_id, DealerCartLine.quantity)
            .join(DealerCart, DealerCart.id == DealerCartLine.cart_id)
            .where(
                DealerCartLine.company_id == self.tenant.company_id,
                DealerCart.company_id == self.tenant.company_id,
                DealerCart.user_id == self.tenant.user_id,
            )
        )
        rows = (await self.session.execute(stmt)).all()
        return {(row.item_id, row.item_variant_id): Decimal(row.quantity) for row in rows}


class DealerWishlistRepository(BaseRepository[DealerWishlistItem]):
    model = DealerWishlistItem
    entity_label = "Dealer wishlist item"

    def _user_stmt(self) -> Select[tuple[DealerWishlistItem]]:
        return (
            self._scoped()
            .where(DealerWishlistItem.user_id == self.tenant.user_id)
            .order_by(DealerWishlistItem.created_at, DealerWishlistItem.id)
        )

    async def for_user(self) -> list[DealerWishlistItem]:
        return list((await self.session.execute(self._user_stmt())).scalars().all())

    async def ids_for_user(self) -> set[tuple[uuid.UUID, uuid.UUID | None]]:
        stmt = select(DealerWishlistItem.item_id, DealerWishlistItem.item_variant_id).where(
            DealerWishlistItem.company_id == self.tenant.company_id,
            DealerWishlistItem.user_id == self.tenant.user_id,
        )
        return {(row.item_id, row.item_variant_id) for row in (await self.session.execute(stmt))}


class CatalogueRepository(BaseRepository[Item]):
    """Sellable items, priced and indicated, without loading the whole item row."""

    model = Item
    entity_label = "Item"

    async def categories_with_sellable(self) -> list[ItemCategory]:
        sellable = (
            select(Item.category_id)
            .where(
                Item.company_id == self.tenant.company_id,
                Item.deleted_at.is_(None),
                Item.is_active.is_(True),
                Item.can_sell.is_(True),
                Item.category_id.is_not(None),
            )
            .distinct()
            .scalar_subquery()
        )
        stmt = (
            select(ItemCategory)
            .where(
                ItemCategory.company_id == self.tenant.company_id,
                ItemCategory.deleted_at.is_(None),
                ItemCategory.is_active.is_(True),
                ItemCategory.id.in_(sellable),
            )
            .order_by(ItemCategory.depth, ItemCategory.code, ItemCategory.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def category_tree(self) -> list[tuple[uuid.UUID, uuid.UUID | None]]:
        stmt = select(ItemCategory.id, ItemCategory.parent_id).where(
            ItemCategory.company_id == self.tenant.company_id,
            ItemCategory.deleted_at.is_(None),
        )
        return [(row.id, row.parent_id) for row in (await self.session.execute(stmt)).all()]

    async def brands_with_sellable(self) -> list[Brand]:
        sellable = (
            select(Item.brand_id)
            .where(
                Item.company_id == self.tenant.company_id,
                Item.deleted_at.is_(None),
                Item.is_active.is_(True),
                Item.can_sell.is_(True),
                Item.brand_id.is_not(None),
            )
            .distinct()
            .scalar_subquery()
        )
        stmt = (
            select(Brand)
            .where(
                Brand.company_id == self.tenant.company_id,
                Brand.deleted_at.is_(None),
                Brand.is_active.is_(True),
                Brand.id.in_(sellable),
            )
            .order_by(Brand.name, Brand.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def sellable_item(self, item_id: uuid.UUID) -> Item | None:
        stmt = self._scoped().where(
            Item.id == item_id,
            Item.is_active.is_(True),
            Item.can_sell.is_(True),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def items_by_ids(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Item]:
        if not item_ids:
            return {}
        stmt = self._scoped().where(Item.id.in_(list(dict.fromkeys(item_ids))))
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.id: row for row in rows}

    async def variants_of(self, item_id: uuid.UUID) -> list[ItemVariant]:
        stmt = (
            select(ItemVariant)
            .where(
                ItemVariant.company_id == self.tenant.company_id,
                ItemVariant.deleted_at.is_(None),
                ItemVariant.item_id == item_id,
            )
            .order_by(ItemVariant.code, ItemVariant.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def variants_by_ids(
        self, variant_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, ItemVariant]:
        if not variant_ids:
            return {}
        stmt = select(ItemVariant).where(
            ItemVariant.company_id == self.tenant.company_id,
            ItemVariant.deleted_at.is_(None),
            ItemVariant.id.in_(list(dict.fromkeys(variant_ids))),
        )
        return {row.id: row for row in (await self.session.execute(stmt)).scalars().all()}

    async def available_by_item(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Decimal]:
        """Free stock across sellable warehouses. Never cached. Empty ids yield nothing."""
        if not item_ids:
            return {}
        stmt = (
            select(
                InventoryBalance.item_id,
                func.coalesce(func.sum(InventoryBalance.available_quantity), 0).label("available"),
            )
            .join(
                Warehouse,
                (Warehouse.id == InventoryBalance.warehouse_id)
                & (Warehouse.company_id == InventoryBalance.company_id),
            )
            .where(
                InventoryBalance.company_id == self.tenant.company_id,
                InventoryBalance.item_id.in_(list(dict.fromkeys(item_ids))),
                Warehouse.deleted_at.is_(None),
                Warehouse.is_active.is_(True),
                Warehouse.warehouse_type.in_(SELLABLE_WAREHOUSE_TYPES),
            )
            .group_by(InventoryBalance.item_id)
        )
        return {
            row.item_id: Decimal(row.available) for row in (await self.session.execute(stmt)).all()
        }

    async def primary_images(
        self, item_ids: Sequence[uuid.UUID], *, limit_per_item: int
    ) -> dict[uuid.UUID, list[uuid.UUID]]:
        """Image document ids per item, oldest first, capped. One query."""
        if not item_ids or limit_per_item < 1:
            return {}
        ranked = (
            select(
                Document.entity_id,
                Document.id,
                func.row_number()
                .over(
                    partition_by=Document.entity_id,
                    order_by=(Document.created_at, Document.id),
                )
                .label("rn"),
            )
            .where(
                Document.company_id == self.tenant.company_id,
                Document.entity_type == CATALOGUE_ENTITY,
                Document.entity_id.in_(list(dict.fromkeys(item_ids))),
                Document.mime_type.in_(IMAGE_MIME_TYPES),
            )
            .subquery()
        )
        stmt = select(ranked.c.entity_id, ranked.c.id).where(ranked.c.rn <= limit_per_item)
        found: dict[uuid.UUID, list[uuid.UUID]] = {}
        for entity_id, document_id in (await self.session.execute(stmt)).all():
            found.setdefault(entity_id, []).append(document_id)
        return found

    async def customer(self, customer_id: uuid.UUID) -> Customer | None:
        stmt = select(Customer).where(
            Customer.company_id == self.tenant.company_id,
            Customer.id == customer_id,
            Customer.deleted_at.is_(None),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def addresses(self, customer_id: uuid.UUID) -> list[CustomerAddress]:
        stmt = (
            select(CustomerAddress)
            .where(
                CustomerAddress.company_id == self.tenant.company_id,
                CustomerAddress.customer_id == customer_id,
                CustomerAddress.deleted_at.is_(None),
                CustomerAddress.is_active.is_(True),
            )
            .order_by(
                CustomerAddress.is_default.desc(), CustomerAddress.created_at, CustomerAddress.id
            )
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def primary_contact(self, customer_id: uuid.UUID) -> CustomerContact | None:
        stmt = select(CustomerContact).where(
            CustomerContact.company_id == self.tenant.company_id,
            CustomerContact.customer_id == customer_id,
            CustomerContact.deleted_at.is_(None),
            CustomerContact.is_active.is_(True),
            CustomerContact.is_primary.is_(True),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def sales_person(self, user_id: uuid.UUID) -> User | None:
        stmt = select(User).where(
            User.company_id == self.tenant.company_id,
            User.id == user_id,
            User.deleted_at.is_(None),
            User.vendor_id.is_(None),
            User.customer_id.is_(None),
            User.is_active.is_(True),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


def catalogue_item_stmt() -> Select[tuple[Item]]:
    """The sellable, active item set. Callers add filters; they do not widen it."""
    return select(Item).where(
        Item.deleted_at.is_(None),
        Item.is_active.is_(True),
        Item.can_sell.is_(True),
    )


__all__ = [
    "IMAGE_MIME_TYPES",
    "CatalogueRepository",
    "DealerCartLineRepository",
    "DealerCartRepository",
    "DealerOfferRepository",
    "DealerWishlistRepository",
    "catalogue_item_stmt",
]
