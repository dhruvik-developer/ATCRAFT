"""A packing order for named serial units that need FINAL QC, and its steps.

``make_packing_kit`` stocks four serialised units of a final-QC item and ten
boxes to pack them in, and raises a packing order for two units; ``start`` begins
packing against named units and returns the inspection it raised, ``decide``
accepts or rejects units on it, and ``carton``, ``contents`` and ``seal`` box
them. ``legacy_sealed_carton`` rewrites a sealed carton into the quantity-only
shape packing kept before serials were recorded, for tests of what still reads
it.

The final-serials suite and the packing HTTP suites all use them; they live
here so none of those files is another's library.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import update

from src.core.clock import today
from src.modules.inventory import service as inventory
from src.modules.inventory.models import TransactionType
from src.modules.inventory.schemas import StockSerialBulkCreate
from src.modules.packing import service as packing
from src.modules.packing.models import PackingOrderLine
from src.modules.packing.schemas import (
    CartonContentIn,
    CartonCreate,
    CompleteRequest,
    PackingOrderCreate,
    PackingOrderLineIn,
    PackingSerialSelection,
    SealCartonRequest,
)
from src.modules.products.models import ItemGroup
from src.modules.quality import service as quality
from src.modules.quality.models import QcInspection
from src.modules.quality.schemas import DecideRequest
from tests.seed_support import seed_customer, seed_item, seed_uom, seed_warehouse


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


__all__ = [
    "carton",
    "contents",
    "decide",
    "legacy_sealed_carton",
    "make_packing_kit",
    "seal",
    "start",
]
