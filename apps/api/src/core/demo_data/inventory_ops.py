"""Stores demonstration data: putaway, scrap and the consumables store.

The documents work package E8a adds, each in a state that shows what it is for:

* two **consumables** in the Consumables Store (``WH-CNS``) - a buffing compound
  and a soluble cutting oil - each with its default bin, opened into that bin;
* a **bin move** putting brass rod away from the raw-material store's unbinned
  receiving position into rack bin ``A-01``, so the Bin column on the stock
  balances screen has something to say;
* **scrap**: brass offcuts set aside as scrap, then most of them sold to a scrap
  dealer by a **scrap disposal**, so the scrap register shows both directions
  and the balances screen still shows what is left;
* **internal issues** to the shop floor and to maintenance - one posted, one
  over the approval value and waiting for a second person, one still a draft.
* **transfers**, adjustments and physical counts with both actionable drafts
  and completed history, all using the stocked buffing-compound position.

Idempotence
-----------
None of the documents has a code this file chooses - each is numbered from its
own series - so each carries its demonstration tag as the first token of
``notes`` and a re-run finds it by that tag (the convention
:mod:`src.core.demo_data.operations` set). Stock that is not a document - the
opening balances and the scrapping - is found by the tag in its ledger
``reference_code``. A document found short of the state it should be in is
driven on from wherever it is.

Everything degrades: a step that cannot run - no brass on the shelf to put away,
say - is recorded in ``ctx.skipped`` with the reason and the rest still runs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from src.core.demo_data import DemoContext, find_by_code
from src.core.errors import AppError
from src.core.pagination import PageParams
from src.modules.inventory import service as inventory_service
from src.modules.inventory.models import (
    AdjustmentReason,
    AdjustmentStatus,
    BinMove,
    InternalIssue,
    InternalIssueStatus,
    InventoryBalance,
    InventoryTransaction,
    PhysicalCount,
    PhysicalCountStatus,
    ScrapDisposal,
    ScrapDisposalKind,
    StockAdjustment,
    StockState,
    StockTransfer,
    TransactionType,
    TransferStatus,
)
from src.modules.inventory.schemas import (
    BinMoveCreate,
    BinMoveLineCreate,
    InternalIssueCreate,
    InternalIssueLineCreate,
    PhysicalCountCreate,
    PhysicalCountEntry,
    PhysicalCountEntryRequest,
    ScrapDisposalCreate,
    ScrapDisposalLineCreate,
    StockAdjustmentCreate,
    StockAdjustmentLineCreate,
    StockTransferCreate,
    StockTransferLineCreate,
)
from src.modules.products.models import Item, ItemGroup
from src.modules.products.schemas import ItemCreate
from src.modules.products.service import create_item
from src.modules.warehouses.models import StorageLocation, StorageLocationType
from src.modules.warehouses.schemas import StorageLocationCreate
from src.modules.warehouses.service import StorageLocationService

STAGE = "inventory_ops"

CONSUMABLES_STORE = "WH-CNS"
RAW_MATERIAL_STORE = "WH-RM"


@dataclass(frozen=True)
class _Consumable:
    code: tuple[str, ...]
    name: str
    description: str
    base_uom: str
    standard_cost: str
    opening_quantity: str


CONSUMABLES: tuple[_Consumable, ...] = (
    _Consumable(
        code=("CN", "BUFF-COMPOUND"),
        name="Buffing Compound - Brown Tripoli Bar",
        description="1 kg bar of brown tripoli compound for the cut-buff stage of brass hardware.",
        base_uom="NOS",
        standard_cost="320.00",
        opening_quantity="120",
    ),
    _Consumable(
        code=("CN", "CUTTING-OIL"),
        name="Soluble Cutting Oil - 20 L Can",
        description="Water-soluble cutting oil for the CNC and VMC machines, 20 litre can.",
        base_uom="NOS",
        standard_cost="2850.00",
        opening_quantity="18",
    ),
)

#: The consumables rack both items default to.
CONSUMABLES_BIN = ("LOC", "CNS", "01")


def _tagged(tag: str, sentence: str) -> str:
    """The note a document carries: its demonstration tag, then plain English."""
    return f"{tag} - {sentence}"


async def _find_tagged(ctx: DemoContext, model: type[Any], tag: str) -> Any | None:
    """Has this document already been raised? First by ``(created_at, id)``."""
    stmt = (
        select(model)
        .where(model.company_id == ctx.company_id, model.notes.like(f"{tag} -%"))
        .order_by(model.created_at, model.id)
    )
    return (await ctx.session.execute(stmt)).scalars().first()


async def _ledger_has(ctx: DemoContext, reference_code: str) -> bool:
    stmt = (
        select(InventoryTransaction.id)
        .where(
            InventoryTransaction.company_id == ctx.company_id,
            InventoryTransaction.reference_code == reference_code,
        )
        .limit(1)
    )
    return (await ctx.session.execute(stmt)).first() is not None


def _skip(ctx: DemoContext, reason: str) -> None:
    ctx.skipped.append((STAGE, reason))


async def seed(ctx: DemoContext) -> None:
    store_id = ctx.maybe_id("warehouse", CONSUMABLES_STORE)
    if store_id is None:
        _skip(ctx, f"no {CONSUMABLES_STORE} warehouse - run the seed first")
        return
    bin_id = await _consumables_bin(ctx, store_id)
    for consumable in CONSUMABLES:
        await _consumable(ctx, consumable, store_id, bin_id)
    await _putaway(ctx)
    await _scrap_and_dispose(ctx)
    await _internal_issues(ctx, store_id, bin_id)
    await _movement_registers(ctx, store_id, bin_id)


async def _movement_registers(ctx: DemoContext, store_id: uuid.UUID, bin_id: uuid.UUID) -> None:
    """Populate the empty document registers without inventing a second stock pool."""
    item_id = ctx.maybe_id("item", ctx.code("CN", "BUFF-COMPOUND"))
    if item_id is None:
        _skip(ctx, "no buffing compound for the transfer, adjustment and count registers")
        return
    await _transfers(ctx, item_id, store_id, bin_id)
    await _adjustments(ctx, item_id, store_id, bin_id)
    await _physical_counts(ctx, item_id, store_id, bin_id)


async def _transfers(
    ctx: DemoContext, item_id: uuid.UUID, store_id: uuid.UUID, bin_id: uuid.UUID
) -> None:
    destination_id = ctx.maybe_id("warehouse", "WH-WIP")
    if destination_id is None:
        _skip(ctx, "no work-in-progress warehouse for the transfer register")
        return
    service = inventory_service.StockTransferService(ctx.session, ctx.tenant)
    for suffix, target, quantity in (
        ("RECEIVED", TransferStatus.RECEIVED, Decimal("6")),
        ("TRANSIT", TransferStatus.IN_TRANSIT, Decimal("4")),
        ("DRAFT", TransferStatus.DRAFT, Decimal("3")),
    ):
        tag = ctx.code("E8", "TRANSFER", suffix)
        existing = await _find_tagged(ctx, StockTransfer, tag)
        try:
            if existing is None:
                created = await service.create(
                    StockTransferCreate(
                        from_warehouse_id=store_id,
                        to_warehouse_id=destination_id,
                        reason="Replenish the polishing cell's buffing compound",
                        notes=_tagged(tag, f"Polishing-cell replenishment - {suffix.lower()}"),
                        lines=[
                            StockTransferLineCreate(
                                item_id=item_id, quantity=quantity, from_location_id=bin_id
                            )
                        ],
                    )
                )
                transfer_id, status = created.id, created.status
                ctx.note(STAGE, created=1)
            else:
                transfer_id, status = existing.id, TransferStatus(existing.status)
                ctx.note(STAGE, existing=1)
            ctx.remember("stock_transfer", tag, transfer_id)
            if target is TransferStatus.DRAFT:
                continue
            if status is TransferStatus.DRAFT:
                status = (await service.approve(transfer_id)).status
            if status is TransferStatus.APPROVED:
                status = (await service.dispatch(transfer_id)).status
            if target is TransferStatus.RECEIVED and status is TransferStatus.IN_TRANSIT:
                await service.receive(transfer_id)
        except AppError as exc:
            _skip(ctx, f"transfer {tag} could not reach {target}: {exc.message}")


async def _adjustments(
    ctx: DemoContext, item_id: uuid.UUID, store_id: uuid.UUID, bin_id: uuid.UUID
) -> None:
    service = inventory_service.StockAdjustmentService(ctx.session, ctx.tenant)
    for suffix, target, quantity in (
        ("POSTED", AdjustmentStatus.POSTED, Decimal("2")),
        ("DRAFT", AdjustmentStatus.DRAFT, Decimal("-1")),
    ):
        tag = ctx.code("E8", "ADJUSTMENT", suffix)
        existing = await _find_tagged(ctx, StockAdjustment, tag)
        try:
            if existing is None:
                created = await service.create(
                    StockAdjustmentCreate(
                        warehouse_id=store_id,
                        reason_code=AdjustmentReason.CORRECTION,
                        reason="Correct the buffing-compound shelf quantity",
                        notes=_tagged(tag, f"Shelf reconciliation - {suffix.lower()}"),
                        lines=[
                            StockAdjustmentLineCreate(
                                item_id=item_id, location_id=bin_id, quantity=quantity
                            )
                        ],
                    )
                )
                adjustment_id, status = created.id, created.status
                ctx.note(STAGE, created=1)
            else:
                adjustment_id, status = existing.id, AdjustmentStatus(existing.status)
                ctx.note(STAGE, existing=1)
            ctx.remember("stock_adjustment", tag, adjustment_id)
            if target is AdjustmentStatus.POSTED and status in (
                AdjustmentStatus.DRAFT,
                AdjustmentStatus.APPROVED,
            ):
                await service.post(adjustment_id)
        except AppError as exc:
            _skip(ctx, f"adjustment {tag} could not reach {target}: {exc.message}")


async def _physical_counts(
    ctx: DemoContext, item_id: uuid.UUID, store_id: uuid.UUID, bin_id: uuid.UUID
) -> None:
    service = inventory_service.PhysicalCountService(ctx.session, ctx.tenant)
    # Only one count may be open in a warehouse: complete the history example
    # before creating the draft an operator can continue from the screen.
    for suffix, target in (
        ("CONFIRMED", PhysicalCountStatus.CONFIRMED),
        ("DRAFT", PhysicalCountStatus.DRAFT),
    ):
        tag = ctx.code("E8", "COUNT", suffix)
        existing = await _find_tagged(ctx, PhysicalCount, tag)
        try:
            if existing is None:
                created = await service.create(
                    PhysicalCountCreate(
                        warehouse_id=store_id,
                        location_id=bin_id,
                        item_ids=[item_id],
                        reason="Cycle count of the polishing consumables rack",
                        notes=_tagged(tag, f"Buffing-compound cycle count - {suffix.lower()}"),
                    )
                )
                count_id, status = created.id, created.status
                ctx.note(STAGE, created=1)
            else:
                count_id, status = existing.id, PhysicalCountStatus(existing.status)
                ctx.note(STAGE, existing=1)
            ctx.remember("physical_count", tag, count_id)
            if target is PhysicalCountStatus.DRAFT:
                continue
            if status is PhysicalCountStatus.DRAFT:
                status = (await service.start(count_id)).status
            if status is PhysicalCountStatus.IN_PROGRESS:
                lines, _ = await service.list_lines(count_id, PageParams(limit=1))
                if lines and lines[0].counted_quantity is None:
                    line = lines[0]
                    counted = max(Decimal("0"), line.system_quantity - Decimal("1"))
                    status = (
                        await service.enter_counts(
                            count_id,
                            PhysicalCountEntryRequest(
                                entries=[
                                    PhysicalCountEntry(
                                        line_id=line.id,
                                        counted_quantity=counted,
                                        notes="One bar missing from the counted shelf position",
                                    )
                                ]
                            ),
                        )
                    ).status
            if status is PhysicalCountStatus.COUNTED:
                await service.confirm(count_id)
        except AppError as exc:
            _skip(ctx, f"physical count {tag} could not reach {target}: {exc.message}")


# -----------------------------------------------------------------------------
# The consumables store
# -----------------------------------------------------------------------------


async def _consumables_bin(ctx: DemoContext, store_id: uuid.UUID) -> uuid.UUID:
    code = ctx.code(*CONSUMABLES_BIN)
    existing = await find_by_code(ctx.session, StorageLocation, ctx.company_id, code)
    if existing is not None:
        ctx.note(STAGE, existing=1)
        return ctx.remember("location", code, existing.id)
    created = await StorageLocationService(ctx.session, ctx.tenant).create_location(
        store_id,
        StorageLocationCreate(
            code=code,
            name="C-01 Shop-floor Consumables",
            location_type=StorageLocationType.BIN,
            barcode=code,
        ),
    )
    ctx.note(STAGE, created=1)
    return ctx.remember("location", code, created.id)


async def _consumable(
    ctx: DemoContext, spec: _Consumable, store_id: uuid.UUID, bin_id: uuid.UUID
) -> None:
    """The item, with the consumables rack as its default bin, opened into that bin."""
    code = ctx.code(*spec.code)
    item = await find_by_code(ctx.session, Item, ctx.company_id, code)
    if item is None:
        created = await create_item(
            ctx.session,
            ctx.tenant,
            ItemCreate(
                code=code,
                name=spec.name,
                description=spec.description,
                item_group=ItemGroup.CONSUMABLE,
                category_id=ctx.maybe_id("category", "CONSUMABLE"),
                base_uom_id=ctx.id_of("uom", spec.base_uom),
                standard_cost=Decimal(spec.standard_cost),
                default_warehouse_id=store_id,
                default_location_id=bin_id,
                can_purchase=True,
            ),
        )
        item_id = created.id
        ctx.note(STAGE, created=1)
    else:
        item_id = item.id
        ctx.note(STAGE, existing=1)
    ctx.remember("item", code, item_id)

    # OPENING_BALANCE is the honest type for stock that predates the
    # demonstration - the precedent the serialised locks in the operations
    # stage set. Into the default bin, at the item's standard cost.
    tag = ctx.code("E8", "OPENING", *spec.code)
    if await _ledger_has(ctx, tag):
        ctx.note(STAGE, existing=1)
        return
    await inventory_service.post_movement(
        ctx.session,
        ctx.tenant,
        item_id=item_id,
        warehouse_id=store_id,
        location_id=bin_id,
        quantity=Decimal(spec.opening_quantity),
        transaction_type=TransactionType.OPENING_BALANCE,
        reference_type="demo_opening_stock",
        reference_id=None,
        reference_code=tag,
        reason="Opening stock of the consumables store for the demonstration",
        unit_cost=Decimal(spec.standard_cost),
    )
    ctx.note(STAGE, created=1)


# -----------------------------------------------------------------------------
# Putaway
# -----------------------------------------------------------------------------


async def _unbinned_available(
    ctx: DemoContext, item_id: uuid.UUID, warehouse_id: uuid.UUID
) -> Decimal:
    stmt = select(InventoryBalance.available_quantity).where(
        InventoryBalance.company_id == ctx.company_id,
        InventoryBalance.item_id == item_id,
        InventoryBalance.warehouse_id == warehouse_id,
        InventoryBalance.location_id.is_(None),
        InventoryBalance.batch_id.is_(None),
    )
    found = (await ctx.session.execute(stmt)).scalar_one_or_none()
    return Decimal(found) if found is not None else Decimal("0")


async def _putaway(ctx: DemoContext) -> None:
    tag = ctx.code("E8", "PUTAWAY")
    if await _find_tagged(ctx, BinMove, tag) is not None:
        ctx.note(STAGE, existing=1)
        return
    store_id = ctx.maybe_id("warehouse", RAW_MATERIAL_STORE)
    brass_id = ctx.maybe_id("item", ctx.code("RM", "BRASS"))
    bin_id = ctx.maybe_id("location", ctx.code("LOC", "RM", "A-01"))
    if store_id is None or brass_id is None or bin_id is None:
        _skip(ctx, "no brass rod, raw-material store or rack bin A-01 to put away into")
        return
    quantity = Decimal("40")
    if await _unbinned_available(ctx, brass_id, store_id) < quantity:
        _skip(ctx, "less than 40 kg of brass rod free at the raw-material receiving position")
        return
    await inventory_service.BinMoveService(ctx.session, ctx.tenant).create(
        BinMoveCreate(
            warehouse_id=store_id,
            reason="Put away brass rod received at the dock into rack A-01",
            notes=_tagged(tag, "Putaway of the last brass rod receipt"),
            lines=[BinMoveLineCreate(item_id=brass_id, quantity=quantity, to_location_id=bin_id)],
        )
    )
    ctx.note(STAGE, created=1)


# -----------------------------------------------------------------------------
# Scrap and its disposal
# -----------------------------------------------------------------------------


async def _scrap_and_dispose(ctx: DemoContext) -> None:
    store_id = ctx.maybe_id("warehouse", RAW_MATERIAL_STORE)
    brass_id = ctx.maybe_id("item", ctx.code("RM", "BRASS"))
    if store_id is None or brass_id is None:
        _skip(ctx, "no brass rod in the raw-material store to scrap")
        return

    scrap_tag = ctx.code("E8", "SCRAP", "BRASS")
    if await _ledger_has(ctx, scrap_tag):
        ctx.note(STAGE, existing=1)
    elif await _unbinned_available(ctx, brass_id, store_id) < Decimal("6"):
        _skip(ctx, "less than 6 kg of brass rod free to set aside as offcut scrap")
        return
    else:
        await inventory_service.change_stock_state(
            ctx.session,
            ctx.tenant,
            item_id=brass_id,
            warehouse_id=store_id,
            quantity=Decimal("6"),
            from_state=StockState.AVAILABLE,
            to_state=StockState.SCRAP,
            reason="Brass rod offcuts below the minimum bar length",
            reference_type="demo",
            reference_code=scrap_tag,
        )
        ctx.note(STAGE, created=1)

    disposal_tag = ctx.code("E8", "SCRAP-SALE")
    if await _find_tagged(ctx, ScrapDisposal, disposal_tag) is not None:
        ctx.note(STAGE, existing=1)
        return
    try:
        await inventory_service.ScrapDisposalService(ctx.session, ctx.tenant).create(
            ScrapDisposalCreate(
                warehouse_id=store_id,
                disposal_kind=ScrapDisposalKind.SALE,
                reason="Monthly sale of brass offcuts to the scrap dealer",
                buyer_reference="Shree Ambica Metal Scrap Traders - weighbridge slip 4471",
                realised_amount=Decimal("1560.00"),
                notes=_tagged(disposal_tag, "4 kg of brass offcuts sold at 390 per kg"),
                lines=[ScrapDisposalLineCreate(item_id=brass_id, quantity=Decimal("4"))],
            )
        )
        ctx.note(STAGE, created=1)
    except AppError as exc:
        _skip(ctx, f"brass scrap could not be disposed of: {exc.message}")


# -----------------------------------------------------------------------------
# Internal issues
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Issue:
    tag: tuple[str, ...]
    item: tuple[str, ...]
    quantity: str
    department: tuple[str, ...]
    cost_centre: tuple[str, ...]
    reason: str
    note: str
    #: Where the document should end up.
    status: InternalIssueStatus


ISSUES: tuple[_Issue, ...] = (
    _Issue(
        tag=("E8", "ISSUE", "BUFF"),
        item=("CN", "BUFF-COMPOUND"),
        quantity="12",
        department=("DEPT", "PROD"),
        cost_centre=("PROD", "BUFF"),
        reason="Weekly buffing compound for the polishing line",
        note="Polishing line consumption for the week",
        status=InternalIssueStatus.POSTED,
    ),
    _Issue(
        # 10 cans at 2 850 is 28 500 - over the approval value, so it waits for
        # a second person, which is what the Approve button is for.
        tag=("E8", "ISSUE", "OIL"),
        item=("CN", "CUTTING-OIL"),
        quantity="10",
        department=("DEPT", "PROD"),
        cost_centre=("PROD", "MACH"),
        reason="Coolant change on the three VMC machines",
        note="Quarterly coolant change - needs approval",
        status=InternalIssueStatus.PENDING_APPROVAL,
    ),
    _Issue(
        tag=("E8", "ISSUE", "MAINT"),
        item=("CN", "CUTTING-OIL"),
        quantity="2",
        department=("DEPT", "MAINT"),
        cost_centre=("MAINT",),
        reason="Top-up for the maintenance workshop lathe",
        note="Raised by maintenance, not yet posted",
        status=InternalIssueStatus.DRAFT,
    ),
)


async def _internal_issues(ctx: DemoContext, store_id: uuid.UUID, bin_id: uuid.UUID) -> None:
    service = inventory_service.InternalIssueService(ctx.session, ctx.tenant)
    for spec in ISSUES:
        tag = ctx.code(*spec.tag)
        item_id = ctx.maybe_id("item", ctx.code(*spec.item))
        if item_id is None:
            _skip(ctx, f"no consumable {ctx.code(*spec.item)} for internal issue {tag}")
            continue
        existing = await _find_tagged(ctx, InternalIssue, tag)
        if existing is None:
            department_id = ctx.maybe_id("department", ctx.code(*spec.department))
            cost_centre_id = ctx.maybe_id("cost_centre", ctx.code("CC", *spec.cost_centre))
            if department_id is None and cost_centre_id is None:
                _skip(ctx, f"no department or cost centre to issue {tag} to")
                continue
            created = await service.create(
                InternalIssueCreate(
                    warehouse_id=store_id,
                    reason=spec.reason,
                    department_id=department_id,
                    cost_centre_id=cost_centre_id,
                    notes=_tagged(tag, spec.note),
                    lines=[
                        InternalIssueLineCreate(
                            item_id=item_id, quantity=Decimal(spec.quantity), location_id=bin_id
                        )
                    ],
                )
            )
            issue_id, status = created.id, created.status
            ctx.note(STAGE, created=1)
        else:
            issue_id, status = existing.id, InternalIssueStatus(existing.status)
            ctx.note(STAGE, existing=1)

        # Drive a document left short of its state on from wherever it is.
        if status is not InternalIssueStatus.DRAFT or spec.status is InternalIssueStatus.DRAFT:
            continue
        try:
            if spec.status is InternalIssueStatus.POSTED:
                await service.post(issue_id)
            elif spec.status is InternalIssueStatus.PENDING_APPROVAL:
                await service.submit(issue_id)
        except AppError as exc:
            _skip(ctx, f"internal issue {tag} stayed a draft: {exc.message}")
