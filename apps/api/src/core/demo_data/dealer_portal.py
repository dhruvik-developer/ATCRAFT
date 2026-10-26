"""Dealer-portal demonstration rows (WP C3a).

Two dealer accounts (owner and buyer) per demo dealer, two schemes - one for a
category, one for a brand - a cart with lines, a wishlist, and one
portal-sourced DRAFT order raised through the checkout itself, so the demo
carries what a real dealer would leave behind: server prices, scheme ids on
the lines, and a "new order request" notification for the sales desk.

Idempotent by identity: users by e-mail, offers by code, the order by the
``[DM-DEALER-...]`` marker in its notes, cart and wishlist grains by their own
uniqueness - never by a date. A re-run only fills what an earlier run left
out. Called at the end of the procurement stage beside the vendor-portal demo:
everything it needs (dealers, sellable items, dealer prices) comes from
masters, and a DRAFT order reserves nothing, so later stages are unaffected.
"""

from __future__ import annotations

import uuid
from contextlib import suppress
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from src.core.clock import today
from src.core.demo_data import DemoContext, find_by_code
from src.modules.customers.models import Customer
from src.modules.dealer_portal.models import DealerOffer
from src.modules.dealer_portal.promotion_schemas import DealerOfferCreate
from src.modules.dealer_portal.promotion_service import PromotionService
from src.modules.dealer_portal.schemas import DealerCheckoutRequest
from src.modules.dealer_portal.service import DealerPortalService
from src.modules.organization.models import Company
from src.modules.products.models import Item
from src.modules.sales.models import SalesOrder
from src.modules.users.models import Role, User
from src.modules.users.schemas import UserCreate
from src.modules.users.service import (
    AuthorizationError,
    BusinessRuleError,
    DuplicateError,
    NotFoundError,
    UserService,
    generate_temporary_password,
)
from src.modules.warehouses.repository import WarehouseRepository
from src.modules.warehouses.service import WarehouseService

STAGE = "procurement"

#: (code parts, full name, e-mail, dealer parts, role).
DEALER_USERS: tuple[tuple[tuple[str, ...], str, str, tuple[str, ...], str], ...] = (
    (
        ("USER", "DLR-VASTU-OWNER"),
        "Jignesh Patel",
        "jignesh.patel@vastuhardware.example",
        ("DLR", "VASTU"),
        "DEALER_OWNER",
    ),
    (
        ("USER", "DLR-VASTU-BUYER"),
        "Vastu Purchase Desk",
        "purchase@vastuhardware.example",
        ("DLR", "VASTU"),
        "DEALER_BUYER",
    ),
    (
        ("USER", "DLR-METRO-OWNER"),
        "Rohit Sethi",
        "rohit.sethi@metrodoors.example",
        ("DLR", "METRO"),
        "DEALER_OWNER",
    ),
    (
        ("USER", "DLR-METRO-BUYER"),
        "Metro Purchase Desk",
        "purchase@metrodoors.example",
        ("DLR", "METRO"),
        "DEALER_BUYER",
    ),
)

#: (code, title, scope, target parts, discount %, min quantity).
OFFERS: tuple[tuple[str, str, str, tuple[str, ...] | None, str, str], ...] = (
    (
        "DM-OFFER-HANDLES-5",
        "Demonstration: 5% across brass lever handles",
        "CATEGORY",
        ("CAT", "LEVER-BRASS"),
        "5",
        "1",
    ),
    (
        "DM-OFFER-ATCRAFT-10",
        "Demonstration: 10% on the house brand from 10 units",
        "BRAND",
        ("BRAND", "ATCRAFT"),
        "10",
        "10",
    ),
)

#: Checkout lines, as ``(item parts, quantity)``.
CHECKOUT_LINES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("FG", "LEVER-AB"), "2"),
    (("FG", "HINGE-SS"), "4"),
)

#: Display cart lines left behind for the portal screens.
CART_LINES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("FG", "LEVER-SS"), "1"),
    (("FG", "MORTISE"), "1"),
)

#: Wishlist rows left behind.
WISHLIST_ITEMS: tuple[tuple[str, ...], ...] = ((("FG", "DIGILOCK")),)

ORDER_MARKER = "DM-DEALER-VASTU-01"


