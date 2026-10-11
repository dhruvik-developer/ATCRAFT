"""Brand and manufacturer masters against a real PostgreSQL.

Two properties matter more than any CRUD assertion here, and both are of a
class this codebase has already shipped once:

* **A foreign key is not a tenant check.** ``items.brand_id`` references
  ``brands.id``, a globally unique id, so the database is perfectly satisfied by
  one company's item naming another company's brand. Every assignment - on item
  create, item update, brand owner and vendor link - is read through a
  tenant-scoped repository first, and these tests prove it on *create* as well
  as update, because the item-attribute precedent only ever checked update.
* **A value something live still names cannot be retired.** Otherwise an item
  shows a brand its own edit form can no longer offer, and saving that form
  silently drops it.

Isolation: one private schema for the module, built once; every test runs in a
transaction that is rolled back.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.config import settings
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    BusinessRuleError,
    DuplicateError,
    ErrorCode,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationError,
)
from src.core.models import Base
from src.core.pagination import CursorParams, PageParams, SortParams
from src.core.permissions import Permission
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.products import service
from src.modules.products.models import Brand, Item, ItemGroup, Manufacturer, UomCategory
from src.modules.products.schemas import (
    BrandCreate,
    BrandOut,
    BrandUpdate,
    ItemCreate,
    ItemFilters,
    ItemUpdate,
    ManufacturerCreate,
    ManufacturerOut,
    ManufacturerUpdate,
    UomCreate,
    VendorLinkCreate,
    VendorLinkUpdate,
)
from tests import seed_support
from tests.schema_support import prepare_metadata, report_database_setup_failure

pytestmark = pytest.mark.integration

prepare_metadata()

Ctx = tuple[AsyncSession, TenantContext, Principal]


@pytest.fixture(scope="module")
def _schema() -> Iterator[tuple[str, uuid.UUID, uuid.UUID]]:
    """This module's private schema, built once with its company and user.

    The same shape as ``test_products_service.py``: synchronous, on a throwaway
    loop, because an asyncpg connection cannot outlive the loop that opened it.
    """
    schema = f"it_brands_{uuid.uuid4().hex[:10]}"
    company_id = uuid.uuid4()
    user_id = uuid.uuid4()

    async def setup() -> None:
        admin = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        finally:
            await admin.dispose()

        engine = _schema_engine(schema)
        try:
            async with engine.begin() as conn:
                # checkfirst=False: see test_products_service.py - `public` stays
                # on the path for pg_trgm and already holds these tables.
                await conn.run_sync(
                    lambda sync_conn: Base.metadata.create_all(sync_conn, checkfirst=False)
                )
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as setup_session, setup_session.begin():
                await seed_support.seed_company(setup_session, company_id=company_id)
                await seed_support.seed_user(
                    setup_session,
                    company_id=company_id,
                    user_id=user_id,
                    email="brands@atcraft.test",
                )
        finally:
            await engine.dispose()

    async def teardown() -> None:
        admin = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            await admin.dispose()

    try:
        asyncio.run(setup())
    except Exception as exc:  # pragma: no cover - environment, not a test failure
        report_database_setup_failure(exc)

    yield schema, company_id, user_id
    asyncio.run(teardown())


def _schema_engine(schema: str):
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


@pytest_asyncio.fixture
async def ctx(_schema: tuple[str, uuid.UUID, uuid.UUID]) -> AsyncIterator[Ctx]:
    """A session whose writes are rolled back at the end of each test."""
    schema, company_id, user_id = _schema
    engine = _schema_engine(schema)
    tenant = TenantContext(company_id=company_id, user_id=user_id)
    principal = Principal(
        user_id=user_id,
        company_id=company_id,
        email="brands@atcraft.test",
        full_name="Tester",
        is_superuser=False,
        permissions=frozenset({str(Permission.ITEM_VIEW)}),
    )
    try:
        async with engine.connect() as connection:
            outer = await connection.begin()
            factory = async_sessionmaker(
                bind=connection,
                class_=AsyncSession,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            async with factory() as session:
                yield session, tenant, principal
            await outer.rollback()
    finally:
        await engine.dispose()


# ----------------------------------------------------------------- builders --


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6].upper()}"


async def _maker(
    session: AsyncSession, tenant: TenantContext, **overrides: object
) -> ManufacturerOut:
    data: dict[str, object] = {"code": _code("MK"), "name": "Demo Cylinder Works"}
    data.update(overrides)
    return await service.create_manufacturer(session, tenant, ManufacturerCreate(**data))  # type: ignore[arg-type]


async def _brand(session: AsyncSession, tenant: TenantContext, **overrides: object) -> BrandOut:
    data: dict[str, object] = {"code": _code("BR"), "name": "Demo Brand"}
    data.update(overrides)
    return await service.create_brand(session, tenant, BrandCreate(**data))  # type: ignore[arg-type]


async def _base_unit(session: AsyncSession, tenant: TenantContext) -> uuid.UUID:
    """A base unit of its own per test - one base per category per company."""
    unit = await service.create_uom(
        session,
        tenant,
        UomCreate(code=_code("U"), name="Numbers", uom_category=UomCategory.COUNT, is_base=True),
    )
    return unit.id


def _item(code: str, base_uom_id: uuid.UUID, **overrides: object) -> ItemCreate:
    data: dict[str, object] = {
        "code": code,
        "name": f"Item {code}",
        "item_group": ItemGroup.COMPONENT,
        "base_uom_id": base_uom_id,
        "can_purchase": True,
    }
    data.update(overrides)
    return ItemCreate(**data)  # type: ignore[arg-type]


async def _foreign_brand_and_maker(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """A brand and a maker belonging to another company, built through the ORM."""
    outsider = await seed_support.seed_company(session, prefix="OTH")
    maker = Manufacturer(company_id=outsider.id, code=_code("XM"), name="Their maker")
    session.add(maker)
    await session.flush()
    brand = Brand(company_id=outsider.id, code=_code("XB"), name="Their brand")
    session.add(brand)
    await session.flush()
    return brand.id, maker.id


async def _audit_actions(session: AsyncSession, entity_id: uuid.UUID) -> list[str]:
    """Audit rows for one record, in insertion order - (created_at, id), never created_at alone."""
    rows = await session.execute(
        select(AuditLog.action)
        .where(AuditLog.entity_id == entity_id)
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    return [str(action) for action in rows.scalars()]


# ----------------------------------------------------------------- the CRUD --


async def test_a_manufacturer_is_created_listed_renamed_and_audited(ctx: Ctx) -> None:
    session, tenant, _ = ctx
    code = _code("MK")

    created = await _maker(session, tenant, code=f"  {code.lower()} ", legal_name="")
    assert created.code == code, "codes are normalised: upper-cased, whitespace collapsed"
    assert created.country == "India"
    assert created.legal_name is None, "a blank optional field is stored as null"

    rows, _ = await service.list_manufacturers(
        session, tenant, PageParams(page=1, limit=50), search=code
    )
    assert [row.id for row in rows] == [created.id], "the list returns the row just created"

    renamed = await service.update_manufacturer(
        session, tenant, created.id, ManufacturerUpdate(name="Demo Cylinder Works Pvt")
    )
    assert renamed.name == "Demo Cylinder Works Pvt"
    assert renamed.code == code, "the code identifies the row and does not move with the name"

    assert await _audit_actions(session, created.id) == [
        str(AuditAction.CREATE),
        str(AuditAction.UPDATE),
    ]


async def test_a_brand_is_created_with_its_owner_listed_and_renamed(ctx: Ctx) -> None:
    session, tenant, _ = ctx
    owner = await _maker(session, tenant)

    brand = await _brand(session, tenant, name="Demo Pro", manufacturer_id=owner.id, sort_order=5)
    assert brand.manufacturer_id == owner.id

    owned, _ = await service.list_brands(
        session, tenant, PageParams(page=1, limit=50), manufacturer_id=owner.id
    )
    assert [row.id for row in owned] == [brand.id]

    renamed = await service.update_brand(session, tenant, brand.id, BrandUpdate(name="Demo Pro+"))
    assert renamed.name == "Demo Pro+"

    cleared = await service.update_brand(
        session, tenant, brand.id, BrandUpdate(manufacturer_id=None)
    )
    assert cleared.manufacturer_id is None, "an explicit null clears the owner"
    assert cleared.sort_order == 5, "an omitted field is left alone"


async def test_listing_brands_sorts_on_the_whitelisted_columns(ctx: Ctx) -> None:
    session, tenant, _ = ctx
    first = await _brand(session, tenant, code=_code("ZZ"), name="Alpha", sort_order=2)
    second = await _brand(session, tenant, code=_code("AA"), name="Bravo", sort_order=1)

    by_position, _ = await service.list_brands(session, tenant, PageParams(page=1, limit=200))
    positions = [row.id for row in by_position if row.id in {first.id, second.id}]
    assert positions == [second.id, first.id], "the default order is the client's position"

    by_name, _ = await service.list_brands(
        session, tenant, PageParams(page=1, limit=200), sort=SortParams(sort_by="name")
    )
    names = [row.id for row in by_name if row.id in {first.id, second.id}]
    assert names == [first.id, second.id]

    with pytest.raises(ValidationError):
        await service.list_brands(
            session, tenant, PageParams(page=1, limit=50), sort=SortParams(sort_by="description")
        )


async def test_a_code_is_never_reused_not_even_a_retired_one(ctx: Ctx) -> None:
    session, tenant, _ = ctx
    brand = await _brand(session, tenant)
    code = brand.code

    with pytest.raises(DuplicateError) as excinfo:
        await _brand(session, tenant, code=code.lower())
    assert excinfo.value.details["fields"] == {"code": [excinfo.value.message]}

    await service.delete_brand(session, tenant, brand.id, reason="Duplicate of the house brand")
    with pytest.raises(DuplicateError):
        await _brand(session, tenant, code=code)


async def test_the_same_code_may_be_a_brand_and_a_manufacturer(ctx: Ctx) -> None:
    """Two separate lists: the house brand and the house maker are both ATCRAFT."""
    session, tenant, _ = ctx
    code = _code("HOUSE")

    maker = await _maker(session, tenant, code=code)
    brand = await _brand(session, tenant, code=code, manufacturer_id=maker.id)

    assert (maker.code, brand.code) == (code, code)


# ------------------------------------------------------------- the lifecycle --


async def test_deactivation_goes_through_the_machine_and_is_audited_as_a_status_change(
    ctx: Ctx,
) -> None:
    session, tenant, _ = ctx
    brand = await _brand(session, tenant)

    off = await service.update_brand(session, tenant, brand.id, BrandUpdate(is_active=False))
    on = await service.update_brand(session, tenant, brand.id, BrandUpdate(is_active=True))

    assert (off.is_active, on.is_active) == (False, True)
    assert await _audit_actions(session, brand.id) == [
        str(AuditAction.CREATE),
        str(AuditAction.UPDATE),
        str(AuditAction.STATUS_CHANGE),
        str(AuditAction.UPDATE),
        str(AuditAction.STATUS_CHANGE),
    ]


async def test_restating_the_current_flag_moves_nothing(ctx: Ctx) -> None:
    session, tenant, _ = ctx
    brand = await _brand(session, tenant)

    await service.update_brand(session, tenant, brand.id, BrandUpdate(is_active=True))

    assert str(AuditAction.STATUS_CHANGE) not in await _audit_actions(session, brand.id)


async def test_an_empty_patch_is_refused(ctx: Ctx) -> None:
    session, tenant, _ = ctx
    maker = await _maker(session, tenant)

    with pytest.raises(BusinessRuleError):
        await service.update_manufacturer(session, tenant, maker.id, ManufacturerUpdate())


async def test_an_unused_brand_retires_and_then_cannot_be_found(ctx: Ctx) -> None:
    session, tenant, _ = ctx
    brand = await _brand(session, tenant)
    brand_id = brand.id

    await service.delete_brand(session, tenant, brand_id, reason="Line discontinued")

    stored = (await session.execute(select(Brand).where(Brand.id == brand_id))).scalar_one()
    assert stored.deleted_at is not None and stored.is_active is False
    deleted = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.entity_id == brand_id, AuditLog.action == AuditAction.DELETE
            )
        )
    ).scalar_one()
    assert deleted.new_values == {"status": "ARCHIVED"}
    assert deleted.context is not None and deleted.context.get("reason") == "Line discontinued"

    with pytest.raises(NotFoundError):
        await service.get_brand(session, tenant, brand_id)
    with pytest.raises(NotFoundError):
        await service.delete_brand(session, tenant, brand_id, reason="Second attempt")


async def test_an_unused_manufacturer_retires(ctx: Ctx) -> None:
    session, tenant, _ = ctx
    maker = await _maker(session, tenant)
    maker_id = maker.id

    await service.delete_manufacturer(session, tenant, maker_id, reason="Closed down")

    with pytest.raises(NotFoundError):
        await service.get_manufacturer(session, tenant, maker_id)


# ------------------------------------------------- refused while still in use --


async def test_a_brand_carried_by_an_item_cannot_be_deactivated_or_retired(ctx: Ctx) -> None:
    session, tenant, _ = ctx
    brand = await _brand(session, tenant)
    brand_id = brand.id
    await service.create_item(
        session, tenant, _item(_code("FG"), await _base_unit(session, tenant), brand_id=brand_id)
    )

    with pytest.raises(BusinessRuleError) as deactivating:
        await service.update_brand(session, tenant, brand_id, BrandUpdate(is_active=False))
    assert deactivating.value.code == ErrorCode.REFERENCED_RECORD_IN_USE

    with pytest.raises(BusinessRuleError) as retiring:
        await service.delete_brand(session, tenant, brand_id, reason="Rebranding")
    assert retiring.value.code == ErrorCode.REFERENCED_RECORD_IN_USE
    assert "live items" in retiring.value.message

    still = (await session.execute(select(Brand).where(Brand.id == brand_id))).scalar_one()
    assert still.is_active is True and still.deleted_at is None


async def test_an_inactive_item_still_counts_as_using_the_brand(ctx: Ctx) -> None:
    """Inactive is not retired: the item is still on screen, showing its brand."""
    session, tenant, _ = ctx
    brand = await _brand(session, tenant)
    brand_id = brand.id
    await service.create_item(
        session,
        tenant,
        _item(_code("FG"), await _base_unit(session, tenant), brand_id=brand_id, is_active=False),
    )

    with pytest.raises(BusinessRuleError):
        await service.delete_brand(session, tenant, brand_id, reason="Rebranding")


async def test_a_retired_item_no_longer_holds_the_brand(ctx: Ctx) -> None:
    session, tenant, _ = ctx
    brand = await _brand(session, tenant)
    brand_id = brand.id
    item = await service.create_item(
        session, tenant, _item(_code("FG"), await _base_unit(session, tenant), brand_id=brand_id)
    )
    await service.delete_item(session, tenant, item.id)

    await service.delete_brand(session, tenant, brand_id, reason="Nothing live carries it")


@pytest.mark.parametrize("holder", ["item", "vendor link", "brand"])
async def test_a_maker_named_anywhere_live_cannot_be_deactivated_or_retired(
    ctx: Ctx, holder: str
) -> None:
    session, tenant, _ = ctx
    maker = await _maker(session, tenant)
    maker_id = maker.id
    unit = await _base_unit(session, tenant)

    if holder == "item":
        await service.create_item(
            session, tenant, _item(_code("BO"), unit, manufacturer_id=maker_id)
        )
    elif holder == "vendor link":
        item = await service.create_item(session, tenant, _item(_code("BO"), unit))
        vendor = await seed_support.seed_vendor(session, company_id=tenant.company_id)
        await service.link_vendor(
            session,
            tenant,
            item.id,
            VendorLinkCreate(vendor_id=vendor.id, manufacturer_id=maker_id),
        )
    else:
        await _brand(session, tenant, manufacturer_id=maker_id)

    with pytest.raises(BusinessRuleError) as deactivating:
        await service.update_manufacturer(
            session, tenant, maker_id, ManufacturerUpdate(is_active=False)
        )
    assert deactivating.value.code == ErrorCode.REFERENCED_RECORD_IN_USE

    with pytest.raises(BusinessRuleError) as retiring:
        await service.delete_manufacturer(session, tenant, maker_id, reason="Closed down")
    assert retiring.value.code == ErrorCode.REFERENCED_RECORD_IN_USE
    expected = {"item": ["items"], "vendor link": ["item-vendor links"], "brand": ["brands"]}
    assert retiring.value.details["referenced_by"] == expected[holder]


async def test_retiring_a_brand_releases_its_owner(ctx: Ctx) -> None:
    """Following the row one step further: the owner is free once the brand is gone."""
    session, tenant, _ = ctx
    maker = await _maker(session, tenant)
    maker_id = maker.id
    brand = await _brand(session, tenant, manufacturer_id=maker_id)

    await service.delete_brand(session, tenant, brand.id, reason="Brand withdrawn")
    await service.delete_manufacturer(session, tenant, maker_id, reason="No longer used")

    with pytest.raises(NotFoundError):
        await service.get_manufacturer(session, tenant, maker_id)


# --------------------------------------------- assignment: tenant and status --


async def test_item_create_refuses_another_company_s_brand_and_maker(ctx: Ctx) -> None:
    session, tenant, _ = ctx
    unit = await _base_unit(session, tenant)
    their_brand, their_maker = await _foreign_brand_and_maker(session)

    with pytest.raises(NotFoundError):
        await service.create_item(session, tenant, _item(_code("X"), unit, brand_id=their_brand))
    with pytest.raises(NotFoundError):
        await service.create_item(
            session, tenant, _item(_code("X"), unit, manufacturer_id=their_maker)
        )


async def test_item_update_refuses_another_company_s_brand_and_maker(ctx: Ctx) -> None:
    session, tenant, _ = ctx
    item = await service.create_item(
        session, tenant, _item(_code("X"), await _base_unit(session, tenant))
    )
    # Held in a plain variable: each refusal below rolls back a savepoint, which
    # expires everything in the identity map.
    item_id = item.id
    their_brand, their_maker = await _foreign_brand_and_maker(session)

    with pytest.raises(NotFoundError):
        await service.update_item(session, tenant, item_id, ItemUpdate(brand_id=their_brand))
    with pytest.raises(NotFoundError):
        await service.update_item(session, tenant, item_id, ItemUpdate(manufacturer_id=their_maker))

    stored = (
        await session.execute(select(Item.brand_id, Item.manufacturer_id).where(Item.id == item_id))
    ).one()
    assert tuple(stored) == (None, None)


async def test_an_inactive_brand_or_maker_is_refused_on_the_way_in(ctx: Ctx) -> None:
    session, tenant, _ = ctx
    unit = await _base_unit(session, tenant)
    brand = await _brand(session, tenant, is_active=False)
    maker = await _maker(session, tenant, is_active=False)

    with pytest.raises(ValidationError):
        await service.create_item(session, tenant, _item(_code("X"), unit, brand_id=brand.id))
    item = await service.create_item(session, tenant, _item(_code("X"), unit))
    item_id = item.id
    with pytest.raises(ValidationError):
        await service.update_item(session, tenant, item_id, ItemUpdate(manufacturer_id=maker.id))
    with pytest.raises(ValidationError):
        await _brand(session, tenant, manufacturer_id=maker.id)


async def test_an_item_takes_a_brand_a_maker_and_a_part_number_and_can_drop_them(
    ctx: Ctx,
) -> None:
    session, tenant, _ = ctx
    brand = await _brand(session, tenant)
    maker = await _maker(session, tenant)
    item = await service.create_item(
        session,
        tenant,
        _item(
            _code("FG"),
            await _base_unit(session, tenant),
            brand_id=brand.id,
            manufacturer_id=maker.id,
            manufacturer_part_number="CYL-60-5P",
        ),
    )
    assert (item.brand_id, item.manufacturer_id) == (brand.id, maker.id)
    assert item.manufacturer_part_number == "CYL-60-5P"

    created_audit = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.entity_id == item.id, AuditLog.action == AuditAction.CREATE
            )
        )
    ).scalar_one()
    assert created_audit.new_values is not None
    assert created_audit.new_values["manufacturer_part_number"] == "CYL-60-5P"

    cleared = await service.update_item(
        session,
        tenant,
        item.id,
        ItemUpdate(brand_id=None, manufacturer_id=None, manufacturer_part_number=None),
    )
    assert (cleared.brand_id, cleared.manufacturer_id, cleared.manufacturer_part_number) == (
        None,
        None,
        None,
    )


async def test_re_saving_an_item_with_its_brand_unchanged_does_not_reassert_it(
    ctx: Ctx,
) -> None:
    """The item form posts every field on every save; an unchanged brand is not re-checked."""
    session, tenant, _ = ctx
    brand = await _brand(session, tenant)
    item = await service.create_item(
        session, tenant, _item(_code("FG"), await _base_unit(session, tenant), brand_id=brand.id)
    )

    saved = await service.update_item(
        session, tenant, item.id, ItemUpdate(brand_id=brand.id, name="Renamed")
    )

    assert saved.name == "Renamed" and saved.brand_id == brand.id


async def test_a_brand_owner_must_be_this_company_s_maker(ctx: Ctx) -> None:
    session, tenant, _ = ctx
    _, their_maker = await _foreign_brand_and_maker(session)

    with pytest.raises(NotFoundError):
        await _brand(session, tenant, manufacturer_id=their_maker)

    brand = await _brand(session, tenant)
    brand_id = brand.id
    with pytest.raises(NotFoundError):
        await service.update_brand(
            session, tenant, brand_id, BrandUpdate(manufacturer_id=their_maker)
        )


async def test_a_vendor_link_names_its_maker_and_can_drop_it(ctx: Ctx) -> None:
    session, tenant, _ = ctx
    maker = await _maker(session, tenant)
    item = await service.create_item(
        session, tenant, _item(_code("BO"), await _base_unit(session, tenant))
    )
    vendor = await seed_support.seed_vendor(session, company_id=tenant.company_id)

    link = await service.link_vendor(
        session,
        tenant,
        item.id,
        VendorLinkCreate(
            vendor_id=vendor.id, manufacturer_id=maker.id, manufacturer_part_number="DCW-60"
        ),
    )
    assert (link.manufacturer_id, link.manufacturer_part_number) == (maker.id, "DCW-60")

    links, _ = await service.list_vendor_links(
        session, tenant, item.id, PageParams(page=1, limit=10)
    )
    assert [row.manufacturer_part_number for row in links] == ["DCW-60"]

    # The two maker fields are the exception to "null means leave alone".
    cleared = await service.update_vendor_link(
        session,
        tenant,
        link.id,
        VendorLinkUpdate(manufacturer_id=None, manufacturer_part_number=None, lead_time_days=9),
    )
    assert (cleared.manufacturer_id, cleared.manufacturer_part_number) == (None, None)
    assert cleared.lead_time_days == 9
    untouched = await service.update_vendor_link(
        session, tenant, link.id, VendorLinkUpdate(vendor_item_code=None, lead_time_days=10)
    )
    assert untouched.lead_time_days == 10


async def test_a_vendor_link_refuses_another_company_s_maker(ctx: Ctx) -> None:
    session, tenant, _ = ctx
    item = await service.create_item(
        session, tenant, _item(_code("BO"), await _base_unit(session, tenant))
    )
    item_id = item.id
    vendor = await seed_support.seed_vendor(session, company_id=tenant.company_id)
    vendor_id = vendor.id
    _, their_maker = await _foreign_brand_and_maker(session)

    with pytest.raises(NotFoundError):
        await service.link_vendor(
            session,
            tenant,
            item_id,
            VendorLinkCreate(vendor_id=vendor_id, manufacturer_id=their_maker),
        )

    link = await service.link_vendor(
        session, tenant, item_id, VendorLinkCreate(vendor_id=vendor_id)
    )
    link_id = link.id
    with pytest.raises(NotFoundError):
        await service.update_vendor_link(
            session, tenant, link_id, VendorLinkUpdate(manufacturer_id=their_maker)
        )


async def test_a_retired_value_cannot_be_reactivated_by_a_patch(ctx: Ctx) -> None:
    session, tenant, _ = ctx
    maker = await _maker(session, tenant)
    maker_id = maker.id
    await service.delete_manufacturer(session, tenant, maker_id, reason="Closed down")

    with pytest.raises(NotFoundError):
        await service.update_manufacturer(
            session, tenant, maker_id, ManufacturerUpdate(is_active=True)
        )


async def test_the_lifecycle_refuses_an_illegal_move_before_writing(ctx: Ctx) -> None:
    """ARCHIVED is terminal: asserting it twice is an error, not a second audit row."""
    with pytest.raises(InvalidStateTransitionError):
        service.BRAND_LIFECYCLE.assert_transition(
            service.MasterLifecycleState.ARCHIVED, service.MasterLifecycleState.INACTIVE
        )


# ------------------------------------------------------- the item list by brand --


async def test_the_item_list_filtered_by_brand_returns_only_that_brand(ctx: Ctx) -> None:
    session, tenant, principal = ctx
    unit = await _base_unit(session, tenant)
    house = await _brand(session, tenant)
    partner = await _brand(session, tenant)
    ours = await service.create_item(session, tenant, _item(_code("FG"), unit, brand_id=house.id))
    await service.create_item(session, tenant, _item(_code("BO"), unit, brand_id=partner.id))
    await service.create_item(session, tenant, _item(_code("RM"), unit))

    rows, pagination = await service.search_items(
        session, tenant, principal, ItemFilters(brand_id=house.id), PageParams(page=1, limit=50)
    )

    assert [row.id for row in rows] == [ours.id]
    assert pagination.total == 1


async def test_an_item_list_holding_a_branded_item_serialises_on_both_pagers(ctx: Ctx) -> None:
    """The raiseload trap: `brand_id` must be in the list's explicit column set.

    Run through both pagers and both response shapes, because `include_cost`
    changes the SELECT and the keyset pager builds its own.
    """
    session, tenant, principal = ctx
    brand = await _brand(session, tenant)
    item = await service.create_item(
        session, tenant, _item(_code("FG"), await _base_unit(session, tenant), brand_id=brand.id)
    )
    with_cost = Principal(
        user_id=principal.user_id,
        company_id=principal.company_id,
        email=principal.email,
        full_name=principal.full_name,
        is_superuser=False,
        permissions=frozenset({str(Permission.ITEM_VIEW), str(Permission.ITEM_COST_VIEW)}),
    )

    for who in (principal, with_cost):
        rows, _ = await service.search_items(
            session, tenant, who, ItemFilters(search=item.code), PageParams(page=1, limit=10)
        )
        assert [row.brand_id for row in rows] == [brand.id]

        streamed, _ = await service.search_items_keyset(
            session, tenant, who, ItemFilters(manufacturer_id=None), CursorParams(limit=200)
        )
        assert brand.id in {row.brand_id for row in streamed}


async def test_the_item_list_filtered_by_maker_returns_only_that_maker(ctx: Ctx) -> None:
    session, tenant, principal = ctx
    unit = await _base_unit(session, tenant)
    maker = await _maker(session, tenant)
    made = await service.create_item(
        session, tenant, _item(_code("BO"), unit, manufacturer_id=maker.id)
    )
    await service.create_item(session, tenant, _item(_code("BO"), unit))

    rows, _ = await service.search_items(
        session,
        tenant,
        principal,
        ItemFilters(manufacturer_id=maker.id),
        PageParams(page=1, limit=50),
    )

    assert [row.id for row in rows] == [made.id]


async def test_the_detail_payload_carries_the_three_new_fields(ctx: Ctx) -> None:
    session, tenant, principal = ctx
    brand = await _brand(session, tenant)
    maker = await _maker(session, tenant)
    item = await service.create_item(
        session,
        tenant,
        _item(
            _code("FG"),
            await _base_unit(session, tenant),
            brand_id=brand.id,
            manufacturer_id=maker.id,
            manufacturer_part_number="MPN-1",
        ),
    )

    detail = await service.get_item(session, tenant, principal, item.id)

    assert (detail.brand_id, detail.manufacturer_id, detail.manufacturer_part_number) == (
        brand.id,
        maker.id,
        "MPN-1",
    )
