"""The dealer portal: catalogue, cart, wishlist, checkout and self-service.

Every method serves the dealer named by the tenant's ``customer_id`` - never a
request-supplied id. Another dealer's order, consignment or statement is "not
found", never "forbidden": confirming it exists would itself be the disclosure.

Prices are computed live, never stored: a cart line carries a quantity, and the
channel price plus the best scheme are resolved on every read, so a price-list
change reaches the next preview without a rewrite. Orders are placed by calling
``sales.service.create_order`` with no unit price and no discount; the server
prices each line and records the scheme it applied.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today
from src.core.database import transaction
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    AuthorizationError,
    BusinessRuleError,
    ConflictError,
    DuplicateError,
    ErrorCode,
    NotFoundError,
    ValidationError,
)
from src.core.pagination import OffsetPagination, PageParams, SortParams
from src.core.permissions import Permission
from src.core.types import to_money, to_quantity, to_unit_price
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit
from src.modules.customers.models import Customer
from src.modules.customers.service import evaluate_credit
from src.modules.dealer_portal.models import (
    CATALOGUE_MAX_IMAGES,
    CATALOGUE_PRICE_QUANTITY,
    MAX_CART_LINES,
    TRACKING_EVENT_LIMIT,
    DealerCart,
    DealerCartLine,
    DealerWishlistItem,
    OfferScopeKind,
)
from src.modules.dealer_portal.pricing import (
    OfferFacts,
    availability_band,
    load_pricing_offers,
    select_best_offer,
)
from src.modules.dealer_portal.repository import (
    CatalogueRepository,
    DealerCartLineRepository,
    DealerWishlistRepository,
)
from src.modules.dealer_portal.repository import (
    DealerCartRepository as CartRepository,
)
from src.modules.dealer_portal.schemas import (
    DealerAddressRead,
    DealerBrandRead,
    DealerCartLineRead,
    DealerCartRead,
    DealerCartWarning,
    DealerCatalogItem,
    DealerCatalogItemDetail,
    DealerCategoryNode,
    DealerCheckoutRequest,
    DealerContactRead,
    DealerCreditPosition,
    DealerDispatchSummary,
    DealerOfferSnippet,
    DealerOrderDetail,
    DealerOrderFilters,
    DealerOrderLineRead,
    DealerOrderSummary,
    DealerOutstanding,
    DealerPortalMe,
    DealerPriceQuote,
    DealerPriceSlab,
    DealerSalesPerson,
    DealerTracking,
    DealerTrackingEvent,
    DealerVariantRead,
    DealerWishlistRead,
)
from src.modules.dispatch.models import Dispatch
from src.modules.dispatch.repository import DispatchRepository, DispatchTrackingEventRepository
from src.modules.finance.models import PartyType
from src.modules.finance.schemas import LedgerStatement, StatementFilters
from src.modules.finance.service import ageing_report, party_statement
from src.modules.notifications.models import NotificationSeverity, NotificationType
from src.modules.notifications.repository import NotificationRepository
from src.modules.notifications.service import notify
from src.modules.products.models import Item, ItemPrice, ItemVariant, PriceType
from src.modules.products.service import ITEM_MASTER_FALLBACK_FIELD, select_price_row
from src.modules.sales import service as sales_service
from src.modules.sales.models import (
    SalesOrder,
    SalesOrderLineStatus,
    SalesOrderStatus,
)
from src.modules.sales.repository import (
    OrderPartyRepository,
    PriceListRepository,
    SalesOrderLineRepository,
    SalesOrderRepository,
)
from src.modules.sales.schemas import SalesOrderCreate, SalesOrderFilters, SalesOrderLineIn
from src.modules.users.models import User
from src.modules.warehouses.repository import WarehouseRepository

#: Sort fields the catalogue list accepts in SQL. ``price`` is refused with a
#: 422 naming these, because a channel price is a slab ranking plus a fallback
#: and cannot be ordered in the database (docs/assumptions.md §C3-9).
CATALOGUE_SORT_FIELDS = ("code", "name")

AUDIT_DEALER_CHECKOUT = "dealer_checkout"


@dataclass(frozen=True, slots=True)
class DealerContext:
    """The dealer's pricing facts, loaded once per catalogue read."""

    customer: Customer
    price_type: PriceType
    offers: tuple[OfferFacts, ...]
    ancestors: dict[uuid.UUID, frozenset[uuid.UUID]]


def offer_snippet(offer: OfferFacts) -> DealerOfferSnippet:
    return DealerOfferSnippet(
        id=offer.id,
        code=offer.code,
        title=offer.title,
        scope_kind=OfferScopeKind(offer.scope_kind),
        discount_percentage=offer.discount_percentage,
        min_quantity=offer.min_quantity,
    )


def credit_position(customer: Customer, order_value: Decimal) -> DealerCreditPosition:
    assessed = evaluate_credit(customer, order_value)
    return DealerCreditPosition(
        credit_limit=assessed.credit_limit,
        credit_days=assessed.credit_days,
        outstanding_amount=assessed.outstanding_amount,
        order_value=assessed.order_value,
        exposure=assessed.exposure,
        headroom=assessed.headroom,
        is_unlimited=assessed.is_unlimited,
        within_limit=assessed.within_limit,
        is_blocked=assessed.is_blocked,
        failure_reason=assessed.failure_reason,
    )


