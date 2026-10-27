"""Revision 34 preserves source links and refuses inconsistent legacy history."""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.util import await_only

from src.core.clock import today
from src.modules.procurement.models import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderSourceAllocation,
    PurchaseOrderStatus,
    PurchaseRequisition,
    PurchaseRequisitionLine,
    RequisitionStatus,
)
from src.modules.vendors.models import Vendor
from src.modules.warehouses.models import Warehouse
from tests import seed_support
from tests.integration.test_procurement_service import _schema as _schema
from tests.integration.test_procurement_service import _schema_engine
from tests.integration.test_procurement_service import db as db
from tests.integration.test_procurement_service import masters as masters
from tests.integration.test_procurement_service import tenant as tenant
from tests.integration.test_product_specification_migration import _differences

pytestmark = pytest.mark.integration
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations/versions/20261103_0100_purchase_order_amendments.py"
)


def _apply(connection, command, migration=MIGRATION):
    spec = importlib.util.spec_from_file_location("purchase_order_amendments_migration", migration)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, command)()


async def _legacy_rows(db, tenant, masters, *, cancelled=False, inconsistent=False):
    header = PurchaseRequisition(
        company_id=tenant.company_id,
        code="MIGRATION-PR",
        requisition_date=today(),
        required_date=today(),
        warehouse_id=masters["warehouse"].id,
        status=RequisitionStatus.CONVERTED,
        requested_by_id=tenant.user_id,
    )
    db.add(header)
    await db.flush()
    source = PurchaseRequisitionLine(
        company_id=tenant.company_id,
        requisition_id=header.id,
        line_number=1,
        item_id=masters["item"].id,
        uom_id=masters["uom"].id,
        quantity=Decimal("10"),
        ordered_quantity=Decimal("9" if inconsistent else "0" if cancelled else "10"),
    )
    order = PurchaseOrder(
        company_id=tenant.company_id,
        code="MIGRATION-PO",
        po_date=today(),
        vendor_id=masters["vendor"].id,
        warehouse_id=masters["warehouse"].id,
        status=PurchaseOrderStatus.CANCELLED if cancelled else PurchaseOrderStatus.DRAFT,
        cancellation_reason="Historical cancellation" if cancelled else None,
    )
    db.add_all([source, order])
    await db.flush()
    line = PurchaseOrderLine(
        company_id=tenant.company_id,
        purchase_order_id=order.id,
        requisition_line_id=source.id,
        line_number=1,
        item_id=masters["item"].id,
        uom_id=masters["uom"].id,
        quantity=Decimal("10"),
        unit_price=Decimal("50"),
    )
    db.add(line)
    await db.flush()
    ids = source.id, line.id
    await db.commit()
    connection = await db.connection()
    await connection.run_sync(lambda sync: _apply(sync, "downgrade"))
    return connection, ids


@pytest.mark.parametrize("cancelled", [False, True])
async def test_backfill_preserves_links_counters_and_inactive_history(
    db, tenant, masters, cancelled
):
    connection, (source_id, line_id) = await _legacy_rows(db, tenant, masters, cancelled=cancelled)
    before = await connection.scalar(
        sa.text("SELECT ordered_quantity FROM purchase_requisition_lines WHERE id=:id"),
        {"id": source_id},
    )
    await connection.run_sync(lambda sync: _apply(sync, "upgrade"))
    mapping = (
        (await connection.execute(sa.select(PurchaseOrderSourceAllocation.__table__)))
        .mappings()
        .one()
    )
    assert mapping["purchase_order_line_id"] == line_id
    assert mapping["requisition_line_id"] == source_id
    assert mapping["quantity"] == Decimal("10")
    assert mapping["is_active"] is (not cancelled)
    assert mapping["id"].version == 7
    assert (
        await connection.scalar(
            sa.text("SELECT ordered_quantity FROM purchase_requisition_lines WHERE id=:id"),
            {"id": source_id},
        )
        == before
    )
    rename = MIGRATION.with_name("20261104_0100_po_allocation_constraint_names.py")
    await connection.run_sync(lambda sync: _apply(sync, "upgrade", rename))
    assert await connection.run_sync(_differences) == []
    await connection.run_sync(lambda sync: _apply(sync, "downgrade", rename))
    await connection.run_sync(lambda sync: _apply(sync, "downgrade"))
    assert (
        await connection.scalar(
            sa.text("SELECT requisition_line_id FROM purchase_order_lines WHERE id=:id"),
            {"id": line_id},
        )
        == source_id
    )


async def test_inconsistent_counters_refuse_upgrade_without_repair(db, tenant, masters):
    connection, (source_id, _) = await _legacy_rows(db, tenant, masters, inconsistent=True)
    with pytest.raises(RuntimeError, match="reconcile legacy ordered counters"):
        await connection.run_sync(lambda sync: _apply(sync, "upgrade"))
    assert await connection.scalar(
        sa.text("SELECT ordered_quantity FROM purchase_requisition_lines WHERE id=:id"),
        {"id": source_id},
    ) == Decimal("9")
    assert not await connection.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='purchase_orders' "
            "AND column_name='amendment_number')"
        )
    )


