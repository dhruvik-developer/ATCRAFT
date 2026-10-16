"""Service-level tests for bills of materials against a real PostgreSQL.

These need PostgreSQL because the behaviour under test is not expressible
without it: the partial unique index that allows exactly one default approved
structure per item, the composite foreign key that makes the self-loop CHECK
possible, ``DISTINCT ON`` effectivity resolution, the advisory lock that hands
out version numbers, and the audit row written inside the same transaction as
the change it describes.

Isolation follows the pattern in ``test_products_service.py``: one throwaway
schema per module, built once, with each test's writes rolled back.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.clock import today, utcnow
from src.core.config import settings
from src.core.deps import Principal, TenantContext
from src.core.errors import AuthorizationError, BusinessRuleError, InvalidStateTransitionError
from src.core.models import Base
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.bom import engine, service
from src.modules.bom.models import Bom, BomLine, BomOperation, BomStatus, BomType
from src.modules.bom.schemas import (
    BomCreate,
    BomFilters,
    BomLineIn,
    BomOperationIn,
    BomUpdate,
    CopyAsNewVersionRequest,
)
from src.modules.products.models import Item, ItemGroup, ProductionType, UnitOfMeasure, UomCategory
from src.modules.routing.models import (
    Routing,
    RoutingOperation,
    RoutingStatus,
    WorkCenter,
)
from tests.schema_support import prepare_metadata, report_database_setup_failure
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
    schema = f"it_bom_{uuid.uuid4().hex[:10]}"
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
                # pg_trgm operator classes. With checkfirst=True SQLAlchemy
                # would find them there and create nothing here.
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
        report_database_setup_failure(exc)

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
        author_principal: Principal,
        reviewer_principal: Principal,
        other_tenant: TenantContext,
    ) -> None:
        self.session = session
        self.author = author
        self.reviewer = reviewer
        self.author_principal = author_principal
        self.reviewer_principal = reviewer_principal
        self.other_tenant = other_tenant


def _principal(company_id: uuid.UUID, user_id: uuid.UUID, *, superuser: bool = False) -> Principal:
    return Principal(
        user_id=user_id,
        company_id=company_id,
        email=f"{user_id}@atcraft.test",
        full_name="Tester",
        is_superuser=superuser,
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
                    author_principal=_principal(company_id, author_id),
                    reviewer_principal=_principal(company_id, reviewer_id),
                    other_tenant=TenantContext(company_id=other_company_id, user_id=uuid.uuid4()),
                )
            await outer.rollback()
    finally:
        await eng.dispose()


# -----------------------------------------------------------------------------
# Seeding helpers
# -----------------------------------------------------------------------------


async def seed_uom(session: AsyncSession, tenant: TenantContext) -> UnitOfMeasure:
    uom = UnitOfMeasure(
        company_id=tenant.company_id,
        code=f"NOS{uuid.uuid4().hex[:6].upper()}",
        name="Numbers",
        uom_category=UomCategory.COUNT,
        is_base=False,
    )
    session.add(uom)
    await session.flush()
    return uom


async def seed_item(
    session: AsyncSession,
    tenant: TenantContext,
    code: str,
    uom: UnitOfMeasure,
    *,
    manufactured: bool = False,
    component: bool = True,
    standard_cost: Decimal = Decimal("0"),
) -> Item:
    item = Item(
        company_id=tenant.company_id,
        code=f"{code}-{uuid.uuid4().hex[:6].upper()}",
        name=f"{code} name",
        item_group=ItemGroup.COMPONENT,
        base_uom_id=uom.id,
        production_type=ProductionType.MAKE if manufactured else ProductionType.BUY,
        can_manufacture=manufactured,
        can_be_bom_component=component,
        can_purchase=not manufactured,
        standard_cost=standard_cost,
    )
    session.add(item)
    await session.flush()
    return item


def create_payload(parent: Item, components: dict[Item, Decimal], **overrides: object) -> BomCreate:
    payload: dict[str, object] = {
        "item_id": parent.id,
        "lines": [
            BomLineIn(line_number=n, component_item_id=item.id, quantity_per=quantity)
            for n, (item, quantity) in enumerate(components.items(), start=1)
        ],
    }
    payload.update(overrides)
    return BomCreate(**payload)  # type: ignore[arg-type]


async def approved_bom(
    ctx: Ctx, parent: Item, components: dict[Item, Decimal], **kw: object
) -> Bom:
    """Create, submit and approve a structure - the usual precondition."""
    bom, _, _ = await service.create_bom(
        ctx.session, ctx.author, create_payload(parent, components, **kw)
    )
    await service.submit_bom(ctx.session, ctx.author, bom.id, reason="Ready for review")
    await service.approve_bom(
        ctx.session, ctx.reviewer, ctx.reviewer_principal, bom.id, reason="Checked against drawing"
    )
    return bom


# -----------------------------------------------------------------------------
# Creation
# -----------------------------------------------------------------------------


async def test_create_issues_a_code_and_version_and_audits_in_the_same_transaction(
    ctx: Ctx,
) -> None:
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)

    bom, lines, _ = await service.create_bom(
        ctx.session, ctx.author, create_payload(parent, {screw: Decimal("4")})
    )

    assert bom.code.startswith("BOM/")
    assert bom.version == 1
    assert bom.status == BomStatus.DRAFT
    assert len(lines) == 1
    # The line's unit defaults to the component's stock unit.
    assert lines[0].uom_id == uom.id
    assert lines[0].parent_item_id == parent.id

    logged = (
        await ctx.session.execute(
            select(AuditLog).where(AuditLog.entity_id == bom.id, AuditLog.entity_type == "bom")
        )
    ).scalar_one()
    assert logged.action == AuditAction.CREATE
    assert logged.new_values is not None
    assert logged.new_values["line_count"] == 1


async def test_versions_increment_per_item(ctx: Ctx) -> None:
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    other_parent = await seed_item(ctx.session, ctx.author, "KIT2", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)

    first, _, _ = await service.create_bom(
        ctx.session, ctx.author, create_payload(parent, {screw: Decimal("1")})
    )
    second, _, _ = await service.create_bom(
        ctx.session, ctx.author, create_payload(parent, {screw: Decimal("2")})
    )
    unrelated, _, _ = await service.create_bom(
        ctx.session, ctx.author, create_payload(other_parent, {screw: Decimal("1")})
    )

    assert (first.version, second.version) == (1, 2)
    # The counter is per item, not global.
    assert unrelated.version == 1
    assert first.code != second.code != unrelated.code


async def test_an_item_that_cannot_be_manufactured_cannot_have_a_structure(ctx: Ctx) -> None:
    """Master rules section 1: the capability flag decides, not item_group."""
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "BOUGHT", uom, manufactured=False)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)

    with pytest.raises(BusinessRuleError, match="not flagged as manufacturable"):
        await service.create_bom(
            ctx.session, ctx.author, create_payload(parent, {screw: Decimal("1")})
        )


async def test_a_component_not_flagged_as_usable_in_a_bom_is_refused(ctx: Ctx) -> None:
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    service_item = await seed_item(ctx.session, ctx.author, "FREIGHT", uom, component=False)

    with pytest.raises(BusinessRuleError, match="not flagged as usable"):
        await service.create_bom(
            ctx.session, ctx.author, create_payload(parent, {service_item: Decimal("1")})
        )


async def test_an_item_cannot_be_a_component_of_itself(ctx: Ctx) -> None:
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)

    with pytest.raises(BusinessRuleError, match="cannot be a component of itself"):
        await service.create_bom(
            ctx.session, ctx.author, create_payload(parent, {parent: Decimal("1")})
        )


async def test_the_database_also_refuses_a_self_referencing_line(ctx: Ctx) -> None:
    """The service check is the friendly message; this is the floor under it.

    A CHECK constraint cannot look at another table, which is why bom_lines
    carries a denormalised parent_item_id tied to boms(id, item_id).
    """
    from sqlalchemy.exc import IntegrityError

    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)
    bom, _, _ = await service.create_bom(
        ctx.session, ctx.author, create_payload(parent, {screw: Decimal("1")})
    )

    with pytest.raises(IntegrityError):
        async with ctx.session.begin_nested():
            ctx.session.add(
                BomLine(
                    company_id=ctx.author.company_id,
                    bom_id=bom.id,
                    parent_item_id=parent.id,
                    line_number=99,
                    component_item_id=parent.id,
                    quantity_per=Decimal("1"),
                    uom_id=uom.id,
                )
            )
            await ctx.session.flush()


async def test_a_line_stated_in_another_unit_is_refused(ctx: Ctx) -> None:
    """Consolidation adds quantities keyed on item_id, so a second unit under
    the same key would produce a plausible, wrong total."""
    uom = await seed_uom(ctx.session, ctx.author)
    box = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)

    payload = BomCreate(
        item_id=parent.id,
        lines=[
            BomLineIn(
                line_number=1,
                component_item_id=screw.id,
                quantity_per=Decimal("1"),
                uom_id=box.id,
            )
        ],
    )
    with pytest.raises(BusinessRuleError, match="stock unit"):
        await service.create_bom(ctx.session, ctx.author, payload)


async def test_a_line_assigned_to_an_undefined_operation_is_refused(ctx: Ctx) -> None:
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)

    payload = BomCreate(
        item_id=parent.id,
        lines=[
            BomLineIn(
                line_number=1,
                component_item_id=screw.id,
                quantity_per=Decimal("1"),
                operation_sequence=20,
            )
        ],
        operations=[BomOperationIn(operation_sequence=10, name="Assemble")],
    )
    with pytest.raises(BusinessRuleError, match="does not define"):
        await service.create_bom(ctx.session, ctx.author, payload)


async def test_substitutes_are_wired_to_the_persisted_line_ids(ctx: Ctx) -> None:
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "LATCH", uom, manufactured=True)
    spring_a = await seed_item(ctx.session, ctx.author, "SPRING-A", uom)
    spring_b = await seed_item(ctx.session, ctx.author, "SPRING-B", uom)

    payload = BomCreate(
        item_id=parent.id,
        lines=[
            BomLineIn(line_number=1, component_item_id=spring_a.id, quantity_per=Decimal("1")),
            BomLineIn(
                line_number=2,
                component_item_id=spring_b.id,
                quantity_per=Decimal("1"),
                substitute_for_line_number=1,
            ),
        ],
    )
    _, lines, _ = await service.create_bom(ctx.session, ctx.author, payload)

    primary = next(line for line in lines if line.line_number == 1)
    substitute = next(line for line in lines if line.line_number == 2)
    assert substitute.substitute_for_line_id == primary.id


# -----------------------------------------------------------------------------
# Lifecycle and approval
# -----------------------------------------------------------------------------


async def test_approval_records_who_and_when_and_writes_a_status_audit_row(ctx: Ctx) -> None:
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom, standard_cost=Decimal("2"))

    bom = await approved_bom(ctx, parent, {screw: Decimal("3")})

    assert bom.status == BomStatus.APPROVED
    assert bom.approved_by_id == ctx.reviewer.user_id
    assert bom.approved_at is not None

    rows = (
        (
            await ctx.session.execute(
                select(AuditLog).where(
                    AuditLog.entity_id == bom.id, AuditLog.action == AuditAction.APPROVE
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].new_values == {"status": "APPROVED"}
    assert rows[0].context == {"reason": "Checked against drawing"}


async def test_an_author_cannot_approve_their_own_structure(ctx: Ctx) -> None:
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)
    bom, _, _ = await service.create_bom(
        ctx.session, ctx.author, create_payload(parent, {screw: Decimal("1")})
    )
    await service.submit_bom(ctx.session, ctx.author, bom.id, reason="Ready")

    with pytest.raises(AuthorizationError, match="someone other than its author"):
        await service.approve_bom(
            ctx.session, ctx.author, ctx.author_principal, bom.id, reason="Looks fine to me"
        )


async def test_a_superuser_may_approve_their_own_structure(ctx: Ctx) -> None:
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)
    bom, _, _ = await service.create_bom(
        ctx.session, ctx.author, create_payload(parent, {screw: Decimal("1")})
    )
    await service.submit_bom(ctx.session, ctx.author, bom.id, reason="Ready")

    approved = await service.approve_bom(
        ctx.session,
        ctx.author,
        _principal(ctx.author.company_id, ctx.author.user_id, superuser=True),
        bom.id,
        reason="Single-engineer plant",
    )
    assert approved.status == BomStatus.APPROVED


async def test_approval_cannot_skip_submission(ctx: Ctx) -> None:
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)
    bom, _, _ = await service.create_bom(
        ctx.session, ctx.author, create_payload(parent, {screw: Decimal("1")})
    )

    with pytest.raises(InvalidStateTransitionError):
        await service.approve_bom(
            ctx.session, ctx.reviewer, ctx.reviewer_principal, bom.id, reason="Too early"
        )


async def test_an_approved_structure_can_no_longer_be_edited(ctx: Ctx) -> None:
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)
    bom = await approved_bom(ctx, parent, {screw: Decimal("1")})

    with pytest.raises(BusinessRuleError, match="Create a new"):
        await service.update_bom(
            ctx.session, ctx.author, bom.id, BomUpdate(notes="second thoughts")
        )


async def test_approving_a_replacement_demotes_the_previous_default(ctx: Ctx) -> None:
    """The partial unique index allows exactly one default approved structure
    per item, and the demotion happens before the promotion in one transaction.
    """
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)

    first = await approved_bom(ctx, parent, {screw: Decimal("1")})
    second, _, _ = await service.copy_as_new_version(
        ctx.session,
        ctx.author,
        first.id,
        CopyAsNewVersionRequest(reason="Revision B: heavier screw"),
    )
    await service.submit_bom(ctx.session, ctx.author, second.id, reason="Ready")
    await service.approve_bom(
        ctx.session, ctx.reviewer, ctx.reviewer_principal, second.id, reason="Approved"
    )

    defaults = (
        (
            await ctx.session.execute(
                select(Bom.id).where(
                    Bom.item_id == parent.id,
                    Bom.is_default.is_(True),
                    Bom.status == BomStatus.APPROVED,
                )
            )
        )
        .scalars()
        .all()
    )
    assert defaults == [second.id]


async def test_retiring_a_structure_frees_the_default_slot(ctx: Ctx) -> None:
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)
    bom = await approved_bom(ctx, parent, {screw: Decimal("1")})

    retired = await service.obsolete_bom(
        ctx.session, ctx.author, bom.id, reason="Product discontinued"
    )
    assert retired.status == BomStatus.OBSOLETE
    assert retired.is_default is False


async def test_a_structure_with_no_mandatory_lines_cannot_be_submitted(ctx: Ctx) -> None:
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    badge = await seed_item(ctx.session, ctx.author, "BADGE", uom)

    payload = BomCreate(
        item_id=parent.id,
        lines=[
            BomLineIn(
                line_number=1,
                component_item_id=badge.id,
                quantity_per=Decimal("1"),
                is_optional=True,
            )
        ],
    )
    bom, _, _ = await service.create_bom(ctx.session, ctx.author, payload)

    with pytest.raises(BusinessRuleError, match="no mandatory component lines"):
        await service.submit_bom(ctx.session, ctx.author, bom.id, reason="Ready")


async def test_rejection_returns_the_structure_to_its_author(ctx: Ctx) -> None:
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)
    bom, _, _ = await service.create_bom(
        ctx.session, ctx.author, create_payload(parent, {screw: Decimal("1")})
    )
    await service.submit_bom(ctx.session, ctx.author, bom.id, reason="Ready")

    returned = await service.reject_bom(
        ctx.session, ctx.reviewer, bom.id, reason="Screw length is wrong"
    )
    assert returned.status == BomStatus.DRAFT
    # Editable again, which is the point of returning rather than deleting.
    updated, _, _ = await service.update_bom(
        ctx.session, ctx.author, bom.id, BomUpdate(notes="Corrected")
    )
    assert updated.notes == "Corrected"


# -----------------------------------------------------------------------------
# Versioning and deletion
# -----------------------------------------------------------------------------


async def test_copy_as_new_version_carries_the_structure_but_not_the_approval(
    ctx: Ctx,
) -> None:
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)
    bush = await seed_item(ctx.session, ctx.author, "BUSH", uom)
    original = await approved_bom(ctx, parent, {screw: Decimal("4"), bush: Decimal("2")})

    copy, lines, _ = await service.copy_as_new_version(
        ctx.session,
        ctx.author,
        original.id,
        CopyAsNewVersionRequest(revision="B", reason="Design change"),
    )

    assert copy.version == original.version + 1
    assert copy.status == BomStatus.DRAFT
    assert copy.approved_at is None and copy.approved_by_id is None
    assert copy.revision == "B"
    assert {(line.component_item_id, line.quantity_per) for line in lines} == {
        (screw.id, Decimal("4.0000")),
        (bush.id, Decimal("2.0000")),
    }
    # The lines belong to the copy, not the original.
    assert all(line.bom_id == copy.id for line in lines)


async def test_a_copied_substitute_points_at_the_new_versions_line(ctx: Ctx) -> None:
    """A copied substitute id would otherwise reference the previous version's
    row - a cross-version link nobody would ever notice."""
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "LATCH", uom, manufactured=True)
    spring_a = await seed_item(ctx.session, ctx.author, "SPRING-A", uom)
    spring_b = await seed_item(ctx.session, ctx.author, "SPRING-B", uom)
    original, original_lines, _ = await service.create_bom(
        ctx.session,
        ctx.author,
        BomCreate(
            item_id=parent.id,
            lines=[
                BomLineIn(line_number=1, component_item_id=spring_a.id, quantity_per=Decimal("1")),
                BomLineIn(
                    line_number=2,
                    component_item_id=spring_b.id,
                    quantity_per=Decimal("1"),
                    substitute_for_line_number=1,
                ),
            ],
        ),
    )

    copy, lines, _ = await service.copy_as_new_version(
        ctx.session, ctx.author, original.id, CopyAsNewVersionRequest(reason="Revision")
    )

    new_primary = next(line for line in lines if line.line_number == 1)
    new_substitute = next(line for line in lines if line.line_number == 2)
    assert new_substitute.substitute_for_line_id == new_primary.id
    assert new_substitute.substitute_for_line_id not in {line.id for line in original_lines}
    assert copy.id != original.id


async def test_deleting_a_draft_takes_its_lines_and_operations_with_it(ctx: Ctx) -> None:
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)
    bom, _, _ = await service.create_bom(
        ctx.session,
        ctx.author,
        create_payload(
            parent,
            {screw: Decimal("1")},
            operations=[BomOperationIn(operation_sequence=10, name="Assemble")],
        ),
    )
    bom_id = bom.id

    await service.delete_draft_bom(ctx.session, ctx.author, bom_id, reason="Raised by mistake")

    for model in (Bom, BomLine, BomOperation):
        column = model.id if model is Bom else model.bom_id
        remaining = (
            await ctx.session.execute(
                select(func.count()).select_from(model).where(column == bom_id)
            )
        ).scalar_one()
        assert remaining == 0

    logged = (
        await ctx.session.execute(
            select(AuditLog).where(
                AuditLog.entity_id == bom_id, AuditLog.action == AuditAction.DELETE
            )
        )
    ).scalar_one()
    assert logged.context == {"reason": "Raised by mistake"}


async def test_a_concurrent_edit_is_rejected_rather_than_silently_overwritten(
    ctx: Ctx,
) -> None:
    """Lost-update protection on a document several engineers edit at once.

    ``boms.row_version`` carries the optimistic lock rather than ``version``,
    which is already taken by the BOM's engineering version. Here the row is
    moved underneath the session (as another user's transaction would), and the
    next write through the stale copy has to fail loudly - the exception handler
    turns StaleDataError into a 409 rather than letting the second writer erase
    the first one's change.
    """
    from sqlalchemy.orm.exc import StaleDataError

    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)
    bom, _, _ = await service.create_bom(
        ctx.session, ctx.author, create_payload(parent, {screw: Decimal("1")})
    )

    await ctx.session.execute(
        text("UPDATE boms SET row_version = row_version + 1, notes = 'theirs' WHERE id = :id"),
        {"id": bom.id},
    )

    with pytest.raises(StaleDataError):
        await service.update_bom(ctx.session, ctx.author, bom.id, BomUpdate(notes="mine"))


async def test_an_approved_structure_cannot_be_deleted(ctx: Ctx) -> None:
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)
    bom = await approved_bom(ctx, parent, {screw: Decimal("1")})

    with pytest.raises(BusinessRuleError, match="Only a draft can be deleted"):
        await service.delete_draft_bom(ctx.session, ctx.author, bom.id, reason="Oops")


# -----------------------------------------------------------------------------
# Engine against real data
# -----------------------------------------------------------------------------


async def test_a_three_level_structure_explodes_and_consolidates_from_the_database(
    ctx: Ctx,
) -> None:
    """The master-rules example, end to end through PostgreSQL."""
    uom = await seed_uom(ctx.session, ctx.author)
    kit = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    rose = await seed_item(ctx.session, ctx.author, "ROSE", uom, manufactured=True)
    lock = await seed_item(ctx.session, ctx.author, "LOCK", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)
    bush = await seed_item(ctx.session, ctx.author, "BUSH", uom)

    await approved_bom(ctx, rose, {bush: Decimal("2"), screw: Decimal("2")})
    await approved_bom(ctx, lock, {screw: Decimal("4")})
    kit_bom = await approved_bom(ctx, kit, {rose: Decimal("1"), lock: Decimal("1")})

    result = await engine.explode(ctx.session, ctx.author, kit.id, Decimal("100"))

    assert result.bom_id == kit_bom.id
    screws = [c for c in result.components if c.item_id == screw.id]
    assert len(screws) == 1
    assert screws[0].scrap_adjusted_quantity == Decimal("600")
    assert screws[0].source_path_count == 2
    assert next(c for c in result.components if c.item_id == bush.id).scrap_adjusted_quantity == (
        Decimal("200")
    )


async def test_only_approved_structures_are_exploded_below_the_root(ctx: Ctx) -> None:
    """A draft sub-assembly must not drive planning, so its children do not
    appear and the sub-assembly is treated as a leaf."""
    uom = await seed_uom(ctx.session, ctx.author)
    kit = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    rose = await seed_item(ctx.session, ctx.author, "ROSE", uom, manufactured=True)
    bush = await seed_item(ctx.session, ctx.author, "BUSH", uom)

    # Rose's structure stays in DRAFT.
    await service.create_bom(ctx.session, ctx.author, create_payload(rose, {bush: Decimal("2")}))
    await approved_bom(ctx, kit, {rose: Decimal("1")})

    result = await engine.explode(ctx.session, ctx.author, kit.id, Decimal("10"))
    assert {c.item_id for c in result.components} == {rose.id}


async def test_an_expired_structure_is_not_used(ctx: Ctx) -> None:
    uom = await seed_uom(ctx.session, ctx.author)
    kit = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    rose = await seed_item(ctx.session, ctx.author, "ROSE", uom, manufactured=True)
    bush = await seed_item(ctx.session, ctx.author, "BUSH", uom)

    yesterday = today() - timedelta(days=1)
    await approved_bom(
        ctx,
        rose,
        {bush: Decimal("2")},
        effective_from=yesterday - timedelta(days=30),
        effective_to=yesterday,
    )
    # The kit's own structure has to be effective on both dates under test.
    await approved_bom(
        ctx, kit, {rose: Decimal("1")}, effective_from=yesterday - timedelta(days=60)
    )

    result = await engine.explode(ctx.session, ctx.author, kit.id, Decimal("10"))
    assert {c.item_id for c in result.components} == {rose.id}

    # The same explosion as of a date inside the window does reach the children.
    historical = await engine.explode(
        ctx.session, ctx.author, kit.id, Decimal("10"), as_of=yesterday
    )
    assert bush.id in {c.item_id for c in historical.components}


async def test_approval_stores_the_rolled_up_cost(ctx: Ctx) -> None:
    uom = await seed_uom(ctx.session, ctx.author)
    kit = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom, standard_cost=Decimal("2.50"))

    bom = await approved_bom(
        ctx,
        kit,
        {screw: Decimal("4")},
        operations=[
            BomOperationIn(
                operation_sequence=10,
                name="Assemble",
                run_time_minutes_per_unit=Decimal("6"),
                cost_per_hour=Decimal("60"),
            )
        ],
    )

    assert bom.total_material_cost == Decimal("10.0000")
    assert bom.total_operation_cost == Decimal("6.0000")
    assert bom.cost_rolled_up_at is not None

    live = await service.bom_cost(ctx.session, ctx.author, bom.id)
    assert live.total_cost == Decimal("16")
    assert live.unit_cost == Decimal("16")


async def test_where_used_lists_the_structures_that_consume_a_part(ctx: Ctx) -> None:
    uom = await seed_uom(ctx.session, ctx.author)
    kit = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    rose = await seed_item(ctx.session, ctx.author, "ROSE", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)

    await approved_bom(ctx, rose, {screw: Decimal("2")})
    await approved_bom(ctx, kit, {rose: Decimal("1"), screw: Decimal("1")})

    direct, pagination = await service.where_used_page(
        ctx.session, ctx.author, screw.id, levels=1, params=PageParams(page=1, limit=50)
    )
    assert {row.parent_item_id for row in direct} == {kit.id, rose.id}
    assert pagination.total == 2

    # Two levels also reaches the kit through the rose handle.
    deep = await engine.where_used(ctx.session, ctx.author, screw.id, levels=2)
    assert {(row.parent_item_id, row.level) for row in deep} >= {
        (kit.id, 1),
        (rose.id, 1),
        (kit.id, 2),
    }


# -----------------------------------------------------------------------------
# Tenant isolation
# -----------------------------------------------------------------------------


async def test_another_tenant_cannot_see_or_load_a_structure(ctx: Ctx) -> None:
    """Tenant isolation that is never tested is a claim, not a property."""
    from src.core.errors import NotFoundError

    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)
    bom, _, _ = await service.create_bom(
        ctx.session, ctx.author, create_payload(parent, {screw: Decimal("1")})
    )

    with pytest.raises(NotFoundError):
        await service.get_bom(ctx.session, ctx.other_tenant, bom.id)

    rows, _ = await service.list_boms(
        ctx.session,
        ctx.other_tenant,
        BomFilters(),
        PageParams(page=1, limit=50),
        SortParams(),
    )
    assert rows == []


async def test_the_list_filters_by_item_and_status(ctx: Ctx) -> None:
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    other = await seed_item(ctx.session, ctx.author, "KIT2", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)

    approved = await approved_bom(ctx, parent, {screw: Decimal("1")})
    await service.create_bom(ctx.session, ctx.author, create_payload(other, {screw: Decimal("1")}))

    rows, _ = await service.list_boms(
        ctx.session,
        ctx.author,
        BomFilters(item_id=parent.id),
        PageParams(page=1, limit=50),
        SortParams(),
    )
    assert [row.id for row in rows] == [approved.id]

    drafts, _ = await service.list_boms(
        ctx.session,
        ctx.author,
        BomFilters(status=BomStatus.DRAFT),
        PageParams(page=1, limit=50),
        SortParams(),
    )
    assert [row.item_id for row in drafts] == [other.id]


async def test_an_alternate_must_belong_to_the_same_item(ctx: Ctx) -> None:
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    other = await seed_item(ctx.session, ctx.author, "KIT2", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)
    primary = await approved_bom(ctx, parent, {screw: Decimal("1")})

    with pytest.raises(BusinessRuleError, match="same item"):
        await service.create_bom(
            ctx.session,
            ctx.author,
            create_payload(
                other,
                {screw: Decimal("1")},
                bom_type=BomType.ALTERNATE,
                parent_bom_id=primary.id,
            ),
        )


# -----------------------------------------------------------------------------
# Partial updates: the two sides of a line/operation reference move separately
# -----------------------------------------------------------------------------


async def _bom_with_staged_line(ctx: Ctx) -> Bom:
    """A draft whose line 1 is staged at operation 20."""
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)

    bom, _, _ = await service.create_bom(
        ctx.session,
        ctx.author,
        BomCreate(
            item_id=parent.id,
            lines=[
                BomLineIn(
                    line_number=1,
                    component_item_id=screw.id,
                    quantity_per=Decimal("1"),
                    operation_sequence=20,
                )
            ],
            operations=[
                BomOperationIn(operation_sequence=10, name="Deburr"),
                BomOperationIn(operation_sequence=20, name="Assemble"),
            ],
        ),
    )
    return bom


async def test_replacing_the_operations_alone_cannot_orphan_a_persisted_line(
    ctx: Ctx,
) -> None:
    """Lines and operations are replaced independently, so the reference between
    them has to be checked against what the BOM will hold after the patch.

    Dropping operation 20 leaves line 1 staged at an operation that no longer
    exists; material issue by operation would then never stage that component
    and the job runs without its screws.
    """
    bom = await _bom_with_staged_line(ctx)

    with pytest.raises(BusinessRuleError, match="does not define"):
        await service.update_bom(
            ctx.session,
            ctx.author,
            bom.id,
            BomUpdate(operations=[BomOperationIn(operation_sequence=10, name="Deburr")]),
        )


async def test_replacing_the_lines_alone_is_checked_against_the_persisted_operations(
    ctx: Ctx,
) -> None:
    """The mirror case: a new line naming an operation this BOM never defined."""
    bom = await _bom_with_staged_line(ctx)
    lines = await service.get_bom_detail(ctx.session, ctx.author, ctx.author_principal, bom.id)
    component_id = lines.lines[0].component_item_id

    with pytest.raises(BusinessRuleError, match="does not define"):
        await service.update_bom(
            ctx.session,
            ctx.author,
            bom.id,
            BomUpdate(
                lines=[
                    BomLineIn(
                        line_number=1,
                        component_item_id=component_id,
                        quantity_per=Decimal("1"),
                        operation_sequence=90,
                    )
                ]
            ),
        )


async def test_a_line_may_still_reference_an_operation_that_is_already_persisted(
    ctx: Ctx,
) -> None:
    """The check must not reject the ordinary case it exists to protect."""
    bom = await _bom_with_staged_line(ctx)
    detail = await service.get_bom_detail(ctx.session, ctx.author, ctx.author_principal, bom.id)
    component_id = detail.lines[0].component_item_id

    _, lines, _ = await service.update_bom(
        ctx.session,
        ctx.author,
        bom.id,
        BomUpdate(
            lines=[
                BomLineIn(
                    line_number=1,
                    component_item_id=component_id,
                    quantity_per=Decimal("2"),
                    operation_sequence=10,
                )
            ]
        ),
    )
    assert lines[0].operation_sequence == 10


async def test_a_draft_cannot_be_patched_into_an_alternate_with_no_parent(
    ctx: Ctx,
) -> None:
    """``BomCreate`` refuses this; a rule that only holds on POST is one a client
    bypasses by using PATCH."""
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)
    bom, _, _ = await service.create_bom(
        ctx.session, ctx.author, create_payload(parent, {screw: Decimal("1")})
    )

    with pytest.raises(BusinessRuleError, match="alternative to"):
        await service.update_bom(
            ctx.session, ctx.author, bom.id, BomUpdate(bom_type=BomType.ALTERNATE)
        )


async def test_the_update_audit_records_the_line_count_before_the_replacement(
    ctx: Ctx,
) -> None:
    """Reading the new list for both sides reports the new count as the old one,
    so 'when did components leave this structure?' has no answer."""
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screws = [await seed_item(ctx.session, ctx.author, f"SCREW-{n}", uom) for n in range(3)]

    bom, _, _ = await service.create_bom(
        ctx.session,
        ctx.author,
        create_payload(parent, dict.fromkeys(screws, Decimal("1"))),
    )
    await service.update_bom(
        ctx.session,
        ctx.author,
        bom.id,
        BomUpdate(
            lines=[
                BomLineIn(line_number=1, component_item_id=screws[0].id, quantity_per=Decimal("1"))
            ]
        ),
    )

    row = (
        (
            await ctx.session.execute(
                select(AuditLog).where(
                    AuditLog.entity_id == bom.id, AuditLog.action == AuditAction.UPDATE
                )
            )
        )
        .scalars()
        .one()
    )
    assert row.old_values["line_count"] == 3
    assert row.new_values["line_count"] == 1


# -----------------------------------------------------------------------------
# Default-routing snapshot
# -----------------------------------------------------------------------------


async def _cell(ctx: Ctx) -> WorkCenter:
    centre = WorkCenter(
        company_id=ctx.author.company_id,
        code=f"WC-{uuid.uuid4().hex[:6].upper()}",
        name="Machining cell",
        cost_per_hour=Decimal("600"),
    )
    ctx.session.add(centre)
    await ctx.session.flush()
    return centre


async def _approved_default_routing(ctx: Ctx, item: Item, centre: WorkCenter) -> Routing:
    """An approved, default, effective routing with one costed step."""
    routing = Routing(
        company_id=ctx.author.company_id,
        code=f"RT-{uuid.uuid4().hex[:6].upper()}",
        item_id=item.id,
        version=1,
        status=RoutingStatus.APPROVED,
        is_default=True,
        effective_from=today() - timedelta(days=30),
        approved_by_id=ctx.reviewer.user_id,
        approved_at=utcnow(),
    )
    ctx.session.add(routing)
    await ctx.session.flush()
    ctx.session.add(
        RoutingOperation(
            company_id=ctx.author.company_id,
            routing_id=routing.id,
            sequence=10,
            operation_code="CUT",
            operation_name="Cut to length",
            work_center_id=centre.id,
            setup_time_minutes=Decimal("30"),
            run_time_per_unit_minutes=Decimal("2"),
            process_cost=Decimal("50"),
        )
    )
    await ctx.session.flush()
    return routing


async def test_creating_a_bom_snapshots_the_default_routing(ctx: Ctx) -> None:
    """A UI-built BOM with no operations still costs labour, from the routing."""
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)
    centre = await _cell(ctx)
    step_routing = await _approved_default_routing(ctx, parent, centre)

    _, _, operations = await service.create_bom(
        ctx.session, ctx.author, create_payload(parent, {screw: Decimal("1")})
    )
    assert len(operations) == 1
    (operation,) = operations
    assert operation.operation_sequence == 10
    assert operation.name == "Cut to length"
    assert operation.routing_operation_id is not None
    assert operation.work_center_id == centre.id
    assert operation.setup_time_minutes == Decimal("30")
    assert operation.run_time_minutes_per_unit == Decimal("2")
    assert operation.cost_per_hour == Decimal("600")
    assert operation.fixed_cost == Decimal("50")
    assert operation.is_outsourced is False
    assert step_routing.id is not None


async def test_approving_a_bom_snapshots_the_routing_before_costing(ctx: Ctx) -> None:
    """A draft raised before any routing existed is costed whole at approval."""
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)
    bom, _, created_operations = await service.create_bom(
        ctx.session, ctx.author, create_payload(parent, {screw: Decimal("1")})
    )
    assert created_operations == []
    centre = await _cell(ctx)
    await _approved_default_routing(ctx, parent, centre)

    await service.submit_bom(ctx.session, ctx.author, bom.id, reason="Ready for review")
    approved = await service.approve_bom(
        ctx.session, ctx.reviewer, ctx.reviewer_principal, bom.id, reason="Checked"
    )
    assert approved.total_operation_cost > Decimal("0")
    stored = await service.get_bom_detail(ctx.session, ctx.author, ctx.author_principal, bom.id)
    assert len(stored.operations) == 1
    assert stored.operations[0].name == "Cut to length"


async def test_explicit_operations_are_never_overwritten_by_the_snapshot(ctx: Ctx) -> None:
    """The engineer chose these steps over the routing; approval keeps them."""
    uom = await seed_uom(ctx.session, ctx.author)
    parent = await seed_item(ctx.session, ctx.author, "KIT", uom, manufactured=True)
    screw = await seed_item(ctx.session, ctx.author, "SCREW", uom)
    centre = await _cell(ctx)
    await _approved_default_routing(ctx, parent, centre)

    bom, _, _ = await service.create_bom(
        ctx.session,
        ctx.author,
        create_payload(
            parent,
            {screw: Decimal("1")},
            operations=[BomOperationIn(operation_sequence=10, name="Deburr")],
        ),
    )
    await service.submit_bom(ctx.session, ctx.author, bom.id, reason="Ready for review")
    await service.approve_bom(
        ctx.session, ctx.reviewer, ctx.reviewer_principal, bom.id, reason="Checked"
    )
    stored = await service.get_bom_detail(ctx.session, ctx.author, ctx.author_principal, bom.id)
    assert [operation.name for operation in stored.operations] == ["Deburr"]
    assert stored.operations[0].routing_operation_id is None
