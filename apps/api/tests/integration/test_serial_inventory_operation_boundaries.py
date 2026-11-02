"""Serial inventory operation boundaries against real PostgreSQL.

Named-unit bin moves must preserve identity and FIFO cost. Internal consumption,
scrap disposal and serial-count variances remain refusal cases. Mixed documents
also contain an otherwise valid plain-stock line, so a failure must preserve
that stock and its FIFO history rather than leaving a partially posted document.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select, text
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
from src.core.errors import BusinessRuleError, ErrorCode, NotFoundError
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
    foreign_serial_id: uuid.UUID


def _engine(schema: str) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


def test_bin_move_schema_bounds_and_uniquely_names_serials() -> None:
    item_id = uuid.uuid4()
    serial_id = uuid.uuid4()
    with pytest.raises(PydanticValidationError):
        BinMoveLineCreate(item_id=item_id, quantity=Decimal("2"), serial_ids=[serial_id])
    with pytest.raises(PydanticValidationError):
        BinMoveLineCreate(
            item_id=item_id,
            quantity=Decimal("2"),
            serial_ids=[serial_id, serial_id],
        )
    with pytest.raises(PydanticValidationError):
        BinMoveLineCreate(
            item_id=item_id,
            quantity=Decimal("501"),
            serial_ids=[uuid.uuid4() for _ in range(501)],
        )
    serial_ids = [uuid.uuid4() for _ in range(501)]
    with pytest.raises(PydanticValidationError):
        BinMoveCreate(
            warehouse_id=uuid.uuid4(),
            reason="Keep the whole document bounded",
            lines=[
                BinMoveLineCreate(
                    item_id=item_id,
                    quantity=Decimal("251"),
                    to_location_id=uuid.uuid4(),
                    serial_ids=serial_ids[:251],
                ),
                BinMoveLineCreate(
                    item_id=item_id,
                    quantity=Decimal("250"),
                    from_location_id=uuid.uuid4(),
                    serial_ids=serial_ids[251:],
                ),
            ],
        )
    with pytest.raises(PydanticValidationError):
        BinMoveCreate(
            warehouse_id=uuid.uuid4(),
            reason="Do not identify a unit twice",
            lines=[
                BinMoveLineCreate(
                    item_id=item_id,
                    quantity=Decimal("1"),
                    to_location_id=uuid.uuid4(),
                    serial_ids=[serial_id],
                ),
                BinMoveLineCreate(
                    item_id=item_id,
                    quantity=Decimal("1"),
                    from_location_id=uuid.uuid4(),
                    serial_ids=[serial_id],
                ),
            ],
        )


def test_internal_issue_schema_bounds_and_uniquely_names_serials() -> None:
    item_id = uuid.uuid4()
    serial_id = uuid.uuid4()
    with pytest.raises(PydanticValidationError):
        InternalIssueLineCreate(item_id=item_id, quantity=Decimal("2"), serial_ids=[serial_id])
    with pytest.raises(PydanticValidationError):
        InternalIssueLineCreate(
            item_id=item_id,
            quantity=Decimal("2"),
            serial_ids=[serial_id, serial_id],
        )
    with pytest.raises(PydanticValidationError):
        InternalIssueLineCreate(
            item_id=item_id,
            quantity=Decimal("501"),
            serial_ids=[uuid.uuid4() for _ in range(501)],
        )
    with pytest.raises(PydanticValidationError):
        InternalIssueCreate(
            warehouse_id=uuid.uuid4(),
            department_id=uuid.uuid4(),
            reason="Name every unit only once",
            lines=[
                InternalIssueLineCreate(
                    item_id=item_id,
                    quantity=Decimal("1"),
                    location_id=uuid.uuid4(),
                    serial_ids=[serial_id],
                ),
                InternalIssueLineCreate(
                    item_id=item_id,
                    quantity=Decimal("1"),
                    location_id=uuid.uuid4(),
                    serial_ids=[serial_id],
                ),
            ],
        )
    serial_ids = [uuid.uuid4() for _ in range(501)]
    with pytest.raises(PydanticValidationError):
        InternalIssueCreate(
            warehouse_id=uuid.uuid4(),
            department_id=uuid.uuid4(),
            reason="Keep the whole document bounded",
            lines=[
                InternalIssueLineCreate(
                    item_id=item_id,
                    quantity=Decimal("251"),
                    location_id=uuid.uuid4(),
                    serial_ids=serial_ids[:251],
                ),
                InternalIssueLineCreate(
                    item_id=item_id,
                    quantity=Decimal("250"),
                    location_id=uuid.uuid4(),
                    serial_ids=serial_ids[251:],
                ),
            ],
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
                unit_cost=Decimal("12") + Decimal(index),
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
        foreign_serial_id=foreign_serials[0].id,
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


@pytest.mark.parametrize("operation", ["bin_move", "scrap_disposal"])
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
    expected_message = (
        "must name exactly one serial per unit"
        if operation == "bin_move"
        else "this path does not accept named units"
    )
    assert expected_message in refused.value.message
    assert await _snapshot(kit.session) == before


async def test_named_serial_internal_issue_posts_one_material_issue_per_unit(kit: Kit) -> None:
    serial_ids = list(kit.serial_ids[:2])
    service = inventory.InternalIssueService(kit.session, kit.tenant)
    draft = await service.create(
        InternalIssueCreate(
            warehouse_id=kit.warehouse_id,
            department_id=kit.department_id,
            reason="Issue the named units to maintenance",
            lines=[
                InternalIssueLineCreate(
                    item_id=kit.item_id,
                    quantity=Decimal("2"),
                    serial_ids=serial_ids,
                )
            ],
        )
    )

    assert draft.status == "DRAFT"
    assert draft.lines[0].serial_ids == serial_ids
    assert draft.lines[0].serial_numbers == ["BOUNDARY-1", "BOUNDARY-2"]

    posted = await service.post(draft.id)
    assert posted.status == "POSTED"
    assert posted.total_value == Decimal("25")
    assert posted.lines[0].unit_cost == Decimal("12.5")
    assert posted.lines[0].total_cost == Decimal("25")
    assert posted.lines[0].serial_ids == serial_ids
    assert posted.lines[0].serial_numbers == ["BOUNDARY-1", "BOUNDARY-2"]

    ledger = list(
        (
            await kit.session.scalars(
                select(InventoryTransaction)
                .where(InventoryTransaction.reference_id == draft.id)
                .order_by(InventoryTransaction.created_at, InventoryTransaction.id)
            )
        ).all()
    )
    assert len(ledger) == 2
    assert {row.serial_id for row in ledger} == set(serial_ids)
    assert all(row.transaction_type is TransactionType.MATERIAL_ISSUE for row in ledger)
    assert all(row.quantity == Decimal("-1") for row in ledger)
    assert sum(abs(Decimal(row.total_cost)) for row in ledger) == Decimal("25")

    balance = await kit.session.scalar(
        select(InventoryBalance).where(
            InventoryBalance.item_id == kit.item_id,
            InventoryBalance.warehouse_id == kit.warehouse_id,
        )
    )
    assert balance is not None and balance.on_hand_quantity == Decimal("0")
    serials = list(
        (await kit.session.scalars(select(StockSerial).where(StockSerial.id.in_(serial_ids)))).all()
    )
    assert {serial.id for serial in serials} == set(serial_ids)
    assert all(serial.status is SerialStatus.ISSUED for serial in serials)


@pytest.mark.parametrize(
    ("serial_index", "use_wrong_bin", "expected_message"),
    [
        (None, False, "must name exactly one serial per unit"),
        (2, False, "not in stock at the named source position"),
        (0, True, "not in stock at the named source position"),
    ],
)
async def test_internal_issue_refuses_missing_or_wrong_position_serials_atomically(
    kit: Kit, serial_index: int | None, use_wrong_bin: bool, expected_message: str
) -> None:
    before = await _snapshot(kit.session)
    with pytest.raises(BusinessRuleError) as refused:
        await inventory.InternalIssueService(kit.session, kit.tenant).create(
            InternalIssueCreate(
                warehouse_id=kit.warehouse_id,
                department_id=kit.department_id,
                reason="Do not issue an unnamed or misplaced serial",
                lines=[
                    InternalIssueLineCreate(
                        item_id=kit.item_id,
                        quantity=Decimal("1"),
                        location_id=kit.bin_id if use_wrong_bin else None,
                        serial_ids=[] if serial_index is None else [kit.serial_ids[serial_index]],
                    )
                ],
            )
        )

    assert expected_message in refused.value.message
    if serial_index is None:
        assert refused.value.code is ErrorCode.SERIAL_REQUIRED
    assert await _snapshot(kit.session) == before


async def test_internal_issue_cannot_name_another_tenants_serial(kit: Kit) -> None:
    before = await _snapshot(kit.session)
    with pytest.raises(NotFoundError, match="Stock serial"):
        await inventory.InternalIssueService(kit.session, kit.tenant).create(
            InternalIssueCreate(
                warehouse_id=kit.warehouse_id,
                department_id=kit.department_id,
                reason="Do not issue a serial owned by another tenant",
                lines=[
                    InternalIssueLineCreate(
                        item_id=kit.item_id,
                        quantity=Decimal("1"),
                        serial_ids=[kit.foreign_serial_id],
                    )
                ],
            )
        )
    assert await _snapshot(kit.session) == before


async def test_serial_internal_issue_keeps_the_two_person_approval_control(kit: Kit) -> None:
    item = await seed_support.seed_item(
        kit.session,
        company_id=kit.tenant.company_id,
        code="SERIAL-APPROVAL-BOUNDARY",
        is_serial_tracked=True,
        standard_cost=Decimal("2000"),
    )
    serials = await inventory.TraceabilityService(kit.session, kit.tenant).bulk_create_serials(
        StockSerialBulkCreate(
            item_id=item.id,
            serial_numbers=[f"APPROVAL-{number}" for number in range(1, 14)],
        )
    )
    serial_ids = [serial.id for serial in serials]
    await inventory.post_movements(
        kit.session,
        kit.tenant,
        [
            inventory.MovementRequest(
                item_id=item.id,
                warehouse_id=kit.warehouse_id,
                serial_id=serial.id,
                quantity=Decimal("1"),
                transaction_type=TransactionType.OPENING_BALANCE,
                reference_type="serial_issue_approval_fixture",
                reason="Open expensive units for the approval control regression",
                unit_cost=Decimal("2000"),
            )
            for serial in serials
        ],
    )

    issuer = inventory.InternalIssueService(kit.session, kit.tenant)
    draft = await issuer.create(
        InternalIssueCreate(
            warehouse_id=kit.warehouse_id,
            department_id=kit.department_id,
            reason="Issue the high-value serialized set to maintenance",
            lines=[
                InternalIssueLineCreate(
                    item_id=item.id,
                    quantity=Decimal("13"),
                    serial_ids=serial_ids,
                )
            ],
        )
    )
    assert draft.requires_approval
    assert draft.total_value == Decimal("26000")
    pending = await issuer.submit(draft.id)
    assert pending.status == "PENDING_APPROVAL"

    with pytest.raises(BusinessRuleError, match="someone other than"):
        await issuer.approve(draft.id)

    approver = await seed_support.seed_user(session=kit.session, company_id=kit.tenant.company_id)
    approval_tenant = replace(kit.tenant, user_id=approver.id)
    approved = await inventory.InternalIssueService(kit.session, approval_tenant).approve(draft.id)
    assert approved.status == "POSTED"
    assert approved.approved_by_id == approver.id
    assert approved.total_value == Decimal("26000")
    assert approved.lines[0].serial_ids == serial_ids
    persisted_serials = list(
        (
            await kit.session.scalars(
                select(StockSerial)
                .where(StockSerial.id.in_(serial_ids))
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    assert all(serial.status is SerialStatus.ISSUED for serial in persisted_serials)


async def test_internal_issue_revalidates_serials_after_another_draft_posts(kit: Kit) -> None:
    service = inventory.InternalIssueService(kit.session, kit.tenant)

    async def draft_issue() -> Any:
        return await service.create(
            InternalIssueCreate(
                warehouse_id=kit.warehouse_id,
                department_id=kit.department_id,
                reason="Issue the same named unit from competing drafts",
                lines=[
                    InternalIssueLineCreate(
                        item_id=kit.item_id,
                        quantity=Decimal("1"),
                        serial_ids=[kit.serial_ids[0]],
                    )
                ],
            )
        )

    earlier = await draft_issue()
    later = await draft_issue()
    await service.post(later.id)
    before = await _snapshot(kit.session)

    with pytest.raises(BusinessRuleError):
        await service.post(earlier.id)

    assert await _snapshot(kit.session) == before
    still_draft = await service.get_issue(earlier.id)
    assert still_draft.status == "DRAFT"


async def test_named_serial_bin_move_posts_both_legs_and_preserves_fifo_cost(kit: Kit) -> None:
    serial_id = kit.serial_ids[0]
    moved = await inventory.BinMoveService(kit.session, kit.tenant).create(
        BinMoveCreate(
            warehouse_id=kit.warehouse_id,
            reason="Put the named unit on its assigned rack",
            lines=[
                BinMoveLineCreate(
                    item_id=kit.item_id,
                    quantity=Decimal("1"),
                    to_location_id=kit.bin_id,
                    serial_ids=[serial_id],
                )
            ],
        )
    )

    line = moved.lines[0]
    assert line.serial_ids == [serial_id]
    assert line.serial_numbers == ["BOUNDARY-1"]
    assert line.total_cost == Decimal("12")
    assert moved.total_value == Decimal("12")

    ledger_rows = list(
        (
            await kit.session.scalars(
                select(InventoryTransaction)
                .where(
                    InventoryTransaction.company_id == kit.tenant.company_id,
                    InventoryTransaction.reference_id == moved.id,
                    InventoryTransaction.serial_id == serial_id,
                )
                .order_by(InventoryTransaction.created_at, InventoryTransaction.id)
            )
        ).all()
    )
    assert [row.transaction_type for row in ledger_rows] == [
        TransactionType.TRANSFER_OUT,
        TransactionType.TRANSFER_IN,
    ]
    assert [row.location_id for row in ledger_rows] == [None, kit.bin_id]
    assert [row.quantity for row in ledger_rows] == [Decimal("-1"), Decimal("1")]

    serial = await kit.session.get(StockSerial, serial_id)
    assert serial is not None
    assert serial.status is SerialStatus.IN_STOCK
    assert serial.warehouse_id == kit.warehouse_id
    assert serial.location_id == kit.bin_id
    source = await kit.session.scalar(
        select(InventoryBalance).where(
            InventoryBalance.company_id == kit.tenant.company_id,
            InventoryBalance.item_id == kit.item_id,
            InventoryBalance.warehouse_id == kit.warehouse_id,
            InventoryBalance.location_id.is_(None),
        )
    )
    destination = await kit.session.scalar(
        select(InventoryBalance).where(
            InventoryBalance.company_id == kit.tenant.company_id,
            InventoryBalance.item_id == kit.item_id,
            InventoryBalance.warehouse_id == kit.warehouse_id,
            InventoryBalance.location_id == kit.bin_id,
        )
    )
    assert source is not None and source.on_hand_quantity == Decimal("1")
    assert destination is not None
    assert destination.on_hand_quantity == Decimal("1")
    assert destination.stock_value == Decimal("12")

    service = inventory.TraceabilityService(kit.session, kit.tenant)
    at_destination, _ = await service.list_serials(
        kit.item_id,
        kit.warehouse_id,
        None,
        PageParams(limit=10),
        status=SerialStatus.IN_STOCK,
        location_id=kit.bin_id,
    )
    assert [row.id for row in at_destination] == [serial_id]
    unbinned, _ = await service.list_serials(
        kit.item_id,
        kit.warehouse_id,
        None,
        PageParams(limit=10),
        status=SerialStatus.IN_STOCK,
        unbinned_only=True,
    )
    assert {row.id for row in unbinned} == {kit.serial_ids[1]}


@pytest.mark.parametrize("foreign", [False, True], ids=["unknown-serial", "other-tenant"])
async def test_bin_move_rejects_unresolvable_serial_without_mutating_stock(
    kit: Kit, foreign: bool
) -> None:
    serial_id = kit.foreign_serial_id if foreign else uuid.uuid4()
    before = await _snapshot(kit.session)
    with pytest.raises(NotFoundError):
        await inventory.BinMoveService(kit.session, kit.tenant).create(
            BinMoveCreate(
                warehouse_id=kit.warehouse_id,
                reason="The unit must belong to this company",
                lines=[
                    BinMoveLineCreate(
                        item_id=kit.item_id,
                        quantity=Decimal("1"),
                        to_location_id=kit.bin_id,
                        serial_ids=[serial_id],
                    )
                ],
            )
        )
    assert await _snapshot(kit.session) == before


async def test_bin_move_refuses_serial_outside_the_named_source_bin_atomically(kit: Kit) -> None:
    before = await _snapshot(kit.session)
    with pytest.raises(BusinessRuleError, match="named source position"):
        await inventory.BinMoveService(kit.session, kit.tenant).create(
            BinMoveCreate(
                warehouse_id=kit.warehouse_id,
                reason="Do not move a unit from the wrong rack",
                lines=[
                    BinMoveLineCreate(
                        item_id=kit.item_id,
                        quantity=Decimal("1"),
                        from_location_id=kit.bin_id,
                        serial_ids=[kit.serial_ids[0]],
                    )
                ],
            )
        )
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