async def test_downgrade_refuses_revision_evidence(db, tenant, masters):
    connection, _ = await _legacy_rows(db, tenant, masters)
    await connection.run_sync(lambda sync: _apply(sync, "upgrade"))
    await connection.execute(
        sa.text("UPDATE purchase_orders SET amendment_number=1,last_amended_at=now()")
    )
    with pytest.raises(RuntimeError, match="revision/confirmation history"):
        await connection.run_sync(lambda sync: _apply(sync, "downgrade"))


async def test_same_tenant_composite_foreign_keys_enforced(db, tenant, masters):
    connection, (_, line_id) = await _legacy_rows(db, tenant, masters)
    await connection.run_sync(lambda sync: _apply(sync, "upgrade"))
    with pytest.raises(sa.exc.IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                sa.text(
                    "UPDATE purchase_order_source_allocations SET company_id=gen_random_uuid() "
                    "WHERE purchase_order_line_id=:id"
                ),
                {"id": line_id},
            )


async def test_downgrade_preflight_freezes_concurrent_amendment_history(_schema, tenant):
    """A real writer cannot commit history between refusal checks and DROP.

    Pause immediately before the first destructive statement, after the actual
    migration has completed both safety checks. The writer's bounded database
    lock timeout proves serialization without a timing sleep. Test DDL always
    rolls back, and the committed fixture rows are explicitly removed.
    """
    engine = _schema_engine(_schema[0])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    reached_drop = asyncio.Event()
    release_drop = asyncio.Event()
    migration_task = None
    identifiers = None
    listener_installed = False

    def pause_before_drop(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("DROP TABLE") and (
            "purchase_order_source_allocations" in statement
        ):
            reached_drop.set()
            await_only(release_drop.wait())

    try:
        async with factory() as setup:
            suffix = uuid.uuid4().hex[:10]
            warehouse = await seed_support.seed_warehouse(
                setup, company_id=tenant.company_id, code=f"MIG-RACE-{suffix}"
            )
            vendor = await seed_support.seed_vendor(setup, company_id=tenant.company_id)
            order = PurchaseOrder(
                company_id=tenant.company_id,
                code=f"MIG-RACE-{suffix}",
                po_date=today(),
                vendor_id=vendor.id,
                warehouse_id=warehouse.id,
            )
            setup.add(order)
            await setup.flush()
            identifiers = order.id, vendor.id, warehouse.id
            await setup.commit()

        sa.event.listen(engine.sync_engine, "before_cursor_execute", pause_before_drop)
        listener_installed = True

        async def downgrade_and_restore():
            async with engine.connect() as connection:
                transaction = await connection.begin()
                try:
                    await connection.run_sync(lambda sync: _apply(sync, "downgrade"))
                finally:
                    # Restore the module fixture's current schema even when an
                    # assertion or the writer fails. No task-specific DB stays.
                    await transaction.rollback()

        migration_task = asyncio.create_task(downgrade_and_restore())
        await asyncio.wait_for(reached_drop.wait(), timeout=15)
        writer_failure = None
        try:
            async with engine.begin() as writer:
                await writer.execute(sa.text("SET LOCAL lock_timeout = '300ms'"))
                await writer.execute(
                    sa.text(
                        "UPDATE purchase_orders SET amendment_number=1,last_amended_at=now() "
                        "WHERE id=:id"
                    ),
                    {"id": identifiers[0]},
                )
        except sa.exc.DBAPIError as exc:
            writer_failure = exc
        assert writer_failure is not None, "Amendment history committed after downgrade preflight"
        assert getattr(writer_failure.orig, "sqlstate", None) == "55P03"
        release_drop.set()
        await asyncio.wait_for(migration_task, timeout=15)

        async with engine.connect() as verification:
            evidence = (
                await verification.execute(
                    sa.text(
                        "SELECT amendment_number,last_amended_at FROM purchase_orders WHERE id=:id"
                    ),
                    {"id": identifiers[0]},
                )
            ).one()
            assert evidence.amendment_number == 0
            assert evidence.last_amended_at is None
    finally:
        release_drop.set()
        if migration_task is not None:
            await asyncio.gather(migration_task, return_exceptions=True)
        if listener_installed:
            sa.event.remove(engine.sync_engine, "before_cursor_execute", pause_before_drop)
        if identifiers is not None:
            order_id, vendor_id, warehouse_id = identifiers
            async with engine.begin() as cleanup:
                await cleanup.execute(sa.delete(PurchaseOrder).where(PurchaseOrder.id == order_id))
                await cleanup.execute(sa.delete(Vendor).where(Vendor.id == vendor_id))
                await cleanup.execute(sa.delete(Warehouse).where(Warehouse.id == warehouse_id))
        await engine.dispose()
