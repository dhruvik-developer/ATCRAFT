"""Service-level tests for routings, work centres and machines against PostgreSQL.

These need a real PostgreSQL because the behaviour under test is not expressible
without it: the partial unique index that allows exactly one default approved
routing per item, the composite foreign key that keeps a machine inside its own
work centre, ``DISTINCT ON`` effectivity resolution, the advisory lock that
hands out version numbers, and the audit row written inside the same transaction
as the change it describes.

Isolation follows the pattern in ``test_bom_service.py``: one throwaway schema
per module, built once, with each test's writes rolled back.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.clock import today
from src.core.config import settings
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    AuthorizationError,
    BusinessRuleError,
    DuplicateError,
    InvalidStateTransitionError,
    NotFoundError,
)
from src.core.models import Base
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.products.models import Item, ItemGroup, ProductionType, UnitOfMeasure, UomCategory
from src.modules.routing.models import (
    Machine,
    MachineStatus,
    Routing,
    RoutingOperation,
    RoutingStatus,
    WorkCenter,
)
from src.modules.routing.schemas import (
    CopyAsNewVersionRequest,
    MachineCreate,
    MachineStatusChange,
    RoutingCreate,
    RoutingFilters,
    RoutingOperationIn,
    RoutingUpdate,
    WorkCenterCreate,
)
from src.modules.routing.service import (
    MachineService,
    RoutingService,
    WorkCenterService,
    manufacture_lead_time_days,
)
from tests.schema_support import prepare_metadata
from tests.seed_support import seed_company, seed_user

pytestmark = pytest.mark.integration


# Foreign keys reach into modules that are built alongside this one, and
# ``create_all`` resolves them against the metadata rather than the database.
# ``prepare_metadata`` imports every module that exists and stands in for the
# targets that are still missing - naming none of them, so nothing here goes
# stale when the owning module lands.
prepare_metadata()


@pytest.fixture(scope="module")
def _schema() -> Iterator[tuple[str, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]]:
    """One private schema, two companies and two users, built once.

    Synchronous, driving its own throwaway event loop: a module-scoped *async*
    fixture would need a module-scoped loop, and asyncpg connections cannot be
    shared across the function-scoped loops pytest-asyncio gives each test.
    """
    schema = f"it_routing_{uuid.uuid4().hex[:10]}"
    company_id = uuid.uuid4()
    other_company_id = uuid.uuid4()
    author_id = uuid.uuid4()
    reviewer_id = uuid.uuid4()

    async def setup() -> None:
        admin = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        finally:
            await admin.dispose()

        eng = _schema_engine(schema)
        try:
            async with eng.begin() as conn:
                # checkfirst=False: the shared conftest already created these
                # tables in `public`, which stays on the search path for the
                # pg_trgm operator classes.
                await conn.run_sync(
                    lambda sync_conn: Base.metadata.create_all(sync_conn, checkfirst=False)
                )

            factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
            async with factory() as setup_session, setup_session.begin():
                for cid, prefix in ((company_id, "ATC"), (other_company_id, "OTH")):
                    await seed_company(setup_session, company_id=cid, prefix=prefix)
                for uid, who in ((author_id, "author"), (reviewer_id, "reviewer")):
                    await seed_user(
                        setup_session,
                        company_id=company_id,
                        user_id=uid,
                        email=f"{who}@atcraft.test",
                    )
        finally:
            await eng.dispose()

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

    yield schema, company_id, other_company_id, author_id, reviewer_id
    asyncio.run(teardown())


def _schema_engine(schema: str):
    """Engine pinned to one schema. `public` stays on the path for pg_trgm."""
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


class Ctx:
    """Everything a test needs: a session and the two actors."""

    def __init__(
        self,
        session: AsyncSession,
        author: TenantContext,
        reviewer: TenantContext,
        reviewer_principal: Principal,
        author_principal: Principal,
        other_tenant: TenantContext,
    ) -> None:
        self.session = session
        self.author = author
        self.reviewer = reviewer
        self.reviewer_principal = reviewer_principal
        self.author_principal = author_principal
        self.other_tenant = other_tenant

    def routings(self, tenant: TenantContext | None = None) -> RoutingService:
        return RoutingService(self.session, tenant or self.author)

    def work_centers(self, tenant: TenantContext | None = None) -> WorkCenterService:
        return WorkCenterService(self.session, tenant or self.author)

    def machines(self, tenant: TenantContext | None = None) -> MachineService:
        return MachineService(self.session, tenant or self.author)


def _principal(company_id: uuid.UUID, user_id: uuid.UUID) -> Principal:
    return Principal(
        user_id=user_id,
        company_id=company_id,
        email=f"{user_id}@atcraft.test",
        full_name="Tester",
        is_superuser=False,
        permissions=frozenset(str(p) for p in Permission),
        role_codes=("DESIGN_ENGINEER",),
    )


@pytest_asyncio.fixture
async def ctx(
    _schema: tuple[str, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
) -> AsyncIterator[Ctx]:
    """A session whose writes are rolled back at the end of each test.

    ``join_transaction_mode="create_savepoint"`` lets the service layer's own
    ``transaction(...)`` blocks commit normally while the test still leaves the
    database untouched.
    """
    schema, company_id, other_company_id, author_id, reviewer_id = _schema
    eng = _schema_engine(schema)
    try:
        async with eng.connect() as connection:
            outer = await connection.begin()
            factory = async_sessionmaker(
                bind=connection,
                class_=AsyncSession,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            async with factory() as session:
                yield Ctx(
                    session=session,
                    author=TenantContext(company_id=company_id, user_id=author_id),
                    reviewer=TenantContext(company_id=company_id, user_id=reviewer_id),
                    reviewer_principal=_principal(company_id, reviewer_id),
                    author_principal=_principal(company_id, author_id),
                    other_tenant=TenantContext(
                        company_id=other_company_id, user_id=uuid.uuid4()
                    ),
                )
            await outer.rollback()
    finally:
        await eng.dispose()


# -----------------------------------------------------------------------------
# Seeding helpers
# -----------------------------------------------------------------------------


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6].upper()}"


async def seed_item(ctx: Ctx, *, manufactured: bool = True, active: bool = True) -> Item:
    uom = UnitOfMeasure(
        company_id=ctx.author.company_id,
        code=_code("NOS"),
        name="Numbers",
        uom_category=UomCategory.COUNT,
        is_base=False,
    )
    ctx.session.add(uom)
    await ctx.session.flush()

    item = Item(
        company_id=ctx.author.company_id,
        code=_code("HANDLE"),
        name="Rose handle",
        item_group=ItemGroup.FINISHED_GOOD,
        base_uom_id=uom.id,
        production_type=ProductionType.MAKE if manufactured else ProductionType.BUY,
        can_manufacture=manufactured,
        can_purchase=not manufactured,
        is_active=active,
    )
    ctx.session.add(item)
    await ctx.session.flush()
    return item


async def seed_work_center(
    ctx: Ctx,
    *,
    cost_per_hour: str = "600",
    working_hours_per_day: str = "8",
) -> WorkCenter:
    created = await ctx.work_centers().create_work_center(
        WorkCenterCreate(
            code=_code("WC"),
            name="Machining cell",
            cost_per_hour=Decimal(cost_per_hour),
            working_hours_per_day=Decimal(working_hours_per_day),
        )
    )
    return await ctx.session.get_one(WorkCenter, created.id)


async def seed_machine(ctx: Ctx, work_center: WorkCenter) -> Machine:
    created = await ctx.machines().create_machine(
        MachineCreate(code=_code("MC"), name="CNC lathe", work_center_id=work_center.id)
    )
    return await ctx.session.get_one(Machine, created.id)


def operation(
    work_center: WorkCenter,
    *,
    sequence: int = 10,
    setup: str = "30",
    run: str = "1.5",
    queue: str = "0",
    move: str = "0",
    process_cost: str = "0",
    machine_id: uuid.UUID | None = None,
) -> RoutingOperationIn:
    return RoutingOperationIn(
        sequence=sequence,
        operation_code=f"OP{sequence}",
        operation_name=f"Operation {sequence}",
        work_center_id=work_center.id,
        machine_id=machine_id,
        setup_time_minutes=Decimal(setup),
        run_time_per_unit_minutes=Decimal(run),
        queue_time_minutes=Decimal(queue),
        move_time_minutes=Decimal(move),
        process_cost=Decimal(process_cost),
    )


async def create_routing(
    ctx: Ctx, item: Item, *operations: RoutingOperationIn, **overrides: object
) -> Routing:
    payload: dict[str, object] = {
        "code": _code("RT"),
        "item_id": item.id,
        "operations": list(operations),
    }
    payload.update(overrides)
    detail = await ctx.routings().create_routing(RoutingCreate(**payload))  # type: ignore[arg-type]
    return await ctx.session.get_one(Routing, detail.id)


async def approved_routing(
    ctx: Ctx, item: Item, *operations: RoutingOperationIn, **overrides: object
) -> Routing:
    routing = await create_routing(ctx, item, *operations, **overrides)
    await ctx.routings(ctx.reviewer).approve_routing(
        ctx.reviewer_principal, routing.id, reason="Checked against the drawing"
    )
    return routing


async def audit_rows(ctx: Ctx, entity_id: uuid.UUID) -> list[AuditLog]:
    rows = await ctx.session.execute(
        select(AuditLog).where(AuditLog.entity_id == entity_id).order_by(AuditLog.created_at)
    )
    return list(rows.scalars().all())


# -----------------------------------------------------------------------------
# Creation and versioning
# -----------------------------------------------------------------------------


async def test_create_issues_version_one_and_audits_in_the_same_transaction(ctx: Ctx) -> None:
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx)

    routing = await create_routing(ctx, item, operation(centre))

    assert routing.version == 1
    assert routing.status == RoutingStatus.DRAFT
    assert routing.created_by_id == ctx.author.user_id

    entries = await audit_rows(ctx, routing.id)
    assert [e.action for e in entries] == [AuditAction.CREATE]
    assert entries[0].new_values["operation_count"] == 1


async def test_a_second_routing_for_the_same_item_takes_the_next_version(ctx: Ctx) -> None:
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx)

    first = await create_routing(ctx, item, operation(centre))
    second = await create_routing(ctx, item, operation(centre))

    assert (first.version, second.version) == (1, 2)


async def test_a_duplicate_code_is_refused(ctx: Ctx) -> None:
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx)
    existing = await create_routing(ctx, item, operation(centre))

    with pytest.raises(DuplicateError):
        await create_routing(ctx, item, operation(centre), code=existing.code)


async def test_an_item_that_cannot_be_manufactured_gets_no_routing(ctx: Ctx) -> None:
    """Read the capability flag, never item_group (master rules section 1)."""
    bought_in = await seed_item(ctx, manufactured=False)
    centre = await seed_work_center(ctx)

    with pytest.raises(BusinessRuleError, match="manufacturable"):
        await create_routing(ctx, bought_in, operation(centre))


async def test_an_operation_cannot_book_a_machine_from_another_work_centre(ctx: Ctx) -> None:
    item = await seed_item(ctx)
    lathe_cell = await seed_work_center(ctx)
    plating_cell = await seed_work_center(ctx)
    lathe = await seed_machine(ctx, lathe_cell)

    with pytest.raises(BusinessRuleError, match="not in work centre"):
        await create_routing(ctx, item, operation(plating_cell, machine_id=lathe.id))


async def test_an_inactive_work_centre_cannot_be_planned_against(ctx: Ctx) -> None:
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx)
    await ctx.work_centers().set_active(centre.id, active=False, reason="Cell mothballed")

    with pytest.raises(BusinessRuleError, match="inactive"):
        await create_routing(ctx, item, operation(centre))


async def test_another_tenant_cannot_see_the_routing(ctx: Ctx) -> None:
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx)
    routing = await create_routing(ctx, item, operation(centre))

    with pytest.raises(NotFoundError):
        await ctx.routings(ctx.other_tenant).get_routing_detail(routing.id)


# -----------------------------------------------------------------------------
# Approval
# -----------------------------------------------------------------------------


async def test_the_author_cannot_approve_their_own_routing(ctx: Ctx) -> None:
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx)
    routing = await create_routing(ctx, item, operation(centre))

    with pytest.raises(AuthorizationError):
        await ctx.routings().approve_routing(
            ctx.author_principal, routing.id, reason="Looks fine to me"
        )


async def test_approval_stamps_the_approver_and_writes_a_status_audit_row(ctx: Ctx) -> None:
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx)
    routing = await create_routing(ctx, item, operation(centre))

    await ctx.routings(ctx.reviewer).approve_routing(
        ctx.reviewer_principal, routing.id, reason="Checked against the drawing"
    )

    assert routing.status == RoutingStatus.APPROVED
    assert routing.approved_by_id == ctx.reviewer.user_id
    assert routing.approved_at is not None

    actions = [e.action for e in await audit_rows(ctx, routing.id)]
    assert AuditAction.APPROVE in actions


async def test_a_routing_with_no_operations_cannot_be_approved(ctx: Ctx) -> None:
    """Guards the case the payload validation cannot: the steps were deleted
    after the draft was created."""
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx)
    routing = await create_routing(ctx, item, operation(centre))
    await RoutingService(ctx.session, ctx.author).operations.delete_for_routing(routing.id)

    with pytest.raises(BusinessRuleError, match="no operations"):
        await ctx.routings(ctx.reviewer).approve_routing(
            ctx.reviewer_principal, routing.id, reason="Ready"
        )


async def test_approving_a_new_default_demotes_the_previous_one(ctx: Ctx) -> None:
    """The partial unique index allows exactly one default approved routing per
    item, and it is checked per statement - so the demotion must happen first."""
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx)

    first = await approved_routing(ctx, item, operation(centre), is_default=True)
    second = await approved_routing(ctx, item, operation(centre), is_default=True)

    await ctx.session.refresh(first)
    assert first.is_default is False
    assert second.is_default is True


async def test_a_work_centre_deactivated_after_drafting_blocks_approval(ctx: Ctx) -> None:
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx)
    routing = await create_routing(ctx, item, operation(centre))
    await ctx.work_centers().set_active(centre.id, active=False, reason="Cell mothballed")

    with pytest.raises(BusinessRuleError, match="no longer active"):
        await ctx.routings(ctx.reviewer).approve_routing(
            ctx.reviewer_principal, routing.id, reason="Ready"
        )


# -----------------------------------------------------------------------------
# Editing, versioning and retirement
# -----------------------------------------------------------------------------


async def test_an_approved_routing_can_no_longer_be_edited(ctx: Ctx) -> None:
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx)
    routing = await approved_routing(ctx, item, operation(centre))

    with pytest.raises(BusinessRuleError, match="can no longer be edited"):
        await ctx.routings().update_routing(routing.id, RoutingUpdate(name="Tweaked"))


async def test_updating_a_draft_replaces_the_whole_operation_set(ctx: Ctx) -> None:
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx)
    routing = await create_routing(ctx, item, operation(centre, sequence=10))

    detail = await ctx.routings().update_routing(
        routing.id,
        RoutingUpdate(
            operations=[operation(centre, sequence=20), operation(centre, sequence=30)]
        ),
    )

    assert [op.sequence for op in detail.operations] == [20, 30]


async def test_copy_as_new_version_leaves_the_approved_original_untouched(ctx: Ctx) -> None:
    """How an approved routing is "edited": the orders raised against v1 must
    keep resolving to exactly what they were planned against."""
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx)
    original = await approved_routing(ctx, item, operation(centre, setup="30"))

    copy = await ctx.routings().copy_as_new_version(
        original.id, CopyAsNewVersionRequest(code=_code("RT"))
    )

    assert copy.version == original.version + 1
    assert copy.status == RoutingStatus.DRAFT
    assert [op.setup_time_minutes for op in copy.operations] == [Decimal("30.0000")]

    await ctx.session.refresh(original)
    assert original.status == RoutingStatus.APPROVED
    assert original.version == 1


async def test_obsoleting_clears_the_default_flag(ctx: Ctx) -> None:
    """Otherwise the retired routing would keep occupying the partial unique
    index slot while being invisible to the effective-routing query, and its
    replacement could never be approved."""
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx)
    routing = await approved_routing(ctx, item, operation(centre), is_default=True)

    await ctx.routings().obsolete_routing(routing.id, reason="Process replaced")

    assert routing.status == RoutingStatus.OBSOLETE
    assert routing.is_default is False

    replacement = await approved_routing(ctx, item, operation(centre), is_default=True)
    assert replacement.is_default is True


async def test_only_a_draft_can_be_deleted(ctx: Ctx) -> None:
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx)
    approved = await approved_routing(ctx, item, operation(centre))

    with pytest.raises(BusinessRuleError, match="Only a draft"):
        await ctx.routings().delete_draft_routing(approved.id, reason="Mistake")


async def test_deleting_a_draft_takes_its_operations_with_it(ctx: Ctx) -> None:
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx)
    routing = await create_routing(ctx, item, operation(centre))
    routing_id = routing.id

    await ctx.routings().delete_draft_routing(routing_id, reason="Raised against the wrong item")

    remaining = await ctx.session.execute(
        select(RoutingOperation.id).where(RoutingOperation.routing_id == routing_id)
    )
    assert remaining.first() is None
    # The audit entry survives the row it describes (master rules section 41).
    assert [e.action for e in await audit_rows(ctx, routing_id)][-1] == AuditAction.DELETE


async def test_an_invalid_status_move_is_refused_by_the_state_machine(ctx: Ctx) -> None:
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx)
    routing = await approved_routing(ctx, item, operation(centre))
    await ctx.routings().obsolete_routing(routing.id, reason="Replaced")

    with pytest.raises(InvalidStateTransitionError):
        await ctx.routings(ctx.reviewer).approve_routing(
            ctx.reviewer_principal, routing.id, reason="Bring it back"
        )


# -----------------------------------------------------------------------------
# Costing and lead time
# -----------------------------------------------------------------------------


async def test_costing_reads_the_work_centre_rate_through_the_join(ctx: Ctx) -> None:
    """The rate is joined in one statement, not lazy-loaded per operation -
    relationships are ``lazy="raise"``, so an N+1 here would not even run."""
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx, cost_per_hour="600")
    routing = await approved_routing(
        ctx, item, operation(centre, setup="30", run="1.5", process_cost="250")
    )

    result = await ctx.routings().routing_cost(routing.id, Decimal("100"))

    # 180 charged minutes = 3 hours at 600, plus a flat 250.
    assert result.total_cost == Decimal("2050.00")
    assert result.operations[0].work_center_cost_per_hour == Decimal("600.0000")


async def test_the_lead_time_rounds_a_part_day_up(ctx: Ctx) -> None:
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx, working_hours_per_day="8")
    await approved_routing(
        ctx, item, operation(centre, setup="486", run="0"), is_default=True
    )

    result = await ctx.routings().item_lead_time(item.id, Decimal("1"))

    assert result.total_minutes == Decimal("486.0000")
    assert result.lead_time_days == 2


async def test_lead_time_resolves_the_default_approved_routing(ctx: Ctx) -> None:
    item = await seed_item(ctx)
    fast = await seed_work_center(ctx)
    slow = await seed_work_center(ctx)

    await approved_routing(ctx, item, operation(fast, setup="60", run="0"))
    await approved_routing(
        ctx, item, operation(slow, setup="600", run="0"), is_default=True
    )

    result = await ctx.routings().item_lead_time(item.id, Decimal("1"))

    assert result.total_minutes == Decimal("600.0000")


async def test_an_item_with_no_effective_routing_reports_no_lead_time(ctx: Ctx) -> None:
    """MRP falls back to items.lead_time_days rather than planning zero days."""
    item = await seed_item(ctx)

    assert await manufacture_lead_time_days(
        ctx.session, ctx.author, item.id, Decimal("10")
    ) is None


async def test_a_routing_not_yet_effective_is_not_used_for_the_lead_time(ctx: Ctx) -> None:
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx)
    await approved_routing(
        ctx,
        item,
        operation(centre, setup="600", run="0"),
        is_default=True,
        effective_from=today() + timedelta(days=30),
    )

    assert await manufacture_lead_time_days(
        ctx.session, ctx.author, item.id, Decimal("1")
    ) is None


async def test_draft_routings_never_drive_planning(ctx: Ctx) -> None:
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx)
    await create_routing(ctx, item, operation(centre, setup="600", run="0"), is_default=True)

    assert await manufacture_lead_time_days(
        ctx.session, ctx.author, item.id, Decimal("1")
    ) is None


# -----------------------------------------------------------------------------
# Work centres and machines
# -----------------------------------------------------------------------------


async def test_a_work_centre_used_by_a_live_routing_cannot_be_retired(ctx: Ctx) -> None:
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx)
    await approved_routing(ctx, item, operation(centre))

    with pytest.raises(BusinessRuleError, match="draft or approved routing"):
        await ctx.work_centers().retire_work_center(centre.id, reason="Cell closed")


async def test_a_work_centre_still_holding_machines_cannot_be_retired(ctx: Ctx) -> None:
    centre = await seed_work_center(ctx)
    await seed_machine(ctx, centre)

    with pytest.raises(BusinessRuleError, match="machine"):
        await ctx.work_centers().retire_work_center(centre.id, reason="Cell closed")


async def test_an_empty_work_centre_retires_and_leaves_an_audit_trail(ctx: Ctx) -> None:
    centre = await seed_work_center(ctx)

    await ctx.work_centers().retire_work_center(centre.id, reason="Cell closed")

    assert centre.deleted_at is not None
    assert centre.is_active is False
    with pytest.raises(NotFoundError):
        await ctx.work_centers().get_work_center(centre.id)
    assert [e.action for e in await audit_rows(ctx, centre.id)][-1] == AuditAction.DELETE


async def test_completing_maintenance_stamps_the_service_date(ctx: Ctx) -> None:
    centre = await seed_work_center(ctx)
    machine = await seed_machine(ctx, centre)

    await ctx.machines().change_status(
        machine.id, MachineStatusChange(status=MachineStatus.MAINTENANCE, reason="Planned service")
    )
    await ctx.machines().change_status(
        machine.id,
        MachineStatusChange(
            status=MachineStatus.AVAILABLE,
            reason="Service complete",
            next_maintenance_date=today() + timedelta(days=180),
        ),
    )

    assert machine.last_maintenance_date == today()
    assert machine.next_maintenance_date == today() + timedelta(days=180)


async def test_a_machine_under_maintenance_cannot_be_handed_a_job(ctx: Ctx) -> None:
    centre = await seed_work_center(ctx)
    machine = await seed_machine(ctx, centre)
    await ctx.machines().change_status(
        machine.id, MachineStatusChange(status=MachineStatus.MAINTENANCE, reason="Planned service")
    )

    with pytest.raises(InvalidStateTransitionError):
        await ctx.machines().change_status(
            machine.id, MachineStatusChange(status=MachineStatus.RUNNING, reason="Start job")
        )


async def test_a_machine_named_on_a_live_routing_cannot_be_retired(ctx: Ctx) -> None:
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx)
    machine = await seed_machine(ctx, centre)
    await approved_routing(ctx, item, operation(centre, machine_id=machine.id))

    with pytest.raises(BusinessRuleError, match="routing operation"):
        await ctx.machines().retire_machine(machine.id, reason="Sold")


async def test_retiring_a_machine_takes_it_out_of_the_available_pool(ctx: Ctx) -> None:
    centre = await seed_work_center(ctx)
    machine = await seed_machine(ctx, centre)

    await ctx.machines().retire_machine(machine.id, reason="Scrapped")

    assert machine.status == MachineStatus.RETIRED
    assert machine.is_active is False
    assert machine.deleted_at is not None


# -----------------------------------------------------------------------------
# Listing
# -----------------------------------------------------------------------------


async def test_routings_can_be_listed_by_the_work_centre_they_use(ctx: Ctx) -> None:
    item = await seed_item(ctx)
    used = await seed_work_center(ctx)
    unused = await seed_work_center(ctx)
    routing = await create_routing(
        ctx, item, operation(used, sequence=10), operation(used, sequence=20)
    )

    rows, _ = await ctx.routings().list_routings(
        RoutingFilters(work_center_id=used.id), PageParams(), SortParams()
    )
    empty, _ = await ctx.routings().list_routings(
        RoutingFilters(work_center_id=unused.id), PageParams(), SortParams()
    )

    # Two operations at the same centre, but the routing appears exactly once.
    assert [r.id for r in rows] == [routing.id]
    assert empty == []


async def test_versions_are_listed_newest_first(ctx: Ctx) -> None:
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx)
    await create_routing(ctx, item, operation(centre))
    await create_routing(ctx, item, operation(centre))

    rows, _ = await ctx.routings().list_versions(item.id, PageParams())

    assert [r.version for r in rows] == [2, 1]


async def test_an_effective_on_filter_excludes_a_closed_window(ctx: Ctx) -> None:
    item = await seed_item(ctx)
    centre = await seed_work_center(ctx)
    await create_routing(
        ctx,
        item,
        operation(centre),
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 3, 31),
    )

    inside, _ = await ctx.routings().list_routings(
        RoutingFilters(item_id=item.id, effective_on=date(2026, 2, 1)),
        PageParams(),
        SortParams(),
    )
    outside, _ = await ctx.routings().list_routings(
        RoutingFilters(item_id=item.id, effective_on=date(2026, 4, 1)),
        PageParams(),
        SortParams(),
    )

    assert len(inside) == 1
    assert outside == []
