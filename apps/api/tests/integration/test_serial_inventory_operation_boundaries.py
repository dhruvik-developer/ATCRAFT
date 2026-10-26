"""Current unsupported serial operations fail safely on real PostgreSQL.

These are refusal regressions, not acceptance tests for named-unit bin moves,
internal consumption, scrap disposal or count reconciliation. Mixed documents
also contain an otherwise valid plain-stock line, so a failure must preserve
that stock and its FIFO history rather than leaving a partially posted document.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.core.config import settings
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, ErrorCode
from src.core.models import Base
from src.core.pagination import PageParams
from src.modules.audit.models import AuditLog
from src.modules.inventory import service as inventory
from src.modules.inventory.models import (
    BinMove,
    BinMoveLine,
    InternalIssue,
    InternalIssueLine,
    InventoryBalance,
    InventoryCostLayer,
    InventoryCostLayerConsumption,
    InventoryTransaction,
    PhysicalCount,
    PhysicalCountLine,
    PhysicalCountStatus,
    ScrapDisposal,
    ScrapDisposalKind,
    ScrapDisposalLine,
    SerialStatus,
    StockSerial,
    TransactionType,
)
from src.modules.inventory.schemas import (
    BinMoveCreate,
    BinMoveLineCreate,
    InternalIssueCreate,
    InternalIssueLineCreate,
    PhysicalCountCreate,
    PhysicalCountEntry,
    PhysicalCountEntryRequest,
    ScrapDisposalCreate,
    ScrapDisposalLineCreate,
    StockSerialBulkCreate,
)
from src.modules.organization.models import Department
from src.modules.warehouses.models import StorageLocation, StorageLocationType
from tests import seed_support
from tests.conftest import TEST_DB_NAME, TEST_DSN
from tests.schema_support import prepare_metadata, report_database_setup_failure

pytestmark = pytest.mark.integration
prepare_metadata()


@dataclass(frozen=True)
class Scope:
    schema: str
    tenant: TenantContext
    other: TenantContext


@dataclass(frozen=True)
class Kit:
    session: AsyncSession
    tenant: TenantContext
    other: TenantContext
    item_id: uuid.UUID
    item_code: str
    plain_item_id: uuid.UUID
    warehouse_id: uuid.UUID
    other_warehouse_id: uuid.UUID
    foreign_warehouse_id: uuid.UUID
    bin_id: uuid.UUID
    department_id: uuid.UUID
    serial_ids: tuple[uuid.UUID, ...]


def _engine(schema: str) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


@pytest.fixture(scope="module")
def _scope() -> Iterator[Scope]:
    # Use the protected local/CI settings resolved by conftest, never an
    # application's source database or a second PostgreSQL installed nearby.
    assert settings.database_url == TEST_DSN
    assert make_url(settings.database_url).database == TEST_DB_NAME
    schema = f"it_serial_boundaries_{uuid.uuid4().hex[:10]}"
    tenant = TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())
    other = TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())

    async def setup() -> None:
        admin = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with admin.begin() as connection:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
                await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        finally:
            await admin.dispose()
        engine = _engine(schema)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(
                    lambda sync: Base.metadata.create_all(sync, checkfirst=False)
                )
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session, session.begin():
                await seed_support.seed_company(session, company_id=tenant.company_id, prefix="SB")
                await seed_support.seed_user(
                    session, company_id=tenant.company_id, user_id=tenant.user_id
                )
                await seed_support.seed_company(session, company_id=other.company_id, prefix="SF")
                await seed_support.seed_user(
                    session, company_id=other.company_id, user_id=other.user_id
                )
        finally:
            await engine.dispose()

    async def teardown() -> None:
        admin = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            await admin.dispose()

    try:
        asyncio.run(setup())
    except Exception as exc:  # pragma: no cover - environment setup
        report_database_setup_failure(exc)
    yield Scope(schema, tenant, other)
    asyncio.run(teardown())


@pytest.fixture
async def kit(_scope: Scope) -> AsyncIterator[Kit]:
    engine = _engine(_scope.schema)
    try:
        async with engine.connect() as connection:
            outer = await connection.begin()
            factory = async_sessionmaker(
                bind=connection,
                expire_on_commit=False,
                autoflush=False,
                join_transaction_mode="create_savepoint",
            )
            async with factory() as session:
                assert await session.scalar(text("SELECT current_schema()")) == _scope.schema
                async with transaction(session):
                    yield_kit = await _seed_kit(session, _scope)
                yield yield_kit
            await outer.rollback()
    finally:
        await engine.dispose()


async def _seed_kit(session: AsyncSession, scope: Scope) -> Kit:
    tenant, other = scope.tenant, scope.other
    item = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code="SERIAL-BOUNDARY",
        is_serial_tracked=True,
        can_scrap=True,
        standard_cost=Decimal("12"),
    )
    plain = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code="PLAIN-BOUNDARY",
        can_scrap=True,
        standard_cost=Decimal("5"),
    )
    foreign_item = await seed_support.seed_item(
        session,
        company_id=other.company_id,
        code=item.code,
        is_serial_tracked=True,
        can_scrap=True,
        standard_cost=Decimal("90"),
    )
    warehouse = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code="MAIN-BOUNDARY"
    )
    second = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code="OTHER-BOUNDARY"
    )
    foreign_warehouse = await seed_support.seed_warehouse(
        session, company_id=other.company_id, code=warehouse.code
    )
    location = StorageLocation(
        company_id=tenant.company_id,
        warehouse_id=warehouse.id,
        code="RACK-BOUNDARY",
        name="Boundary test destination",
        location_type=StorageLocationType.BIN,
    )
    department = Department(
        company_id=tenant.company_id, code="DEPT-BOUNDARY", name="Boundary test department"
    )
    session.add_all([location, department])
    await session.flush()
    serials = await inventory.TraceabilityService(session, tenant).bulk_create_serials(
        StockSerialBulkCreate(
            item_id=item.id, serial_numbers=["BOUNDARY-1", "BOUNDARY-2", "BOUNDARY-3"]
        )
    )
    foreign_serials = await inventory.TraceabilityService(session, other).bulk_create_serials(
        StockSerialBulkCreate(item_id=foreign_item.id, serial_numbers=["BOUNDARY-1"])
    )
    await inventory.post_movements(
        session,
        tenant,
        [
            inventory.MovementRequest(
                item_id=item.id,
                warehouse_id=warehouse.id if index < 2 else second.id,
                serial_id=serial.id,
                quantity=Decimal("1"),
                transaction_type=TransactionType.OPENING_BALANCE,
                reference_type="serial_boundary_fixture",
                reason="Opening named stock for operation boundary checks",
                unit_cost=Decimal("12"),
            )
            for index, serial in enumerate(serials)
        ]
        + [
            inventory.MovementRequest(
                item_id=plain.id,
                warehouse_id=warehouse.id,
                quantity=Decimal("4"),
                transaction_type=TransactionType.OPENING_BALANCE,
                reference_type="serial_boundary_fixture",
                reason="Opening ordinary stock to detect partial posting",
                unit_cost=Decimal("5"),
            )
        ],
    )
    await inventory.post_movements(
        session,
        other,
        [
            inventory.MovementRequest(
                item_id=foreign_item.id,
                warehouse_id=foreign_warehouse.id,
                serial_id=foreign_serials[0].id,
                quantity=Decimal("1"),
                transaction_type=TransactionType.OPENING_BALANCE,
                reference_type="serial_boundary_fixture",
                reason="A different tenant's identically labelled stock must remain untouched",
                unit_cost=Decimal("90"),
            )
        ],
    )
    return Kit(
        session=session,
        tenant=tenant,
        other=other,
        item_id=item.id,
        item_code=item.code,
        plain_item_id=plain.id,
        warehouse_id=warehouse.id,
        other_warehouse_id=second.id,
        foreign_warehouse_id=foreign_warehouse.id,
        bin_id=location.id,
        department_id=department.id,
        serial_ids=tuple(serial.id for serial in serials),
    )


STOCK_MODELS = (
    InventoryBalance,
    InventoryTransaction,
    StockSerial,
    InventoryCostLayer,
    InventoryCostLayerConsumption,
)
OPERATION_MODELS = (
    AuditLog,
    BinMove,
    BinMoveLine,
    InternalIssue,
    InternalIssueLine,
    ScrapDisposal,
    ScrapDisposalLine,
    PhysicalCount,
    PhysicalCountLine,
)


async def _snapshot(session: AsyncSession, *, stock_only: bool = False) -> dict[str, Any]:
    """One read compares every persisted column, including other tenants' rows."""
    models = STOCK_MODELS if stock_only else STOCK_MODELS + OPERATION_MODELS
    # Identifiers come only from the owning ORM models. The private schema is
    # already asserted by the fixture; no other test or application schema is read.
    fields = ", ".join(
        f"'{model.__tablename__}', (SELECT coalesce(jsonb_agg(to_jsonb(row) ORDER BY row.id), "
        f"'[]'::jsonb) FROM {model.__tablename__} row)"
        for model in models
    )
    return (await session.execute(text(f"SELECT jsonb_build_object({fields})"))).scalar_one()


