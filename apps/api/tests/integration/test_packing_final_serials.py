"""FINAL QC approves physical units, and a sealed carton must contain those units."""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from src.core.clock import today
from src.core.errors import BusinessRuleError, InvalidStateTransitionError
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.inventory import service as inventory
from src.modules.inventory.models import InventoryTransaction, TransactionType
from src.modules.inventory.schemas import StockSerialBulkCreate
from src.modules.packing import service as packing
from src.modules.packing.models import Carton, CartonContent, PackingOrderLine, PackingOrderStatus
from src.modules.packing.schemas import (
    CartonContentIn,
    CartonContentsReplace,
    CartonCreate,
    CompleteRequest,
    PackingOrderCreate,
    PackingOrderLineIn,
    PackingSerialSelection,
    SealCartonRequest,
)
from src.modules.products.models import Item, ItemGroup
from src.modules.quality import service as quality
from src.modules.quality.models import InspectionStatus, QcInspection
from src.modules.quality.schemas import DecideRequest, InspectionCreate
from tests.dispatch_support import consignment_line, raise_consignment
from tests.seed_support import seed_customer, seed_item, seed_uom, seed_warehouse

pytestmark = pytest.mark.integration


@pytest.fixture
async def kit(session, tenant):
    return await make_packing_kit(session, tenant)


async def make_packing_kit(session, tenant):
    uom = await seed_uom(session, company_id=tenant.company_id)
    item = await seed_item(
        session,
        company_id=tenant.company_id,
        code=f"FINAL-{uuid.uuid4().hex[:8]}",
        base_uom_id=uom.id,
        is_serial_tracked=True,
        requires_final_qc=True,
        can_sell=True,
    )
    box = await seed_item(
        session,
        company_id=tenant.company_id,
        code=f"BOX-{uuid.uuid4().hex[:8]}",
        base_uom_id=uom.id,
        item_group=ItemGroup.PACKING_MATERIAL,
    )
    warehouse = await seed_warehouse(
        session, company_id=tenant.company_id, code=f"PACK-WH-{uuid.uuid4().hex[:8]}"
    )
    customer = await seed_customer(session, company_id=tenant.company_id)
    units = await inventory.TraceabilityService(session, tenant).bulk_create_serials(
        StockSerialBulkCreate(
            item_id=item.id, serial_numbers=[f"PACK-{uuid.uuid4().hex[:12]}" for _ in range(4)]
        )
    )
    await inventory.post_movements(
        session,
        tenant,
        [
            inventory.MovementRequest(
                item_id=item.id,
                warehouse_id=warehouse.id,
                serial_id=unit.id,
                quantity=Decimal("1"),
                transaction_type=TransactionType.OPENING_BALANCE,
                reference_type="opening",
                reason="Named packing fixture stock",
                unit_cost=Decimal("10"),
            )
            for unit in units
        ]
        + [
            inventory.MovementRequest(
                item_id=box.id,
                warehouse_id=warehouse.id,
                quantity=Decimal("10"),
                transaction_type=TransactionType.OPENING_BALANCE,
                reference_type="opening",
                reason="Packing material opening stock",
                unit_cost=Decimal("1"),
            )
        ],
    )
    order, lines = await packing.create_order(
        session,
        tenant,
        PackingOrderCreate(
            customer_id=customer.id,
            warehouse_id=warehouse.id,
            planned_date=today(),
            lines=[PackingOrderLineIn(item_id=item.id, quantity_to_pack=2, uom_id=uom.id)],
        ),
    )
    return SimpleNamespace(
        session=session,
        tenant=tenant,
        order_id=order.id,
        line_id=lines[0].id,
        item_id=item.id,
        box_id=box.id,
        warehouse_id=warehouse.id,
        ids=[unit.id for unit in units],
        uom_id=uom.id,
        customer_id=customer.id,
    )


async def start(k, serial_ids=None):
    _, lines = await packing.start_packing(
        k.session,
        k.tenant,
        k.order_id,
        reason="Inspect named units before sealing",
        serial_selections=[
            PackingSerialSelection(line_id=k.line_id, serial_ids=serial_ids or k.ids[:2])
        ],
    )
    return lines[0].qc_inspection_id


