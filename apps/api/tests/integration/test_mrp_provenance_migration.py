"""The demand-history index can be removed/rebuilt without changing saved facts."""

from __future__ import annotations

import importlib.util
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today
from src.core.deps import TenantContext
from src.modules.mrp.models import MrpRun, MrpSuggestion, MrpSuggestionType
from tests import seed_support

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations/versions"
    / "20261102_0100_mrp_demand_provenance_index.py"
)
INDEX_NAME = "ix_mrp_suggestions_source_demands"


def _apply(connection: sa.Connection, direction: str) -> None:
    spec = importlib.util.spec_from_file_location("mrp_provenance_index", MIGRATION)
    assert spec is not None and spec.loader is not None
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    assert revision.revision == "20261102_0100"
    assert revision.down_revision == "20261101_0100"
    with Operations.context(MigrationContext.configure(connection)):
        getattr(revision, direction)()


async def test_index_round_trip_preserves_saved_roots_and_both_containment_queries(
    session: AsyncSession,
    tenant: TenantContext,
) -> None:
    item = await seed_support.seed_item(
        session, company_id=tenant.company_id, code="PROVENANCE-INDEX"
    )
    run = MrpRun(company_id=tenant.company_id, code=f"MRP-{uuid.uuid4().hex}", horizon_days=30)
    session.add(run)
    await session.flush()
    root_id = str(uuid.uuid4())
    facts = [
        {
            "source": "SALES_ORDER",
            "source_id": root_id,
            "root_demand_refs": [
                {"demand_type": "SALES_ORDER", "demand_id": root_id, "demand_line_id": None}
            ],
            "root_demand_refs_complete": True,
        }
    ]
    suggestion = MrpSuggestion(
        company_id=tenant.company_id,
        run_id=run.id,
        item_id=item.id,
        item_code=item.code,
        suggestion_type=MrpSuggestionType.PURCHASE,
        required_quantity=Decimal("10"),
        suggested_quantity=Decimal("10"),
        shortage_quantity=Decimal("10"),
        required_date=today(),
        suggested_order_date=today(),
        source_demands=facts,
    )
    session.add(suggestion)
    await session.flush()
    before = dict(
        (
            await session.execute(
                sa.select(MrpSuggestion.__table__).where(MrpSuggestion.id == suggestion.id)
            )
        )
        .mappings()
        .one()
    )
    connection = await session.connection()
    await connection.run_sync(lambda sync: _apply(sync, "downgrade"))
    names = await connection.run_sync(
        lambda sync: {index["name"] for index in sa.inspect(sync).get_indexes("mrp_suggestions")}
    )
    assert INDEX_NAME not in names
    await connection.run_sync(lambda sync: _apply(sync, "upgrade"))
    definition = await session.scalar(
        sa.text(
            "SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() AND indexname=:name"
        ),
        {"name": INDEX_NAME},
    )
    assert definition is not None and "USING gin (source_demands)" in definition
    after = dict(
        (
            await session.execute(
                sa.select(MrpSuggestion.__table__).where(MrpSuggestion.id == suggestion.id)
            )
        )
        .mappings()
        .one()
    )
    assert after == before
    direct = await session.scalars(
        sa.select(MrpSuggestion.id).where(
            MrpSuggestion.company_id == tenant.company_id,
            MrpSuggestion.source_demands.contains([{"source_id": root_id}]),
        )
    )
    nested = await session.scalars(
        sa.select(MrpSuggestion.id).where(
            MrpSuggestion.company_id == tenant.company_id,
            MrpSuggestion.source_demands.contains(
                [{"root_demand_refs": [{"demand_type": "SALES_ORDER", "demand_id": root_id}]}]
            ),
        )
    )
    assert direct.all() == nested.all() == [suggestion.id]
