"""Freeze provable returned FIFO pieces when a RETURN credit is issued.

The caller holds the sales-return header lock. All reads are batched before
planning; incomplete legacy evidence leaves cost pending without refusing an
otherwise valid financial credit. Allocations are never recalculated later.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import TenantContext
from src.core.types import ZERO
from src.modules.finance.credit_cost_repository import CreditCostRepository
from src.modules.finance.models import (
    CreditNote,
    CreditNoteCostAllocation,
    CreditNoteLine,
    CreditNoteReason,
    SalesInvoiceLine,
)
from src.modules.inventory.models import InventoryCostLayer, InventoryTransaction
from src.modules.inventory.service import LayerCost, to_stock_value
from src.modules.sales_returns.crediting import accepted_return_quantity
from src.modules.sales_returns.models import SalesReturnLine


@dataclass(frozen=True)
class ReceiptCostSource:
    transaction_id: uuid.UUID
    quantity: Decimal
    eligible_quantity: Decimal
    total_cost: Decimal
    layers: tuple[LayerCost, ...]
    divisible: bool = True

    def prefix_cost(self, quantity: Decimal) -> Decimal:
        """Value a cumulative prefix, preserving the receipt's booked total.

        Ledger money can be rounded more coarsely than FIFO layers. Scale the
        cumulative FIFO value to that frozen ledger amount, then round once at
        stock-value precision. Adjacent pieces telescope exactly, including
        zero-cost layers and cancellation/reissue of a fractional range.
        """
        if quantity == ZERO or self.total_cost == ZERO:
            return ZERO
        if quantity == self.quantity:
            return self.total_cost
        raw_total = sum((layer.quantity * layer.unit_cost for layer in self.layers), ZERO)
        remaining = quantity
        value = ZERO
        for layer in self.layers:
            taken = min(remaining, layer.quantity)
            value += taken * layer.unit_cost
            remaining -= taken
            if remaining == ZERO:
                break
        return to_stock_value(value * self.total_cost / raw_total)


@dataclass(frozen=True)
class CostPiece:
    transaction_id: uuid.UUID
    offset: Decimal
    quantity: Decimal
    cost_amount: Decimal


def free_receipt_ranges(
    eligible_quantity: Decimal, occupied: Sequence[tuple[Decimal, Decimal]]
) -> tuple[tuple[Decimal, Decimal], ...] | None:
    """Return gaps without moving surviving allocations; reject corrupt history."""
    cursor = ZERO
    free: list[tuple[Decimal, Decimal]] = []
    for offset, quantity in sorted(occupied):
        end = offset + quantity
        if offset < cursor or quantity <= ZERO or end > eligible_quantity:
            return None
        if offset > cursor:
            free.append((cursor, offset - cursor))
        cursor = end
    if cursor < eligible_quantity:
        free.append((cursor, eligible_quantity - cursor))
    return tuple(free)


def plan_cost_pieces(
    sources: Sequence[ReceiptCostSource],
    occupied: dict[uuid.UUID, list[tuple[Decimal, Decimal]]],
    quantity: Decimal,
) -> tuple[CostPiece, ...]:
    """All-or-pending per credit line: never reserve guessed or incomplete pieces."""
    if quantity <= ZERO:
        return ()
    remaining = quantity
    pieces: list[CostPiece] = []
    for source in sources:
        gaps = free_receipt_ranges(
            source.eligible_quantity, occupied.get(source.transaction_id, ())
        )
        if gaps is None:
            return ()
        for offset, available in gaps:
            taken = min(remaining, available)
            if not source.divisible and (offset != ZERO or taken != source.quantity):
                return ()
            cost = source.prefix_cost(offset + taken) - source.prefix_cost(offset)
            pieces.append(CostPiece(source.transaction_id, offset, taken, cost))
            remaining -= taken
            if remaining == ZERO:
                return tuple(pieces)
    return ()


def receipt_cost_source(
    movement: InventoryTransaction,
    layers: Sequence[InventoryCostLayer],
    *,
    eligible_quantity: Decimal,
) -> ReceiptCostSource | None:
    quantity, total = Decimal(movement.quantity), Decimal(movement.total_cost)
    if quantity <= ZERO or total < ZERO or not ZERO < eligible_quantity <= quantity:
        return None
    costs = tuple(
        LayerCost(Decimal(layer.original_quantity), Decimal(layer.unit_cost)) for layer in layers
    )
    if any(layer.quantity <= ZERO or layer.unit_cost < ZERO for layer in costs):
        return None
    divisible = True
    if sum((layer.quantity for layer in costs), ZERO) != quantity:
        if costs:
            return None
        # A named unit has one price. A whole ordinary receipt can also use
        # its frozen ledger total, but a partial legacy receipt cannot use a
        # blended ledger unit price in place of missing original FIFO layers.
        divisible = movement.serial_id is not None or total == ZERO
        costs = (LayerCost(quantity, total / quantity),)
    raw_total = sum((layer.quantity * layer.unit_cost for layer in costs), ZERO)
    if raw_total == ZERO and total != ZERO:
        return None
    if (
        eligible_quantity < quantity
        and movement.serial_id is None
        and total != ZERO
        and (not divisible or len({layer.unit_cost for layer in costs}) != 1)
    ):
        # Partial quantity-only QC cannot identify which differently priced
        # physical units passed. Preserve the revenue credit and expose pending
        # cost until a workflow records that missing identity evidence.
        return None
    return ReceiptCostSource(movement.id, quantity, eligible_quantity, total, costs, divisible)


async def snapshot_return_credit_costs(
    session: AsyncSession,
    tenant: TenantContext,
    note: CreditNote,
    note_lines: Sequence[CreditNoteLine],
    invoice_lines: Sequence[SalesInvoiceLine],
    return_lines: Sequence[SalesReturnLine],
) -> list[str]:
    """Write immutable snapshots and return line IDs whose cost remains pending."""
    if note.reason_type != CreditNoteReason.RETURN or note.sales_return_id is None:
        return []
    repo = CreditCostRepository(session, tenant)
    receipts = await repo.receipts(note.sales_return_id)
    layers = await repo.layers([receipt.id for receipt in receipts])
    inspections = await repo.inspections(
        [line.qc_inspection_id for line in return_lines if line.qc_inspection_id is not None]
    )
    allocations = await repo.occupied_ranges(note.sales_return_id, exclude_note_id=note.id)
    blocked = await repo.unallocated_dispatch_lines(note.sales_return_id, exclude_note_id=note.id)
    by_receipt = {movement.id: movement for movement in receipts}
    layers_by_receipt: dict[uuid.UUID, list[InventoryCostLayer]] = defaultdict(list)
    for layer in layers:
        if layer.source_transaction_id is not None:
            layers_by_receipt[layer.source_transaction_id].append(layer)
    occupied: dict[uuid.UUID, list[tuple[Decimal, Decimal]]] = defaultdict(list)
    for allocation in allocations:
        occupied[allocation.receipt_transaction_id].append(
            (Decimal(allocation.source_quantity_offset), Decimal(allocation.quantity))
        )
    inspections_by_id = {inspection.id: inspection for inspection in inspections}
    sources_by_invoice = {line.id: line for line in invoice_lines}
    returns_by_dispatch = {line.dispatch_line_id: line for line in return_lines}
    anchor_counts = Counter(
        line.receipt_transaction_id for line in return_lines if line.receipt_transaction_id
    )
    serial_receipts: dict[uuid.UUID, list[InventoryTransaction]] = defaultdict(list)
    for receipt in receipts:
        if receipt.serial_id is not None:
            serial_receipts[receipt.serial_id].append(receipt)
    pending: list[str] = []
    additions: list[CreditNoteCostAllocation] = []
    for note_line in note_lines:
        source = sources_by_invoice[note_line.sales_invoice_line_id]
        return_line = returns_by_dispatch[source.dispatch_line_id]
        candidates: list[InventoryTransaction] = []
        if source.dispatch_line_id not in blocked:
            if return_line.serial_ids:
                accepted = set(return_line.accepted_serial_ids)
                if all(len(serial_receipts[serial_id]) == 1 for serial_id in accepted):
                    candidates = [
                        receipt
                        for receipt in receipts
                        if receipt.serial_id in accepted and Decimal(receipt.quantity) == 1
                    ]
            elif (
                return_line.receipt_transaction_id is not None
                and anchor_counts[return_line.receipt_transaction_id] == 1
            ):
                anchored_receipt = by_receipt.get(return_line.receipt_transaction_id)
                if (
                    anchored_receipt is not None
                    and anchored_receipt.serial_id is None
                    and Decimal(anchored_receipt.quantity) == Decimal(return_line.quantity)
                ):
                    candidates = [anchored_receipt]
        accepted_quantity = accepted_return_quantity(
            return_line,
            inspections_by_id.get(return_line.qc_inspection_id)
            if return_line.qc_inspection_id is not None
            else None,
        )
        sources: list[ReceiptCostSource] = []
        for candidate in candidates:
            if candidate.item_id != return_line.item_id:
                continue
            cost_source = receipt_cost_source(
                candidate,
                layers_by_receipt[candidate.id],
                eligible_quantity=Decimal("1") if return_line.serial_ids else accepted_quantity,
            )
            if cost_source is not None:
                sources.append(cost_source)
        pieces = plan_cost_pieces(sources, occupied, Decimal(note_line.quantity or ZERO))
        if not pieces:
            pending.append(str(note_line.id))
            continue
        for piece in pieces:
            additions.append(
                CreditNoteCostAllocation(
                    company_id=tenant.company_id,
                    created_by_id=tenant.user_id,
                    credit_note_line_id=note_line.id,
                    sales_return_line_id=return_line.id,
                    receipt_transaction_id=piece.transaction_id,
                    source_quantity_offset=piece.offset,
                    quantity=piece.quantity,
                    cost_amount=piece.cost_amount,
                )
            )
            occupied[piece.transaction_id].append((piece.offset, piece.quantity))
    session.add_all(additions)
    return pending


__all__ = [
    "CostPiece",
    "ReceiptCostSource",
    "free_receipt_ranges",
    "plan_cost_pieces",
    "receipt_cost_source",
    "snapshot_return_credit_costs",
]