def contents(ids):
    return [CartonContentIn(line_number=1, quantity=len(ids), serial_ids=ids, net_weight_kg=1)]


async def carton(k, ids=None):
    return await packing.add_carton(
        k.session,
        k.tenant,
        k.order_id,
        CartonCreate(
            packing_material_item_id=k.box_id,
            contents=contents(ids or k.ids[:2]),
        ),
    )


async def decide(k, inspection_id, accepted, rejected=()):
    return await quality.decide_inspection(
        k.session,
        k.tenant,
        inspection_id,
        DecideRequest(
            accepted_quantity=len(accepted),
            rejected_quantity=len(rejected),
            serial_dispositions={
                **{str(unit): "ACCEPTED" for unit in accepted},
                **{str(unit): "REJECTED" for unit in rejected},
            },
            rejection_reason="Rejected serial surface damaged" if rejected else None,
            reason="Physical units checked individually",
        ),
    )


async def seal(k, carton_id):
    return await packing.seal_carton(
        k.session, k.tenant, k.order_id, carton_id, SealCartonRequest(reason="Accepted units boxed")
    )


async def consignment_for(k, serial_ids):
    """An open consignment for the packed units - what a hand-over loads them onto."""
    return await raise_consignment(
        k.session,
        k.tenant,
        customer_id=k.customer_id,
        warehouse_id=k.warehouse_id,
        lines=[consignment_line(k.item_id, len(serial_ids), serial_ids=serial_ids)],
    )


async def test_start_requires_named_units_and_rolls_back_draft(kit):
    k = kit
    with pytest.raises(BusinessRuleError, match="Select every serial"):
        await packing.start_packing(
            k.session, k.tenant, k.order_id, reason="Start final inspection"
        )
    order = await packing.PackingOrderRepository(k.session, k.tenant).get_or_404(k.order_id)
    assert order.status == PackingOrderStatus.DRAFT
    assert (
        await k.session.scalar(
            select(QcInspection.id).where(QcInspection.reference_id == k.line_id)
        )
        is None
    )


@pytest.mark.parametrize("indices", [(0,), (0, 1, 2)])
async def test_start_requires_one_serial_per_line_unit(kit, indices):
    with pytest.raises(BusinessRuleError, match="one distinct serial"):
        await start(kit, [kit.ids[index] for index in indices])


async def test_selected_units_are_persisted_and_passed_to_final_inspection(kit):
    k = kit
    inspection_id = await start(k)
    inspection = await k.session.get(QcInspection, inspection_id)
    line = await k.session.get(PackingOrderLine, k.line_id)
    assert line.qc_serial_ids == k.ids[:2]
    assert inspection.serial_ids == k.ids[:2]
    assert inspection.reference_id == line.id
    assert inspection.reference_type == packing.QUALITY_REFERENCE_PACKING_ORDER
    assert inspection.inspected_quantity == 2
    read = await packing.get_order_detail(k.session, k.tenant, k.order_id)
    assert read.lines[0].is_serial_tracked
    assert read.lines[0].requires_final_qc
    assert not read.lines[0].is_batch_tracked
    assert read.lines[0].qc_inspection_status == "PENDING"


async def test_carton_cannot_substitute_an_uninspected_available_unit(kit):
    k = kit
    inspection_id = await start(k)
    await decide(k, inspection_id, k.ids[:2])
    with pytest.raises(BusinessRuleError, match="selected FINAL-QC serials"):
        await carton(k, [k.ids[0], k.ids[2]])
    assert (
        await k.session.scalar(select(Carton.id).where(Carton.packing_order_id == k.order_id))
        is None
    )


async def test_pending_final_qc_keeps_carton_open_and_material_unissued(kit):
    k = kit
    await start(k)
    carton_id = (await carton(k)).id
    with pytest.raises(BusinessRuleError, match="ACCEPTED FINAL inspection"):
        await seal(k, carton_id)
    stored = await k.session.get(Carton, carton_id)
    assert not stored.is_sealed
    assert stored.material_transaction_id is None
    assert await inventory.get_available(k.session, k.tenant, k.box_id, k.warehouse_id) == 10


