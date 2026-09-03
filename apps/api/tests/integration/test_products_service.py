"""Service-level tests for the item master against a real PostgreSQL.

These need PostgreSQL because the behaviour under test is not expressible in
SQLite: trigram search, partial and expression unique indexes, the materialised
path subtree query, and the audit row written inside the same transaction.

Isolation: every test builds its own throwaway schema and drops it afterwards,
so the suite can run against a developer database without disturbing it and two
parallel runs cannot collide.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.config import settings
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    BusinessRuleError,
    DuplicateError,
    InvalidStateTransitionError,
    NotFoundError,
)
from src.core.models import Base
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.modules.audit.models import AuditLog
from src.modules.products import service
from src.modules.products.models import (
    ItemGroup,
    PriceType,
    ProductionType,
    UomCategory,
)
from src.modules.products.repository import ItemRepository
from src.modules.products.schemas import (
    CategoryCreate,
    CategoryUpdate,
    ItemCreate,
    ItemDetailWithCost,
    ItemFilters,
    ItemStatusChange,
    ItemUpdate,
    PriceCreate,
    UomConversionCreate,
    UomCreate,
    VariantCreate,
    VendorLinkCreate,
)
from src.modules.vendors.models import Vendor, VendorType
from tests import seed_support
from tests.schema_support import prepare_metadata

pytestmark = pytest.mark.integration


# Foreign keys reach into modules that are built alongside this one, and
# ``create_all`` resolves them against the metadata rather than the database.
# ``prepare_metadata`` imports every module that exists and stands in for the
# targets that are still missing - naming none of them, so nothing here goes
# stale when the owning module lands.
prepare_metadata()


@pytest.fixture(scope="module")
def _schema() -> Iterator[tuple[str, uuid.UUID, uuid.UUID]]:
    """Create this module's private schema once, with its company and user.

    Synchronous, driving its own throwaway event loop: a module-scoped *async*
    fixture would need a module-scoped loop, and asyncpg connections cannot be
    shared across the function-scoped loops pytest-asyncio gives each test.

    Building the schema once rather than per test is the difference between a
    suite that runs in seconds and one that runs in minutes - ``create_all``
    over the full ERP metadata is not cheap, and it grows with every module.
    """
    schema = f"it_products_{uuid.uuid4().hex[:10]}"
    company_id = uuid.uuid4()
    user_id = uuid.uuid4()

    async def setup() -> None:
        admin = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                # The GIN indexes on items.code/name use gin_trgm_ops.
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        finally:
            await admin.dispose()

        engine = _schema_engine(schema)
        try:
            async with engine.begin() as conn:
                # checkfirst=False is load-bearing: the shared conftest already
                # created these tables in `public`, which stays on the search
                # path for the pg_trgm operator classes. With checkfirst=True
                # SQLAlchemy would find them there, create nothing here, and
                # every insert would land in the shared schema instead.
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
                    email="tester@atcraft.test",
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
        pytest.skip(f"PostgreSQL is not available for integration tests: {exc}")

    yield schema, company_id, user_id
    asyncio.run(teardown())


def _schema_engine(schema: str):
    """Engine pinned to one schema. `public` stays on the path for pg_trgm."""
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


@pytest_asyncio.fixture
async def ctx(
    _schema: tuple[str, uuid.UUID, uuid.UUID],
) -> AsyncIterator[tuple[AsyncSession, TenantContext, Principal]]:
    """A session whose writes are rolled back at the end of each test.

    The outer transaction is owned here and never committed, so tests share one
    prepared schema without sharing data. `join_transaction_mode` makes the
    service layer's own `transaction(...)` blocks nest as savepoints, which lets
    a service commit normally while the test still leaves nothing behind.
    """
    schema, company_id, user_id = _schema
    engine = _schema_engine(schema)

    tenant = TenantContext(company_id=company_id, user_id=user_id)
    principal = Principal(
        user_id=user_id,
        company_id=company_id,
        email="tester@atcraft.test",
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


async def seed_units(session: AsyncSession, tenant: TenantContext) -> dict[str, uuid.UUID]:
    """NOS (base), BOX = 24 NOS, and KG in a different measurement category."""
    nos = await service.create_uom(
        session,
        tenant,
        UomCreate(code="NOS", name="Numbers", uom_category=UomCategory.COUNT, is_base=True),
    )
    box = await service.create_uom(
        session, tenant, UomCreate(code="BOX", name="Box", uom_category=UomCategory.COUNT)
    )
    kg = await service.create_uom(
        session,
        tenant,
        UomCreate(code="KG", name="Kilogram", uom_category=UomCategory.WEIGHT, is_base=True),
    )
    await service.create_conversion(
        session,
        tenant,
        UomConversionCreate(from_uom_id=box.id, to_uom_id=nos.id, factor=Decimal("24")),
    )
    return {"NOS": nos.id, "BOX": box.id, "KG": kg.id}


def item_payload(code: str, base_uom_id: uuid.UUID, **overrides: object) -> ItemCreate:
    data: dict[str, object] = {
        "code": code,
        "name": f"Item {code}",
        "item_group": ItemGroup.COMPONENT,
        "base_uom_id": base_uom_id,
        "production_type": ProductionType.BUY,
    }
    data.update(overrides)
    return ItemCreate(**data)  # type: ignore[arg-type]


# -----------------------------------------------------------------------------
# Items
# -----------------------------------------------------------------------------


async def test_create_item_writes_the_audit_row_in_the_same_transaction(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    session, tenant, _ = ctx
    units = await seed_units(session, tenant)

    item = await service.create_item(session, tenant, item_payload("RM-001", units["NOS"]))

    logged = (
        await session.execute(
            select(AuditLog).where(AuditLog.entity_id == item.id, AuditLog.entity_type == "item")
        )
    ).scalar_one()
    assert logged.entity_code == "RM-001"
    assert logged.new_values is not None
    assert logged.new_values["code"] == "RM-001"


async def test_item_code_is_normalised_and_unique_per_company(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    session, tenant, _ = ctx
    units = await seed_units(session, tenant)

    created = await service.create_item(session, tenant, item_payload(" rm 002 ", units["NOS"]))
    assert created.code == "RM 002"

    with pytest.raises(DuplicateError):
        await service.create_item(session, tenant, item_payload("rm  002", units["NOS"]))


async def test_one_item_can_hold_several_roles_at_once(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    """The rule the whole model exists for (master rules section 1)."""
    session, tenant, _ = ctx
    units = await seed_units(session, tenant)

    item = await service.create_item(
        session,
        tenant,
        item_payload(
            "MULTI-1",
            units["NOS"],
            item_group=ItemGroup.RAW_MATERIAL,
            production_type=ProductionType.MAKE_OR_BUY,
            can_purchase=True,
            can_manufacture=True,
            can_sell=True,
            can_be_spare=True,
            can_job_work=True,
        ),
    )

    assert (item.can_purchase, item.can_manufacture, item.can_sell) == (True, True, True)
    assert item.can_be_spare and item.can_job_work
    # Group stayed a label; it did not veto any of the capabilities above.
    assert item.item_group == ItemGroup.RAW_MATERIAL


async def test_purchase_unit_without_a_conversion_is_refused(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    session, tenant, _ = ctx
    units = await seed_units(session, tenant)

    with pytest.raises(BusinessRuleError):
        await service.create_item(
            session,
            tenant,
            item_payload("BAD-UOM", units["NOS"], purchase_uom_id=units["KG"]),
        )


async def test_status_change_is_state_machine_governed_and_audited(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    session, tenant, _ = ctx
    units = await seed_units(session, tenant)
    item = await service.create_item(session, tenant, item_payload("SM-1", units["NOS"]))

    await service.change_item_status(
        session, tenant, item.id, ItemStatusChange(is_active=False, reason="Superseded")
    )
    assert item.is_active is False

    # Held as a plain value: the rejected call below rolls back its savepoint,
    # which expires every object in the identity map. Reading `item.id`
    # afterwards would trigger a refresh, and a lazy load in async SQLAlchemy
    # raises MissingGreenlet rather than quietly issuing a query.
    item_id = item.id

    # Deactivating an already-inactive item is not a transition in the graph.
    with pytest.raises(InvalidStateTransitionError):
        await service.change_item_status(
            session, tenant, item_id, ItemStatusChange(is_active=False, reason="Again")
        )

    status_rows = (
        await session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.entity_id == item_id, AuditLog.action == "STATUS_CHANGE")
        )
    ).scalar_one()
    assert status_rows == 1


async def test_retired_item_disappears_from_reads(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    session, tenant, principal = ctx
    units = await seed_units(session, tenant)
    item = await service.create_item(session, tenant, item_payload("GONE-1", units["NOS"]))

    await service.delete_item(session, tenant, item.id)

    with pytest.raises(NotFoundError):
        await service.get_item(session, tenant, principal, item.id)


async def test_update_cannot_leave_production_type_without_its_capability(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    session, tenant, _ = ctx
    units = await seed_units(session, tenant)
    item = await service.create_item(
        session,
        tenant,
        item_payload(
            "MAKE-1", units["NOS"], production_type=ProductionType.MAKE, can_manufacture=True
        ),
    )

    with pytest.raises(BusinessRuleError):
        await service.update_item(session, tenant, item.id, ItemUpdate(can_manufacture=False))


# -----------------------------------------------------------------------------
# Cost visibility
# -----------------------------------------------------------------------------


async def test_standard_cost_is_absent_without_the_cost_permission(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    session, tenant, principal = ctx
    units = await seed_units(session, tenant)
    item = await service.create_item(
        session, tenant, item_payload("COST-1", units["NOS"], standard_cost=Decimal("123.4500"))
    )

    detail = await service.get_item(session, tenant, principal, item.id)
    assert "standard_cost" not in detail.model_dump()


async def test_standard_cost_is_present_with_the_cost_permission(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    session, tenant, principal = ctx
    units = await seed_units(session, tenant)
    item = await service.create_item(
        session, tenant, item_payload("COST-2", units["NOS"], standard_cost=Decimal("123.4500"))
    )

    privileged = Principal(
        user_id=principal.user_id,
        company_id=principal.company_id,
        email=principal.email,
        full_name=principal.full_name,
        is_superuser=False,
        permissions=frozenset({str(Permission.ITEM_VIEW), str(Permission.ITEM_COST_VIEW)}),
    )

    detail = await service.get_item(session, tenant, privileged, item.id)
    assert isinstance(detail, ItemDetailWithCost)
    assert detail.standard_cost == Decimal("123.4500")


async def test_search_rows_omit_cost_without_the_permission(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    session, tenant, principal = ctx
    units = await seed_units(session, tenant)
    await service.create_item(
        session, tenant, item_payload("COST-3", units["NOS"], standard_cost=Decimal("99.0000"))
    )

    rows, _ = await service.search_items(
        session, tenant, principal, ItemFilters(), PageParams(page=1, limit=10)
    )
    assert rows and "standard_cost" not in rows[0].model_dump()


# -----------------------------------------------------------------------------
# Search
# -----------------------------------------------------------------------------


async def test_search_filters_by_term_capability_and_category_subtree(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    session, tenant, principal = ctx
    units = await seed_units(session, tenant)

    hardware = await service.create_category(
        session, tenant, CategoryCreate(code="HW", name="Hardware")
    )
    handles = await service.create_category(
        session, tenant, CategoryCreate(code="HW-HANDLE", name="Handles", parent_id=hardware.id)
    )
    finishes = await service.create_category(
        session, tenant, CategoryCreate(code="HW-FINISH", name="Finishes", parent_id=handles.id)
    )
    other = await service.create_category(
        session, tenant, CategoryCreate(code="PACK", name="Packing")
    )

    await service.create_item(
        session,
        tenant,
        item_payload(
            "ROSE-HANDLE", units["NOS"], name="Rose Handle", category_id=finishes.id, can_sell=True
        ),
    )
    await service.create_item(
        session,
        tenant,
        item_payload(
            "LOCK-BODY",
            units["NOS"],
            # Name carries "handle" on purpose: the assertions below use this
            # item to show the term matching by NAME while can_sell=False then
            # filters it out. With a name of just "Lock Body" the capability
            # filter would appear to work while actually testing nothing.
            name="Lock Body Handle Set",
            category_id=handles.id,
            can_sell=False,
            can_purchase=True,
        ),
    )
    await service.create_item(
        session,
        tenant,
        item_payload(
            "CARTON-A",
            units["NOS"],
            name="Carton Handle Print",
            category_id=other.id,
            can_sell=True,
        ),
    )

    # Subtree: two levels below the requested root, and nothing from the sibling.
    rows, _ = await service.search_items(
        session,
        tenant,
        principal,
        ItemFilters(category_id=hardware.id),
        PageParams(page=1, limit=50),
    )
    assert {row.code for row in rows} == {"ROSE-HANDLE", "LOCK-BODY"}

    # Without the subtree the root itself holds no items.
    rows, _ = await service.search_items(
        session,
        tenant,
        principal,
        ItemFilters(category_id=hardware.id, include_subcategories=False),
        PageParams(page=1, limit=50),
    )
    assert rows == []

    # Trigram term matches code or name, anywhere in the string.
    rows, _ = await service.search_items(
        session, tenant, principal, ItemFilters(search="handle"), PageParams(page=1, limit=50)
    )
    assert {row.code for row in rows} == {"ROSE-HANDLE", "LOCK-BODY", "CARTON-A"}

    # Capability flags narrow further, and combine with the term.
    rows, _ = await service.search_items(
        session,
        tenant,
        principal,
        ItemFilters(search="handle", can_sell=True),
        PageParams(page=1, limit=50),
    )
    assert {row.code for row in rows} == {"ROSE-HANDLE", "CARTON-A"}


async def test_search_honours_an_explicit_sort_column(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    """An explicit sort must reach the query instead of raising a 500.

    ``search_page`` used to default with ``order_by or Item.code.asc()``. A
    SQLAlchemy clause has no truth value, so ``or`` evaluating its left operand
    raised ``TypeError: Boolean value of this clause is not defined`` for every
    request that named a sort column - while the *unsorted* path went on working,
    because ``None`` is falsy quite legally.

    Nothing caught it. The unit suite never passed an ``order_by``, and the
    frontend list sends ``sort_by=code`` by default, so the effect was that the
    item list - the most ordinary screen in the application - answered 500 to its
    own first request, and only a browser ever saw it.
    """
    session, tenant, principal = ctx
    units = await seed_units(session, tenant)

    for code, name in (("Z-LAST", "Zinc Bracket"), ("A-FIRST", "Aluminium Rail")):
        await service.create_item(session, tenant, item_payload(code, units["NOS"], name=name))

    ascending = SortParams(sort_by="code", sort_dir="asc").resolve(
        ItemRepository.sortable_fields, default=None
    )
    rows, _ = await service.search_items(
        session,
        tenant,
        principal,
        ItemFilters(),
        PageParams(page=1, limit=50),
        sort_by=ascending,
    )
    assert [row.code for row in rows] == ["A-FIRST", "Z-LAST"]

    descending = SortParams(sort_by="name", sort_dir="desc").resolve(
        ItemRepository.sortable_fields, default=None
    )
    rows, _ = await service.search_items(
        session,
        tenant,
        principal,
        ItemFilters(),
        PageParams(page=1, limit=50),
        sort_by=descending,
    )
    assert [row.code for row in rows] == ["Z-LAST", "A-FIRST"]

    # The unsorted default still falls back to code ascending.
    rows, _ = await service.search_items(
        session, tenant, principal, ItemFilters(), PageParams(page=1, limit=50)
    )
    assert [row.code for row in rows] == ["A-FIRST", "Z-LAST"]


async def test_keyset_paging_walks_the_whole_result_without_repeats(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    session, tenant, principal = ctx
    units = await seed_units(session, tenant)
    for index in range(7):
        await service.create_item(session, tenant, item_payload(f"KS-{index:02d}", units["NOS"]))

    from src.core.pagination import CursorParams

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(5):
        rows, pagination = await service.search_items_keyset(
            session, tenant, principal, ItemFilters(), CursorParams(cursor=cursor, limit=3)
        )
        seen.extend(row.code for row in rows)
        if not pagination.has_next:
            break
        cursor = pagination.next_cursor

    assert len(seen) == 7
    assert len(set(seen)) == 7


# -----------------------------------------------------------------------------
# Categories
# -----------------------------------------------------------------------------


async def test_reparenting_rewrites_every_descendant_path(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    session, tenant, _ = ctx
    root_a = await service.create_category(session, tenant, CategoryCreate(code="A", name="A"))
    root_b = await service.create_category(session, tenant, CategoryCreate(code="B", name="B"))
    child = await service.create_category(
        session, tenant, CategoryCreate(code="A1", name="A1", parent_id=root_a.id)
    )
    grandchild = await service.create_category(
        session, tenant, CategoryCreate(code="A1X", name="A1X", parent_id=child.id)
    )
    assert grandchild.depth == 2

    await service.update_category(session, tenant, child.id, CategoryUpdate(parent_id=root_b.id))

    moved = await service.get_category(session, tenant, grandchild.id)
    assert moved.path.startswith(root_b.path)
    assert moved.depth == 2


async def test_a_category_cannot_be_moved_under_its_own_descendant(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    session, tenant, _ = ctx
    root = await service.create_category(session, tenant, CategoryCreate(code="R", name="R"))
    child = await service.create_category(
        session, tenant, CategoryCreate(code="R1", name="R1", parent_id=root.id)
    )

    with pytest.raises(BusinessRuleError):
        await service.update_category(session, tenant, root.id, CategoryUpdate(parent_id=child.id))


async def test_category_in_use_cannot_be_retired(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    session, tenant, _ = ctx
    units = await seed_units(session, tenant)
    category = await service.create_category(session, tenant, CategoryCreate(code="C", name="C"))
    await service.create_item(
        session, tenant, item_payload("IN-USE", units["NOS"], category_id=category.id)
    )

    with pytest.raises(BusinessRuleError):
        await service.delete_category(session, tenant, category.id)


# -----------------------------------------------------------------------------
# Units and conversions
# -----------------------------------------------------------------------------


async def test_conversion_resolves_through_the_base_unit(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    session, tenant, _ = ctx
    units = await seed_units(session, tenant)
    dozen = await service.create_uom(
        session, tenant, UomCreate(code="DOZ", name="Dozen", uom_category=UomCategory.COUNT)
    )
    await service.create_conversion(
        session,
        tenant,
        UomConversionCreate(from_uom_id=dozen.id, to_uom_id=units["NOS"], factor=Decimal("12")),
    )

    result = await service.convert_quantity(
        session, tenant, quantity=Decimal("2"), from_uom_id=units["BOX"], to_uom_id=dozen.id
    )
    assert result.converted_quantity == Decimal("4.0000")


async def test_conversion_across_measurement_categories_is_refused(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    session, tenant, _ = ctx
    units = await seed_units(session, tenant)

    with pytest.raises(BusinessRuleError):
        await service.convert_quantity(
            session,
            tenant,
            quantity=Decimal("1"),
            from_uom_id=units["KG"],
            to_uom_id=units["NOS"],
        )


async def test_a_second_base_unit_for_a_category_is_refused(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    session, tenant, _ = ctx
    await seed_units(session, tenant)

    with pytest.raises(BusinessRuleError):
        await service.create_uom(
            session,
            tenant,
            UomCreate(code="PCS", name="Pieces", uom_category=UomCategory.COUNT, is_base=True),
        )


async def test_reverse_conversion_row_is_rejected_as_a_duplicate(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    session, tenant, _ = ctx
    units = await seed_units(session, tenant)

    with pytest.raises(DuplicateError):
        await service.create_conversion(
            session,
            tenant,
            UomConversionCreate(
                from_uom_id=units["NOS"], to_uom_id=units["BOX"], factor=Decimal("0.0416")
            ),
        )


# -----------------------------------------------------------------------------
# Variants, prices and vendors
# -----------------------------------------------------------------------------


async def test_duplicate_attribute_combination_is_refused(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    session, tenant, _ = ctx
    units = await seed_units(session, tenant)
    item = await service.create_item(session, tenant, item_payload("VAR-1", units["NOS"]))

    await service.create_variant(
        session,
        tenant,
        item.id,
        VariantCreate(code="VAR-1-AB", name="Antique Brass", finish="Antique Brass"),
    )

    with pytest.raises(DuplicateError):
        await service.create_variant(
            session,
            tenant,
            item.id,
            VariantCreate(code="VAR-1-AB2", name="Antique Brass again", finish="Antique Brass"),
        )


async def test_price_resolution_applies_slabs_and_falls_back_to_the_item_master(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    session, tenant, _ = ctx
    units = await seed_units(session, tenant)
    item = await service.create_item(
        session,
        tenant,
        item_payload("PR-1", units["NOS"], can_sell=True, sale_price=Decimal("150.000000")),
    )

    await service.create_price(
        session,
        tenant,
        item.id,
        PriceCreate(
            price_type=PriceType.SALE,
            price=Decimal("100"),
            min_quantity=Decimal("0"),
            valid_from=date(2026, 4, 1),
        ),
    )
    await service.create_price(
        session,
        tenant,
        item.id,
        PriceCreate(
            price_type=PriceType.SALE,
            price=Decimal("90"),
            min_quantity=Decimal("100"),
            valid_from=date(2026, 4, 1),
        ),
    )

    small = await service.resolve_price(
        session,
        tenant,
        item_id=item.id,
        price_type=PriceType.SALE,
        quantity=Decimal("10"),
        on_date=date(2026, 6, 1),
    )
    assert small.price == Decimal("100.000000")
    assert small.source == "PRICE_LIST"

    bulk = await service.resolve_price(
        session,
        tenant,
        item_id=item.id,
        price_type=PriceType.SALE,
        quantity=Decimal("250"),
        on_date=date(2026, 6, 1),
    )
    assert bulk.price == Decimal("90.000000")

    # Before the price list is effective, the item master is the only source.
    early = await service.resolve_price(
        session,
        tenant,
        item_id=item.id,
        price_type=PriceType.SALE,
        quantity=Decimal("10"),
        on_date=date(2026, 1, 1),
    )
    assert early.source == "ITEM_MASTER"
    assert early.price == Decimal("150.000000")


async def test_overlapping_price_windows_are_refused(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    session, tenant, _ = ctx
    units = await seed_units(session, tenant)
    item = await service.create_item(session, tenant, item_payload("PR-2", units["NOS"]))

    await service.create_price(
        session,
        tenant,
        item.id,
        PriceCreate(
            price_type=PriceType.SALE,
            price=Decimal("100"),
            valid_from=date(2026, 4, 1),
            valid_to=date(2026, 9, 30),
        ),
    )

    with pytest.raises(BusinessRuleError):
        await service.create_price(
            session,
            tenant,
            item.id,
            PriceCreate(
                price_type=PriceType.SALE, price=Decimal("110"), valid_from=date(2026, 9, 1)
            ),
        )


async def test_only_one_preferred_vendor_survives_per_item(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    session, tenant, _ = ctx
    units = await seed_units(session, tenant)
    item = await service.create_item(
        session, tenant, item_payload("VEND-1", units["NOS"], can_purchase=True)
    )

    # Built from the vendors model rather than through its service: this suite is
    # about item-vendor linking, and driving the other module's service would
    # couple the two test suites together. The model still owns the row shape,
    # so a column added there cannot leave this fixture behind.
    async with session.begin():
        vendors = [
            Vendor(
                company_id=tenant.company_id,
                code=f"V{index}-{uuid.uuid4().hex[:6].upper()}",
                name=f"Test Vendor {index}",
                vendor_types=[VendorType.COMPONENT_SUPPLIER],
            )
            for index in range(2)
        ]
        session.add_all(vendors)
        await session.flush()
        vendor_a, vendor_b = (vendor.id for vendor in vendors)

    first = await service.link_vendor(
        session, tenant, item.id, VendorLinkCreate(vendor_id=vendor_a, is_preferred=True)
    )
    second = await service.link_vendor(
        session, tenant, item.id, VendorLinkCreate(vendor_id=vendor_b, is_preferred=True)
    )

    links, _ = await service.list_vendor_links(
        session, tenant, item.id, PageParams(page=1, limit=10)
    )
    preferred = [link.id for link in links if link.is_preferred]
    assert preferred == [second.id]
    assert first.id not in preferred


async def test_vendor_cannot_be_linked_to_a_non_purchasable_item(
    ctx: tuple[AsyncSession, TenantContext, Principal],
) -> None:
    session, tenant, _ = ctx
    units = await seed_units(session, tenant)
    item = await service.create_item(
        session,
        tenant,
        item_payload(
            "NOBUY-1",
            units["NOS"],
            item_group=ItemGroup.FINISHED_GOOD,
            production_type=ProductionType.MAKE,
            can_purchase=False,
            can_manufacture=True,
        ),
    )

    with pytest.raises(BusinessRuleError):
        await service.link_vendor(
            session, tenant, item.id, VendorLinkCreate(vendor_id=uuid.uuid4())
        )
