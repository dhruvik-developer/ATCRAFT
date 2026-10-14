"""Tenant-scoped reads and writes for the dealer portal.

One query per concern per page. An empty id list is a false predicate, never
the absence of a filter.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.orm import InstrumentedAttribute

from src.common.repository import BaseRepository
from src.core.pagination import OffsetPagination, PageParams, paginate
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

    async def get_in_cart(self, cart_id: uuid.UUID, line_id: uuid.UUID) -> DealerCartLine | None:
        """One line of one cart. A foreign cart's lines are "not found"."""
        stmt = self._scoped().where(DealerCartLine.id == line_id, DealerCartLine.cart_id == cart_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def find_grain(
        self, cart_id: uuid.UUID, item_id: uuid.UUID, variant_id: uuid.UUID | None
    ) -> DealerCartLine | None:
        """The cart's line for an item/variant grain, if one is already there."""
        stmt = self._scoped().where(
            DealerCartLine.cart_id == cart_id,
            DealerCartLine.item_id == item_id,
            DealerCartLine.item_variant_id.is_(None)
            if variant_id is None
            else DealerCartLine.item_variant_id == variant_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def count_for_cart(self, cart_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(DealerCartLine)
            .where(
                DealerCartLine.company_id == self.tenant.company_id,
                DealerCartLine.cart_id == cart_id,
            )
        )
        return int((await self.session.execute(stmt)).scalar_one() or 0)

    async def delete_for_cart(self, cart_id: uuid.UUID) -> int:
        """Empty a cart. Returns the removed line count, for the audit row."""
        from src.core.database import affected_rows

        result = await self.session.execute(
            delete(DealerCartLine).where(
                DealerCartLine.company_id == self.tenant.company_id,
                DealerCartLine.cart_id == cart_id,
            )
        )
        return affected_rows(result)

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

    async def get_for_user(self, entry_id: uuid.UUID) -> DealerWishlistItem | None:
        """One of this user's saved rows. Another user's row is "not found"."""
        stmt = self._scoped().where(
            DealerWishlistItem.id == entry_id,
            DealerWishlistItem.user_id == self.tenant.user_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def find_grain(
        self, item_id: uuid.UUID, variant_id: uuid.UUID | None
    ) -> DealerWishlistItem | None:
        stmt = self._scoped().where(
            DealerWishlistItem.user_id == self.tenant.user_id,
            DealerWishlistItem.item_id == item_id,
            DealerWishlistItem.item_variant_id.is_(None)
            if variant_id is None
            else DealerWishlistItem.item_variant_id == variant_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

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

    async def category(self, category_id: uuid.UUID) -> ItemCategory | None:
        stmt = select(ItemCategory).where(
            ItemCategory.company_id == self.tenant.company_id,
            ItemCategory.id == category_id,
            ItemCategory.deleted_at.is_(None),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def category_subtree_ids(self, path_prefix: str) -> list[uuid.UUID]:
        """Ids of a category and everything filed under it, in one predicate.

        The materialised path makes the subtree one LIKE - the same device the
        products module's own ``list_subtree`` uses.
        """
        stmt = select(ItemCategory.id).where(
            ItemCategory.company_id == self.tenant.company_id,
            ItemCategory.deleted_at.is_(None),
            ItemCategory.path.like(f"{path_prefix}%"),
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def page_items(
        self,
        params: PageParams,
        *,
        search: str | None,
        category_ids: Sequence[uuid.UUID] | None,
        brand_id: uuid.UUID | None,
        in_stock_only: bool,
        on_offer_only: bool,
        price_type: str,
        customer_id: uuid.UUID,
        on_date: date,
        sort_by: str,
    ) -> tuple[list[Item], OffsetPagination]:
        """Sellable items for the catalogue, filtered and paged in SQL.

        ``on_offer_only`` is a coarse prefilter: an item passes when any active
        scheme could apply to it on ``on_date``. CATEGORY-scoped schemes are
        not resolved to the subtree here - the exact match runs per row in
        Python, so a page can come back short and the count is the coarse one
        (docs/assumptions.md §C3-9).
        """
        stmt = catalogue_item_stmt().where(Item.company_id == self.tenant.company_id)
        if search:
            pattern = f"%{search.strip()}%"
            stmt = stmt.where(or_(Item.code.ilike(pattern), Item.name.ilike(pattern)))
        if category_ids is not None:
            ids = list(dict.fromkeys(category_ids))
            # An empty id list is a false predicate, never the absence of a
            # filter: nothing can be filed under categories that do not exist.
            stmt = stmt.where(Item.category_id.in_(ids) if ids else Item.id.is_(None))
        if brand_id is not None:
            stmt = stmt.where(Item.brand_id == brand_id)
        if in_stock_only:
            stocked = (
                select(InventoryBalance.item_id)
                .join(
                    Warehouse,
                    (Warehouse.id == InventoryBalance.warehouse_id)
                    & (Warehouse.company_id == InventoryBalance.company_id),
                )
                .where(
                    InventoryBalance.item_id == Item.id,
                    InventoryBalance.company_id == self.tenant.company_id,
                    InventoryBalance.available_quantity > 0,
                    Warehouse.deleted_at.is_(None),
                    Warehouse.is_active.is_(True),
                    Warehouse.warehouse_type.in_(SELLABLE_WAREHOUSE_TYPES),
                )
                .exists()
            )
            stmt = stmt.where(stocked)
        if on_offer_only:
            stmt = stmt.where(self._offer_may_apply(price_type, customer_id, on_date).exists())
        order_column = Item.name if sort_by == "name" else Item.code
        # Ordered here, not through list_page: the catalogue sorts on two keys
        # (the field plus the id tiebreaker) and the base helper takes one.
        return await paginate(
            self.session, stmt.order_by(order_column.asc(), Item.id.asc()), params
        )

    def _offer_may_apply(
        self, price_type: str, customer_id: uuid.UUID, on_date: date
    ) -> Select[tuple[uuid.UUID]]:
        """Ids of offers that could discount some line, correlated to the item."""
        return select(DealerOffer.id).where(
            DealerOffer.company_id == self.tenant.company_id,
            DealerOffer.deleted_at.is_(None),
            DealerOffer.is_active.is_(True),
            or_(DealerOffer.valid_from.is_(None), DealerOffer.valid_from <= on_date),
            or_(DealerOffer.valid_to.is_(None), DealerOffer.valid_to >= on_date),
            or_(
                DealerOffer.audience_price_type.is_(None),
                DealerOffer.audience_price_type == price_type,
            ),
            or_(DealerOffer.customer_id.is_(None), DealerOffer.customer_id == customer_id),
            or_(
                DealerOffer.scope_kind == "ALL",
                (DealerOffer.scope_kind == "ITEM") & (DealerOffer.item_id == Item.id),
                (DealerOffer.scope_kind == "BRAND") & (DealerOffer.brand_id == Item.brand_id),
                DealerOffer.scope_kind == "CATEGORY",
            ),
        )

    async def order_line_counts(self, order_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        """Live line count per sales order, in one grouped read.

        A read-only probe into a table the sales module owns: the portal's
        order list shows a count, and counting per row would be the N+1
        section 32 forbids.
        """
        from src.modules.sales.models import SalesOrderLine

        ids = list(dict.fromkeys(order_ids))
        if not ids:
            return {}
        stmt = (
            select(SalesOrderLine.sales_order_id, func.count().label("lines"))
            .where(
                SalesOrderLine.company_id == self.tenant.company_id,
                SalesOrderLine.sales_order_id.in_(ids),
            )
            .group_by(SalesOrderLine.sales_order_id)
        )
        return {row.sales_order_id: int(row.lines) for row in (await self.session.execute(stmt))}

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
