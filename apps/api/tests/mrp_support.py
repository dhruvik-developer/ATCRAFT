"""The planning engine's test double, and the rows it plans over.

``FakeSource`` answers the engine's ``MrpDataSource`` protocol from dicts and
counts every call, so a test can assert the merge arithmetic exactly and the
shape of the loading at all - one batched read per level is an assertion, not a
comment. ``make_item`` and ``make_product`` build planning items, and
``sales_line`` one dated sales demand; ``AS_OF``, ``HORIZON_END`` and
``REQUIRED`` are the dates every case plans against.

They live here rather than in ``test_mrp_engine_merge.py`` because the date
settling, demand provenance, level ordering and time phasing suites drive the
same engine through the same double. Imported from a test module, they made one
test file a library for four others.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from src.modules.bom.models import BomType
from src.modules.mrp.engine import (
    BomComponent,
    BomStructure,
    DemandLine,
    PlanningItem,
    StockPosition,
    StockPositions,
    SupplyArrival,
    SupplyTerms,
)
from src.modules.mrp.models import MrpDemandSource
from src.modules.products.models import ProductionType

D = Decimal
AS_OF = date(2026, 6, 1)
HORIZON_END = date(2026, 9, 1)
REQUIRED = date(2026, 7, 15)


def make_item(
    code: str,
    *,
    production_type: str = ProductionType.BUY,
    lead_time_days: int = 0,
    min_order_quantity: str = "0",
    safety_stock: str = "0",
    can_manufacture: bool = False,
    can_purchase: bool = True,
    is_batch_tracked: bool = False,
    is_serial_tracked: bool = False,
) -> PlanningItem:
    return PlanningItem(
        id=uuid.uuid4(),
        code=code,
        name=f"{code} description",
        base_uom_id=uuid.uuid4(),
        production_type=production_type,
        lead_time_days=lead_time_days,
        min_order_quantity=D(min_order_quantity),
        safety_stock=D(safety_stock),
        can_purchase=can_purchase,
        can_manufacture=can_manufacture,
        can_job_work=False,
        is_active=True,
        is_batch_tracked=is_batch_tracked,
        is_serial_tracked=is_serial_tracked,
    )


def make_product(code: str, **kwargs: Any) -> PlanningItem:
    kwargs.setdefault("production_type", ProductionType.MAKE)
    kwargs.setdefault("can_manufacture", True)
    kwargs.setdefault("can_purchase", False)
    return make_item(code, **kwargs)


@dataclass
class FakeSource:
    """In-memory :class:`~src.modules.mrp.engine.MrpDataSource`.

    ``structure`` maps a parent item to its *single-level* component lines -
    ``(component, quantity_per)`` per one unit of the parent - exactly as the
    BOM module's lines store them. The engine walks the levels itself now, so a
    fixture that wants a three-level structure says so with three entries, one
    per edge; a consolidated multi-level line is not a shape this data source
    can return, and a test built on one would be asserting the old engine's
    flattening rather than the structure.

    ``calls`` records round-trips and ``batch_sizes`` how many ids each call
    was given, which is what turns "no per-item query" into an assertion rather
    than an aspiration.
    """

    items: dict[uuid.UUID, PlanningItem]
    stock: dict[uuid.UUID, StockPosition] = field(default_factory=dict)
    warehouse_stock: dict[tuple[uuid.UUID, uuid.UUID], StockPosition] = field(default_factory=dict)
    structure: dict[uuid.UUID, list[tuple[PlanningItem, str]]] = field(default_factory=dict)
    default_bom: set[uuid.UUID] = field(default_factory=set)
    phantom: set[uuid.UUID] = field(default_factory=set)
    routing_leads: dict[uuid.UUID, int] = field(default_factory=dict)
    terms: dict[uuid.UUID, SupplyTerms] = field(default_factory=dict)
    incoming: dict[uuid.UUID, tuple[SupplyArrival, ...]] = field(default_factory=dict)
    in_production: dict[uuid.UUID, tuple[SupplyArrival, ...]] = field(default_factory=dict)
    assembly: dict[uuid.UUID, tuple[SupplyArrival, ...]] = field(default_factory=dict)
    job_work: dict[uuid.UUID, tuple[SupplyArrival, ...]] = field(default_factory=dict)
    incoming_available: bool = True
    production_available: bool = True
    assembly_available: bool = True
    job_work_available: bool = True
    calls: Counter[str] = field(default_factory=Counter)
    batch_sizes: dict[str, list[int]] = field(default_factory=dict)
    lead_time_requests: list[dict[uuid.UUID, Decimal]] = field(default_factory=list)

    def _record(self, name: str, size: int) -> None:
        self.calls[name] += 1
        self.batch_sizes.setdefault(name, []).append(size)

    async def planning_items(self, item_ids: Any) -> dict[uuid.UUID, PlanningItem]:
        ids = list(item_ids)
        self._record("planning_items", len(ids))
        return {i: self.items[i] for i in ids if i in self.items}

    async def stock_positions(self, item_ids: Any) -> StockPositions:
        ids = list(item_ids)
        self._record("stock_positions", len(ids))
        return StockPositions(
            by_item={i: self.stock[i] for i in ids if i in self.stock},
            by_warehouse={
                key: value for key, value in self.warehouse_stock.items() if key[0] in ids
            },
        )

    async def bom_structures(self, item_ids: Any, as_of: date) -> dict[uuid.UUID, BomStructure]:
        ids = list(item_ids)
        self._record("bom_structures", len(ids))
        return {
            i: BomStructure(
                output_quantity=D("1"),
                is_default=i in self.default_bom,
                is_phantom=i in self.phantom,
                lines=tuple(
                    BomComponent(
                        item_id=component.id,
                        quantity_per=D(per_unit),
                        scrap_percentage=D("0"),
                    )
                    for component, per_unit in self.structure[i]
                ),
                bom_type=BomType.PRODUCTION,
            )
            for i in ids
            if i in self.structure
        }

    async def manufacture_lead_times(self, quantities: Any, as_of: date) -> dict[uuid.UUID, int]:
        self._record("manufacture_lead_times", len(quantities))
        self.lead_time_requests.append(dict(quantities))
        return {i: self.routing_leads[i] for i in quantities if i in self.routing_leads}

    async def supply_terms(self, item_ids: Any) -> dict[uuid.UUID, SupplyTerms]:
        ids = list(item_ids)
        self._record("supply_terms", len(ids))
        return {i: self.terms[i] for i in ids if i in self.terms}

    async def incoming_supply(
        self, item_ids: Any, upto: date
    ) -> tuple[dict[uuid.UUID, tuple[SupplyArrival, ...]], bool]:
        ids = list(item_ids)
        self._record("incoming_supply", len(ids))
        if not self.incoming_available:
            return {}, False
        return {i: self.incoming[i] for i in ids if i in self.incoming}, True

    async def in_production_supply(
        self, item_ids: Any, upto: date
    ) -> tuple[dict[uuid.UUID, tuple[SupplyArrival, ...]], bool]:
        ids = list(item_ids)
        self._record("in_production_supply", len(ids))
        if not self.production_available:
            return {}, False
        return {i: self.in_production[i] for i in ids if i in self.in_production}, True

    async def job_work_supply(
        self, item_ids: Any, upto: date
    ) -> tuple[dict[uuid.UUID, tuple[SupplyArrival, ...]], bool]:
        ids = list(item_ids)
        self._record("job_work_supply", len(ids))
        if not self.job_work_available:
            return {}, False
        return {i: self.job_work[i] for i in ids if i in self.job_work}, True

    async def assembly_supply(
        self, item_ids: Any, upto: date
    ) -> tuple[dict[uuid.UUID, tuple[SupplyArrival, ...]], bool]:
        ids = list(item_ids)
        self._record("assembly_supply", len(ids))
        if not self.assembly_available:
            return {}, False
        return {i: self.assembly[i] for i in ids if i in self.assembly}, True


def sales_line(
    item: PlanningItem,
    quantity: str,
    *,
    code: str,
    required: date = REQUIRED,
    warehouse_id: uuid.UUID | None = None,
    priority: int = 5,
) -> DemandLine:
    return DemandLine(
        item_id=item.id,
        quantity=D(quantity),
        required_date=required,
        source=MrpDemandSource.SALES_ORDER,
        source_id=uuid.uuid4(),
        source_line_id=uuid.uuid4(),
        source_code=code,
        warehouse_id=warehouse_id,
        priority=priority,
    )


__all__ = [
    "AS_OF",
    "HORIZON_END",
    "REQUIRED",
    "D",
    "FakeSource",
    "make_item",
    "make_product",
    "sales_line",
]
