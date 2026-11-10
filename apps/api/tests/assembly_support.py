"""A lock set, its three components and the assembly orders that build it.

``make_kit`` stocks a handle (one a set), keys (two) and a screw kit (four) at a
store - a thousand of each unless the test names other stock - and approves the
assembly BOM that puts them together; ``raise_order`` raises an assembly order
for the set, ``to_in_progress`` walks one to the bench, and ``completed_order``
assembles one end to end. Opening stock goes through the real engine
(``stock_in``), so every cost and reservation downstream is real.

The completion suite and the assembly and job-work HTTP suites all use them;
they live here so none of those files is another's library.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.database import transaction
from src.core.deps import TenantContext
from src.modules.assembly import service
from src.modules.assembly.models import AssemblyOrder
from src.modules.assembly.schemas import (
    AssemblyOrderCreate,
    CompleteRequest,
    IssueComponentsRequest,
)
from src.modules.bom.models import Bom, BomLine, BomStatus, BomType
from src.modules.inventory import service as inv
from src.modules.inventory.models import TransactionType
from src.modules.products.models import ItemGroup, ProductionType
from tests import seed_support


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


class Kit:
    """A lock set and its components, stocked at one store."""

    def __init__(
        self,
        *,
        uom_id: uuid.UUID,
        output_item_id: uuid.UUID,
        components: dict[uuid.UUID, Decimal],
        warehouse_id: uuid.UUID,
        output_warehouse_id: uuid.UUID,
    ) -> None:
        self.uom_id = uom_id
        self.output_item_id = output_item_id
        self.components = components
        self.warehouse_id = warehouse_id
        self.output_warehouse_id = output_warehouse_id


async def stock_in(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    quantity: str,
) -> None:
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
            reason="assembly completion fixture opening stock",
            unit_cost=Decimal("10"),
        )


async def make_kit(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    stock: dict[str, str] | None = None,
    separate_output_warehouse: bool = False,
) -> Kit:
    """A lock set of a handle (x1), keys (x2) and a screw kit (x4)."""
    uom = await seed_support.seed_uom(session, company_id=tenant.company_id)
    warehouse = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=_code("WH")
    )
    output_warehouse = (
        await seed_support.seed_warehouse(session, company_id=tenant.company_id, code=_code("FG"))
        if separate_output_warehouse
        else warehouse
    )
    output = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=_code("LOCKSET"),
        base_uom_id=uom.id,
        item_group=ItemGroup.ASSEMBLY,
        standard_cost=Decimal("40"),
        production_type=ProductionType.MAKE,
        can_manufacture=True,
    )
    per_set = {"HANDLE": Decimal("1"), "KEYS": Decimal("2"), "SCREWKIT": Decimal("4")}
    stock = stock or {}
    components: dict[uuid.UUID, Decimal] = {}
    for name, quantity_per in per_set.items():
        item = await seed_support.seed_item(
            session,
            company_id=tenant.company_id,
            code=_code(name),
            base_uom_id=uom.id,
            item_group=ItemGroup.COMPONENT,
            standard_cost=Decimal("10"),
            can_be_assembly_component=True,
        )
        components[item.id] = quantity_per
        on_hand = stock.get(name, "1000")
        if Decimal(on_hand) > 0:
            await stock_in(session, tenant, item.id, warehouse.id, on_hand)

    bom = Bom(
        company_id=tenant.company_id,
        code=_code("BOM"),
        item_id=output.id,
        version=1,
        status=BomStatus.APPROVED,
        bom_type=BomType.ASSEMBLY,
        is_default=True,
        output_quantity=Decimal("1"),
        base_uom_id=uom.id,
        effective_from=today(),
        approved_by_id=tenant.user_id,
        approved_at=utcnow(),
    )
    session.add(bom)
    await session.flush()
    for number, (item_id, quantity_per) in enumerate(components.items(), start=1):
        session.add(
            BomLine(
                company_id=tenant.company_id,
                bom_id=bom.id,
                parent_item_id=output.id,
                line_number=number,
                component_item_id=item_id,
                quantity_per=quantity_per,
                uom_id=uom.id,
            )
        )
    await session.flush()
    return Kit(
        uom_id=uom.id,
        output_item_id=output.id,
        components=components,
        warehouse_id=warehouse.id,
        output_warehouse_id=output_warehouse.id,
    )


async def raise_order(
    session: AsyncSession,
    tenant: TenantContext,
    kit: Kit,
    *,
    planned: str = "10",
    requires_qc: bool = False,
    **fields: object,
) -> AssemblyOrder:
    order, _ = await service.create_order(
        session,
        tenant,
        AssemblyOrderCreate(
            item_id=kit.output_item_id,
            planned_quantity=Decimal(planned),
            warehouse_id=kit.warehouse_id,
            output_warehouse_id=kit.output_warehouse_id,
            requires_qc=requires_qc,
            **fields,  # type: ignore[arg-type]
        ),
    )
    return order


async def to_in_progress(session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID) -> None:
    await service.approve_order(session, tenant, order_id, reason="Scheduled")
    await service.release_order(session, tenant, order_id, reason="To the bench")
    await service.issue_components(
        session, tenant, order_id, IssueComponentsRequest(reason="Kitted")
    )
    await service.start_assembly(session, tenant, order_id, reason="Shift started")


async def completed_order(
    session: AsyncSession, tenant: TenantContext, kit: Kit, *, quantity: str = "6", **fields
) -> uuid.UUID:
    order = await raise_order(session, tenant, kit, planned=quantity, **fields)
    order_id = order.id
    await to_in_progress(session, tenant, order_id)
    await service.complete_assembly(
        session,
        tenant,
        order_id,
        CompleteRequest(assembled_quantity=Decimal(quantity), reason="Bench finished"),
    )
    return order_id


__all__ = [
    "Kit",
    "_code",
    "completed_order",
    "make_kit",
    "raise_order",
    "stock_in",
    "to_in_progress",
]
