"""Receipt/revision serialization and preservation of agreed PO evidence."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.core.clock import utcnow
from src.core.errors import BusinessRuleError, ConflictError
from src.modules.procurement import purchase_order_changes as changes
from src.modules.procurement import service
from src.modules.procurement.models import (
    GoodsReceipt,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderSourceAllocation,
    PurchaseOrderStatus,
    PurchaseRequisition,
    PurchaseRequisitionLine,
    RequisitionLineStatus,
    RequisitionStatus,
)
from src.modules.procurement.repository import PurchaseOrderRepository
from src.modules.procurement.schemas import (
    GoodsReceiptCreate,
    GoodsReceiptLineIn,
    PurchaseOrderAmendRequest,
    PurchaseOrderDuplicateResolutionRequest,
    PurchaseOrderLineIn,
)
from tests import seed_support
from tests.integration.test_procurement_service import _schema as _schema
from tests.integration.test_procurement_service import _schema_engine, make_order, send_order
from tests.integration.test_procurement_service import approver as approver
from tests.integration.test_procurement_service import buyer as buyer
from tests.integration.test_procurement_service import (
    buyer_without_approval as buyer_without_approval,
)
from tests.integration.test_procurement_service import db as db
from tests.integration.test_procurement_service import masters as masters
from tests.integration.test_procurement_service import tenant as tenant
from tests.integration.test_purchase_order_changes import (
    approved_request,
    fresh,
    linked_order,
    manual_payload,
    manual_resolution,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("first_actor", ["receipt", "amendment"])
async def test_receipt_creation_and_amendment_serialize_on_the_same_refreshed_header(
    _schema, tenant, buyer, monkeypatch, first_actor
):
    """Events establish lock order; no timing sleep decides the winner."""
    engine = _schema_engine(_schema[0])
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    tasks = []
    release_first = asyncio.Event()
    try:
        async with factory() as setup:
            uom = await seed_support.seed_uom(setup, company_id=tenant.company_id)
            warehouse = await seed_support.seed_warehouse(
                setup, company_id=tenant.company_id, code=f"RACE-{first_actor}"
            )
            vendor = await seed_support.seed_vendor(
                setup,
                company_id=tenant.company_id,
                is_approved=True,
                approved_at=utcnow(),
                approved_by_id=tenant.user_id,
                is_active=True,
            )
            item = await seed_support.seed_item(
                setup,
                company_id=tenant.company_id,
                code=f"RACE-{first_actor}",
                base_uom_id=uom.id,
                purchase_uom_id=uom.id,
                can_purchase=True,
                is_active=True,
                gst_rate=Decimal("18"),
            )
            race_masters = {"uom": uom, "warehouse": warehouse, "vendor": vendor, "item": item}
            order, lines, _ = await make_order(setup, tenant, buyer, race_masters, quantity="10")
            await send_order(setup, tenant, buyer, order)
            order_id, line_id, version = order.id, lines[0].id, order.version
        first_acquired = asyncio.Event()
        second_attempted = asyncio.Event()
        original = PurchaseOrderRepository.lock_for_update

        async def controlled_lock(repository, identifier):
            actor = asyncio.current_task().get_name()
            if identifier == order_id and actor == f"po-race-{first_actor}":
                locked = await original(repository, identifier)
                first_acquired.set()
                await release_first.wait()
                return locked
            if identifier == order_id and actor.startswith("po-race-"):
                second_attempted.set()
            return await original(repository, identifier)

        monkeypatch.setattr(PurchaseOrderRepository, "lock_for_update", controlled_lock)

        async def create_receipt():
            async with factory() as session:
                return await service.create_goods_receipt(
                    session,
                    tenant,
                    GoodsReceiptCreate(
                        purchase_order_id=order_id,
                        lines=[
                            GoodsReceiptLineIn(
                                line_number=1,
                                purchase_order_line_id=line_id,
                                received_quantity=Decimal("1"),
                                accepted_quantity=Decimal("1"),
                                rejected_quantity=Decimal("0"),
                            )
                        ],
                    ),
                )

        async def amend_order():
            async with factory() as session:
                return await changes.amend_purchase_order(
                    session,
                    tenant,
                    order_id,
                    PurchaseOrderAmendRequest(
                        expected_version=version,
                        reason="Supplier agrees to twelve units",
                        lines=[{"line_id": line_id, "quantity": "12"}],
                    ),
                )

        actions = {"receipt": create_receipt, "amendment": amend_order}
        second_actor = "amendment" if first_actor == "receipt" else "receipt"
        tasks.append(asyncio.create_task(actions[first_actor](), name=f"po-race-{first_actor}"))
        await asyncio.wait_for(first_acquired.wait(), timeout=15)
        tasks.append(asyncio.create_task(actions[second_actor](), name=f"po-race-{second_actor}"))
        await asyncio.wait_for(second_attempted.wait(), timeout=15)
        release_first.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=30)
        assert not isinstance(results[0], BaseException), results[0]
        assert isinstance(results[1], BusinessRuleError), results[1]
        async with factory() as verification:
            current = await verification.get(PurchaseOrder, order_id)
            line = await verification.get(PurchaseOrderLine, line_id)
            receipt_count = await verification.scalar(
                select(func.count(GoodsReceipt.id)).where(
                    GoodsReceipt.purchase_order_id == order_id
                )
            )
            if first_actor == "receipt":
                assert "receipt history" in str(results[1])
                assert receipt_count == 1
                assert current.status == PurchaseOrderStatus.SENT
                assert line.quantity == Decimal("10")
            else:
                assert receipt_count == 0
                assert current.status == PurchaseOrderStatus.PENDING_APPROVAL
                assert current.amendment_number == 1
                assert line.quantity == Decimal("12")
    finally:
        release_first.set()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()


async def test_revision_preserves_each_retained_lines_frozen_gst_after_master_change(
    db, tenant, buyer, masters
):
    order, lines, _ = await service.create_purchase_order(
        db,
        tenant,
        buyer,
        manual_payload(
            masters,
            lines=[
                PurchaseOrderLineIn(
                    line_number=1,
                    item_id=masters["item"].id,
                    quantity=Decimal("10"),
                    unit_price=Decimal("50"),
                ),
                PurchaseOrderLineIn(
                    line_number=2,
                    item_id=masters["batched_item"].id,
                    quantity=Decimal("10"),
                    unit_price=Decimal("50"),
                ),
            ],
        ),
    )
    await send_order(db, tenant, buyer, order)
    masters["batched_item"].gst_rate = Decimal("28")
    await db.flush()
    revised, retained = await changes.amend_purchase_order(
        db,
        tenant,
        order.id,
        PurchaseOrderAmendRequest(
            expected_version=order.version,
            reason="Increase only the first item quantity",
            lines=[
                {"line_id": lines[0].id, "quantity": "12"},
                {"line_id": lines[1].id, "quantity": "10"},
            ],
        ),
    )
    assert [line.tax_rate for line in retained] == [Decimal("18"), Decimal("18")]
    assert retained[1].tax_amount == Decimal("90")
    assert revised.total_amount == Decimal("1298")


async def test_cancel_released_converted_requisition_uses_closed_header_state(
    db, tenant, buyer, approver, masters
):
    header, demand = await approved_request(db, tenant, buyer, approver, masters, "10")
    order, _, _ = await linked_order(db, tenant, buyer, masters, [(demand[0].id, "10")])
    await service.transition_purchase_order(
        db, tenant, buyer, order.id, PurchaseOrderStatus.CANCELLED, reason="Supplier cannot deliver"
    )
    assert (await fresh(db, PurchaseRequisition, header.id)).status == RequisitionStatus.CONVERTED
    await changes.resolve_duplicate(
        db,
        tenant,
        buyer,
        PurchaseOrderDuplicateResolutionRequest(
            action="CANCEL_REQUEST",
            reason="The duplicate demand is no longer required",
            draft={
                "kind": "REQUISITION",
                "requisition_ids": [header.id],
                "requisition_line_ids": [demand[0].id],
                "conversion": {},
            },
        ),
    )
    assert (await fresh(db, PurchaseRequisition, header.id)).status == RequisitionStatus.CLOSED
    source = await fresh(db, PurchaseRequisitionLine, demand[0].id)
    assert source.status == RequisitionLineStatus.CANCELLED
    assert source.ordered_quantity == 0
    mapping = (await db.execute(select(PurchaseOrderSourceAllocation))).scalar_one()
    assert mapping.quantity == Decimal("10")
    assert mapping.is_active is False


async def test_repeated_zero_value_draft_merge_changes_version_and_rejects_stale_snapshot(
    db, tenant, buyer, masters
):
    masters["item"].gst_rate = Decimal("0")
    await db.flush()
    incoming = manual_payload(
        masters,
        lines=[
            PurchaseOrderLineIn(
                line_number=1, item_id=masters["item"].id, quantity="1", unit_price="0"
            )
        ],
    )
    order, _, _ = await service.create_purchase_order(db, tenant, buyer, incoming)
    await changes.resolve_duplicate(db, tenant, buyer, manual_resolution(order, incoming))
    stale_version = order.version
    await changes.resolve_duplicate(db, tenant, buyer, manual_resolution(order, incoming))
    assert order.version > stale_version
    order_id = order.id
    with pytest.raises(ConflictError):
        await changes.resolve_duplicate(
            db, tenant, buyer, manual_resolution(order, incoming, version=stale_version)
        )
    assert (await fresh(db, PurchaseOrder, order_id)).version > stale_version


async def test_create_with_approval_can_include_candidate_without_update_version(
    db, tenant, buyer, buyer_without_approval, masters
):
    incoming = manual_payload(masters)
    order, _, _ = await service.create_purchase_order(db, tenant, buyer, incoming)
    result = await changes.resolve_duplicate(
        db,
        tenant,
        buyer_without_approval,
        PurchaseOrderDuplicateResolutionRequest(
            action="CREATE_WITH_APPROVAL",
            candidate_purchase_order_id=order.id,
            reason="Additional independent confirmed requirement",
            draft={"kind": "MANUAL", "purchase_order": incoming},
        ),
    )
    assert len(result.purchase_orders) == 1
    assert result.purchase_orders[0].id != order.id
    separate = await fresh(db, PurchaseOrder, result.purchase_orders[0].id)
    assert separate.duplicate_acknowledged is True
