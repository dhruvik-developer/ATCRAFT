"""Restore customer goods at their recorded dispatch costs, including zero.

An ordinary line with a recorded movement restores its own original layers.
Legacy unanchored lines within a dispatch/item/batch share a FIFO sequence,
advancing by the quantity already returned. Serial units use their own movement.
No current master price participates (assumptions.md, customer-return FIFO basis).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError
from src.core.types import ZERO
from src.modules.inventory.repository import CostConsumptionRepository
from src.modules.inventory.service import (
    LayerCost,
    LayerView,
    carried_cost_layers,
    consume_layers,
)
from src.modules.sales_returns.models import SalesReturnLine
from src.modules.sales_returns.repository import SalesReturnRepository

CostGroup = tuple[uuid.UUID, uuid.UUID | None]


def returned_layer_slice(
    layers: Sequence[LayerCost], *, already_returned: Decimal, quantity: Decimal
) -> tuple[LayerCost, ...]:
    """Select the next original costs through the existing FIFO arithmetic."""
    if already_returned < ZERO or quantity <= ZERO:
        raise BusinessRuleError("The original dispatch return quantities are inconsistent.")
    used: dict[int, Decimal] = {}
    if already_returned:
        skipped = consume_layers(
            [LayerView(remaining=layer.quantity, unit_cost=layer.unit_cost) for layer in layers],
            already_returned,
            uncovered_unit_cost=ZERO,
        )
        if skipped.uncovered_quantity:
            raise BusinessRuleError(
                "The original dispatch cost could not be verified for earlier returns."
            )
        used = {draw.index: draw.quantity for draw in skipped.draws}
    returned = consume_layers(
        [
            LayerView(remaining=layer.quantity - used.get(index, ZERO), unit_cost=layer.unit_cost)
            for index, layer in enumerate(layers)
        ],
        quantity,
        uncovered_unit_cost=ZERO,
    )
    if returned.uncovered_quantity:
        raise BusinessRuleError(
            "The original dispatch cost could not be verified for the returned quantity."
        )
    return tuple(
        LayerCost(quantity=draw.quantity, unit_cost=draw.unit_cost) for draw in returned.draws
    )


async def return_cost_plans(
    session: AsyncSession,
    tenant: TenantContext,
    dispatch_id: uuid.UUID,
    lines: Sequence[SalesReturnLine],
) -> tuple[dict[uuid.UUID, tuple[LayerCost, ...]], dict[uuid.UUID, tuple[LayerCost, ...]]]:
    """Batch reads before any posting; caller holds the source dispatch lock."""
    ordinary_items = {line.item_id for line in lines if not line.serial_ids}
    serial_ids = {serial_id for line in lines for serial_id in line.serial_ids}
    repo = SalesReturnRepository(session, tenant)
    movements = await repo.dispatch_cost_movements(
        dispatch_id, ordinary_item_ids=ordinary_items, serial_ids=serial_ids
    )
    breakdown = await CostConsumptionRepository(session, tenant).by_transaction(
        [movement.id for movement in movements]
    )
    source_lines = await repo.dispatch_cost_lines(dispatch_id, ordinary_items)
    by_source = {line.id: line for line in source_lines}
    movement_costs: dict[uuid.UUID, tuple[LayerCost, ...]] = {}
    serial_costs: dict[uuid.UUID, tuple[LayerCost, ...]] = {}
    for movement in movements:
        costs = carried_cost_layers(breakdown, movement.id)
        quantity = -Decimal(movement.quantity)
        covered = sum((cost.quantity for cost in costs), ZERO)
        if quantity <= ZERO or covered > quantity:
            raise BusinessRuleError("The original dispatch cost history is inconsistent.")
        if covered < quantity:
            # A legacy/uncovered issue still records its own unit cost. Carry
            # that frozen basis, as stock transfers do, instead of today's cost.
            costs += (
                LayerCost(quantity=quantity - covered, unit_cost=Decimal(movement.unit_cost)),
            )
        if movement.serial_id is not None:
            if movement.serial_id in serial_costs or quantity != Decimal("1"):
                raise BusinessRuleError(
                    "The original dispatch serial cost history is inconsistent."
                )
            serial_costs[movement.serial_id] = costs
        else:
            movement_costs[movement.id] = costs
    if set(serial_costs) != serial_ids:
        raise BusinessRuleError(
            "The original dispatch cost for every returned serial could not be verified."
        )

    by_movement = {movement.id: movement for movement in movements}
    anchored: dict[uuid.UUID, tuple[LayerCost, ...]] = {}
    anchored_prior: dict[uuid.UUID, Decimal] = {}
    claimed: set[uuid.UUID] = set()
    prior: dict[CostGroup, Decimal] = defaultdict(lambda: ZERO)
    for recorded_line in source_lines:
        original = (
            by_movement.get(recorded_line.inventory_transaction_id)
            if recorded_line.inventory_transaction_id
            else None
        )
        if (
            original is not None
            and original.id in movement_costs
            and original.item_id == recorded_line.item_id
            and original.batch_id == recorded_line.batch_id
            and -Decimal(original.quantity) == Decimal(recorded_line.quantity)
        ):
            if original.id in claimed:
                raise BusinessRuleError(
                    "The original dispatch movement belongs to more than one line."
                )
            claimed.add(original.id)
            anchored[recorded_line.id] = movement_costs[original.id]
            anchored_prior[recorded_line.id] = Decimal(recorded_line.returned_quantity)
        else:
            prior[(recorded_line.item_id, recorded_line.batch_id)] += Decimal(
                recorded_line.returned_quantity
            )
    groups: dict[CostGroup, list[LayerCost]] = defaultdict(list)
    for movement in movements:
        if movement.id in movement_costs and movement.id not in claimed:
            groups[(movement.item_id, movement.batch_id)].extend(movement_costs[movement.id])

    ordinary_costs: dict[uuid.UUID, tuple[LayerCost, ...]] = {}
    for line in sorted(lines, key=lambda entry: (entry.created_at, entry.id)):
        if line.serial_ids:
            continue
        source = by_source.get(line.dispatch_line_id)
        if source is None or source.item_id != line.item_id:
            raise BusinessRuleError("The original dispatch cost source could not be verified.")
        if source.id in anchored:
            ordinary_costs[line.id] = returned_layer_slice(
                anchored[source.id],
                already_returned=anchored_prior[source.id],
                quantity=Decimal(line.quantity),
            )
            anchored_prior[source.id] += Decimal(line.quantity)
            continue
        key = (source.item_id, source.batch_id)
        ordinary_costs[line.id] = returned_layer_slice(
            groups[key], already_returned=prior[key], quantity=Decimal(line.quantity)
        )
        prior[key] += Decimal(line.quantity)
    return ordinary_costs, serial_costs
