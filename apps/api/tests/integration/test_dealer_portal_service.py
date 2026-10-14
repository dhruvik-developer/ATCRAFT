"""Dealer portal backend, end to end on real Postgres (WP C3a).

Covers the spec's integration list: channel pricing per customer type, offers on
both the portal and the ERP path, cart CRUD plus version conflicts and the
priced preview, wishlist CRUD, checkout (DRAFT portal order, server prices,
cleared cart, notification, foreign-address and stale-version refusals),
dealer cancel, portal-draft delete refusal, cross-dealer scoping, and the
``users.customer_id`` validation rules. Rows are built through the ORM.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    BusinessRuleError,
    ConflictError,
    DuplicateError,
    NotFoundError,
    ValidationError,
)
from src.core.pagination import PageParams
from src.core.permissions import ROLE_PRESETS
from src.modules.audit.models import AuditLog
from src.modules.customers.models import AddressType, CustomerAddress, CustomerType
from src.modules.dealer_portal.models import DealerOffer
from src.modules.dealer_portal.schemas import DealerCheckoutRequest, DealerOrderFilters
from src.modules.dealer_portal.service import AUDIT_DEALER_CHECKOUT, DealerPortalService
from src.modules.notifications.models import Notification, NotificationType
from src.modules.products.models import ItemGroup, ItemPrice, PriceType
from src.modules.sales import service as sales_service
from src.modules.sales.models import SalesOrder, SalesOrderLineStatus, SalesOrderStatus
from src.modules.sales.schemas import SalesOrderCreate, SalesOrderLineIn
from src.modules.users.models import Role, User
from src.modules.users.schemas import UserCreate
from src.modules.users.service import UserService
from src.modules.warehouses.models import WarehouseType
from tests import seed_support

pytestmark = [pytest.mark.integration]


def code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def dealer_principal(
    company: Any, user: User, customer_id: uuid.UUID, preset: str = "DEALER_OWNER"
) -> Principal:
    granted = ROLE_PRESETS[preset]["permissions"]
    return Principal(
        user_id=user.id,
        company_id=company.id,
        email=user.email,
        full_name=user.full_name,
        is_superuser=False,
        permissions=frozenset(str(p) for p in granted),
        role_codes=(preset,),
        customer_id=customer_id,
    )


@pytest.fixture
async def dealer(session: AsyncSession, company):
    """A dealer-channel customer the portal may be bound to."""
    company.state = "Gujarat"
    company.state_code = "24"
    await session.flush()
    return await seed_support.seed_customer(
        session,
        company_id=company.id,
        code=code("DLR"),
        customer_type=CustomerType.DEALER,
        price_list_type=PriceType.DEALER,
        credit_limit=Decimal("500000"),
        credit_days=30,
    )


@pytest.fixture
async def other_dealer(session: AsyncSession, company):
    return await seed_support.seed_customer(
        session,
        company_id=company.id,
        code=code("DLR"),
        customer_type=CustomerType.DEALER,
        price_list_type=PriceType.DEALER,
    )


@pytest.fixture
async def distributor(session: AsyncSession, company):
    return await seed_support.seed_customer(
        session,
        company_id=company.id,
        code=code("DIST"),
        customer_type=CustomerType.DISTRIBUTOR,
        price_list_type=PriceType.DISTRIBUTOR,
    )


@pytest.fixture
async def plain_customer(session: AsyncSession, company):
    return await seed_support.seed_customer(
        session,
        company_id=company.id,
        code=code("CUST"),
        customer_type=CustomerType.CUSTOMER,
    )


@pytest.fixture
async def dealer_role(session: AsyncSession, company) -> Role:
    granted = ROLE_PRESETS["DEALER_OWNER"]["permissions"]
    record = Role(
        company_id=company.id,
        code=code("DEALER_OWNER"),
        name="Dealer Owner",
        permissions=sorted(str(p) for p in granted),
        is_system=True,
        is_dealer_role=True,
        is_active=True,
    )
    session.add(record)
    await session.flush()
    return record


@pytest.fixture
async def dealer_user(session: AsyncSession, company, dealer, dealer_role) -> User:
    record = User(
        company_id=company.id,
        customer_id=dealer.id,
        email=f"{uuid.uuid4().hex[:10]}@dealer.example",
        full_name="Dealer Owner",
        password_hash="unused",
        is_active=True,
        roles=[dealer_role],
    )
    session.add(record)
    await session.flush()
    return record


@pytest.fixture
async def distributor_user(session: AsyncSession, company, distributor) -> User:
    record = User(
        company_id=company.id,
        customer_id=distributor.id,
        email=f"{uuid.uuid4().hex[:10]}@dealer.example",
        full_name="Distributor Buyer",
        password_hash="unused",
        is_active=True,
    )
    session.add(record)
    await session.flush()
    return record


@pytest.fixture
def dealer_tenant(company, dealer_user, dealer) -> TenantContext:
    return TenantContext(company_id=company.id, user_id=dealer_user.id, customer_id=dealer.id)


@pytest.fixture
def portal(session: AsyncSession, dealer_tenant) -> DealerPortalService:
    return DealerPortalService(session, dealer_tenant)


@pytest.fixture
async def warehouse(session: AsyncSession, company):
    return await seed_support.seed_warehouse(
        session,
        company_id=company.id,
        code=code("WH"),
        warehouse_type=WarehouseType.FINISHED_GOODS,
        is_default=True,
    )


@pytest.fixture
async def category(session: AsyncSession, company):
    return await seed_support.seed_category(session, company_id=company.id, code=code("CAT"))


@pytest.fixture
async def item(session: AsyncSession, company, category):
    return await seed_support.seed_item(
        session,
        company_id=company.id,
        code=code("FG"),
        item_group=ItemGroup.FINISHED_GOOD,
        can_sell=True,
        is_active=True,
        sale_price=Decimal("120"),
        dealer_price=Decimal("100"),
        gst_rate=Decimal("18"),
        category_id=category.id,
    )


@pytest.fixture
async def list_price(session: AsyncSession, company, item) -> ItemPrice:
    """A DEALER list row below the item-master fallback, so the list wins."""
    row = ItemPrice(
        company_id=company.id,
        item_id=item.id,
        price_type=PriceType.DEALER,
        currency="INR",
        price=Decimal("90"),
        min_quantity=Decimal("0"),
        valid_from=today() - timedelta(days=1),
        valid_to=None,
        is_active=True,
    )
    session.add(row)
    await session.flush()
    return row


@pytest.fixture
async def item_offer(session: AsyncSession, company, item) -> DealerOffer:
    row = DealerOffer(
        company_id=company.id,
        code=code("OFFER"),
        title="Ten percent off",
        scope_kind="ITEM",
        item_id=item.id,
        audience_price_type=None,
        customer_id=None,
        min_quantity=Decimal("1"),
        discount_percentage=Decimal("10"),
        is_active=True,
        sort_order=0,
    )
    session.add(row)
    await session.flush()
    return row


def params(**overrides: Any) -> PageParams:
    return PageParams(page=1, limit=50, **overrides)


# -----------------------------------------------------------------------------
# Catalogue pricing per channel
# -----------------------------------------------------------------------------


async def test_the_list_price_beats_the_item_master_fallback(portal, item, list_price) -> None:
    rows, _ = await portal.catalog_items(
        params(),
        search=None,
        category_id=None,
        brand_id=None,
        in_stock_only=False,
        on_offer_only=False,
        sort_by="code",
    )
    assert [(row.code, row.price) for row in rows] == [(item.code, Decimal("90.000000"))]
    assert rows[0].price_on_request is False and rows[0].orderable is True


async def test_the_master_price_applies_without_a_list_row(portal, item) -> None:
    rows, _ = await portal.catalog_items(
        params(),
        search=None,
        category_id=None,
        brand_id=None,
        in_stock_only=False,
        on_offer_only=False,
        sort_by="code",
    )
    assert [(row.code, row.price) for row in rows] == [(item.code, Decimal("100.000000"))]


async def test_a_distributor_without_a_row_is_price_on_request(
    session, company, distributor, item
) -> None:
    tenant = TenantContext(company_id=company.id, user_id=uuid.uuid4(), customer_id=distributor.id)
    portal = DealerPortalService(session, tenant)
    rows, _ = await portal.catalog_items(
        params(),
        search=None,
        category_id=None,
        brand_id=None,
        in_stock_only=False,
        on_offer_only=False,
        sort_by="code",
    )
    assert len(rows) == 1
    assert rows[0].price is None
    assert rows[0].price_on_request is True
    assert rows[0].orderable is False


async def test_sorting_by_price_is_refused_with_the_allowed_fields(portal, item) -> None:
    with pytest.raises(ValidationError) as excinfo:
        await portal.catalog_items(
            params(),
            search=None,
            category_id=None,
            brand_id=None,
            in_stock_only=False,
            on_offer_only=False,
            sort_by="price",
        )
    assert excinfo.value.details["allowed"] == ["code", "name"]


async def test_category_filter_covers_the_subtree(portal, session, company, item, category) -> None:
    child = await seed_support.seed_category(
        session, company_id=company.id, code=code("CAT"), parent=category
    )
    item.category_id = child.id
    await session.flush()
    rows, _ = await portal.catalog_items(
        params(),
        search=None,
        category_id=category.id,
        brand_id=None,
        in_stock_only=False,
        on_offer_only=False,
        sort_by="code",
    )
    assert [row.code for row in rows] == [item.code]


async def test_availability_bands_come_from_live_stock(
    portal, session, company, item, warehouse
) -> None:
    from src.modules.inventory.models import InventoryBalance

    async def band() -> str:
        rows, _ = await portal.catalog_items(
            params(),
            search=None,
            category_id=None,
            brand_id=None,
            in_stock_only=False,
            on_offer_only=False,
            sort_by="code",
        )
        return str(rows[0].availability_band)

    assert await band() == "MADE_TO_ORDER"
    session.add(
        InventoryBalance(
            company_id=company.id,
            item_id=item.id,
            warehouse_id=warehouse.id,
            on_hand_quantity=Decimal("3"),
        )
    )
    await session.flush()
    assert await band() == "LIMITED"


# -----------------------------------------------------------------------------
# Offers, on both paths
# -----------------------------------------------------------------------------


async def test_the_best_offer_reaches_the_catalogue(portal, item, list_price, item_offer) -> None:
    rows, _ = await portal.catalog_items(
        params(),
        search=None,
        category_id=None,
        brand_id=None,
        in_stock_only=False,
        on_offer_only=False,
        sort_by="code",
    )
    assert rows[0].best_offer is not None
    assert rows[0].best_offer.discount_percentage == Decimal("10.0000")

    offered, _ = await portal.catalog_items(
        params(),
        search=None,
        category_id=None,
        brand_id=None,
        in_stock_only=False,
        on_offer_only=True,
        sort_by="code",
    )
    assert [row.code for row in offered] == [item.code]


async def test_an_erp_order_takes_the_scheme_by_default(
    session, dealer_tenant, principal, dealer, warehouse, item, list_price, item_offer
) -> None:
    _order, lines = await sales_service.create_order(
        session,
        dealer_tenant,
        principal,
        SalesOrderCreate(
            customer_id=dealer.id,
            required_date=today() + timedelta(days=7),
            warehouse_id=warehouse.id,
            lines=[SalesOrderLineIn(line_number=1, item_id=item.id, quantity=Decimal("2"))],
        ),
    )
    assert lines[0].unit_price == Decimal("90.000000")
    assert lines[0].discount_percentage == Decimal("10.0000")
    assert lines[0].offer_id == item_offer.id
    assert lines[0].discount_amount == Decimal("18.00")


async def test_an_explicit_erp_discount_wins_and_carries_no_scheme(
    session, dealer_tenant, principal, dealer, warehouse, item, list_price, item_offer
) -> None:
    _order, lines = await sales_service.create_order(
        session,
        dealer_tenant,
        principal,
        SalesOrderCreate(
            customer_id=dealer.id,
            required_date=today() + timedelta(days=7),
            warehouse_id=warehouse.id,
            lines=[
                SalesOrderLineIn(
                    line_number=1,
                    item_id=item.id,
                    quantity=Decimal("2"),
                    discount_percentage=Decimal("5"),
                )
            ],
        ),
    )
    assert lines[0].discount_percentage == Decimal("5")
    assert lines[0].offer_id is None
    assert lines[0].discount_amount == Decimal("9.00")


# -----------------------------------------------------------------------------
# Cart and wishlist
# -----------------------------------------------------------------------------


async def test_cart_crud_and_duplicate_refusal(portal, item) -> None:
    cart = await portal.add_cart_line(item.id, None, Decimal("2"))
    assert len(cart.lines) == 1 and cart.lines[0].quantity == Decimal("2.0000")

    with pytest.raises(DuplicateError):
        await portal.add_cart_line(item.id, None, Decimal("1"))

    updated = await portal.update_cart_line(cart.lines[0].id, Decimal("5"))
    assert updated.lines[0].quantity == Decimal("5.0000")

    cleared = await portal.remove_cart_line(cart.lines[0].id)
    assert cleared.lines == []


async def test_the_cart_preview_prices_tax_and_credit(portal, item, list_price, item_offer) -> None:
    await portal.add_cart_line(item.id, None, Decimal("2"))
    cart = await portal.cart()
    assert cart.cart_version is not None
    (line,) = cart.lines
    assert line.unit_price == Decimal("90.000000")
    assert line.discount_percentage == Decimal("10.0000")
    assert line.offer is not None and line.offer.id == item_offer.id
    # 2 x 90 = 180 gross, 10% = 18 discount, 18% IGST on 162 = 29.16.
    assert cart.subtotal == Decimal("180.00")
    assert cart.discount_amount == Decimal("18.00")
    assert cart.tax_amount == Decimal("29.16")
    assert cart.total_amount == Decimal("191.16")
    assert cart.warnings == []
    assert cart.credit.within_limit is True


async def test_the_preview_warns_on_a_price_on_request_line(
    session, company, distributor, distributor_user, item
) -> None:
    tenant = TenantContext(
        company_id=company.id, user_id=distributor_user.id, customer_id=distributor.id
    )
    portal = DealerPortalService(session, tenant)
    await portal.add_cart_line(item.id, None, Decimal("1"))
    cart = await portal.cart()
    assert cart.lines[0].price_on_request is True
    assert [warning.code for warning in cart.warnings] == ["PRICE_ON_REQUEST"]
    assert cart.total_amount == Decimal("0")


async def test_wishlist_crud(portal, item) -> None:
    saved = await portal.add_wishlist_item(item.id, None)
    assert saved.item_code == item.code
    with pytest.raises(DuplicateError):
        await portal.add_wishlist_item(item.id, None)
    assert [row.id for row in await portal.wishlist()] == [saved.id]
    await portal.remove_wishlist_item(saved.id)
    assert await portal.wishlist() == []


async def test_move_to_cart_merges_quantities(portal, item) -> None:
    await portal.add_cart_line(item.id, None, Decimal("2"))
    saved = await portal.add_wishlist_item(item.id, None)
    cart = await portal.move_wishlist_to_cart(saved.id, Decimal("3"))
    assert cart.lines[0].quantity == Decimal("5.0000")
    assert await portal.wishlist() == []


# -----------------------------------------------------------------------------
# Checkout
# -----------------------------------------------------------------------------


async def test_checkout_places_a_draft_portal_order(
    session,
    portal,
    company,
    dealer_tenant,
    dealer_user,
    dealer,
    warehouse,
    item,
    list_price,
    item_offer,
    principal,
) -> None:
    """The staff approver (``principal``) is the sales desk that must be told."""
    await portal.add_cart_line(item.id, None, Decimal("2"))
    cart = await portal.cart()
    assert cart.cart_version is not None

    order = await portal.checkout(
        dealer_principal(company, dealer_user, dealer.id),
        DealerCheckoutRequest(
            cart_version=cart.cart_version,
            required_date=today() + timedelta(days=7),
            notes="Portal demo",
        ),
    )
    assert order.status == SalesOrderStatus.DRAFT
    assert order.source == "DEALER_PORTAL"
    (line,) = order.lines
    assert line.unit_price == Decimal("90.000000")
    assert line.discount_percentage == Decimal("10.0000")
    assert line.offer_id == item_offer.id
    assert line.allocated_quantity == Decimal("0")

    cleared = await portal.cart()
    assert cleared.lines == []

    audit = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.company_id == dealer_tenant.company_id,
                AuditLog.entity_type == AUDIT_DEALER_CHECKOUT,
                AuditLog.entity_id == order.id,
            )
        )
    ).scalar_one()
    assert order.code in audit.summary

    notices = (
        (
            await session.execute(
                select(Notification).where(
                    Notification.company_id == dealer_tenant.company_id,
                    Notification.notification_type == NotificationType.APPROVAL_PENDING.value,
                    Notification.entity_id == order.id,
                )
            )
        )
        .scalars()
        .all()
    )
    if not notices:
        everything = (
            (
                await session.execute(
                    select(Notification).where(Notification.company_id == dealer_tenant.company_id)
                )
            )
            .scalars()
            .all()
        )
        raise AssertionError(
            "sales desk heard nothing about the dealer order; "
            f"all notices: {[(n.user_id, n.notification_type) for n in everything]}"
        )


async def test_checkout_refuses_a_stale_version(portal, item) -> None:
    await portal.add_cart_line(item.id, None, Decimal("1"))
    with pytest.raises(ConflictError):
        await portal.checkout(
            _staff_principal(portal),
            DealerCheckoutRequest(cart_version=999, required_date=today() + timedelta(days=7)),
        )


def _staff_principal(portal: DealerPortalService) -> Principal:
    return Principal(
        user_id=portal.tenant.user_id,
        company_id=portal.tenant.company_id,
        email="dealer@test",
        full_name="Dealer",
        is_superuser=False,
        permissions=frozenset(),
        role_codes=(),
        customer_id=portal.tenant.customer_id,
    )


async def test_checkout_refuses_an_empty_cart(portal) -> None:
    with pytest.raises(BusinessRuleError):
        await portal.checkout(
            _staff_principal(portal),
            DealerCheckoutRequest(cart_version=1, required_date=today() + timedelta(days=7)),
        )


async def test_checkout_refuses_a_foreign_address(
    portal, session, company, other_dealer, item, warehouse
) -> None:
    foreign = CustomerAddress(
        company_id=company.id,
        customer_id=other_dealer.id,
        address_type=AddressType.SHIPPING,
        address_line1="Elsewhere",
        city="Surat",
        state="Gujarat",
        state_code="24",
        country="India",
        is_active=True,
    )
    session.add(foreign)
    await session.flush()
    await portal.add_cart_line(item.id, None, Decimal("1"))
    cart = await portal.cart()
    assert cart.cart_version is not None
    with pytest.raises(NotFoundError):
        await portal.checkout(
            _staff_principal(portal),
            DealerCheckoutRequest(
                cart_version=cart.cart_version,
                required_date=today() + timedelta(days=7),
                shipping_address_id=foreign.id,
            ),
        )


# -----------------------------------------------------------------------------
# Orders, cancel, delete refusal, scoping
# -----------------------------------------------------------------------------


async def test_the_dealer_lists_cancels_but_cannot_delete_its_order(
    session, portal, company, dealer_tenant, dealer_user, dealer, warehouse, item, list_price
) -> None:
    await portal.add_cart_line(item.id, None, Decimal("1"))
    cart = await portal.cart()
    assert cart.cart_version is not None
    order = await portal.checkout(
        dealer_principal(company, dealer_user, dealer.id),
        DealerCheckoutRequest(
            cart_version=cart.cart_version, required_date=today() + timedelta(days=7)
        ),
    )
    rows, _ = await portal.orders(DealerOrderFilters(), params())
    assert [row.code for row in rows] == [order.code]

    detail = await portal.order(order.id)
    assert detail.lines[0].allocation_status == SalesOrderLineStatus.PENDING

    cancelled = await portal.cancel_order(order.id, "Ordered twice by mistake")
    assert cancelled.status == SalesOrderStatus.CANCELLED

    with pytest.raises(BusinessRuleError):
        await sales_service.delete_draft_order(session, dealer_tenant, order.id, reason="x" * 10)


async def test_delete_of_a_portal_draft_is_refused_while_erp_drafts_delete(
    session, dealer_tenant, principal, dealer, warehouse, item, list_price
) -> None:
    erp, _ = await sales_service.create_order(
        session,
        dealer_tenant,
        principal,
        SalesOrderCreate(
            customer_id=dealer.id,
            required_date=today() + timedelta(days=7),
            warehouse_id=warehouse.id,
            lines=[SalesOrderLineIn(line_number=1, item_id=item.id, quantity=Decimal("1"))],
        ),
    )
    assert erp.source == "ERP"
    await sales_service.delete_draft_order(
        session, dealer_tenant, erp.id, reason="no longer needed"
    )

    portal_order, _ = await sales_service.create_order(
        session,
        dealer_tenant,
        principal,
        SalesOrderCreate(
            customer_id=dealer.id,
            required_date=today() + timedelta(days=7),
            warehouse_id=warehouse.id,
            lines=[SalesOrderLineIn(line_number=1, item_id=item.id, quantity=Decimal("1"))],
        ),
        source="DEALER_PORTAL",
    )
    with pytest.raises(BusinessRuleError) as excinfo:
        await sales_service.delete_draft_order(
            session, dealer_tenant, portal_order.id, reason="no longer needed"
        )
    assert "cancelled" in str(excinfo.value).lower()


async def test_a_dealer_cannot_read_another_dealers_order(
    session, company, dealer, other_dealer, dealer_user, warehouse, item, list_price, principal
) -> None:
    tenant_a = TenantContext(company_id=company.id, user_id=dealer_user.id, customer_id=dealer.id)
    order, _ = await sales_service.create_order(
        session,
        tenant_a,
        principal,
        SalesOrderCreate(
            customer_id=dealer.id,
            required_date=today() + timedelta(days=7),
            warehouse_id=warehouse.id,
            lines=[SalesOrderLineIn(line_number=1, item_id=item.id, quantity=Decimal("1"))],
        ),
        source="DEALER_PORTAL",
    )
    tenant_b = TenantContext(
        company_id=company.id, user_id=dealer_user.id, customer_id=other_dealer.id
    )
    stranger = DealerPortalService(session, tenant_b)
    with pytest.raises(NotFoundError):
        await stranger.order(order.id)
    rows, _ = await stranger.orders(DealerOrderFilters(), params())
    assert rows == []
    stored = await session.get(SalesOrder, order.id)
    assert stored is not None and str(stored.customer_id) == str(dealer.id)


# -----------------------------------------------------------------------------
# users.customer_id validation
# -----------------------------------------------------------------------------


async def test_a_non_dealer_customer_is_refused(
    session, tenant, principal, plain_customer, dealer_role
) -> None:
    with pytest.raises(BusinessRuleError) as excinfo:
        await UserService(session, tenant).create_user(
            principal,
            UserCreate(
                email=f"{uuid.uuid4().hex[:8]}@dealer.example",
                full_name="Not A Dealer",
                password="Demonstration!234",
                customer_id=plain_customer.id,
                role_ids=[dealer_role.id],
            ),
        )
    assert "not a dealer" in str(excinfo.value).lower()


async def test_vendor_and_customer_together_are_refused(
    session, tenant, principal, dealer, dealer_role
) -> None:
    from src.modules.vendors.models import Vendor, VendorType

    vendor = Vendor(
        company_id=tenant.company_id,
        code=code("V"),
        name="Vendor",
        vendor_types=[VendorType.RAW_MATERIAL_SUPPLIER.value],
        is_active=True,
    )
    session.add(vendor)
    await session.flush()
    with pytest.raises(BusinessRuleError):
        await UserService(session, tenant).create_user(
            principal,
            UserCreate(
                email=f"{uuid.uuid4().hex[:8]}@dealer.example",
                full_name="Two Portals",
                password="Demonstration!234",
                vendor_id=vendor.id,
                customer_id=dealer.id,
                role_ids=[dealer_role.id],
            ),
        )


async def test_an_unknown_customer_is_not_found(session, tenant, principal, dealer_role) -> None:
    with pytest.raises(NotFoundError):
        await UserService(session, tenant).create_user(
            principal,
            UserCreate(
                email=f"{uuid.uuid4().hex[:8]}@dealer.example",
                full_name="Ghost Dealer",
                password="Demonstration!234",
                customer_id=uuid.uuid4(),
                role_ids=[dealer_role.id],
            ),
        )


async def test_a_matching_dealer_account_is_created(
    session, tenant, principal, dealer, dealer_role
) -> None:
    created = await UserService(session, tenant).create_user(
        principal,
        UserCreate(
            email=f"{uuid.uuid4().hex[:8]}@dealer.example",
            full_name="Dealer Owner",
            password="Demonstration!234",
            customer_id=dealer.id,
            role_ids=[dealer_role.id],
        ),
    )
    assert created.is_dealer_user is True
    assert created.customer_id == dealer.id
    assert created.role_codes == [dealer_role.code]