async def _count(kit: Kit, *, include_plain: bool) -> tuple[uuid.UUID, dict[uuid.UUID, Any]]:
    service = inventory.PhysicalCountService(kit.session, kit.tenant)
    count = await service.create(
        PhysicalCountCreate(
            warehouse_id=kit.warehouse_id,
            item_ids=[kit.item_id, kit.plain_item_id] if include_plain else [kit.item_id],
            reason="Check named stock without changing unrelated warehouses or tenants",
        )
    )
    await service.start(count.id)
    lines, page = await service.list_lines(count.id, PageParams(limit=20))
    assert not page.has_next
    expected = {kit.item_id, kit.plain_item_id} if include_plain else {kit.item_id}
    assert {line.item_id for line in lines} == expected
    assert all(line.location_id is None and line.batch_id is None for line in lines)
    by_item = {line.item_id: line for line in lines}
    assert by_item[kit.item_id].system_quantity == Decimal("2")
    return count.id, by_item


@pytest.mark.parametrize("operation", ["bin_move", "internal_issue", "scrap_disposal"])
async def test_serial_document_refusal_preserves_both_tenants_and_every_stock_record(
    kit: Kit, operation: str
) -> None:
    if operation == "scrap_disposal":
        await inventory.post_movements(
            kit.session,
            kit.tenant,
            [
                inventory.MovementRequest(
                    item_id=item_id,
                    warehouse_id=kit.warehouse_id,
                    serial_id=serial_id,
                    quantity=Decimal("1"),
                    transaction_type=TransactionType.SCRAP,
                    reference_type="serial_boundary_fixture",
                    reason="Prepare real scrap stock before checking the disposal boundary",
                )
                for item_id, serial_id in [
                    (kit.plain_item_id, None),
                    (kit.item_id, kit.serial_ids[0]),
                ]
            ],
        )
    before = await _snapshot(kit.session)
    with pytest.raises(BusinessRuleError) as refused:
        if operation == "bin_move":
            await inventory.BinMoveService(kit.session, kit.tenant).create(
                BinMoveCreate(
                    warehouse_id=kit.warehouse_id,
                    reason="Put away ordinary stock followed by a named unit",
                    lines=[
                        BinMoveLineCreate(
                            item_id=item_id, quantity=Decimal("1"), to_location_id=kit.bin_id
                        )
                        for item_id in [kit.plain_item_id, kit.item_id]
                    ],
                )
            )
        elif operation == "internal_issue":
            await inventory.InternalIssueService(kit.session, kit.tenant).create(
                InternalIssueCreate(
                    warehouse_id=kit.warehouse_id,
                    department_id=kit.department_id,
                    reason="Issue ordinary stock followed by a named unit to maintenance",
                    lines=[
                        InternalIssueLineCreate(item_id=item_id, quantity=Decimal("1"))
                        for item_id in [kit.plain_item_id, kit.item_id]
                    ],
                )
            )
        else:
            await inventory.ScrapDisposalService(kit.session, kit.tenant).create(
                ScrapDisposalCreate(
                    warehouse_id=kit.warehouse_id,
                    disposal_kind=ScrapDisposalKind.WRITE_OFF,
                    reason="Write off real ordinary and named scrap without losing traceability",
                    lines=[
                        ScrapDisposalLineCreate(item_id=item_id, quantity=Decimal("1"))
                        for item_id in [kit.plain_item_id, kit.item_id]
                    ],
                )
            )
    assert refused.value.code is ErrorCode.SERIAL_REQUIRED
    assert refused.value.details == {"line_number": 2, "item_code": kit.item_code}
    assert "no contract for naming the units" in refused.value.message
    assert await _snapshot(kit.session) == before