async def seed(ctx: DemoContext) -> None:
    owner_id = await _seed_users(ctx)
    await _ensure_company_state(ctx)
    await _ensure_default_warehouse(ctx)
    await _seed_offers(ctx)
    if owner_id is None:
        return
    dealer_id = ctx.maybe_id("customer", ctx.code("DLR", "VASTU"))
    if dealer_id is None:
        ctx.skipped.append((STAGE, "Dealer demo order skipped: dealer DLR-VASTU missing"))
        return
    dealer = await ctx.session.get(Customer, dealer_id)
    if dealer is None:  # pragma: no cover - the register just named it
        ctx.skipped.append((STAGE, "Dealer demo order skipped: dealer DLR-VASTU missing"))
        return
    portal = DealerPortalService(
        ctx.session,
        replace(ctx.tenant, user_id=owner_id, customer_id=dealer.id),
    )
    if not await _order_exists(ctx):
        await _seed_order(ctx, portal)
    await _seed_cart(ctx, portal)
    await _seed_wishlist(ctx, portal)


async def _seed_users(ctx: DemoContext) -> uuid.UUID | None:
    """One owner and one buyer per demo dealer. Returns the Vastu owner id."""
    service = UserService(ctx.session, ctx.tenant)
    owner_id: uuid.UUID | None = None
    for parts, full_name, email, dealer_parts, role_code in DEALER_USERS:
        code = ctx.code(*parts)
        stmt = select(User).where(User.company_id == ctx.company_id, User.email == email)
        existing = (await ctx.session.execute(stmt)).scalars().first()
        if existing is not None:
            user_id = existing.id
            created = False
        else:
            dealer_id = ctx.maybe_id("customer", ctx.code(*dealer_parts))
            role = await find_by_code(ctx.session, Role, ctx.company_id, role_code)
            if dealer_id is None or role is None:
                ctx.skipped.append(
                    (STAGE, f"dealer portal account {email} not created: dealer or role missing")
                )
                continue
            try:
                detail = await service.create_user(
                    ctx.principal,
                    UserCreate(
                        email=email,
                        full_name=full_name,
                        password=generate_temporary_password(email),
                        customer_id=dealer_id,
                        role_ids=[role.id],
                    ),
                )
            except (BusinessRuleError, DuplicateError, NotFoundError, AuthorizationError) as exc:
                ctx.skipped.append((STAGE, f"dealer portal account {email} not created: {exc}"))
                continue
            user_id = detail.id
            created = True
        _remember(ctx, "user", code, user_id, created)
        if parts == ("USER", "DLR-VASTU-OWNER"):
            owner_id = user_id
    return owner_id


def _remember(ctx: DemoContext, kind: str, code: str, row_id: uuid.UUID, created: bool) -> None:
    ctx.remember(kind, code, row_id)
    ctx.note(STAGE, created=1 if created else 0, existing=0 if created else 1)


async def _ensure_company_state(ctx: DemoContext) -> None:
    """The seller's state, filled in once: without it no taxed order can price.

    The works is in Rajkot, Gujarat. Fill-only, so a company that already
    carries a state keeps it.
    """
    company = await ctx.session.get(Company, ctx.company_id)
    if company is None:  # pragma: no cover - the company always exists here
        return
    if company.state_code is None:
        company.state = company.state or "Gujarat"
        company.state_code = "24"
        await ctx.session.flush()
        ctx.note(STAGE, created=1)
    else:
        ctx.note(STAGE, existing=1)


async def _ensure_default_warehouse(ctx: DemoContext) -> None:
    """Dealer checkout fulfils from the default warehouse; ensure there is one.

    The finished-goods store is the fulfilment point. Set once, never moved:
    a database whose default is already chosen keeps it.
    """
    service = WarehouseService(ctx.session, ctx.tenant)
    repo = WarehouseRepository(ctx.session, ctx.tenant)
    current = await repo.default_warehouse()
    if current is not None:
        ctx.note(STAGE, existing=1)
        return
    finished_id = ctx.maybe_id("warehouse", "WH-FG")
    if finished_id is None:
        ctx.skipped.append((STAGE, "Dealer demo skipped: no WH-FG to make the default"))
        return
    try:
        await service.set_default(finished_id)
    except (BusinessRuleError, NotFoundError) as exc:
        ctx.skipped.append((STAGE, f"Dealer demo skipped: no default warehouse: {exc}"))
        return
    ctx.note(STAGE, created=1)


