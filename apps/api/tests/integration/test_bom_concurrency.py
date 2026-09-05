"""Two reviewers approving two revisions of the same item at the same moment.

The rule at stake is the one the rest of the ERP resolves "which structure do I
use?" through: **at most one is_default APPROVED BOM per item**. Approval reads
the current defaults, demotes them, then promotes its own row - a check-then-act
that only holds if nothing changes the answer between the read and the write.

This cannot be faked with one session, or with two sessions sharing a connection
inside an outer transaction: both serialise on the client side and the test
would pass with the locking removed. So this module commits for real, in its own
schema, and drops it at the end.

Two properties are asserted:

1. the invariant survives - exactly one default approved structure at the end,
   whatever the interleaving;
2. the loser is not told "a record with these details already exists". Without
   the per-item advisory lock the second approval reaches the partial unique
   index with a stale view of the world and dies there, discarding a cost
   roll-up it had already computed and reporting a duplicate-key conflict about
   a structure the reviewer has never seen.
"""

from __future__ import annotations

import asyncio
import uuid
import warnings
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.config import settings
from src.core.deps import Principal, TenantContext
from src.core.models import Base
from src.core.permissions import Permission
from src.modules.bom import service
from src.modules.bom.models import Bom, BomStatus
from src.modules.bom.schemas import BomCreate, BomLineIn
from src.modules.products.models import ProductionType
from tests import seed_support
from tests.schema_support import prepare_metadata, report_database_setup_failure

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]

prepare_metadata()


def _schema_engine(schema: str) -> Any:
    """Engine pinned to the test schema, one connection per session (NullPool).

    NullPool matters: the point is that the two approvals hold two *different*
    PostgreSQL backends, so one can genuinely block on the other.
    """
    return create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )


@pytest.fixture(scope="module")
def bom_conc_schema() -> Iterator[tuple[str, uuid.UUID, uuid.UUID, uuid.UUID]]:
    schema = f"it_bom_conc_{uuid.uuid4().hex[:10]}"
    company_id = uuid.uuid4()
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
                await conn.run_sync(
                    lambda sync_conn: Base.metadata.create_all(sync_conn, checkfirst=False)
                )
            factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
            async with factory() as session, session.begin():
                await seed_support.seed_company(session, company_id=company_id, prefix="BOMC")
                for who, uid in (("author", author_id), ("reviewer", reviewer_id)):
                    await seed_support.seed_user(
                        session,
                        company_id=company_id,
                        user_id=uid,
                        email=f"{who}-{uuid.uuid4().hex[:8]}@atcraft.test",
                    )
        finally:
            await eng.dispose()

    async def teardown() -> None:
        # Best effort. ``DROP SCHEMA ... CASCADE`` takes one lock per object in a
        # single transaction, and a developer box left at the default
        # ``max_locks_per_transaction = 64`` can run the shared lock table out
        # while another schema-per-module suite is up. A schema this run failed
        # to clean up is disposable - failing the suite over it would report an
        # environment limit as a defect in the code under test.
        admin = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        except Exception as exc:  # pragma: no cover - environment, not a test failure
            warnings.warn(
                f"could not drop test schema {schema}: {exc}",
                stacklevel=1,
            )
        finally:
            await admin.dispose()

    try:
        asyncio.run(setup())
    except Exception as exc:  # pragma: no cover - environment, not a test failure
        report_database_setup_failure(exc)

    yield schema, company_id, author_id, reviewer_id
    asyncio.run(teardown())


@pytest_asyncio.fixture
async def engine(
    bom_conc_schema: tuple[str, uuid.UUID, uuid.UUID, uuid.UUID],
) -> AsyncIterator[Any]:
    schema, _, _, _ = bom_conc_schema
    eng = _schema_engine(schema)
    try:
        yield eng
    finally:
        await eng.dispose()