async def test_matching_serial_physical_count_confirms_without_inventory_changes(kit: Kit) -> None:
    count_id, lines = await _count(kit, include_plain=False)
    service = inventory.PhysicalCountService(kit.session, kit.tenant)
    counted = await service.enter_counts(
        count_id,
        PhysicalCountEntryRequest(
            entries=[
                PhysicalCountEntry(line_id=lines[kit.item_id].id, counted_quantity=Decimal("2"))
            ]
        ),
    )
    assert counted.status is PhysicalCountStatus.COUNTED
    before = await _snapshot(kit.session, stock_only=True)
    confirmed = await service.confirm(count_id)
    assert confirmed.status is PhysicalCountStatus.CONFIRMED
    assert confirmed.variance_line_count == 0
    assert confirmed.variance_value == Decimal("0")
    assert confirmed.confirmed_by_id == kit.tenant.user_id
    assert confirmed.confirmed_at is not None
    after = await _snapshot(kit.session, stock_only=True)
    prior_balances = {row["id"]: row for row in before[InventoryBalance.__tablename__]}
    refreshed = 0
    for balance in after[InventoryBalance.__tablename__]:
        if (
            balance["company_id"] == str(kit.tenant.company_id)
            and balance["warehouse_id"] == str(kit.warehouse_id)
            and balance["item_id"] == str(kit.item_id)
        ):
            # Acquiring the count's balance lock refreshes updated_at through
            # the shared upsert. No quantity, valuation or identity may change.
            prior = prior_balances[balance["id"]]
            assert balance["updated_at"] >= prior["updated_at"]
            balance["updated_at"] = prior["updated_at"]
            refreshed += 1
    assert refreshed == 1
    assert after == before
    serial_rows = before[StockSerial.__tablename__]
    assert {row["id"] for row in serial_rows if row["warehouse_id"] == str(kit.warehouse_id)} == {
        str(serial_id) for serial_id in kit.serial_ids[:2]
    }
    assert all(row["status"] == SerialStatus.IN_STOCK for row in serial_rows)