async def _seed_offers(ctx: DemoContext) -> None:
    for code, title, scope, target_parts, discount, minimum in OFFERS:
        existing = await find_by_code(ctx.session, DealerOffer, ctx.company_id, code)
        if existing is not None:
            ctx.note(STAGE, existing=1)
            continue
        target_id = (
            ctx.maybe_id("category" if scope == "CATEGORY" else "brand", ctx.code(*target_parts))
            if target_parts is not None
            else None
        )
        if target_id is None:
            ctx.skipped.append((STAGE, f"dealer offer {code} not created: target missing"))
            continue
        await PromotionService(ctx.session, ctx.tenant).create(
            "offers",
            DealerOfferCreate(
                code=code,
                title=title,
                description="Demonstration scheme - safe to edit or retire.",
                scope_kind=scope,
                category_id=target_id if scope == "CATEGORY" else None,
                brand_id=target_id if scope == "BRAND" else None,
                item_id=None,
                audience_price_type=None,
                customer_id=None,
                min_quantity=Decimal(minimum),
                discount_percentage=Decimal(discount),
                valid_from=None,
                valid_to=None,
                is_active=True,
                sort_order=10,
            ),
        )
        ctx.note(STAGE, created=1)


async def _order_exists(ctx: DemoContext) -> bool:
    stmt = select(SalesOrder.id).where(
        SalesOrder.company_id == ctx.company_id,
        SalesOrder.notes.like(f"[{ORDER_MARKER}]%"),
    )
    return (await ctx.session.execute(stmt)).first() is not None


async def _seed_order(ctx: DemoContext, portal: DealerPortalService) -> None:
    """Raise the demonstration order through the checkout, then restock the cart."""
    items = {
        row.id: row
        for row in (
            await ctx.session.execute(
                select(Item).where(Item.company_id == ctx.company_id, Item.deleted_at.is_(None))
            )
        ).scalars()
    }
    with suppress(NotFoundError):
        await portal.clear_cart()
    for item_parts, quantity in CHECKOUT_LINES:
        item_id = ctx.maybe_id("item", ctx.code(*item_parts))
        if item_id not in items:
            ctx.skipped.append(
                (STAGE, f"Dealer demo order skipped: item {ctx.code(*item_parts)} missing")
            )
            return
        try:
            await portal.add_cart_line(item_id, None, Decimal(quantity))
        except (BusinessRuleError, DuplicateError, NotFoundError) as exc:
            ctx.skipped.append((STAGE, f"Dealer demo order skipped: {exc}"))
            return
    cart = await portal.cart()
    if cart.cart_id is None or cart.cart_version is None or not cart.lines:
        ctx.skipped.append((STAGE, "Dealer demo order skipped: cart would not fill"))
        return
    try:
        order = await portal.checkout(
            ctx.principal,
            DealerCheckoutRequest(
                cart_version=cart.cart_version,
                required_date=today() + timedelta(days=14),
                notes=f"[{ORDER_MARKER}] Dealer portal demonstration",
            ),
        )
    except (BusinessRuleError, DuplicateError, NotFoundError) as exc:
        ctx.skipped.append((STAGE, f"Dealer demo order skipped: {exc}"))
        return
    ctx.remember("sales_order", ORDER_MARKER, order.id)
    ctx.note(STAGE, created=1)


async def _seed_cart(ctx: DemoContext, portal: DealerPortalService) -> None:
    cart = await portal.cart()
    present = {(line.item_id, line.item_variant_id) for line in cart.lines}
    for item_parts, quantity in CART_LINES:
        item_id = ctx.maybe_id("item", ctx.code(*item_parts))
        if item_id is None:
            ctx.skipped.append(
                (STAGE, f"Dealer demo cart skipped: item {ctx.code(*item_parts)} missing")
            )
            continue
        if (item_id, None) in present:
            ctx.note(STAGE, existing=1)
            continue
        try:
            await portal.add_cart_line(item_id, None, Decimal(quantity))
        except (BusinessRuleError, DuplicateError, NotFoundError) as exc:
            ctx.skipped.append((STAGE, f"Dealer demo cart line skipped: {exc}"))
            continue
        ctx.note(STAGE, created=1)


async def _seed_wishlist(ctx: DemoContext, portal: DealerPortalService) -> None:
    present = {(row.item_id, row.item_variant_id) for row in await portal.wishlist()}
    for item_parts in WISHLIST_ITEMS:
        item_id = ctx.maybe_id("item", ctx.code(*item_parts))
        if item_id is None:
            ctx.skipped.append(
                (STAGE, f"Dealer demo wishlist skipped: item {ctx.code(*item_parts)} missing")
            )
            continue
        if (item_id, None) in present:
            ctx.note(STAGE, existing=1)
            continue
        try:
            await portal.add_wishlist_item(item_id, None)
        except (BusinessRuleError, DuplicateError, NotFoundError) as exc:
            ctx.skipped.append((STAGE, f"Dealer demo wishlist row skipped: {exc}"))
            continue
        ctx.note(STAGE, created=1)


__all__ = ["DEALER_USERS", "OFFERS", "seed"]