async def test_accepted_named_units_can_be_sealed_completed_and_handed_over(kit):
    k = kit
    inspection_id = await start(k)
    await decide(k, inspection_id, k.ids[:2])
    carton_id = (await carton(k)).id
    await seal(k, carton_id)
    result = await packing.complete_packing(
        k.session, k.tenant, k.order_id, CompleteRequest(reason="All inspected units packed")
    )
    assert result.order.status == PackingOrderStatus.PACKED
    dispatch_id = await consignment_for(k, k.ids[:2])
    order = await packing.hand_over_to_dispatch(
        k.session,
        k.tenant,
        k.order_id,
        dispatch_id=dispatch_id,
        reason="Accepted cartons assigned to dispatch",
    )
    assert order.status == PackingOrderStatus.HANDED_OVER
    assert order.dispatch_id == dispatch_id


async def test_rejected_units_are_replaced_before_reinspection_and_sealing(kit):
    k = kit
    inspection_id = await start(k)
    carton_id = (await carton(k)).id
    await decide(k, inspection_id, [], k.ids[:2])
    with pytest.raises(BusinessRuleError, match="replacement serial units"):
        await packing.reinspect_line(
            k.session, k.tenant, k.order_id, k.line_id, reason="Replace damaged units"
        )
    line = await packing.reinspect_line(
        k.session,
        k.tenant,
        k.order_id,
        k.line_id,
        reason="Replace damaged units",
        serial_ids=k.ids[2:],
    )
    replacement_id = line.qc_inspection_id
    assert replacement_id != inspection_id
    assert line.qc_serial_ids == k.ids[2:]
    await packing.replace_carton_contents(
        k.session,
        k.tenant,
        k.order_id,
        carton_id,
        CartonContentsReplace(reason="Damaged units removed", contents=contents(k.ids[2:])),
    )
    await decide(k, replacement_id, k.ids[2:])
    await seal(k, carton_id)
    result = await packing.complete_packing(
        k.session, k.tenant, k.order_id, CompleteRequest(reason="Replacement units packed")
    )
    assert result.order.status == PackingOrderStatus.PACKED


@pytest.mark.parametrize("gate", ["complete", "hand over"])
async def test_completion_and_handover_recheck_stored_carton_membership(kit, gate):
    k = kit
    inspection_id = await start(k)
    await decide(k, inspection_id, k.ids[:2])
    carton_id = (await carton(k)).id
    await seal(k, carton_id)
    dispatch_id = None
    if gate == "hand over":
        await packing.complete_packing(
            k.session, k.tenant, k.order_id, CompleteRequest(reason="Accepted units completed")
        )
        # A real consignment, open for loading: the FINAL-QC gate is what has to
        # refuse here, not a missing dispatch.
        dispatch_id = await consignment_for(k, k.ids[:2])
    # Model a historic/imported inconsistent carton, bypassing the write service.
    await k.session.execute(
        update(CartonContent)
        .where(CartonContent.carton_id == carton_id)
        .values(serial_ids=[k.ids[0], k.ids[2]])
    )
    with pytest.raises(BusinessRuleError, match="selected FINAL-QC serials"):
        if dispatch_id is None:
            await packing.complete_packing(
                k.session, k.tenant, k.order_id, CompleteRequest(reason="Complete imported carton")
            )
        else:
            await packing.hand_over_to_dispatch(
                k.session,
                k.tenant,
                k.order_id,
                dispatch_id=dispatch_id,
                reason="Claim imported carton",
            )