def _reviewer(company_id: uuid.UUID, user_id: uuid.UUID) -> Principal:
    return Principal(
        user_id=user_id,
        company_id=company_id,
        email=f"{user_id}@atcraft.test",
        full_name="Reviewer",
        is_superuser=False,
        permissions=frozenset(str(p) for p in Permission),
        role_codes=("DESIGN_ENGINEER",),
    )


async def _two_submitted_revisions(
    engine: Any, author: TenantContext
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """One item with two competing default revisions, both awaiting approval."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            uom = await seed_support.seed_uom(session, company_id=author.company_id)
            parent = await seed_support.seed_item(
                session,
                company_id=author.company_id,
                code=f"KIT-{uuid.uuid4().hex[:8].upper()}",
                base_uom_id=uom.id,
                production_type=ProductionType.MAKE,
                can_manufacture=True,
                can_purchase=False,
            )
            screw = await seed_support.seed_item(
                session,
                company_id=author.company_id,
                code=f"SCREW-{uuid.uuid4().hex[:8].upper()}",
                base_uom_id=uom.id,
                standard_cost=Decimal("2"),
                can_be_bom_component=True,
            )
            parent_id, screw_id = parent.id, screw.id

        bom_ids: list[uuid.UUID] = []
        for quantity in (Decimal("1"), Decimal("2")):
            bom, _, _ = await service.create_bom(
                session,
                author,
                BomCreate(
                    item_id=parent_id,
                    is_default=True,
                    lines=[
                        BomLineIn(
                            line_number=1,
                            component_item_id=screw_id,
                            quantity_per=quantity,
                        )
                    ],
                ),
            )
            await service.submit_bom(session, author, bom.id, reason="Ready for review")
            bom_ids.append(bom.id)
        await session.commit()

    return parent_id, bom_ids[0], bom_ids[1]


async def _approve(engine: Any, tenant: TenantContext, principal: Principal, bom_id: uuid.UUID):
    """Approve on a connection of its own. Returns the exception, or None."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        try:
            await service.approve_bom(
                session, tenant, principal, bom_id, reason="Checked against drawing"
            )
            await session.commit()
        except Exception as exc:
            await session.rollback()
            return exc
    return None


async def test_two_simultaneous_approvals_leave_exactly_one_default(
    engine: Any, bom_conc_schema: tuple[str, uuid.UUID, uuid.UUID, uuid.UUID]
) -> None:
    _, company_id, author_id, reviewer_id = bom_conc_schema
    author = TenantContext(company_id=company_id, user_id=author_id)
    reviewer = TenantContext(company_id=company_id, user_id=reviewer_id)
    principal = _reviewer(company_id, reviewer_id)

    item_id, first_id, second_id = await _two_submitted_revisions(engine, author)

    outcomes = await asyncio.gather(
        _approve(engine, reviewer, principal, first_id),
        _approve(engine, reviewer, principal, second_id),
    )

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        defaults = (
            (
                await session.execute(
                    select(Bom.id).where(
                        Bom.company_id == company_id,
                        Bom.item_id == item_id,
                        Bom.is_default.is_(True),
                        Bom.status == BomStatus.APPROVED,
                    )
                )
            )
            .scalars()
            .all()
        )
        approved = (
            (
                await session.execute(
                    select(Bom.id).where(
                        Bom.company_id == company_id,
                        Bom.item_id == item_id,
                        Bom.status == BomStatus.APPROVED,
                    )
                )
            )
            .scalars()
            .all()
        )

    # The invariant the whole ERP resolves "which structure?" through.
    assert len(defaults) == 1, f"expected one default approved BOM, found {len(defaults)}"

    # Serialised on the advisory lock, the second approval re-reads a default
    # that is now the first one's revision and demotes it, so both land.
    assert outcomes == [None, None], f"an approval failed: {outcomes}"
    assert set(approved) == {first_id, second_id}
    assert defaults[0] in {first_id, second_id}
