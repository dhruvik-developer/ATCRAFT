"""Reporting against a real PostgreSQL.

What is under test is the sentence master rules section 25 turns on: a dashboard
reads a summary, the summary is a faithful roll-up of the balances it was built
from, and a position that no longer exists disappears from it.

These need PostgreSQL and cannot be faked. ``inventory_balances.available_quantity``
is a ``GENERATED ALWAYS AS ... STORED`` column, the summary is maintained with
``INSERT ... ON CONFLICT DO UPDATE`` against a composite unique constraint, the
dashboard is one statement of ten scalar subqueries, and the job lifecycle writes
its audit rows inside the same transaction as the status change. A SQLite stand-in
would pass while every one of those was broken.

Fixtures (``session``, ``tenant``, ``company``, ``other_company``, ``user``) come
from ``tests/conftest.py``; the session's outer transaction is rolled back after
every test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import TenantContext
from src.core.errors import InvalidStateTransitionError, NotFoundError
from src.core.pagination import PageParams
from src.modules.audit.models import AuditLog
from src.modules.inventory.models import InventoryBalance
from src.modules.reports import service
from src.modules.reports.models import InventorySummary, ReportJobStatus, ReportKey, ReportSnapshot
from src.modules.reports.repository import ReportSnapshotRepository
from src.modules.reports.schemas import ReportFilters
from src.modules.warehouses.models import StorageLocation, StorageLocationType
from tests import seed_support

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


async def _balance(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    on_hand: str,
    reserved: str = "0",
    qc_hold: str = "0",
    damaged: str = "0",
    stock_value: str = "0",
    location_id: uuid.UUID | None = None,
    last_movement_at: datetime | None = None,
) -> InventoryBalance:
    """One balance row.

    ``available_quantity`` is deliberately not passed: the database computes it,
    and a test that supplied it would be asserting its own arithmetic rather than
    the schema's.
    """
    record = InventoryBalance(
        company_id=company_id,
        item_id=item_id,
        warehouse_id=warehouse_id,
        location_id=location_id,
        on_hand_quantity=Decimal(on_hand),
        reserved_quantity=Decimal(reserved),
        qc_hold_quantity=Decimal(qc_hold),
        damaged_quantity=Decimal(damaged),
        stock_value=Decimal(stock_value),
        last_movement_at=last_movement_at,
    )
    session.add(record)
    await session.flush()
    return record


async def _summary_rows(session: AsyncSession, company_id: uuid.UUID) -> list[InventorySummary]:
    stmt = (
        select(InventorySummary)
        .where(InventorySummary.company_id == company_id)
        .order_by(InventorySummary.item_id, InventorySummary.warehouse_id)
    )
    return list((await session.execute(stmt)).scalars().all())


@pytest.fixture
async def stock(session: AsyncSession, company, unique_code):
    """One warehouse with a bin, and two items to hold in it."""
    warehouse = await seed_support.seed_warehouse(
        session, company_id=company.id, code=unique_code("WH")
    )
    bin_location = StorageLocation(
        company_id=company.id,
        warehouse_id=warehouse.id,
        code=unique_code("BIN"),
        name="Bin A",
        location_type=StorageLocationType.BIN,
    )
    session.add(bin_location)
    await session.flush()
    first = await seed_support.seed_item(
        session, company_id=company.id, code=unique_code("ITEM"), reorder_level=Decimal("50")
    )
    second = await seed_support.seed_item(
        session, company_id=company.id, code=unique_code("ITEM"), reorder_level=Decimal("0")
    )
    return {"warehouse": warehouse, "bin": bin_location, "first": first, "second": second}


# -----------------------------------------------------------------------------
# The refresh produces correct totals
# -----------------------------------------------------------------------------


async def test_the_refresh_rolls_several_balance_rows_into_one_position(
    session: AsyncSession, tenant: TenantContext, company, stock
) -> None:
    # The summary grain is (item, warehouse); the balance grain is finer. The
    # same part in two bins of one store is one reporting position.
    await _balance(
        session,
        company_id=company.id,
        item_id=stock["first"].id,
        warehouse_id=stock["warehouse"].id,
        on_hand="100",
        reserved="20",
        qc_hold="10",
        stock_value="1000.0000",
        last_movement_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    await _balance(
        session,
        company_id=company.id,
        item_id=stock["first"].id,
        warehouse_id=stock["warehouse"].id,
        on_hand="40",
        reserved="5",
        stock_value="400.0000",
        location_id=stock["bin"].id,
        last_movement_at=datetime(2026, 9, 1, tzinfo=UTC),
    )

    result = await service.refresh_inventory_summary(session, tenant, now=NOW)

    assert result.position_count == 1
    rows = await _summary_rows(session, company.id)
    assert len(rows) == 1
    position = rows[0]
    assert position.on_hand == Decimal("140.0000")
    assert position.reserved == Decimal("25.0000")
    # available is the sum of the database's own generated column:
    # (100 - 20 - 10) + (40 - 5) = 105.
    assert position.available == Decimal("105.0000")
    assert position.valuation_amount == Decimal("1400.0000")
    # The newest movement across the rolled-up rows, not the first one seen.
    assert position.last_movement_at == datetime(2026, 9, 1, tzinfo=UTC)
    assert position.refreshed_at == NOW
    assert result.stock_value == Decimal("1400.0000")


async def test_two_items_in_one_warehouse_stay_two_positions(
    session: AsyncSession, tenant: TenantContext, company, stock
) -> None:
    for item in (stock["first"], stock["second"]):
        await _balance(
            session,
            company_id=company.id,
            item_id=item.id,
            warehouse_id=stock["warehouse"].id,
            on_hand="10",
            stock_value="100.0000",
        )

    result = await service.refresh_inventory_summary(session, tenant, now=NOW)

    assert result.position_count == 2
    assert result.stock_value == Decimal("200.0000")


async def test_refreshing_twice_updates_in_place_rather_than_duplicating(
    session: AsyncSession, tenant: TenantContext, company, stock
) -> None:
    # The beat job runs every fifteen minutes forever, so this is the property
    # the whole design rests on. A second insert would also violate the grain's
    # unique constraint and take the sweep down.
    balance = await _balance(
        session,
        company_id=company.id,
        item_id=stock["first"].id,
        warehouse_id=stock["warehouse"].id,
        on_hand="10",
        stock_value="100.0000",
    )
    await service.refresh_inventory_summary(session, tenant, now=NOW)

    balance.on_hand_quantity = Decimal("30")
    balance.stock_value = Decimal("300.0000")
    await session.flush()
    later = NOW + timedelta(minutes=15)
    await service.refresh_inventory_summary(session, tenant, now=later)

    rows = await _summary_rows(session, company.id)
    assert len(rows) == 1
    assert rows[0].on_hand == Decimal("30.0000")
    assert rows[0].valuation_amount == Decimal("300.0000")
    assert rows[0].refreshed_at == later


async def test_a_position_whose_balance_has_gone_is_removed_from_the_summary(
    session: AsyncSession, tenant: TenantContext, company, stock
) -> None:
    # Otherwise the summary would report stock that no longer exists, and the
    # dashboard's stock value would drift upwards for ever.
    balance = await _balance(
        session,
        company_id=company.id,
        item_id=stock["first"].id,
        warehouse_id=stock["warehouse"].id,
        on_hand="10",
        stock_value="100.0000",
    )
    await service.refresh_inventory_summary(session, tenant, now=NOW)
    assert len(await _summary_rows(session, company.id)) == 1

    await session.delete(balance)
    await session.flush()
    result = await service.refresh_inventory_summary(
        session, tenant, now=NOW + timedelta(minutes=15)
    )

    assert result.removed_count == 1
    assert await _summary_rows(session, company.id) == []


async def test_the_refresh_never_reaches_another_tenants_stock(
    session: AsyncSession, tenant: TenantContext, company, other_company, stock, unique_code
) -> None:
    other_warehouse = await seed_support.seed_warehouse(
        session, company_id=other_company.id, code=unique_code("WH")
    )
    other_item = await seed_support.seed_item(
        session, company_id=other_company.id, code=unique_code("ITEM")
    )
    await _balance(
        session,
        company_id=other_company.id,
        item_id=other_item.id,
        warehouse_id=other_warehouse.id,
        on_hand="999",
        stock_value="99900.0000",
    )
    await _balance(
        session,
        company_id=company.id,
        item_id=stock["first"].id,
        warehouse_id=stock["warehouse"].id,
        on_hand="10",
        stock_value="100.0000",
    )

    result = await service.refresh_inventory_summary(session, tenant, now=NOW)

    assert result.position_count == 1
    assert result.stock_value == Decimal("100.0000")
    assert await _summary_rows(session, other_company.id) == []


async def test_the_refresh_records_one_audit_entry_for_the_sweep(
    session: AsyncSession, tenant: TenantContext, company, stock
) -> None:
    # One row per company per sweep, not one per position: an audit trail of an
    # automated job that wrote a line per item would bury the human actions the
    # log exists to make findable.
    await _balance(
        session,
        company_id=company.id,
        item_id=stock["first"].id,
        warehouse_id=stock["warehouse"].id,
        on_hand="10",
        stock_value="100.0000",
    )
    await service.refresh_inventory_summary(session, tenant, now=NOW)

    count = (
        await session.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.company_id == company.id,
                AuditLog.entity_type == service.SUMMARY_AUDIT_ENTITY,
            )
        )
    ).scalar_one()
    assert count == 1


async def test_a_sweep_that_changes_nothing_writes_no_audit_entry(
    session: AsyncSession, tenant: TenantContext, company
) -> None:
    result = await service.refresh_inventory_summary(session, tenant, now=NOW)

    assert result.position_count == 0
    count = (
        await session.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.company_id == company.id,
                AuditLog.entity_type == service.SUMMARY_AUDIT_ENTITY,
            )
        )
    ).scalar_one()
    assert count == 0


# -----------------------------------------------------------------------------
# Reports read the summary
# -----------------------------------------------------------------------------


async def test_stock_availability_reports_the_refreshed_position(
    session: AsyncSession, tenant: TenantContext, company, stock
) -> None:
    await _balance(
        session,
        company_id=company.id,
        item_id=stock["first"].id,
        warehouse_id=stock["warehouse"].id,
        on_hand="40",
        reserved="5",
        stock_value="400.0000",
    )
    await service.refresh_inventory_summary(session, tenant, now=NOW)

    result = await service.run_report(
        session,
        tenant,
        key=ReportKey.STOCK_AVAILABILITY,
        period=None,
        filters=ReportFilters(),
        params=PageParams(page=1, limit=50),
        may_export=True,
        now=NOW,
    )

    assert isinstance(result, service.InlineReport)
    assert result.pagination.total == 1
    row = result.rows[0]
    assert row.item_code == stock["first"].code
    assert row.warehouse_code == stock["warehouse"].code
    assert row.on_hand == Decimal("40.0000")
    assert row.available == Decimal("35.0000")
    # Reorder level is 50 and only 35 can be promised.
    assert row.below_reorder_level is True


async def test_dead_stock_lists_only_positions_older_than_the_threshold(
    session: AsyncSession, tenant: TenantContext, company, stock
) -> None:
    await _balance(
        session,
        company_id=company.id,
        item_id=stock["first"].id,
        warehouse_id=stock["warehouse"].id,
        on_hand="10",
        stock_value="100.0000",
        last_movement_at=NOW - timedelta(days=5),
    )
    await _balance(
        session,
        company_id=company.id,
        item_id=stock["second"].id,
        warehouse_id=stock["warehouse"].id,
        on_hand="7",
        stock_value="70.0000",
        last_movement_at=NOW - timedelta(days=400),
    )
    await service.refresh_inventory_summary(session, tenant, now=NOW)

    result = await service.run_report(
        session,
        tenant,
        key=ReportKey.DEAD_STOCK,
        period=None,
        filters=ReportFilters(),
        params=PageParams(page=1, limit=50),
        may_export=True,
        now=NOW,
    )

    assert isinstance(result, service.InlineReport)
    assert [row.item_code for row in result.rows] == [stock["second"].code]
    assert result.rows[0].days_since_movement == 400
    assert result.rows[0].ageing_bucket == "Over 365 days"


async def test_a_position_that_has_never_moved_is_dead_stock(
    session: AsyncSession, tenant: TenantContext, company, stock
) -> None:
    # A NULL last movement must not be read as "moved just now"; a bare ``<``
    # comparison in SQL would drop exactly the rows the report is for.
    await _balance(
        session,
        company_id=company.id,
        item_id=stock["first"].id,
        warehouse_id=stock["warehouse"].id,
        on_hand="10",
        stock_value="100.0000",
        last_movement_at=None,
    )
    await service.refresh_inventory_summary(session, tenant, now=NOW)

    result = await service.run_report(
        session,
        tenant,
        key=ReportKey.DEAD_STOCK,
        period=None,
        filters=ReportFilters(),
        params=PageParams(page=1, limit=50),
        may_export=True,
        now=NOW,
    )

    assert isinstance(result, service.InlineReport)
    assert len(result.rows) == 1
    assert result.rows[0].ageing_bucket == "Never moved"


async def test_the_dead_stock_threshold_is_overridable_per_request(
    session: AsyncSession, tenant: TenantContext, company, stock
) -> None:
    await _balance(
        session,
        company_id=company.id,
        item_id=stock["first"].id,
        warehouse_id=stock["warehouse"].id,
        on_hand="10",
        stock_value="100.0000",
        last_movement_at=NOW - timedelta(days=45),
    )
    await service.refresh_inventory_summary(session, tenant, now=NOW)

    strict = await service.run_report(
        session,
        tenant,
        key=ReportKey.DEAD_STOCK,
        period=None,
        filters=ReportFilters(min_days=30),
        params=PageParams(page=1, limit=50),
        may_export=True,
        now=NOW,
    )
    default = await service.run_report(
        session,
        tenant,
        key=ReportKey.DEAD_STOCK,
        period=None,
        filters=ReportFilters(),
        params=PageParams(page=1, limit=50),
        may_export=True,
        now=NOW,
    )

    assert isinstance(strict, service.InlineReport)
    assert isinstance(default, service.InlineReport)
    assert len(strict.rows) == 1
    assert default.rows == []


async def test_a_report_page_is_bounded_and_reports_its_total(
    session: AsyncSession, tenant: TenantContext, company, stock, unique_code
) -> None:
    for _ in range(5):
        item = await seed_support.seed_item(
            session, company_id=company.id, code=unique_code("ITEM")
        )
        await _balance(
            session,
            company_id=company.id,
            item_id=item.id,
            warehouse_id=stock["warehouse"].id,
            on_hand="1",
            stock_value="10.0000",
        )
    await service.refresh_inventory_summary(session, tenant, now=NOW)

    result = await service.run_report(
        session,
        tenant,
        key=ReportKey.INVENTORY_VALUATION,
        period=None,
        filters=ReportFilters(),
        params=PageParams(page=1, limit=2),
        may_export=True,
        now=NOW,
    )

    assert isinstance(result, service.InlineReport)
    assert len(result.rows) == 2
    assert result.pagination.total == 5
    assert result.pagination.has_next is True
    assert result.pagination.total_pages == 3


async def test_a_report_cannot_see_another_tenants_rows(
    session: AsyncSession, tenant: TenantContext, company, other_company, stock, unique_code
) -> None:
    other_warehouse = await seed_support.seed_warehouse(
        session, company_id=other_company.id, code=unique_code("WH")
    )
    other_item = await seed_support.seed_item(
        session, company_id=other_company.id, code=unique_code("ITEM")
    )
    await _balance(
        session,
        company_id=other_company.id,
        item_id=other_item.id,
        warehouse_id=other_warehouse.id,
        on_hand="500",
        stock_value="5000.0000",
    )
    other_tenant = TenantContext(company_id=other_company.id, user_id=tenant.user_id)
    await service.refresh_inventory_summary(session, other_tenant, now=NOW)

    result = await service.run_report(
        session,
        tenant,
        key=ReportKey.STOCK_AVAILABILITY,
        period=None,
        filters=ReportFilters(),
        params=PageParams(page=1, limit=50),
        may_export=True,
        now=NOW,
    )

    assert isinstance(result, service.InlineReport)
    assert result.rows == []


# -----------------------------------------------------------------------------
# Dashboard
# -----------------------------------------------------------------------------


async def test_the_dashboard_takes_its_stock_value_from_the_summary(
    session: AsyncSession, tenant: TenantContext, company, stock
) -> None:
    await _balance(
        session,
        company_id=company.id,
        item_id=stock["first"].id,
        warehouse_id=stock["warehouse"].id,
        on_hand="10",
        stock_value="1234.5600",
    )
    await service.refresh_inventory_summary(session, tenant, now=NOW)

    tiles = await service.dashboard(session, tenant, now=NOW)

    assert tiles.stock_value == Decimal("1234.5600")
    assert tiles.stock_refreshed_at == NOW
    assert tiles.generated_at == NOW


async def test_the_dashboard_renders_for_a_company_with_no_data_at_all(
    session: AsyncSession, tenant: TenantContext
) -> None:
    # Every tile is a count or a sum, so a brand-new tenant must get zeroes
    # rather than nulls that break the response model.
    tiles = await service.dashboard(session, tenant, now=NOW)

    assert tiles.open_sales_orders == 0
    assert tiles.stock_value == Decimal("0")
    assert tiles.pending_purchase_orders == 0
    assert tiles.production_in_progress == 0
    assert tiles.qc_pending == 0
    assert tiles.dispatches_in_transit == 0
    assert tiles.overdue_deliveries == 0
    assert tiles.stock_refreshed_at is None


# -----------------------------------------------------------------------------
# The background job lifecycle
# -----------------------------------------------------------------------------


async def _queued_snapshot(session: AsyncSession, tenant: TenantContext) -> ReportSnapshot:
    repo = ReportSnapshotRepository(session, tenant)
    snapshot = ReportSnapshot(
        report_key=ReportKey.STOCK_AVAILABILITY,
        parameters_hash="a" * 64,
        parameters={"report_key": "STOCK_AVAILABILITY", "filters": {}},
        status=ReportJobStatus.QUEUED,
        expires_at=NOW + timedelta(hours=service.REPORT_SNAPSHOT_TTL_HOURS),
    )
    repo.add(snapshot)
    await session.flush()
    return snapshot


async def test_a_job_runs_through_its_lifecycle_and_audits_every_move(
    session: AsyncSession, tenant: TenantContext, company
) -> None:
    snapshot = await _queued_snapshot(session, tenant)

    await service.start_snapshot(session, tenant, snapshot.id)
    assert snapshot.status is ReportJobStatus.RUNNING

    await service.complete_snapshot(session, tenant, snapshot_id=snapshot.id, row_count=42)

    assert snapshot.status is ReportJobStatus.COMPLETED
    assert snapshot.row_count == 42
    # The CHECK constraint requires this of a completed job, so a missing stamp
    # would have failed the write rather than produced a job with no result date.
    assert snapshot.generated_at is not None

    audited = (
        (
            await session.execute(
                select(AuditLog.action).where(
                    AuditLog.company_id == company.id,
                    AuditLog.entity_type == service.AUDIT_ENTITY,
                    AuditLog.entity_id == snapshot.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert "STATUS_CHANGE" in audited
    assert "DATA_EXPORT" in audited


async def test_a_re_delivered_task_cannot_start_a_job_twice(
    session: AsyncSession, tenant: TenantContext
) -> None:
    # ``task_acks_late`` means a worker killed mid-render has its task delivered
    # again. The state machine is what stops the second delivery writing a second
    # workbook for the same job.
    snapshot = await _queued_snapshot(session, tenant)
    await service.start_snapshot(session, tenant, snapshot.id)

    with pytest.raises(InvalidStateTransitionError):
        await service.start_snapshot(session, tenant, snapshot.id)


async def test_a_failed_job_records_a_message_a_person_can_act_on(
    session: AsyncSession, tenant: TenantContext
) -> None:
    snapshot = await _queued_snapshot(session, tenant)
    await service.start_snapshot(session, tenant, snapshot.id)

    await service.fail_snapshot(
        session, tenant, snapshot_id=snapshot.id, error="The report could not be generated."
    )

    assert snapshot.status is ReportJobStatus.FAILED
    assert snapshot.error_message == "The report could not be generated."
    job = await service.get_job(session, tenant, snapshot.id)
    assert job.status is ReportJobStatus.FAILED
    assert job.download_url is None


async def test_a_job_belonging_to_another_tenant_is_reported_as_missing(
    session: AsyncSession, tenant: TenantContext, other_company
) -> None:
    snapshot = await _queued_snapshot(session, tenant)
    intruder = TenantContext(company_id=other_company.id, user_id=tenant.user_id)

    with pytest.raises(NotFoundError):
        await service.get_job(session, intruder, snapshot.id)


async def test_a_live_snapshot_is_reused_rather_than_regenerated(
    session: AsyncSession, tenant: TenantContext
) -> None:
    # Five people opening the same link must not queue five identical exports.
    snapshot = await _queued_snapshot(session, tenant)
    repo = ReportSnapshotRepository(session, tenant)

    found = await repo.find_reusable(ReportKey.STOCK_AVAILABILITY, "a" * 64, now=NOW)

    assert found is not None
    assert found.id == snapshot.id


async def test_an_expired_snapshot_is_not_reused_and_is_purged(
    session: AsyncSession, tenant: TenantContext
) -> None:
    snapshot = await _queued_snapshot(session, tenant)
    await service.start_snapshot(session, tenant, snapshot.id)
    await service.complete_snapshot(session, tenant, snapshot_id=snapshot.id, row_count=1)
    snapshot.expires_at = datetime(2020, 1, 1, tzinfo=UTC)
    await session.flush()

    repo = ReportSnapshotRepository(session, tenant)
    assert await repo.find_reusable(ReportKey.STOCK_AVAILABILITY, "a" * 64, now=NOW) is None

    removed = await service.purge_expired_snapshots(session, tenant)
    assert removed == 1
    assert await repo.get(snapshot.id) is None