async def test_cancellation_withdraws_untouched_inspection_and_releases_units_for_fresh_qc(kit):
    k = kit
    inspection_id = await start(k)
    await packing.cancel_order(
        k.session, k.tenant, k.order_id, reason="Customer cancelled before inspection"
    )
    inspection = await k.session.get(QcInspection, inspection_id)
    assert inspection.status == InspectionStatus.WITHDRAWN
    assert inspection.result == "PENDING"
    assert inspection.serial_ids == k.ids[:2]
    assert inspection.serial_dispositions == {}
    audit = await k.session.scalar(
        select(AuditLog).where(
            AuditLog.entity_id == inspection_id,
            AuditLog.action == AuditAction.CANCEL,
        )
    )
    assert audit.new_values["status"] == "WITHDRAWN"
    assert audit.context["reason"] == "Customer cancelled before inspection"
    assert await inventory.get_available(k.session, k.tenant, k.item_id, k.warehouse_id) == 4
    with pytest.raises(InvalidStateTransitionError):
        await decide(k, inspection_id, k.ids[:2])
    order, lines = await packing.create_order(
        k.session,
        k.tenant,
        PackingOrderCreate(
            customer_id=k.customer_id,
            warehouse_id=k.warehouse_id,
            planned_date=today(),
            lines=[PackingOrderLineIn(item_id=k.item_id, quantity_to_pack=2, uom_id=k.uom_id)],
        ),
    )
    _, fresh = await packing.start_packing(
        k.session,
        k.tenant,
        order.id,
        reason="Repack against a fresh request",
        serial_selections=[PackingSerialSelection(line_id=lines[0].id, serial_ids=k.ids[:2])],
    )
    assert fresh[0].qc_inspection_id != inspection_id


async def test_cancelling_an_order_preserves_an_existing_accepted_verdict(kit):
    inspection_id = await start(kit)
    await decide(kit, inspection_id, kit.ids[:2])
    await packing.cancel_order(
        kit.session, kit.tenant, kit.order_id, reason="Customer cancelled after inspection"
    )
    inspection = await kit.session.get(QcInspection, inspection_id)
    assert inspection.status == "ACCEPTED"
    assert inspection.serial_dispositions == {str(unit): "ACCEPTED" for unit in kit.ids[:2]}


async def test_cancellation_cannot_withdraw_an_inspection_already_started(kit):
    inspection_id = await start(kit)
    cached = await kit.session.get(QcInspection, inspection_id)
    await kit.session.execute(
        update(QcInspection)
        .where(QcInspection.id == inspection_id)
        .values(status=InspectionStatus.IN_PROGRESS)
        .execution_options(synchronize_session=False)
    )
    # Model another transaction's newer state while this session still carries
    # its earlier PENDING object: locking must refresh, not overwrite, that state.
    assert cached.status == InspectionStatus.PENDING
    with pytest.raises(BusinessRuleError, match="already in progress"):
        await packing.cancel_order(
            kit.session, kit.tenant, kit.order_id, reason="Cancel measured lot"
        )


async def test_cancellation_cannot_withdraw_a_manually_held_packing_inspection(kit):
    k = kit
    inspection = await quality.create_inspection(
        k.session,
        k.tenant,
        InspectionCreate(
            item_id=k.item_id,
            warehouse_id=k.warehouse_id,
            qc_stage="FINAL",
            inspected_quantity=2,
            serial_ids=k.ids[:2],
            reference_type="packing_order",
            reference_id=k.line_id,
            hold_stock=True,
            reason="Manual final inspection explicitly held stock",
        ),
    )
    await k.session.execute(
        update(PackingOrderLine)
        .where(PackingOrderLine.id == k.line_id)
        .values(qc_inspection_id=inspection.id)
    )
    with pytest.raises(BusinessRuleError, match="already in progress"):
        await packing.cancel_order(
            k.session, k.tenant, k.order_id, reason="Cancel manually held lot"
        )


async def legacy_sealed_carton(k, *, packed=False):
    inspection_id = await start(k)
    await decide(k, inspection_id, k.ids[:2])
    carton_id = (await carton(k)).id
    await seal(k, carton_id)
    if packed:
        await packing.complete_packing(
            k.session,
            k.tenant,
            k.order_id,
            CompleteRequest(reason="Historical quantity-only packing completed"),
        )
    await k.session.execute(
        update(PackingOrderLine)
        .where(PackingOrderLine.id == k.line_id)
        .values(qc_serial_ids=[], qc_batch_id=None)
    )
    await k.session.execute(
        update(QcInspection)
        .where(QcInspection.id == inspection_id)
        .values(serial_ids=[], serial_dispositions={})
    )
    return carton_id


