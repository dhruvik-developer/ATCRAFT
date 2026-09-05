"""Service-level tests for the reservation and reallocation engine.

Everything here needs a real PostgreSQL, because what is under test is exactly
what SQLite cannot express: the generated ``available_quantity`` column that a
reservation has to move without touching on-hand, the check constraints that make
an over-drawn reservation impossible, ``SELECT ... FOR UPDATE``, and the audit and
notification rows written inside the same transaction as the change.

The invariant every test in this module is really defending is one sentence:
``inventory_balances.reserved_quantity`` and ``stock_reservations`` must always
tell the same story, and neither may ever disagree with ``on_hand_quantity``.

Isolation follows ``test_inventory_service.py``: one private schema per module,
created once and dropped afterwards, with every test's writes rolled back.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.clock import today
from src.core.config import settings
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import (
    BusinessRuleError,
    InsufficientStockError,
    InvalidStateTransitionError,
    NotFoundError,
)
from src.core.models import Base
from src.core.pagination import PageParams, SortParams
from src.modules.allocation import service as alloc
from src.modules.allocation.models import (
    DemandType,
    ReallocationRequest,
    ReallocationStatus,
    ReservationStatus,
    ReservationType,
    StockReservation,
)
from src.modules.allocation.schemas import (
    ReallocationFilters,
    ReallocationRequestCreate,
    ReservationFilters,
)
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.inventory import service as inv
from src.modules.inventory.models import InventoryTransaction, StockState, TransactionType
from src.modules.notifications.models import Notification, NotificationType
from src.modules.products.models import ItemGroup
from tests import seed_support
from tests.schema_support import prepare_metadata, report_database_setup_failure

pytestmark = pytest.mark.integration

# Foreign keys reach into modules that are built alongside this one, and
# ``create_all`` resolves them against the metadata rather than the database.
# ``prepare_metadata`` imports every module that exists and stands in for the
# targets that are still missing - naming none of them, so nothing here goes
# stale when the owning module lands.
prepare_metadata()


def _schema_engine(schema: str):
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


@pytest.fixture(scope="module")
def _schema() -> Iterator[tuple[str, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]]:
    """Private schema with a company, a planner, an approver and a second tenant."""
    schema = f"it_allocation_{uuid.uuid4().hex[:10]}"
    company_id = uuid.uuid4()
    other_company_id = uuid.uuid4()
    user_id = uuid.uuid4()
    approver_id = uuid.uuid4()

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
                await conn.run_sync(
                    lambda sync_conn: Base.metadata.create_all(sync_conn, checkfirst=False)
                )
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session, session.begin():
                await _insert_company(session, company_id, "ALC")
                await _insert_company(session, other_company_id, "OTH")
                await _insert_user(session, user_id, company_id, "planner@atcraft.test")
                await _insert_user(session, approver_id, company_id, "manager@atcraft.test")
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

    yield schema, company_id, other_company_id, user_id, approver_id
    asyncio.run(teardown())


async def _insert_company(session: AsyncSession, company_id: uuid.UUID, prefix: str) -> None:
    await seed_support.seed_company(session, company_id=company_id, prefix=prefix)


async def _insert_user(
    session: AsyncSession, user_id: uuid.UUID, company_id: uuid.UUID, email: str
) -> None:
    await seed_support.seed_user(session, company_id=company_id, user_id=user_id, email=email)


# The seeds below return ids because that is all a reservation needs; the rows
# themselves are built by the owning module's model, so the Python-side defaults
# on the item master and the warehouse apply.


async def seed_item(session: AsyncSession, company_id: uuid.UUID, *, code: str) -> uuid.UUID:
    item = await seed_support.seed_item(
        session,
        company_id=company_id,
        code=code,
        item_group=ItemGroup.COMPONENT,
        gst_rate=Decimal("18"),
        standard_cost=Decimal("10"),
    )
    return item.id


async def seed_warehouse(session: AsyncSession, company_id: uuid.UUID, *, code: str) -> uuid.UUID:
    warehouse = await seed_support.seed_warehouse(session, company_id=company_id, code=code)
    return warehouse.id


@pytest_asyncio.fixture
async def ctx(
    _schema: tuple[str, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
) -> AsyncIterator[tuple[AsyncSession, TenantContext, TenantContext, TenantContext]]:
    """Session rolled back after each test, plus planner, approver and other-tenant scopes."""
    schema, company_id, other_company_id, user_id, approver_id = _schema
    engine = _schema_engine(schema)
    tenant = TenantContext(company_id=company_id, user_id=user_id)
    approver = TenantContext(company_id=company_id, user_id=approver_id)
    stranger = TenantContext(company_id=other_company_id, user_id=uuid.uuid4())
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
                yield session, tenant, approver, stranger
            await outer.rollback()
    finally:
        await engine.dispose()


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


async def stock_at(
    session: AsyncSession, tenant: TenantContext, quantity: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """An item and a warehouse holding ``quantity`` of it. Committed to the savepoint."""
    item_id = await seed_item(session, tenant.company_id, code=_code("CMP"))
    warehouse_id = await seed_warehouse(session, tenant.company_id, code=_code("WH"))
    async with transaction(session):
        await inv.post_movement(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            quantity=Decimal(quantity),
            transaction_type=TransactionType.OPENING_BALANCE,
            reference_type="opening",
            reference_id=None,
            reference_code="OPENING",
            reason="allocation fixture opening stock",
            unit_cost=Decimal("10"),
        )
    return item_id, warehouse_id


async def reserve_for(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    quantity: str,
    **overrides: object,
) -> StockReservation:
    kwargs: dict[str, object] = {
        "demand_type": DemandType.SALES_ORDER,
        "demand_id": uuid.uuid4(),
        "demand_code": _code("SO"),
        "reservation_type": ReservationType.HARD,
        "priority": 100,
        "required_date": today() + timedelta(days=30),
        "reason": "test reservation",
    }
    kwargs.update(overrides)
    return await alloc.reserve(
        session,
        tenant,
        item_id=item_id,
        warehouse_id=warehouse_id,
        quantity=Decimal(quantity),
        **kwargs,  # type: ignore[arg-type]
    )


async def balance_of(session: AsyncSession, tenant: TenantContext, item_id, warehouse_id):
    balance = await inv.get_balance(session, tenant, item_id, warehouse_id)
    assert balance is not None
    await session.refresh(balance)
    return balance


# -----------------------------------------------------------------------------
# Reserving
# -----------------------------------------------------------------------------


class TestReserve:
    async def test_reserving_moves_reserved_not_on_hand_and_writes_no_ledger_row(self, ctx) -> None:
        """The whole premise of the module: nothing has moved, so the ledger -
        which answers *why* the balance changed - must stay untouched."""
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")

        reservation = await reserve_for(session, tenant, item_id, warehouse_id, "40")

        balance = await balance_of(session, tenant, item_id, warehouse_id)
        assert balance.on_hand_quantity == Decimal("100.0000")
        assert balance.reserved_quantity == Decimal("40.0000")
        assert balance.available_quantity == Decimal("60.0000")

        ledger_rows = (
            await session.execute(
                select(func.count())
                .select_from(InventoryTransaction)
                .where(
                    InventoryTransaction.item_id == item_id,
                    InventoryTransaction.transaction_type != TransactionType.OPENING_BALANCE,
                )
            )
        ).scalar_one()
        assert ledger_rows == 0, "a reservation must not write an inventory transaction"

        assert reservation.status is ReservationStatus.ACTIVE
        assert reservation.quantity == Decimal("40.0000")

    async def test_reserving_writes_a_reservation_change_audit_entry(self, ctx) -> None:
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")

        reservation = await reserve_for(session, tenant, item_id, warehouse_id, "25")

        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == alloc.AUDIT_ENTITY_RESERVATION,
                    AuditLog.entity_id == reservation.id,
                )
            )
        ).scalar_one()
        assert audit.action == AuditAction.RESERVATION_CHANGE
        assert audit.context["promisable_before"] == "100.0000"
        assert audit.context["promisable_after"] == "75.0000"

    async def test_reserving_more_than_is_available_is_refused(self, ctx) -> None:
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        await reserve_for(session, tenant, item_id, warehouse_id, "80")

        with pytest.raises(InsufficientStockError) as exc:
            await reserve_for(session, tenant, item_id, warehouse_id, "30")
        assert exc.value.details["available"] == "20.0000"

        balance = await balance_of(session, tenant, item_id, warehouse_id)
        assert balance.reserved_quantity == Decimal("80.0000"), "the refusal must not have written"

    async def test_stock_on_qc_hold_is_present_but_not_promisable(self, ctx) -> None:
        """Stricter than the database's ``reserved <= on_hand`` floor on purpose:
        promising goods that are physically here but unusable is how a delivery
        date is missed on the day of dispatch."""
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        async with transaction(session):
            await inv.change_stock_state(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("70"),
                from_state=StockState.AVAILABLE,
                to_state=StockState.QC_HOLD,
                reason="incoming inspection",
            )

        with pytest.raises(InsufficientStockError):
            await reserve_for(session, tenant, item_id, warehouse_id, "50")

        # What is left over the hold is still promisable.
        reservation = await reserve_for(session, tenant, item_id, warehouse_id, "30")
        assert reservation.quantity == Decimal("30.0000")

    async def test_two_reservations_share_one_balance_row(self, ctx) -> None:
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        await reserve_for(session, tenant, item_id, warehouse_id, "40")
        await reserve_for(session, tenant, item_id, warehouse_id, "35")

        balance = await balance_of(session, tenant, item_id, warehouse_id)
        assert balance.reserved_quantity == Decimal("75.0000")
        assert balance.available_quantity == Decimal("25.0000")

    async def test_a_zero_quantity_reservation_is_refused(self, ctx) -> None:
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        with pytest.raises(BusinessRuleError, match="positive quantity"):
            await reserve_for(session, tenant, item_id, warehouse_id, "0")


# -----------------------------------------------------------------------------
# Releasing
# -----------------------------------------------------------------------------


class TestRelease:
    async def test_a_partial_release_returns_exactly_that_much_to_availability(self, ctx) -> None:
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        reservation = await reserve_for(session, tenant, item_id, warehouse_id, "60")

        updated = await alloc.release(
            session,
            tenant,
            reservation_id=reservation.id,
            reason="customer reduced the order",
            quantity=Decimal("25"),
        )

        assert updated.released_quantity == Decimal("25.0000")
        assert updated.status is ReservationStatus.ACTIVE
        balance = await balance_of(session, tenant, item_id, warehouse_id)
        assert balance.reserved_quantity == Decimal("35.0000")
        assert balance.available_quantity == Decimal("65.0000")

    async def test_releasing_without_a_quantity_gives_back_everything_outstanding(
        self, ctx
    ) -> None:
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        reservation = await reserve_for(session, tenant, item_id, warehouse_id, "60")
        await alloc.release(
            session, tenant, reservation_id=reservation.id, reason="trim", quantity=Decimal("10")
        )

        updated = await alloc.release(
            session, tenant, reservation_id=reservation.id, reason="order cancelled"
        )

        assert updated.status is ReservationStatus.RELEASED
        assert updated.released_quantity == Decimal("60.0000")
        assert updated.release_reason == "order cancelled"
        assert updated.released_by_id == tenant.user_id
        balance = await balance_of(session, tenant, item_id, warehouse_id)
        assert balance.reserved_quantity == Decimal("0.0000")

    async def test_releasing_more_than_remains_is_refused(self, ctx) -> None:
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        reservation = await reserve_for(session, tenant, item_id, warehouse_id, "40")

        with pytest.raises(BusinessRuleError, match="only"):
            await alloc.release(
                session,
                tenant,
                reservation_id=reservation.id,
                reason="typo",
                quantity=Decimal("41"),
            )

        balance = await balance_of(session, tenant, item_id, warehouse_id)
        assert balance.reserved_quantity == Decimal("40.0000")

    async def test_a_released_reservation_cannot_be_released_again(self, ctx) -> None:
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        reservation = await reserve_for(session, tenant, item_id, warehouse_id, "40")
        await alloc.release(session, tenant, reservation_id=reservation.id, reason="cancelled")

        with pytest.raises(BusinessRuleError):
            await alloc.release(
                session, tenant, reservation_id=reservation.id, reason="cancelled again"
            )

    async def test_every_release_is_audited_with_its_reason(self, ctx) -> None:
        """Section 83: a reason for every stock event, and the row only keeps the
        latest one - so each individual release has to be in the audit log."""
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        reservation = await reserve_for(session, tenant, item_id, warehouse_id, "60")

        await alloc.release(
            session,
            tenant,
            reservation_id=reservation.id,
            reason="first trim",
            quantity=Decimal("10"),
        )
        await alloc.release(
            session,
            tenant,
            reservation_id=reservation.id,
            reason="second trim",
            quantity=Decimal("10"),
        )

        reasons = {
            row.context["release_reason"]
            for row in (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.entity_type == alloc.AUDIT_ENTITY_RESERVATION,
                        AuditLog.entity_id == reservation.id,
                        AuditLog.action == AuditAction.RESERVATION_CHANGE,
                    )
                )
            )
            .scalars()
            .all()
            if row.context and "release_reason" in row.context
        }
        assert reasons == {"first trim", "second trim"}


# -----------------------------------------------------------------------------
# Consuming
# -----------------------------------------------------------------------------


class TestConsume:
    async def test_consuming_lowers_reserved_and_on_hand_together(self, ctx) -> None:
        """The reason consume exists at all: two columns that must move in one
        transaction or never move."""
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        reservation = await reserve_for(
            session, tenant, item_id, warehouse_id, "60", demand_type=DemandType.PRODUCTION_ORDER
        )

        updated = await alloc.consume(
            session,
            tenant,
            reservation_id=reservation.id,
            quantity=Decimal("60"),
            reference_type="material_issue",
            reference_id=uuid.uuid4(),
            reference_code="MI/2026-27/00001",
        )

        assert updated.status is ReservationStatus.CONSUMED
        assert updated.consumed_quantity == Decimal("60.0000")
        balance = await balance_of(session, tenant, item_id, warehouse_id)
        assert balance.on_hand_quantity == Decimal("40.0000")
        assert balance.reserved_quantity == Decimal("0.0000")
        assert balance.available_quantity == Decimal("40.0000")

    async def test_the_demand_type_decides_the_movement_type(self, ctx) -> None:
        """A dispatch posted as a material issue would put a customer delivery in
        the production consumption report."""
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        reservation = await reserve_for(
            session, tenant, item_id, warehouse_id, "10", demand_type=DemandType.SALES_ORDER
        )

        await alloc.consume(
            session,
            tenant,
            reservation_id=reservation.id,
            quantity=Decimal("10"),
            reference_type="dispatch",
            reference_id=uuid.uuid4(),
            reference_code="DSP/2026-27/00001",
        )

        posted = (
            await session.execute(
                select(InventoryTransaction).where(
                    InventoryTransaction.item_id == item_id,
                    InventoryTransaction.transaction_type == TransactionType.DISPATCH,
                )
            )
        ).scalar_one()
        assert posted.quantity == Decimal("-10.0000")
        assert posted.reference_code == "DSP/2026-27/00001"

    async def test_a_partial_consumption_leaves_the_rest_reserved(self, ctx) -> None:
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        reservation = await reserve_for(
            session, tenant, item_id, warehouse_id, "60", demand_type=DemandType.PRODUCTION_ORDER
        )

        updated = await alloc.consume(
            session,
            tenant,
            reservation_id=reservation.id,
            quantity=Decimal("20"),
            reference_type="material_issue",
            reference_id=None,
            reference_code="MI/PART",
        )

        assert updated.status is ReservationStatus.PARTIALLY_CONSUMED
        balance = await balance_of(session, tenant, item_id, warehouse_id)
        assert balance.on_hand_quantity == Decimal("80.0000")
        assert balance.reserved_quantity == Decimal("40.0000")
        assert balance.available_quantity == Decimal("40.0000")

    async def test_consuming_more_than_remains_leaves_everything_unchanged(self, ctx) -> None:
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        reservation = await reserve_for(
            session, tenant, item_id, warehouse_id, "20", demand_type=DemandType.PRODUCTION_ORDER
        )
        reservation_id = reservation.id

        with pytest.raises(BusinessRuleError):
            await alloc.consume(
                session,
                tenant,
                reservation_id=reservation_id,
                quantity=Decimal("21"),
                reference_type="material_issue",
                reference_id=None,
                reference_code="MI/TOO/BIG",
            )

        balance = await balance_of(session, tenant, item_id, warehouse_id)
        assert balance.on_hand_quantity == Decimal("100.0000")
        assert balance.reserved_quantity == Decimal("20.0000")

    async def test_consuming_records_both_a_movement_and_a_reservation_audit(self, ctx) -> None:
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        reservation = await reserve_for(
            session, tenant, item_id, warehouse_id, "15", demand_type=DemandType.ASSEMBLY_ORDER
        )

        await alloc.consume(
            session,
            tenant,
            reservation_id=reservation.id,
            quantity=Decimal("15"),
            reference_type="assembly_order",
            reference_id=None,
            reference_code="AO/2026-27/00007",
        )

        actions = {
            row.action
            for row in (
                await session.execute(select(AuditLog).where(AuditLog.entity_id == reservation.id))
            )
            .scalars()
            .all()
        }
        assert AuditAction.RESERVATION_CHANGE in actions
        movement_audits = (
            await session.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == AuditAction.INVENTORY_MOVEMENT)
            )
        ).scalar_one()
        assert movement_audits >= 1


# -----------------------------------------------------------------------------
# Available to promise
# -----------------------------------------------------------------------------


class TestAvailableToPromise:
    async def test_atp_is_on_hand_less_everything_promised_or_held(self, ctx) -> None:
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        await reserve_for(session, tenant, item_id, warehouse_id, "30")

        promisable = await alloc.available_to_promise(
            session, tenant, item_id=item_id, warehouse_id=warehouse_id
        )
        assert promisable == Decimal("70.0000")

    async def test_atp_without_a_warehouse_sums_every_position(self, ctx) -> None:
        session, tenant, _, _ = ctx
        item_id, first = await stock_at(session, tenant, "100")
        second = await seed_warehouse(session, tenant.company_id, code=_code("WH"))
        async with transaction(session):
            await inv.post_movement(
                session,
                tenant,
                item_id=item_id,
                warehouse_id=second,
                quantity=Decimal("40"),
                transaction_type=TransactionType.OPENING_BALANCE,
                reference_type="opening",
                reference_id=None,
                reference_code="OPENING",
                reason="second warehouse",
                unit_cost=Decimal("10"),
            )
        await reserve_for(session, tenant, item_id, first, "30")

        assert await alloc.available_to_promise(session, tenant, item_id=item_id) == Decimal(
            "110.0000"
        )


# -----------------------------------------------------------------------------
# Reallocation
# -----------------------------------------------------------------------------


class TestSuggestReallocation:
    async def test_free_stock_is_netted_off_before_any_donor_is_considered(self, ctx) -> None:
        """Taking stock from another demand when there is unreserved stock on the
        shelf would be gratuitous."""
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        await reserve_for(session, tenant, item_id, warehouse_id, "40", priority=500)

        candidates = await alloc.suggest_reallocation(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            quantity_needed=Decimal("50"),
            for_demand_type=DemandType.PRODUCTION_ORDER,
            for_demand_id=uuid.uuid4(),
            required_date=today() + timedelta(days=7),
            priority=10,
        )
        assert candidates == [], "60 is free; nothing needs to be taken"

    async def test_only_shortfall_is_sought_from_donors(self, ctx) -> None:
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        await reserve_for(session, tenant, item_id, warehouse_id, "90", priority=500)

        candidates = await alloc.suggest_reallocation(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            quantity_needed=Decimal("30"),
            for_demand_type=DemandType.PRODUCTION_ORDER,
            for_demand_id=uuid.uuid4(),
            required_date=today() + timedelta(days=7),
            priority=10,
        )
        assert len(candidates) == 1
        # 10 is already free, so only 20 has to come from the donor.
        assert candidates[0].takeable_quantity == Decimal("20.0000")

    async def test_a_more_urgent_holder_is_never_suggested(self, ctx) -> None:
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        await reserve_for(session, tenant, item_id, warehouse_id, "100", priority=5)

        candidates = await alloc.suggest_reallocation(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            quantity_needed=Decimal("50"),
            for_demand_type=DemandType.PRODUCTION_ORDER,
            for_demand_id=uuid.uuid4(),
            required_date=today() + timedelta(days=7),
            priority=10,
        )
        assert candidates == []

    async def test_a_soft_holder_outranks_a_hard_one_in_the_suggestion(self, ctx) -> None:
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        hard = await reserve_for(
            session,
            tenant,
            item_id,
            warehouse_id,
            "50",
            priority=500,
            reservation_type=ReservationType.HARD,
        )
        soft = await reserve_for(
            session,
            tenant,
            item_id,
            warehouse_id,
            "50",
            priority=500,
            reservation_type=ReservationType.SOFT,
        )

        candidates = await alloc.suggest_reallocation(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            quantity_needed=Decimal("80"),
            for_demand_type=DemandType.PRODUCTION_ORDER,
            for_demand_id=uuid.uuid4(),
            required_date=today() + timedelta(days=7),
            priority=10,
        )
        assert [c.reservation_id for c in candidates] == [soft.id, hard.id]
        assert candidates[0].requires_approval is False
        assert candidates[1].requires_approval is True


class TestReallocationLifecycle:
    async def _pending_request(
        self, session: AsyncSession, tenant: TenantContext
    ) -> tuple[ReallocationRequest, StockReservation, uuid.UUID, uuid.UUID]:
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        donor = await reserve_for(
            session, tenant, item_id, warehouse_id, "100", priority=500, reason="low-urgency order"
        )
        read = await alloc.AllocationService(session, tenant).create_reallocation_request(
            ReallocationRequestCreate(
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("40"),
                to_demand_type=DemandType.PRODUCTION_ORDER,
                to_demand_id=uuid.uuid4(),
                to_demand_code=_code("MO"),
                priority=10,
                required_date=today() + timedelta(days=5),
                reason="rush order for a key customer",
            )
        )
        request = (
            await session.execute(
                select(ReallocationRequest).where(ReallocationRequest.id == read.id)
            )
        ).scalar_one()
        return request, donor, item_id, warehouse_id

    async def test_raising_a_request_moves_no_stock_and_captures_the_impact(self, ctx) -> None:
        """A HARD reservation is never taken without an approval, so raising the
        request must leave every quantity exactly where it was."""
        session, tenant, _, _ = ctx
        request, donor, item_id, warehouse_id = await self._pending_request(session, tenant)

        assert request.status is ReallocationStatus.PENDING
        assert request.quantity == Decimal("40.0000")
        assert request.impact_summary["donor"]["quantity_after"] == "60.0000"
        assert request.impact_summary["requester"]["priority"] == 10

        balance = await balance_of(session, tenant, item_id, warehouse_id)
        assert balance.reserved_quantity == Decimal("100.0000")
        await session.refresh(donor)
        assert donor.status is ReservationStatus.ACTIVE
        assert donor.released_quantity == Decimal("0.0000")

    async def test_approving_releases_the_donor_and_reserves_for_the_recipient(self, ctx) -> None:
        """One transaction, and the net effect on the balance is exactly zero -
        which is what makes a reallocation incapable of losing or inventing stock."""
        session, tenant, approver, _ = ctx
        request, donor, item_id, warehouse_id = await self._pending_request(session, tenant)

        executed = await alloc.reallocate(
            session,
            approver,
            request_id=request.id,
            approved_by=approver.user_id,
            decision_note="customer escalation",
        )

        assert executed.status is ReallocationStatus.EXECUTED
        assert executed.approved_by_id == approver.user_id
        assert executed.approved_at is not None

        balance = await balance_of(session, tenant, item_id, warehouse_id)
        assert balance.on_hand_quantity == Decimal("100.0000"), "no stock moved"
        assert balance.reserved_quantity == Decimal("100.0000"), "the promise only changed hands"

        await session.refresh(donor)
        assert donor.released_quantity == Decimal("40.0000")
        assert donor.status is ReservationStatus.ACTIVE

        recipient_id = uuid.UUID(executed.impact_summary["executed"]["to_reservation_id"])
        recipient = (
            await session.execute(
                select(StockReservation).where(StockReservation.id == recipient_id)
            )
        ).scalar_one()
        assert recipient.quantity == Decimal("40.0000")
        assert recipient.reservation_type is ReservationType.HARD
        assert recipient.priority == 10
        assert recipient.demand_type is DemandType.PRODUCTION_ORDER

    async def test_executing_writes_a_reallocation_audit_entry(self, ctx) -> None:
        session, tenant, approver, _ = ctx
        request, donor, _, _ = await self._pending_request(session, tenant)

        await alloc.reallocate(
            session, approver, request_id=request.id, approved_by=approver.user_id
        )

        audit = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.entity_type == alloc.AUDIT_ENTITY_REALLOCATION,
                        AuditLog.entity_id == request.id,
                        AuditLog.action == AuditAction.REALLOCATION,
                        AuditLog.new_values.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        executed = [row for row in audit if row.new_values.get("status") == "EXECUTED"]
        assert len(executed) == 1
        assert executed[0].context["from_reservation_id"] == str(donor.id)
        assert executed[0].context["quantity"] == "40.0000"

    async def test_the_losing_demands_owner_is_notified(self, ctx) -> None:
        """Their order just lost material it was promised. Finding that out from
        a shortage report next week is not acceptable."""
        session, tenant, approver, _ = ctx
        request, donor, _, _ = await self._pending_request(session, tenant)

        await alloc.reallocate(
            session, approver, request_id=request.id, approved_by=approver.user_id
        )

        notification = (
            await session.execute(
                select(Notification).where(
                    Notification.entity_id == donor.id,
                    Notification.notification_type == NotificationType.ALLOCATION_CHANGED,
                )
            )
        ).scalar_one()
        assert notification.user_id == donor.created_by_id == tenant.user_id
        assert "reallocated" in notification.body.lower()

    async def test_an_approver_cannot_approve_their_own_request(self, ctx) -> None:
        session, tenant, _, _ = ctx
        request, _, _, _ = await self._pending_request(session, tenant)

        with pytest.raises(BusinessRuleError, match="someone other than"):
            await alloc.reallocate(
                session, tenant, request_id=request.id, approved_by=tenant.user_id
            )

    async def test_an_executed_request_cannot_be_executed_twice(self, ctx) -> None:
        session, tenant, approver, _ = ctx
        request, _, item_id, warehouse_id = await self._pending_request(session, tenant)
        request_id = request.id

        await alloc.reallocate(
            session, approver, request_id=request_id, approved_by=approver.user_id
        )
        with pytest.raises(InvalidStateTransitionError):
            await alloc.reallocate(
                session, approver, request_id=request_id, approved_by=approver.user_id
            )

        balance = await balance_of(session, tenant, item_id, warehouse_id)
        assert balance.reserved_quantity == Decimal("100.0000")

    async def test_rejecting_leaves_the_donor_untouched(self, ctx) -> None:
        session, tenant, approver, _ = ctx
        request, donor, item_id, warehouse_id = await self._pending_request(session, tenant)

        rejected = await alloc.AllocationService(session, approver).reject_reallocation_request(
            request.id, decision_note="the donor is already kitted"
        )

        assert rejected.status is ReallocationStatus.REJECTED
        await session.refresh(donor)
        assert donor.released_quantity == Decimal("0.0000")
        balance = await balance_of(session, tenant, item_id, warehouse_id)
        assert balance.reserved_quantity == Decimal("100.0000")

    async def test_naming_an_ineligible_donor_is_refused(self, ctx) -> None:
        """The request exists so the eligibility rules cannot be bypassed by id."""
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        protected = await reserve_for(
            session,
            tenant,
            item_id,
            warehouse_id,
            "100",
            priority=500,
            required_date=today() + timedelta(days=1),
        )

        with pytest.raises(BusinessRuleError, match="not an eligible donor"):
            await alloc.AllocationService(session, tenant).create_reallocation_request(
                ReallocationRequestCreate(
                    item_id=item_id,
                    warehouse_id=warehouse_id,
                    quantity=Decimal("40"),
                    to_demand_type=DemandType.PRODUCTION_ORDER,
                    to_demand_id=uuid.uuid4(),
                    priority=10,
                    required_date=today() + timedelta(days=5),
                    reason="want it anyway",
                    from_reservation_id=protected.id,
                )
            )

    async def test_a_donor_is_not_offered_to_two_requests_at_once(self, ctx) -> None:
        session, tenant, _, _ = ctx
        service = alloc.AllocationService(session, tenant)
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        await reserve_for(session, tenant, item_id, warehouse_id, "100", priority=500)

        payload = ReallocationRequestCreate(
            item_id=item_id,
            warehouse_id=warehouse_id,
            quantity=Decimal("100"),
            to_demand_type=DemandType.PRODUCTION_ORDER,
            to_demand_id=uuid.uuid4(),
            priority=10,
            required_date=today() + timedelta(days=5),
            reason="first claim",
        )
        await service.create_reallocation_request(payload)

        with pytest.raises(BusinessRuleError, match="No reservation can give up stock"):
            await service.create_reallocation_request(
                payload.model_copy(update={"to_demand_id": uuid.uuid4()})
            )


# -----------------------------------------------------------------------------
# Housekeeping, listing and tenant isolation
# -----------------------------------------------------------------------------


class TestHousekeeping:
    async def test_an_expired_reservation_gives_its_stock_back(self, ctx) -> None:
        """Stale reservations hide stock from availability, which makes MRP order
        material the company already owns."""
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        reservation = await reserve_for(
            session,
            tenant,
            item_id,
            warehouse_id,
            "60",
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        live = await reserve_for(session, tenant, item_id, warehouse_id, "20")

        expired = await alloc.expire_stale_reservations(session, tenant)

        assert expired == [reservation.id]
        await session.refresh(reservation)
        assert reservation.status is ReservationStatus.EXPIRED
        assert reservation.released_quantity == Decimal("60.0000")
        await session.refresh(live)
        assert live.status is ReservationStatus.ACTIVE

        balance = await balance_of(session, tenant, item_id, warehouse_id)
        assert balance.reserved_quantity == Decimal("20.0000")
        assert balance.available_quantity == Decimal("80.0000")

        # The sweep is a lifecycle transition like any other, and a consumer
        # filtering the audit trail on ``changed_fields == ["status"]`` must
        # see it: this row once silently dropped out.
        lifecycle_rows = list(
            (
                await session.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.entity_type == alloc.AUDIT_ENTITY_RESERVATION,
                        AuditLog.entity_id == reservation.id,
                    )
                    .order_by(AuditLog.created_at, AuditLog.id)
                )
            )
            .scalars()
            .all()
        )
        assert any(
            row.old_values == {"status": "ACTIVE"}
            and row.new_values == {"status": "EXPIRED"}
            and row.changed_fields == ["status"]
            for row in lifecycle_rows
        ), [(row.action, row.old_values, row.new_values) for row in lifecycle_rows]

    async def test_a_reservation_with_no_expiry_is_never_swept(self, ctx) -> None:
        session, tenant, _, _ = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        await reserve_for(session, tenant, item_id, warehouse_id, "60")
        assert await alloc.expire_stale_reservations(session, tenant) == []


class TestListingAndIsolation:
    async def test_reservations_can_be_filtered_by_demand_and_status(self, ctx) -> None:
        session, tenant, _, _ = ctx
        service = alloc.AllocationService(session, tenant)
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        demand_id = uuid.uuid4()
        await reserve_for(
            session,
            tenant,
            item_id,
            warehouse_id,
            "30",
            demand_type=DemandType.SALES_ORDER,
            demand_id=demand_id,
        )
        await reserve_for(session, tenant, item_id, warehouse_id, "20")

        rows, pagination = await service.list_reservations(
            ReservationFilters(demand_type=DemandType.SALES_ORDER, demand_id=demand_id),
            PageParams(page=1, limit=50),
            SortParams(),
        )
        assert pagination.total == 1
        assert rows[0].remaining_quantity == Decimal("30.0000")
        assert rows[0].item_code is not None
        assert rows[0].warehouse_code is not None

    async def test_open_only_hides_closed_reservations(self, ctx) -> None:
        session, tenant, _, _ = ctx
        service = alloc.AllocationService(session, tenant)
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        closed = await reserve_for(session, tenant, item_id, warehouse_id, "30")
        await alloc.release(session, tenant, reservation_id=closed.id, reason="cancelled")
        await reserve_for(session, tenant, item_id, warehouse_id, "20")

        rows, _ = await service.list_reservations(
            ReservationFilters(item_id=item_id, open_only=True),
            PageParams(page=1, limit=50),
            SortParams(),
        )
        assert [row.id for row in rows] != []
        assert closed.id not in {row.id for row in rows}

    async def test_another_tenants_reservation_is_invisible(self, ctx) -> None:
        """Tenant isolation that is never tested is a claim, not a property."""
        session, tenant, _, stranger = ctx
        item_id, warehouse_id = await stock_at(session, tenant, "100")
        reservation = await reserve_for(session, tenant, item_id, warehouse_id, "30")

        rows, _ = await alloc.AllocationService(session, stranger).list_reservations(
            ReservationFilters(item_id=item_id),
            PageParams(page=1, limit=50),
            SortParams(),
        )
        assert rows == []

        # Phrased as "not found", never "forbidden": confirming that a record
        # exists in another tenant is itself a disclosure.
        with pytest.raises(NotFoundError):
            await alloc.release(session, stranger, reservation_id=reservation.id, reason="not mine")

    async def test_the_pending_queue_lists_oldest_first(self, ctx) -> None:
        session, tenant, _, _ = ctx
        service = alloc.AllocationService(session, tenant)
        item_id, warehouse_id = await stock_at(session, tenant, "200")
        await reserve_for(session, tenant, item_id, warehouse_id, "100", priority=500)
        await reserve_for(session, tenant, item_id, warehouse_id, "100", priority=600)

        first = await service.create_reallocation_request(
            ReallocationRequestCreate(
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("50"),
                to_demand_type=DemandType.PRODUCTION_ORDER,
                to_demand_id=uuid.uuid4(),
                priority=10,
                required_date=today() + timedelta(days=5),
                reason="first",
            )
        )
        second = await service.create_reallocation_request(
            ReallocationRequestCreate(
                item_id=item_id,
                warehouse_id=warehouse_id,
                quantity=Decimal("50"),
                to_demand_type=DemandType.PRODUCTION_ORDER,
                to_demand_id=uuid.uuid4(),
                priority=10,
                required_date=today() + timedelta(days=5),
                reason="second",
            )
        )

        rows, _ = await service.list_reallocation_requests(
            ReallocationFilters(status=ReallocationStatus.PENDING),
            PageParams(page=1, limit=50),
            SortParams(),
        )
        assert [row.id for row in rows] == [first.id, second.id]
        assert rows[0].from_demand_code is not None
        assert rows[0].item_code is not None
