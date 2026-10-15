"""The one offer rule the catalogue, the cart, checkout and ERP orders share.

Percentage only, no stacking, the single best offer per line. An explicit
discount on a sales-order line wins and this function is not consulted.
ASSUMPTION: docs/assumptions.md §C3-3.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import TenantContext
from src.modules.dealer_portal.models import (
    AVAILABILITY_IN_STOCK_AT,
    AvailabilityBand,
    DealerOffer,
    OfferScopeKind,
)
from src.modules.products.models import ItemCategory, PriceType

_SPECIFICITY: dict[str, int] = {
    OfferScopeKind.ITEM: 3,
    OfferScopeKind.BRAND: 2,
    OfferScopeKind.CATEGORY: 1,
    OfferScopeKind.ALL: 0,
}


@dataclass(frozen=True, slots=True)
class OfferFacts:
    """One offer, already loaded, with nothing the matcher does not need."""

    id: uuid.UUID
    code: str
    title: str
    scope_kind: str
    category_id: uuid.UUID | None
    brand_id: uuid.UUID | None
    item_id: uuid.UUID | None
    audience_price_type: str | None
    customer_id: uuid.UUID | None
    min_quantity: Decimal
    discount_percentage: Decimal
    valid_from: date | None
    valid_to: date | None
    is_active: bool
    sort_order: int


def availability_band(available: Decimal) -> AvailabilityBand:
    """Turn a live free quantity into a band. The quantity itself is not returned."""
    if available >= AVAILABILITY_IN_STOCK_AT:
        return AvailabilityBand.IN_STOCK
    if available > 0:
        return AvailabilityBand.LIMITED
    return AvailabilityBand.MADE_TO_ORDER


def ancestor_sets(
    rows: Sequence[tuple[uuid.UUID, uuid.UUID | None]],
) -> dict[uuid.UUID, frozenset[uuid.UUID]]:
    """Each category id to itself and its ancestors.

    A category offer applies to the named category and everything filed under
    it, so an item matches when the offer's category is in this set.
    """
    parent = dict(rows)
    cache: dict[uuid.UUID, frozenset[uuid.UUID]] = {}

    def walk(category_id: uuid.UUID) -> frozenset[uuid.UUID]:
        cached = cache.get(category_id)
        if cached is not None:
            return cached
        seen: list[uuid.UUID] = []
        guard: set[uuid.UUID] = set()
        current: uuid.UUID | None = category_id
        while current is not None and current not in guard:
            guard.add(current)
            seen.append(current)
            current = parent.get(current)
        found = frozenset(seen)
        cache[category_id] = found
        return found

    return {category_id: walk(category_id) for category_id in parent}


def select_best_offer(
    offers: Sequence[OfferFacts],
    *,
    item_id: uuid.UUID,
    category_ids: frozenset[uuid.UUID],
    brand_id: uuid.UUID | None,
    quantity: Decimal,
    price_type: PriceType | str,
    customer_id: uuid.UUID | None,
    on_date: date,
) -> OfferFacts | None:
    """The single best offer for one line, or none.

    Highest percentage wins. A tie breaks toward the more specific scope, then
    the lower ``sort_order``, then the smaller id so the choice cannot drift
    between processes. ASSUMPTION: docs/assumptions.md §C3-3.
    """
    channel = str(price_type)
    applicable: list[OfferFacts] = []
    for offer in offers:
        if not offer.is_active:
            continue
        if offer.valid_from is not None and on_date < offer.valid_from:
            continue
        if offer.valid_to is not None and on_date > offer.valid_to:
            continue
        if quantity < offer.min_quantity:
            continue
        if offer.audience_price_type is not None and offer.audience_price_type != channel:
            continue
        if offer.customer_id is not None and offer.customer_id != customer_id:
            continue
        if not _scope_matches(offer, item_id, category_ids, brand_id):
            continue
        applicable.append(offer)
    if not applicable:
        return None
    return min(
        applicable,
        key=lambda offer: (
            -offer.discount_percentage,
            -_SPECIFICITY.get(offer.scope_kind, 0),
            offer.sort_order,
            offer.id.int,
        ),
    )


def _scope_matches(
    offer: OfferFacts,
    item_id: uuid.UUID,
    category_ids: frozenset[uuid.UUID],
    brand_id: uuid.UUID | None,
) -> bool:
    kind = offer.scope_kind
    if kind == OfferScopeKind.ALL:
        return True
    if kind == OfferScopeKind.ITEM:
        return offer.item_id == item_id
    if kind == OfferScopeKind.BRAND:
        return brand_id is not None and offer.brand_id == brand_id
    if kind == OfferScopeKind.CATEGORY:
        return offer.category_id is not None and offer.category_id in category_ids
    return False


def facts_from(row: DealerOffer) -> OfferFacts:
    return OfferFacts(
        id=row.id,
        code=row.code,
        title=row.title,
        scope_kind=str(row.scope_kind),
        category_id=row.category_id,
        brand_id=row.brand_id,
        item_id=row.item_id,
        audience_price_type=row.audience_price_type,
        customer_id=row.customer_id,
        min_quantity=Decimal(row.min_quantity),
        discount_percentage=Decimal(row.discount_percentage),
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        is_active=row.is_active,
        sort_order=row.sort_order,
    )


async def load_pricing_offers(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    on_date: date,
    price_type: PriceType | str,
    customer_id: uuid.UUID,
) -> tuple[tuple[OfferFacts, ...], dict[uuid.UUID, frozenset[uuid.UUID]]]:
    """Active offers this customer could use on ``on_date``, plus the category tree.

    Two statements for a whole order. Quantity and the item's own category or
    brand are applied later, per line, in memory.
    """
    channel = str(price_type)
    offer_stmt = select(DealerOffer).where(
        DealerOffer.company_id == tenant.company_id,
        DealerOffer.deleted_at.is_(None),
        DealerOffer.is_active.is_(True),
        or_(DealerOffer.valid_from.is_(None), DealerOffer.valid_from <= on_date),
        or_(DealerOffer.valid_to.is_(None), DealerOffer.valid_to >= on_date),
        or_(
            DealerOffer.audience_price_type.is_(None),
            DealerOffer.audience_price_type == channel,
        ),
        or_(DealerOffer.customer_id.is_(None), DealerOffer.customer_id == customer_id),
    )
    offers = tuple(facts_from(row) for row in (await session.execute(offer_stmt)).scalars().all())
    category_stmt = select(ItemCategory.id, ItemCategory.parent_id).where(
        ItemCategory.company_id == tenant.company_id,
        ItemCategory.deleted_at.is_(None),
    )
    rows = list((await session.execute(category_stmt)).all())
    return offers, ancestor_sets([(row.id, row.parent_id) for row in rows])


__all__ = [
    "OfferFacts",
    "ancestor_sets",
    "availability_band",
    "facts_from",
    "load_pricing_offers",
    "select_best_offer",
]