@pytest.mark.parametrize("packed", [False, True])
async def test_legacy_recovery_returns_original_cost_once_then_allows_cancellation(kit, packed):
    k = kit
    carton_id = await legacy_sealed_carton(k, packed=packed)
    original_id = (await k.session.get(Carton, carton_id)).material_transaction_id
    original = await k.session.get(InventoryTransaction, original_id)
    original_cost = original.unit_cost
    await k.session.execute(update(Item).where(Item.id == k.box_id).values(standard_cost=99))
    recovered = await packing.recover_legacy_carton(
        k.session,
        k.tenant,
        k.order_id,
        carton_id,
        reason="Recovered reusable box and counted legacy goods for repacking",
    )
    assert not recovered.is_sealed
    assert recovered.material_transaction_id is None
    returned = await k.session.scalar(
        select(InventoryTransaction).where(
            InventoryTransaction.reference_id == carton_id,
            InventoryTransaction.transaction_type == TransactionType.MATERIAL_RETURN,
        )
    )
    assert returned.quantity == 1
    assert returned.unit_cost == original_cost
    audit = await k.session.scalar(
        select(AuditLog).where(
            AuditLog.entity_id == carton_id,
            AuditLog.summary.like("Recovered legacy carton %"),
        )
    )
    assert audit.context["original_material_issue_id"] == str(original_id)
    assert audit.context["material_return_id"] == str(returned.id)
    assert await inventory.get_available(k.session, k.tenant, k.box_id, k.warehouse_id) == 10
    with pytest.raises(BusinessRuleError, match="Only a sealed legacy carton"):
        await packing.recover_legacy_carton(
            k.session, k.tenant, k.order_id, carton_id, reason="Duplicate recovery retry"
        )
    cancelled = await packing.cancel_order(
        k.session,
        k.tenant,
        k.order_id,
        reason="Repack recovered goods with actual serial selections",
    )
    assert cancelled.status == PackingOrderStatus.CANCELLED


async def test_named_qc_sealed_cartons_cannot_use_the_legacy_exception(kit):
    inspection_id = await start(kit)
    await decide(kit, inspection_id, kit.ids[:2])
    carton_id = (await carton(kit)).id
    await seal(kit, carton_id)
    with pytest.raises(BusinessRuleError, match="Only legacy serial FINAL"):
        await packing.recover_legacy_carton(
            kit.session,
            kit.tenant,
            kit.order_id,
            carton_id,
            reason="Try to reopen a valid named-unit carton",
        )


async def test_legacy_recovery_rejects_a_mismatched_material_ledger_reference(kit):
    carton_id = await legacy_sealed_carton(kit)
    foreign_issue = await kit.session.scalar(
        select(InventoryTransaction.id).where(InventoryTransaction.item_id == kit.item_id)
    )
    await kit.session.execute(
        update(Carton).where(Carton.id == carton_id).values(material_transaction_id=foreign_issue)
    )
    with pytest.raises(BusinessRuleError, match="matching original material issue"):
        await packing.recover_legacy_carton(
            kit.session,
            kit.tenant,
            kit.order_id,
            carton_id,
            reason="Verify legacy material source before recovery",
        )


async def test_handed_over_legacy_orders_cannot_be_reopened(kit):
    carton_id = await legacy_sealed_carton(kit, packed=True)
    # Preserve a real historical handover without attempting to bypass the new gate.
    from src.core.clock import utcnow
    from src.modules.packing.models import PackingOrder

    await kit.session.execute(
        update(PackingOrder)
        .where(PackingOrder.id == kit.order_id)
        .values(
            status=PackingOrderStatus.HANDED_OVER, dispatch_id=uuid.uuid4(), handed_over_at=utcnow()
        )
    )
    with pytest.raises(BusinessRuleError, match="not claimed by dispatch"):
        await packing.recover_legacy_carton(
            kit.session,
            kit.tenant,
            kit.order_id,
            carton_id,
            reason="Try to recover goods after dispatch took them",
        )