@pytest.mark.parametrize(
    "serial_count", [Decimal("1"), Decimal("3")], ids=["missing-unit", "extra-unit"]
)
async def test_serial_count_variance_refuses_atomically_including_an_ordinary_variance(
    kit: Kit, serial_count: Decimal
) -> None:
    count_id, lines = await _count(kit, include_plain=True)
    service = inventory.PhysicalCountService(kit.session, kit.tenant)
    counted = await service.enter_counts(
        count_id,
        PhysicalCountEntryRequest(
            entries=[
                PhysicalCountEntry(
                    line_id=lines[kit.plain_item_id].id, counted_quantity=Decimal("3")
                ),
                PhysicalCountEntry(line_id=lines[kit.item_id].id, counted_quantity=serial_count),
            ]
        ),
    )
    assert counted.status is PhysicalCountStatus.COUNTED
    before = await _snapshot(kit.session)
    with pytest.raises(BusinessRuleError) as refused:
        await service.confirm(count_id)
    assert refused.value.code is ErrorCode.SERIAL_REQUIRED
    assert refused.value.details == {"item_code": kit.item_code, "item_id": str(kit.item_id)}
    assert "a serial number is required to move it" in refused.value.message
    assert await _snapshot(kit.session) == before
    remaining = await service.get_count(count_id)
    assert remaining.status is PhysicalCountStatus.COUNTED
    assert remaining.confirmed_at is None
    assert remaining.confirmed_by_id is None
