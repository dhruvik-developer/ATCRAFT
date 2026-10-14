"""Dealer catalogue offers, carts and wishlists.

Offers are percentage schemes. A cart stores quantities only: the price is
read live, so a list change reaches the next preview without a rewrite.
``sales_order_lines.offer_id`` is a bare UUID back to an offer, with no
foreign key, so the sales table does not depend on this module's schema.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models import (
    OptimisticLockMixin,
    TenantEntity,
    TenantMasterEntity,
    sql_in_list,
)
from src.core.types import GUID, Percentage, Quantity
from src.modules.products.models import ATTRIBUTE_NULL_SENTINEL, PriceType

#: Free quantity at or above this reads as in stock. Between one unit and this
#: threshold the catalogue says limited. Zero is made to order. Exact quantities
#: are never returned. ASSUMPTION: docs/assumptions.md §C3-2.
AVAILABILITY_IN_STOCK_AT = Decimal("10")

#: The list price is the channel price for one unit. The price endpoint takes
#: the quantity the dealer typed. ASSUMPTION: docs/assumptions.md §C3-2.
CATALOGUE_PRICE_QUANTITY = Decimal("1")

#: How many images an item detail returns. ASSUMPTION: docs/assumptions.md §C3-8.
CATALOGUE_MAX_IMAGES = 8

#: The tracking feed is a bounded newest-first read, not a cursor: a
#: consignment's history is short and the dealer always sees the whole of it.
#: ASSUMPTION: docs/assumptions.md §C3-7.
TRACKING_EVENT_LIMIT = 200

#: A cart is a working draft, not a bulk import: at most this many grains per
#: cart. Well under the sales-order line bound, so a full cart always fits the
#: checkout payload. ASSUMPTION: docs/assumptions.md §C3-9.
MAX_CART_LINES = 50


class OfferScopeKind(StrEnum):
    """What an offer applies to. The matching id column is required; the others are null."""

    ALL = "ALL"
    CATEGORY = "CATEGORY"
    BRAND = "BRAND"
    ITEM = "ITEM"


class AvailabilityBand(StrEnum):
    """A stock indication that does not reveal a quantity."""

    IN_STOCK = "IN_STOCK"
    LIMITED = "LIMITED"
    MADE_TO_ORDER = "MADE_TO_ORDER"


class SalesOrderSource(StrEnum):
    """Who raised the sales order. Set by the server, never by a payload."""

    ERP = "ERP"
    DEALER_PORTAL = "DEALER_PORTAL"


#: Warehouses whose free stock a dealer may be promised. Scrap, returns, QC
#: hold, job work and work in progress are not. ASSUMPTION: docs/assumptions.md §C3-2.
SELLABLE_WAREHOUSE_TYPES: tuple[str, ...] = ("FINISHED_GOODS", "PACKING")

_SCOPE_TARGETS = """
(
    scope_kind = 'ALL'
    AND category_id IS NULL AND brand_id IS NULL AND item_id IS NULL
) OR (
    scope_kind = 'CATEGORY'
    AND category_id IS NOT NULL AND brand_id IS NULL AND item_id IS NULL
) OR (
    scope_kind = 'BRAND'
    AND brand_id IS NOT NULL AND category_id IS NULL AND item_id IS NULL
) OR (
    scope_kind = 'ITEM'
    AND item_id IS NOT NULL AND category_id IS NULL AND brand_id IS NULL
)
"""


class DealerOffer(TenantMasterEntity):
    """One percentage scheme. The best single match wins; offers do not stack."""

    __tablename__ = "dealer_offers"

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    scope_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("item_categories.id", ondelete="RESTRICT"), nullable=True
    )
    brand_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("brands.id", ondelete="RESTRICT"), nullable=True
    )
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=True
    )

    #: NULL means every price channel. Otherwise only that ``PriceType``.
    audience_price_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: NULL means the audience. Set, the offer is that dealer's negotiated scheme.
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("customers.id", ondelete="RESTRICT"), nullable=True
    )

    min_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("1"), server_default="1"
    )
    discount_percentage: Mapped[Decimal] = mapped_column(Percentage, nullable=False)

    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_dealer_offers_company_id_code"),
        Index("ix_dealer_offers_active", "company_id", "is_active", "sort_order"),
        Index("ix_dealer_offers_customer", "company_id", "customer_id"),
        CheckConstraint(f"scope_kind IN ({sql_in_list(OfferScopeKind)})", name="scope_kind_known"),
        CheckConstraint(_SCOPE_TARGETS, name="scope_targets_match"),
        CheckConstraint(
            f"audience_price_type IS NULL OR audience_price_type IN ({sql_in_list(PriceType)})",
            name="audience_known",
        ),
        CheckConstraint("min_quantity > 0", name="min_quantity_positive"),
        CheckConstraint(
            "discount_percentage > 0 AND discount_percentage <= 100",
            name="discount_open_interval",
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_from <= valid_to",
            name="dates_ordered",
        ),
    )


class DealerCart(TenantEntity, OptimisticLockMixin):
    """One open cart per dealer user. ASSUMPTION: docs/assumptions.md §C3-4."""

    __tablename__ = "dealer_carts"

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("company_id", "user_id", name="uq_dealer_carts_company_user"),
        Index("ix_dealer_carts_customer", "company_id", "customer_id"),
    )


class DealerCartLine(TenantEntity):
    """A quantity on a cart. The price is not stored."""

    __tablename__ = "dealer_cart_lines"

    cart_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("dealer_carts.id", ondelete="CASCADE"), nullable=False
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    item_variant_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("item_variants.id", ondelete="RESTRICT"), nullable=True
    )
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)

    __table_args__ = (
        Index(
            "uq_dealer_cart_lines_grain",
            "cart_id",
            "item_id",
            text(f"coalesce(item_variant_id, '{ATTRIBUTE_NULL_SENTINEL}'::uuid)"),
            unique=True,
        ),
        Index("ix_dealer_cart_lines_cart", "company_id", "cart_id"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
    )


class DealerWishlistItem(TenantEntity):
    """An item a dealer user saved. Same grain as a cart line, no quantity."""

    __tablename__ = "dealer_wishlist_items"

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    item_variant_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("item_variants.id", ondelete="RESTRICT"), nullable=True
    )

    __table_args__ = (
        Index(
            "uq_dealer_wishlist_grain",
            "company_id",
            "user_id",
            "item_id",
            text(f"coalesce(item_variant_id, '{ATTRIBUTE_NULL_SENTINEL}'::uuid)"),
            unique=True,
        ),
        Index("ix_dealer_wishlist_customer", "company_id", "customer_id"),
    )


__all__ = [
    "AVAILABILITY_IN_STOCK_AT",
    "CATALOGUE_MAX_IMAGES",
    "CATALOGUE_PRICE_QUANTITY",
    "MAX_CART_LINES",
    "SELLABLE_WAREHOUSE_TYPES",
    "TRACKING_EVENT_LIMIT",
    "AvailabilityBand",
    "DealerCart",
    "DealerCartLine",
    "DealerOffer",
    "DealerWishlistItem",
    "OfferScopeKind",
    "SalesOrderSource",
]