class DealerPortalService:
    """One request's view of the dealer portal. The dealer is the tenant's."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.catalog = CatalogueRepository(session, tenant)
        self.cart_repo = CartRepository(session, tenant)
        self.cart_line_repo = DealerCartLineRepository(session, tenant)
        self.wishlist_repo = DealerWishlistRepository(session, tenant)
        self.order_repo = SalesOrderRepository(session, tenant)
        self.order_line_repo = SalesOrderLineRepository(session, tenant)
        self.dispatch_repo = DispatchRepository(session, tenant)
        self.dispatch_event_repo = DispatchTrackingEventRepository(session, tenant)

    # --- context ----------------------------------------------------------

    async def _customer(self) -> Customer:
        """The dealer's own customer row: live, and of a dealer channel.

        Anything else is a token whose account should not exist - retired, or
        bound to a customer nobody may order for - so it is refused as scope,
        not reported as a missing record (docs/assumptions.md §C3-1).
        """
        if self.tenant.customer_id is None:  # pragma: no cover - the gate owns this
            raise AuthorizationError("A dealer account is required.", code=ErrorCode.SCOPE_DENIED)
        customer = await self.catalog.customer(self.tenant.customer_id)
        if (
            customer is None
            or customer.deleted_at is not None
            or not customer.is_active
            or str(customer.customer_type) not in ("DEALER", "DISTRIBUTOR")
        ):
            raise AuthorizationError(
                "This dealer account is not bound to an orderable customer.",
                code=ErrorCode.SCOPE_DENIED,
            )
        return customer

    async def _pricing(self, customer: Customer, on_date: date) -> DealerContext:
        price_type = PriceType(customer.price_list_type)
        offers, ancestors = await load_pricing_offers(
            self.session,
            self.tenant,
            on_date=on_date,
            price_type=price_type,
            customer_id=customer.id,
        )
        return DealerContext(
            customer=customer, price_type=price_type, offers=offers, ancestors=ancestors
        )

    def _category_ids(self, context: DealerContext, category_id: uuid.UUID | None) -> frozenset:
        if category_id is None:
            return frozenset()
        return context.ancestors.get(category_id, frozenset({category_id}))

    @staticmethod
    def _primary_image(image_ids: list[uuid.UUID] | None) -> uuid.UUID | None:
        """The first image id, if the item has any images at all."""
        if not image_ids:
            return None
        return image_ids[0]

    # --- price helpers ----------------------------------------------------

    @staticmethod
    def _channel_price(
        item: Item,
        rows: list[ItemPrice],
        quantity: Decimal,
        price_type: PriceType,
        variant: ItemVariant | None = None,
    ) -> Decimal | None:
        """The channel price for a quantity, or None when it is on request.

        A DISTRIBUTOR channel has no item-master column, so without a price
        row there is no price - reusing the dealer rate would quietly invoice
        the wrong party at the wrong rate (docs/assumptions.md §C3-2).
        """
        chosen = select_price_row(
            rows, quantity, variant_id=variant.id if variant is not None else None
        )
        if chosen is not None:
            delta = (
                Decimal(variant.price_delta)
                if variant is not None and chosen.variant_id is None
                else Decimal("0")
            )
            return to_unit_price(Decimal(chosen.price) + delta)
        fallback = ITEM_MASTER_FALLBACK_FIELD[price_type]
        if fallback is None:
            return None
        base = Decimal(getattr(item, fallback))
        return to_unit_price(base + (Decimal(variant.price_delta) if variant is not None else 0))

    def _best_offer(
        self,
        context: DealerContext,
        item: Item,
        quantity: Decimal,
    ) -> OfferFacts | None:
        return select_best_offer(
            context.offers,
            item_id=item.id,
            category_ids=self._category_ids(context, item.category_id),
            brand_id=item.brand_id,
            quantity=quantity,
            price_type=context.price_type,
            customer_id=context.customer.id,
            on_date=today(),
        )

    # --- me ---------------------------------------------------------------

    async def me(self) -> DealerPortalMe:
        customer = await self._customer()
        addresses = await self.catalog.addresses(customer.id)
        contact = await self.catalog.primary_contact(customer.id)
        sales_person: DealerSalesPerson | None = None
        if customer.assigned_sales_user_id is not None:
            user = await self.catalog.sales_person(customer.assigned_sales_user_id)
            if user is not None:
                sales_person = DealerSalesPerson(name=user.full_name, phone=user.phone)
        return DealerPortalMe(
            customer_id=customer.id,
            customer_code=customer.code,
            customer_name=customer.name,
            customer_type=str(customer.customer_type),
            price_list_type=str(customer.price_list_type),
            credit=credit_position(customer, Decimal("0")),
            addresses=[
                DealerAddressRead(
                    id=row.id,
                    address_type=str(row.address_type),
                    label=row.label,
                    address_line1=row.address_line1,
                    city=row.city,
                    state=row.state,
                    state_code=row.state_code,
                    postal_code=row.postal_code,
                    is_default=row.is_default,
                )
                for row in addresses
            ],
            primary_contact=(
                DealerContactRead(
                    name=contact.name,
                    designation=contact.designation,
                    phone=contact.phone,
                    email=contact.email,
                )
                if contact is not None
                else None
            ),
            sales_person=sales_person,
        )

    # --- catalogue --------------------------------------------------------

    async def categories(self) -> list[DealerCategoryNode]:
        rows = await self.catalog.categories_with_sellable()
        return [
            DealerCategoryNode(
                id=row.id, code=row.code, name=row.name, parent_id=row.parent_id, depth=row.depth
            )
            for row in rows
        ]

    async def brands(self) -> list[DealerBrandRead]:
        return [
            DealerBrandRead(id=row.id, code=row.code, name=row.name)
            for row in await self.catalog.brands_with_sellable()
        ]

    async def catalog_items(
        self,
        params: PageParams,
        *,
        search: str | None,
        category_id: uuid.UUID | None,
        brand_id: uuid.UUID | None,
        in_stock_only: bool,
        on_offer_only: bool,
        sort_by: str,
    ) -> tuple[list[DealerCatalogItem], OffsetPagination]:
        """One page of the dealer catalogue, priced and indicated.

        One query per concern per page: the page itself, the channel prices,
        the live availability, the schemes, the wishlist, the cart and the
        primary images. No await inside a loop (section 32).
        """
        if sort_by not in CATALOGUE_SORT_FIELDS:
            raise ValidationError(
                f"Catalogue sorting supports {', '.join(CATALOGUE_SORT_FIELDS)} only: "
                "a channel price is a slab ranking plus a fallback and cannot be "
                "ordered in the database.",
                details={"sort_by": sort_by, "allowed": list(CATALOGUE_SORT_FIELDS)},
            )
        customer = await self._customer()
        context = await self._pricing(customer, today())

        subtree: list[uuid.UUID] | None = None
        if category_id is not None:
            category = await self.catalog.category(category_id)
            if category is None:
                raise NotFoundError("Item category", category_id)
            subtree = await self.catalog.category_subtree_ids(category.path)

        rows, pagination = await self.catalog.page_items(
            params,
            search=search,
            category_ids=subtree,
            brand_id=brand_id,
            in_stock_only=in_stock_only,
            on_offer_only=on_offer_only,
            price_type=str(context.price_type),
            customer_id=customer.id,
            on_date=today(),
            sort_by=sort_by,
        )
        if not rows:
            return [], pagination

        item_ids = [row.id for row in rows]
        prices = await PriceListRepository(self.session, self.tenant).candidates_for_items(
            item_ids, price_type=context.price_type, variant_ids=[], on_date=today()
        )
        available = await self.catalog.available_by_item(item_ids)
        wishlist = await self.wishlist_repo.ids_for_user()
        wishlist_items = {item_id for item_id, _ in wishlist}
        cart = await self.cart_line_repo.quantities_for_user()
        images = await self.catalog.primary_images(item_ids, limit_per_item=1)

        items: list[DealerCatalogItem] = []
        for row in rows:
            price = self._channel_price(
                row, prices.get(row.id, []), CATALOGUE_PRICE_QUANTITY, context.price_type
            )
            best = (
                self._best_offer(context, row, CATALOGUE_PRICE_QUANTITY)
                if price is not None
                else None
            )
            if on_offer_only and best is None:
                # The SQL prefilter is coarse on CATEGORY scopes; the exact
                # match decides (docs/assumptions.md §C3-9).
                continue
            items.append(
                DealerCatalogItem(
                    id=row.id,
                    code=row.code,
                    name=row.name,
                    category_id=row.category_id,
                    brand_id=row.brand_id,
                    price=price,
                    price_on_request=price is None,
                    orderable=price is not None,
                    availability_band=availability_band(available.get(row.id, Decimal("0"))),
                    best_offer=offer_snippet(best) if best is not None else None,
                    in_wishlist=row.id in wishlist_items,
                    in_cart_quantity=cart.get((row.id, None), Decimal("0")),
                    primary_image_id=self._primary_image(images.get(row.id)),
                )
            )
        return items, pagination

    async def catalog_item(self, item_id: uuid.UUID) -> DealerCatalogItemDetail:
        customer = await self._customer()
        context = await self._pricing(customer, today())
        item = await self.catalog.sellable_item(item_id)
        if item is None:
            raise NotFoundError("Item", item_id)

        variants = await self.catalog.variants_of(item.id)
        prices = await PriceListRepository(self.session, self.tenant).candidates_for_items(
            [item.id],
            price_type=context.price_type,
            variant_ids=[variant.id for variant in variants],
            on_date=today(),
        )
        rows = prices.get(item.id, [])
        available = await self.catalog.available_by_item([item.id])
        wishlist = await self.wishlist_repo.ids_for_user()
        cart = await self.cart_line_repo.quantities_for_user()
        images = await self.catalog.primary_images([item.id], limit_per_item=CATALOGUE_MAX_IMAGES)

        price = self._channel_price(item, rows, CATALOGUE_PRICE_QUANTITY, context.price_type)
        offers = self._matching_offers(context, item, CATALOGUE_PRICE_QUANTITY)
        best = offers[0] if offers and price is not None else None
        return DealerCatalogItemDetail(
            id=item.id,
            code=item.code,
            name=item.name,
            category_id=item.category_id,
            brand_id=item.brand_id,
            price=price,
            price_on_request=price is None,
            orderable=price is not None,
            availability_band=availability_band(available.get(item.id, Decimal("0"))),
            best_offer=offer_snippet(best) if best is not None else None,
            in_wishlist=item.id in {item_id for item_id, _ in wishlist},
            in_cart_quantity=cart.get((item.id, None), Decimal("0")),
            primary_image_id=self._primary_image(images.get(item.id)),
            description=item.description,
            hsn_code=item.hsn_code,
            gst_rate=Decimal(item.gst_rate),
            can_return=item.can_return,
            variants=[
                DealerVariantRead(
                    id=variant.id,
                    code=variant.code,
                    price=self._channel_price(
                        item, rows, CATALOGUE_PRICE_QUANTITY, context.price_type, variant
                    ),
                    price_on_request=self._channel_price(
                        item, rows, CATALOGUE_PRICE_QUANTITY, context.price_type, variant
                    )
                    is None,
                    is_active=variant.is_active,
                )
                for variant in variants
            ],
            slabs=[
                DealerPriceSlab(
                    min_quantity=Decimal(row.min_quantity),
                    price=Decimal(row.price),
                    variant_id=row.variant_id,
                    valid_from=row.valid_from,
                    valid_to=row.valid_to,
                )
                for row in sorted(rows, key=lambda row: (Decimal(row.min_quantity), row.valid_from))
            ],
            offers=[offer_snippet(offer) for offer in offers] if price is not None else [],
            image_ids=images.get(item.id, []),
            # Item specifications and kit accessories are D5 masters, which do
            # not exist yet: the detail carries the shape with no rows rather
            # than inventing tables this package does not own
            # (docs/assumptions.md §C3-8).
            specifications=[],
            accessories=[],
        )

    def _matching_offers(
        self, context: DealerContext, item: Item, quantity: Decimal
    ) -> list[OfferFacts]:
        """Every applicable scheme, best first.

        The single-winner rule is reused by elimination: take the best, set it
        aside, and ask again. Offer counts per item are tiny; a second matcher
        that could disagree with the catalogue, the cart and checkout would
        cost more than the loop.
        """
        remaining = list(context.offers)
        matched: list[OfferFacts] = []
        while remaining:
            best = select_best_offer(
                remaining,
                item_id=item.id,
                category_ids=self._category_ids(context, item.category_id),
                brand_id=item.brand_id,
                quantity=quantity,
                price_type=context.price_type,
                customer_id=context.customer.id,
                on_date=today(),
            )
            if best is None:
                break
            matched.append(best)
            remaining = [offer for offer in remaining if offer.id != best.id]
        return matched

    async def price_quote(
        self, item_id: uuid.UUID, quantity: Decimal, variant_id: uuid.UUID | None
    ) -> DealerPriceQuote:
        if quantity <= 0:
            raise ValidationError("Quantity must be greater than zero to resolve a price.")
        customer = await self._customer()
        context = await self._pricing(customer, today())
        item = await self.catalog.sellable_item(item_id)
        if item is None:
            raise NotFoundError("Item", item_id)

        variant: ItemVariant | None = None
        if variant_id is not None:
            variants = await self.catalog.variants_by_ids([variant_id])
            variant = variants.get(variant_id)
            if variant is None or variant.item_id != item.id:
                raise NotFoundError("Item variant", variant_id)

        prices = await PriceListRepository(self.session, self.tenant).candidates_for_items(
            [item.id],
            price_type=context.price_type,
            variant_ids=[variant_id] if variant_id is not None else [],
            on_date=today(),
        )
        gross = self._channel_price(
            item, prices.get(item.id, []), quantity, context.price_type, variant
        )
        if gross is None:
            return DealerPriceQuote(
                item_id=item.id,
                variant_id=variant_id,
                quantity=to_quantity(quantity),
                gross_unit=None,
                offer=None,
                discount_percentage=Decimal("0"),
                net_unit=None,
                tax_rate=Decimal(item.gst_rate),
                tax_estimate=None,
                price_on_request=True,
            )
        best = select_best_offer(
            context.offers,
            item_id=item.id,
            category_ids=self._category_ids(context, item.category_id),
            brand_id=item.brand_id,
            quantity=quantity,
            price_type=context.price_type,
            customer_id=customer.id,
            on_date=today(),
        )
        discount = Decimal(best.discount_percentage) if best is not None else Decimal("0")
        net = to_unit_price(gross * (Decimal("100") - discount) / Decimal("100"))
        taxable = to_money(net * to_quantity(quantity))
        tax = to_money(taxable * Decimal(item.gst_rate) / Decimal("100"))
        return DealerPriceQuote(
            item_id=item.id,
            variant_id=variant_id,
            quantity=to_quantity(quantity),
            gross_unit=gross,
            offer=offer_snippet(best) if best is not None else None,
            discount_percentage=discount,
            net_unit=net,
            tax_rate=Decimal(item.gst_rate),
            tax_estimate=tax,
            price_on_request=False,
        )

    # --- cart ---------------------------------------------------------------

    async def _require_cart(self, *, lock: bool = False) -> DealerCart:
        cart = await self.cart_repo.lock_for_user() if lock else await self.cart_repo.for_user()
        if cart is None:
            raise NotFoundError("Dealer cart", self.tenant.user_id)
        return cart

    async def _get_or_create_cart(self, customer: Customer) -> DealerCart:
        cart = await self.cart_repo.for_user()
        if cart is not None:
            return cart
        cart = DealerCart(
            company_id=self.tenant.company_id,
            user_id=self.tenant.user_id,
            customer_id=customer.id,
        )
        self.cart_repo.add(cart)
        await self.session.flush()
        return cart

    async def _validate_grain(
        self, item_id: uuid.UUID, variant_id: uuid.UUID | None
    ) -> tuple[Item, ItemVariant | None]:
        """The item is sellable and live; the variant is its own and live."""
        item = await self.catalog.sellable_item(item_id)
        if item is None:
            raise NotFoundError("Item", item_id)
        variant: ItemVariant | None = None
        if variant_id is not None:
            variants = await self.catalog.variants_by_ids([variant_id])
            variant = variants.get(variant_id)
            if variant is None or variant.item_id != item.id:
                raise NotFoundError("Item variant", variant_id)
            if not variant.is_active:
                raise BusinessRuleError(
                    f"Variant {variant.code} of {item.code} is inactive.",
                    details={"item_id": str(item.id), "variant_id": str(variant.id)},
                )
        return item, variant

    async def cart(self) -> DealerCartRead:
        """The priced preview: live channel prices, schemes, tax and credit."""
        customer = await self._customer()
        cart = await self.cart_repo.for_user()
        if cart is None:
            return DealerCartRead(
                cart_id=None,
                cart_version=None,
                lines=[],
                subtotal=Decimal("0"),
                discount_amount=Decimal("0"),
                tax_amount=Decimal("0"),
                total_amount=Decimal("0"),
                credit=credit_position(customer, Decimal("0")),
                warnings=[],
            )
        return await self._price_cart(customer, cart)

    async def _price_cart(self, customer: Customer, cart: DealerCart) -> DealerCartRead:
        """Price every line live. One query per concern for the whole cart."""
        context = await self._pricing(customer, today())
        lines = await self.cart_line_repo.for_cart(cart.id)
        items = await self.catalog.items_by_ids([line.item_id for line in lines])
        variants = await self.catalog.variants_by_ids(
            [line.item_variant_id for line in lines if line.item_variant_id is not None]
        )
        prices = await PriceListRepository(self.session, self.tenant).candidates_for_items(
            [line.item_id for line in lines],
            price_type=context.price_type,
            variant_ids=[
                line.item_variant_id for line in lines if line.item_variant_id is not None
            ],
            on_date=today(),
        )
        parties = OrderPartyRepository(self.session, self.tenant)
        shipping = await parties.default_shipping_address(customer.id)
        seller_state = await parties.seller_state_code(None)
        interstate = sales_service.resolve_tax_treatment(
            seller_state, shipping.state_code if shipping is not None else None
        )

        read_lines: list[DealerCartLineRead] = []
        warnings: list[DealerCartWarning] = []
        amounts: list[sales_service.LineAmounts] = []
        for line in lines:
            item = items.get(line.item_id)
            variant = (
                variants.get(line.item_variant_id) if line.item_variant_id is not None else None
            )
            line_warnings: list[DealerCartWarning] = []
            unit: Decimal | None = None
            discount = Decimal("0")
            offer: OfferFacts | None = None
            if (
                item is None
                or not item.is_active
                or not item.can_sell
                or (variant is not None and (variant.item_id != item.id or not variant.is_active))
            ):
                line_warnings.append(
                    DealerCartWarning(
                        code="INACTIVE_ITEM",
                        message="This item is no longer sellable and will be skipped at checkout.",
                        line_id=line.id,
                    )
                )
            else:
                unit = self._channel_price(
                    item,
                    prices.get(item.id, []),
                    Decimal(line.quantity),
                    context.price_type,
                    variant,
                )
                if unit is None:
                    line_warnings.append(
                        DealerCartWarning(
                            code="PRICE_ON_REQUEST",
                            message=(
                                f"{item.code} has no {context.price_type} price and "
                                "cannot be ordered yet."
                            ),
                            line_id=line.id,
                        )
                    )
                else:
                    offer = self._best_offer_at(context, item, Decimal(line.quantity))
                    if offer is not None:
                        discount = Decimal(offer.discount_percentage)
                    if item.min_order_quantity and Decimal(line.quantity) < Decimal(
                        item.min_order_quantity
                    ):
                        line_warnings.append(
                            DealerCartWarning(
                                code="BELOW_MOQ",
                                message=(
                                    f"{item.code} is below its minimum order quantity "
                                    f"of {item.min_order_quantity}."
                                ),
                                line_id=line.id,
                            )
                        )
            if unit is not None:
                computed = sales_service.compute_line_amounts(
                    quantity=Decimal(line.quantity),
                    unit_price=unit,
                    tax_rate=Decimal(item.gst_rate) if item is not None else Decimal("0"),
                    is_interstate=interstate,
                    discount_percentage=discount or None,
                    discount_amount=None,
                )
                amounts.append(computed)
                net: Decimal | None = computed.taxable_amount
                tax: Decimal | None = computed.tax_amount
                total: Decimal | None = computed.line_total
            else:
                net = tax = total = None
            warnings.extend(line_warnings)
            read_lines.append(
                DealerCartLineRead(
                    id=line.id,
                    item_id=line.item_id,
                    item_code=item.code if item is not None else "?",
                    item_name=item.name if item is not None else "?",
                    item_variant_id=line.item_variant_id,
                    quantity=Decimal(line.quantity),
                    unit_price=unit,
                    discount_percentage=discount,
                    offer=offer_snippet(offer) if offer is not None else None,
                    net_amount=net,
                    tax_amount=tax,
                    line_total=total,
                    price_on_request=unit is None,
                    warnings=line_warnings,
                )
            )

        totals = sales_service.sum_order_totals(amounts)
        credit = credit_position(customer, totals.total_amount)
        if credit.failure_reason is not None:
            warnings.append(
                DealerCartWarning(code="OVER_CREDIT_LIMIT", message=credit.failure_reason)
            )
        return DealerCartRead(
            cart_id=cart.id,
            cart_version=cart.version,
            lines=read_lines,
            subtotal=totals.subtotal,
            discount_amount=totals.discount_amount,
            tax_amount=totals.tax_amount,
            total_amount=totals.total_amount,
            credit=credit,
            warnings=warnings,
        )

    def _best_offer_at(
        self, context: DealerContext, item: Item, quantity: Decimal
    ) -> OfferFacts | None:
        return select_best_offer(
            context.offers,
            item_id=item.id,
            category_ids=self._category_ids(context, item.category_id),
            brand_id=item.brand_id,
            quantity=quantity,
            price_type=context.price_type,
            customer_id=context.customer.id,
            on_date=today(),
        )

    async def add_cart_line(
        self, item_id: uuid.UUID, variant_id: uuid.UUID | None, quantity: Decimal
    ) -> DealerCartRead:
        customer = await self._customer()
        await self._validate_grain(item_id, variant_id)
        async with transaction(self.session):
            cart = await self._get_or_create_cart(customer)
            if await self.cart_line_repo.find_grain(cart.id, item_id, variant_id) is not None:
                raise DuplicateError(
                    "This item is already in the cart. Change its quantity instead.",
                    details={
                        "item_id": str(item_id),
                        "variant_id": str(variant_id) if variant_id else None,
                    },
                )
            if await self.cart_line_repo.count_for_cart(cart.id) >= MAX_CART_LINES:
                raise BusinessRuleError(
                    f"A cart holds at most {MAX_CART_LINES} lines.",
                    details={"max_cart_lines": MAX_CART_LINES},
                )
            self.cart_line_repo.add(
                DealerCartLine(
                    company_id=self.tenant.company_id,
                    cart_id=cart.id,
                    item_id=item_id,
                    item_variant_id=variant_id,
                    quantity=to_quantity(quantity),
                )
            )
            await self.session.flush()
            self.cart_repo.touch(cart)
            await self.session.flush()
        return await self.cart()

    async def update_cart_line(self, line_id: uuid.UUID, quantity: Decimal) -> DealerCartRead:
        # The binding check: a token whose customer went inactive or
        # non-dealer is refused before it touches the cart.
        await self._customer()
        async with transaction(self.session):
            cart = await self._require_cart(lock=True)
            line = await self.cart_line_repo.get_in_cart(cart.id, line_id)
            if line is None:
                raise NotFoundError("Dealer cart line", line_id)
            await self._validate_grain(line.item_id, line.item_variant_id)
            line.quantity = to_quantity(quantity)
            self.cart_line_repo.touch(line)
            self.cart_repo.touch(cart)
            await self.session.flush()
        return await self.cart()

    async def remove_cart_line(self, line_id: uuid.UUID) -> DealerCartRead:
        await self._customer()
        async with transaction(self.session):
            cart = await self._require_cart(lock=True)
            line = await self.cart_line_repo.get_in_cart(cart.id, line_id)
            if line is None:
                raise NotFoundError("Dealer cart line", line_id)
            await self.session.delete(line)
            self.cart_repo.touch(cart)
            await self.session.flush()
        return await self.cart()

    async def clear_cart(self) -> DealerCartRead:
        await self._customer()
        async with transaction(self.session):
            cart = await self._require_cart(lock=True)
            await self.cart_line_repo.delete_for_cart(cart.id)
            self.cart_repo.touch(cart)
            await self.session.flush()
        return await self.cart()

    # --- wishlist -----------------------------------------------------------

    async def wishlist(self) -> list[DealerWishlistRead]:
        customer = await self._customer()
        context = await self._pricing(customer, today())
        rows = await self.wishlist_repo.for_user()
        if not rows:
            return []
        items = await self.catalog.items_by_ids([row.item_id for row in rows])
        prices = await PriceListRepository(self.session, self.tenant).candidates_for_items(
            [row.item_id for row in rows],
            price_type=context.price_type,
            variant_ids=[row.item_variant_id for row in rows if row.item_variant_id is not None],
            on_date=today(),
        )
        available = await self.catalog.available_by_item([row.item_id for row in rows])
        read: list[DealerWishlistRead] = []
        for row in rows:
            item = items.get(row.item_id)
            if item is None or not item.is_active or not item.can_sell:
                continue
            price = self._channel_price(
                item, prices.get(item.id, []), CATALOGUE_PRICE_QUANTITY, context.price_type
            )
            read.append(
                DealerWishlistRead(
                    id=row.id,
                    item_id=row.item_id,
                    item_code=item.code,
                    item_name=item.name,
                    item_variant_id=row.item_variant_id,
                    price=price,
                    price_on_request=price is None,
                    availability_band=availability_band(available.get(row.item_id, Decimal("0"))),
                )
            )
        return read

    async def add_wishlist_item(
        self, item_id: uuid.UUID, variant_id: uuid.UUID | None
    ) -> DealerWishlistRead:
        customer = await self._customer()
        await self._validate_grain(item_id, variant_id)
        async with transaction(self.session):
            if await self.wishlist_repo.find_grain(item_id, variant_id) is not None:
                raise DuplicateError(
                    "This item is already in the wishlist.",
                    details={"item_id": str(item_id)},
                )
            row = DealerWishlistItem(
                company_id=self.tenant.company_id,
                user_id=self.tenant.user_id,
                customer_id=customer.id,
                item_id=item_id,
                item_variant_id=variant_id,
            )
            self.wishlist_repo.add(row)
            await self.session.flush()
            entry_id = row.id
        return await self.wishlist_list_after(entry_id)

    async def wishlist_list_after(self, entry_id: uuid.UUID) -> DealerWishlistRead:
        for row in await self.wishlist():
            if row.id == entry_id:
                return row
        raise NotFoundError("Dealer wishlist item", entry_id)  # pragma: no cover

    async def remove_wishlist_item(self, entry_id: uuid.UUID) -> None:
        await self._customer()
        async with transaction(self.session):
            row = await self.wishlist_repo.get_for_user(entry_id)
            if row is None:
                raise NotFoundError("Dealer wishlist item", entry_id)
            await self.session.delete(row)
            await self.session.flush()

    async def move_wishlist_to_cart(self, entry_id: uuid.UUID, quantity: Decimal) -> DealerCartRead:
        """Save a wishlist row into the cart. Needs the order right."""
        customer = await self._customer()
        async with transaction(self.session):
            row = await self.wishlist_repo.get_for_user(entry_id)
            if row is None:
                raise NotFoundError("Dealer wishlist item", entry_id)
            await self._validate_grain(row.item_id, row.item_variant_id)
            cart = await self._get_or_create_cart(customer)
            existing = await self.cart_line_repo.find_grain(
                cart.id, row.item_id, row.item_variant_id
            )
            if existing is not None:
                existing.quantity = to_quantity(Decimal(existing.quantity) + quantity)
                self.cart_line_repo.touch(existing)
            else:
                if await self.cart_line_repo.count_for_cart(cart.id) >= MAX_CART_LINES:
                    raise BusinessRuleError(
                        f"A cart holds at most {MAX_CART_LINES} lines.",
                        details={"max_cart_lines": MAX_CART_LINES},
                    )
                self.cart_line_repo.add(
                    DealerCartLine(
                        company_id=self.tenant.company_id,
                        cart_id=cart.id,
                        item_id=row.item_id,
                        item_variant_id=row.item_variant_id,
                        quantity=to_quantity(quantity),
                    )
                )
            await self.session.delete(row)
            self.cart_repo.touch(cart)
            await self.session.flush()
        return await self.cart()

    # --- checkout -----------------------------------------------------------

    async def checkout(
        self, principal: Principal, payload: DealerCheckoutRequest
    ) -> DealerOrderDetail:
        """Turn the cart into a DRAFT sales order for sales review.

        One outermost transaction: lock the cart and check its version,
        re-validate every line, resolve the warehouse and the addresses, build
        the order with no client prices or discounts, clear the cart, audit and
        notify - or nothing at all happens (docs/assumptions.md §C3-6).
        """
        async with transaction(self.session):
            cart = await self.cart_repo.lock_for_user()
            if cart is None:
                raise BusinessRuleError("The cart is empty.")
            if cart.version != payload.cart_version:
                raise ConflictError(
                    "The cart changed since it was last read. Refresh it and try again.",
                    details={"cart_version": cart.version},
                )
            lines = await self.cart_line_repo.for_cart(cart.id)
            if not lines:
                raise BusinessRuleError("The cart is empty.")

            customer = await self._customer()
            items = await self.catalog.items_by_ids([line.item_id for line in lines])
            variants = await self.catalog.variants_by_ids(
                [line.item_variant_id for line in lines if line.item_variant_id is not None]
            )
            for line in lines:
                item = items.get(line.item_id)
                if item is None or not item.is_active or not item.can_sell:
                    raise BusinessRuleError(
                        "The cart holds an item that is no longer sellable. "
                        "Refresh the cart and try again.",
                        details={"item_id": str(line.item_id)},
                    )
                variant = (
                    variants.get(line.item_variant_id) if line.item_variant_id is not None else None
                )
                if variant is not None and (variant.item_id != item.id or not variant.is_active):
                    raise BusinessRuleError(
                        "The cart holds a variant that is no longer sellable. "
                        "Refresh the cart and try again.",
                        details={"item_variant_id": str(line.item_variant_id)},
                    )

            warehouse = await WarehouseRepository(self.session, self.tenant).default_warehouse()
            if warehouse is None:
                raise BusinessRuleError(
                    "No default warehouse is configured, so a dealer order cannot "
                    "be fulfilled yet.",
                    details={"missing": "warehouses.is_default"},
                )

            parties = OrderPartyRepository(self.session, self.tenant)
            requested = [
                address_id
                for address_id in (payload.shipping_address_id, payload.billing_address_id)
                if address_id is not None
            ]
            found = await parties.addresses_of(customer.id, requested)
            for address_id in requested:
                # Scoped to the dealer: a foreign address is "not found".
                if address_id not in found:
                    raise NotFoundError("Customer address", address_id)

            order_lines = [
                SalesOrderLineIn(
                    line_number=index,
                    item_id=line.item_id,
                    item_variant_id=line.item_variant_id,
                    quantity=Decimal(line.quantity),
                )
                for index, line in enumerate(lines, start=1)
            ]
            order, created_lines = await sales_service.create_order(
                self.session,
                self.tenant,
                principal,
                SalesOrderCreate(
                    customer_id=customer.id,
                    customer_po_number=payload.customer_po_number,
                    required_date=payload.required_date,
                    warehouse_id=warehouse.id,
                    billing_address_id=payload.billing_address_id,
                    shipping_address_id=payload.shipping_address_id,
                    notes=payload.notes,
                    lines=order_lines,
                ),
                source="DEALER_PORTAL",
            )
            # Plain values from here on: the objects above belong to a
            # transaction that later steps share, and a rolled-back savepoint
            # would expire them.
            order_id = order.id
            order_code = order.code

            removed = await self.cart_line_repo.delete_for_cart(cart.id)
            self.cart_repo.touch(cart)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_DEALER_CHECKOUT,
                entity_id=order_id,
                entity_code=order_code,
                summary=(
                    f"Dealer checkout {order_code} for {customer.code}: "
                    f"{len(created_lines)} line(s), {order.currency} {order.total_amount}"
                ),
                user_id=self.tenant.user_id,
                new_values={
                    "sales_order_id": str(order_id),
                    "customer_id": str(customer.id),
                    "line_count": len(created_lines),
                    "cart_lines_removed": removed,
                    "total_amount": str(order.total_amount),
                },
            )

            await self._notify_new_order(customer, order, len(created_lines))
            await self.session.flush()

        return await self.order(order_id)

    async def _notify_new_order(
        self, customer: Customer, order: SalesOrder, line_count: int
    ) -> None:
        """Raise "New order request" for the assigned salesperson, else approvers.

        In the checkout transaction, like every state-change notification: the
        order and its alert commit together or not at all. Recipients are
        internal users only - never the dealer, whatever they hold
        (docs/assumptions.md §C3-6).
        """
        recipients = await self._live_internal_users(
            [customer.assigned_sales_user_id] if customer.assigned_sales_user_id else []
        )
        if not recipients:
            holders = await NotificationRepository(
                self.session, self.tenant
            ).user_ids_with_permission(str(Permission.SALES_ORDER_APPROVE))
            recipients = await self._live_internal_users(holders)
        recipients = [user_id for user_id in recipients if user_id != self.tenant.user_id]
        await notify(
            self.session,
            company_id=self.tenant.company_id,
            user_ids=recipients,
            notification_type=NotificationType.DEALER_ORDER_REQUEST_RECEIVED,
            title=f"New order request from {customer.name}",
            body=(
                f"{order.code}: {line_count} line(s), "
                f"{order.currency} {order.total_amount}. Raised through the dealer portal."
            ),
            severity=NotificationSeverity.INFO,
            entity_type="sales_order",
            entity_id=order.id,
            action_url=f"/sales/orders/{order.id}",
            actor_id=self.tenant.user_id,
        )

    async def _live_internal_users(self, user_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        """Those ids that are active internal staff - never portal accounts."""
        ids = list(dict.fromkeys(user_id for user_id in user_ids if user_id is not None))
        if not ids:
            return []
        stmt = (
            select(User.id)
            .where(
                User.company_id == self.tenant.company_id,
                User.id.in_(ids),
                User.vendor_id.is_(None),
                User.customer_id.is_(None),
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
            .order_by(User.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    # --- orders ---------------------------------------------------------------

    def _order_summary(
        self, order: SalesOrder, line_counts: dict[uuid.UUID, int]
    ) -> DealerOrderSummary:
        return DealerOrderSummary(
            id=order.id,
            code=order.code,
            status=SalesOrderStatus(order.status),
            order_date=order.order_date,
            required_date=order.required_date,
            currency=order.currency,
            total_amount=Decimal(order.total_amount),
            source=str(order.source),
            line_count=line_counts.get(order.id, 0),
            created_at=order.created_at,
        )

    async def orders(
        self, filters: DealerOrderFilters, params: PageParams
    ) -> tuple[list[DealerOrderSummary], OffsetPagination]:
        """The dealer's own orders. The customer is forced by the tenant scope."""
        await self._customer()
        rows, pagination = await self.order_repo.list_page_filtered(
            SalesOrderFilters(
                search=filters.search, status=filters.status, open_only=filters.open_only
            ),
            params,
            SortParams(),
        )
        counts = await self.catalog.order_line_counts([row.id for row in rows])
        return [self._order_summary(row, counts) for row in rows], pagination

    async def order(self, order_id: uuid.UUID) -> DealerOrderDetail:
        """One of the dealer's orders. A foreign order is 404."""
        await self._customer()
        order = await self.order_repo.get_or_404(order_id)
        return await self._order_detail(order)

    async def _order_detail(self, order: SalesOrder) -> DealerOrderDetail:
        lines = await self.order_line_repo.list_for_order(order.id)
        items = await self.catalog.items_by_ids([line.item_id for line in lines])
        counts = await self.catalog.order_line_counts([order.id])
        summary = self._order_summary(order, counts)
        read_lines: list[DealerOrderLineRead] = []
        for line in lines:
            item = items.get(line.item_id)
            cancelled = SalesOrderLineStatus(line.status) is SalesOrderLineStatus.CANCELLED
            read_lines.append(
                DealerOrderLineRead(
                    id=line.id,
                    line_number=line.line_number,
                    item_id=line.item_id,
                    item_code=item.code if item is not None else "?",
                    item_name=item.name if item is not None else (line.description or "?"),
                    item_variant_id=line.item_variant_id,
                    quantity=Decimal(line.quantity),
                    unit_price=Decimal(line.unit_price),
                    discount_percentage=Decimal(line.discount_percentage),
                    offer_id=line.offer_id,
                    tax_amount=Decimal(line.tax_amount),
                    line_total=Decimal(line.line_total),
                    allocated_quantity=Decimal(line.allocated_quantity),
                    dispatched_quantity=Decimal(line.dispatched_quantity),
                    invoiced_quantity=Decimal(line.invoiced_quantity),
                    allocation_status=sales_service.line_status_for(
                        quantity=Decimal(line.quantity),
                        allocated_quantity=Decimal(line.allocated_quantity),
                        dispatched_quantity=Decimal(line.dispatched_quantity),
                        cancelled=cancelled,
                    ),
                )
            )
        return DealerOrderDetail(
            **summary.model_dump(),
            customer_po_number=order.customer_po_number,
            subtotal=Decimal(order.subtotal),
            discount_amount=Decimal(order.discount_amount),
            tax_amount=Decimal(order.tax_amount),
            notes=order.notes,
            lines=read_lines,
        )

    async def cancel_order(self, order_id: uuid.UUID, reason: str) -> DealerOrderDetail:
        """Cancel the dealer's own DRAFT order, with a reason.

        The scoped read makes a foreign order 404; the state machine refuses
        anything past DRAFT. A portal draft is cancelled, never deleted, so the
        dealer and the desk see the same trail (docs/assumptions.md §C3-6).
        """
        await self._customer()
        order = await self.order_repo.get_or_404(order_id)
        if SalesOrderStatus(order.status) is not SalesOrderStatus.DRAFT:
            raise BusinessRuleError(
                f"{order.code} is {order.status} and can no longer be cancelled "
                "through the portal.",
                details={"order_id": str(order.id), "status": str(order.status)},
            )
        async with transaction(self.session):
            await sales_service.cancel_order(self.session, self.tenant, order.id, reason=reason)
        return await self.order(order_id)

    # --- dispatches and tracking ------------------------------------------------

    @staticmethod
    def _dispatch_summary(
        row: Dispatch, transporter_name: str | None = None
    ) -> DealerDispatchSummary:
        return DealerDispatchSummary(
            id=row.id,
            code=row.code,
            status=str(row.status),
            courier_name=row.courier_name,
            transport_name=row.transport_name or transporter_name,
            tracking_id=row.tracking_id,
            lr_number=row.lr_number,
            dispatch_date=row.dispatch_date,
            dispatched_at=row.dispatched_at,
            expected_delivery_date=row.expected_delivery_date,
            actual_delivery_date=row.actual_delivery_date,
        )

    async def order_dispatches(self, order_id: uuid.UUID) -> list[DealerDispatchSummary]:
        await self._customer()
        await self.order_repo.get_or_404(order_id)
        return [
            self._dispatch_summary(row)
            for row in await self.dispatch_repo.list_for_sales_order(order_id)
        ]

    async def dispatch_tracking(self, dispatch_id: uuid.UUID) -> DealerTracking:
        """A dealer-safe projection of one consignment.

        Courier or transporter name, tracking id, LR number and the dates - but
        never the driver's mobile number and never the freight cost
        (docs/assumptions.md §C3-7).
        """
        await self._customer()
        row = await self.dispatch_repo.get(dispatch_id)
        if row is None:
            raise NotFoundError("Dispatch", dispatch_id)
        events = await self.dispatch_event_repo.latest_for_dispatch(
            dispatch_id, limit=TRACKING_EVENT_LIMIT
        )
        summary = self._dispatch_summary(row)
        return DealerTracking(
            **summary.model_dump(),
            events=[
                DealerTrackingEvent(
                    event_status=str(event.event_status),
                    event_time=event.event_time,
                    location=event.location,
                    remark=event.remark,
                )
                for event in reversed(events)
            ],
            # Bounded newest-first read, not a cursor: the feed is short and
            # the dealer always sees the whole of it (docs/assumptions.md §C3-7).
            next_cursor=None,
        )

    # --- statement and outstanding ------------------------------------------------

    async def statement(self, filters: StatementFilters, params: PageParams) -> LedgerStatement:
        """The dealer's own ledger statement. The party is forced, never taken."""
        customer = await self._customer()
        statement, _ = await party_statement(
            self.session,
            self.tenant,
            party_type=PartyType.CUSTOMER,
            party_id=customer.id,
            filters=filters,
            params=params,
        )
        return statement

    async def outstanding(self) -> DealerOutstanding:
        """Ageing for the dealer plus its overdue position.

        Sales invoices are not in this release, so the overdue list stays empty
        and the FIFO-netted receivables ageing is the overdue position
        (docs/assumptions.md §C3-10).
        """
        customer = await self._customer()
        report = await ageing_report(
            self.session, self.tenant, party_type=PartyType.CUSTOMER, party_id=customer.id
        )
        block = report.currencies[0] if report.currencies else None
        row = None
        if block is not None:
            row = next(
                (candidate for candidate in block.rows if candidate.party_id == customer.id),
                None,
            )
        if row is not None:
            overdue = row.total_outstanding - row.not_due
            oldest = row.oldest_due_date
            documents = row.open_document_count
        elif block is not None:
            overdue = block.total_outstanding - block.total_not_due
            oldest = None
            documents = block.document_count
        else:  # pragma: no cover - the report always carries at least a zeroed block
            overdue = Decimal("0")
            oldest = None
            documents = 0
        return DealerOutstanding(
            overdue_amount=to_money(overdue),
            currency=report.currency,
            ageing_total_outstanding=to_money(block.total_outstanding if block else 0),
            ageing_not_due=to_money(block.total_not_due if block else 0),
            oldest_due_date=oldest,
            open_document_count=documents,
            overdue_invoices=[],
        )


__all__ = [
    "AUDIT_DEALER_CHECKOUT",
    "CATALOGUE_SORT_FIELDS",
    "DealerPortalService",
    "credit_position",
    "offer_snippet",
]
