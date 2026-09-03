"""The common-part merge, and the sold-and-consumed case.

Everything here runs against an in-memory :class:`FakeSource` rather than a
database. That is not a shortcut - it is what makes the two properties the engine
exists for actually testable:

* the merge arithmetic can be asserted exactly, and
* the *shape of the loading* can be asserted at all. ``FakeSource`` counts its
  calls, so "a fixed handful of statements plus one explosion per short product"
  is a failing test rather than a comment somebody has to keep honouring.

The worked structure is the one from the master rules:

    Premium Lock Kit
      Rose Handle  -> Handle Body, Bush, Screw (2)
      Lock Body    -> Lock Case, Spring, Pin, Screw (4)
      Cylinder
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from src.modules.mrp import engine
from src.modules.mrp.engine import (
    DemandLine,
    ExplodedLine,
    PlanningItem,
    StockPosition,
    SupplyArrival,
    SupplyTerms,
)
from src.modules.mrp.models import MAX_DEMAND_LINES, MrpDemandSource, MrpSuggestionType
from src.modules.products.models import ProductionType

D = Decimal
AS_OF = date(2026, 6, 1)
HORIZON_END = date(2026, 9, 1)
REQUIRED = date(2026, 7, 15)
#: Inbound supply arriving comfortably before :data:`REQUIRED`. Supply is dated
#: now - the engine nets a requirement only against tranches that arrive in time
#: - so a fixture that means "this is on order and it will be here" has to say
#: when. The time phasing itself is exercised in ``test_mrp_time_phasing.py``.
IN_TIME = date(2026, 7, 1)


def arriving(quantity: str, *, on: date = IN_TIME) -> tuple[SupplyArrival, ...]:
    """One dated tranche of supply, as a data source would report it."""
    return (SupplyArrival(due_date=on, quantity=D(quantity)),)


# -----------------------------------------------------------------------------
# Test doubles
# -----------------------------------------------------------------------------


def make_item(
    code: str,
    *,
    production_type: str = ProductionType.BUY,
    lead_time_days: int = 0,
    min_order_quantity: str = "0",
    safety_stock: str = "0",
    can_manufacture: bool = False,
    can_purchase: bool = True,
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
    )


def make_product(code: str, **kwargs: Any) -> PlanningItem:
    kwargs.setdefault("production_type", ProductionType.MAKE)
    kwargs.setdefault("can_manufacture", True)
    kwargs.setdefault("can_purchase", False)
    return make_item(code, **kwargs)


@dataclass
class FakeSource:
    """In-memory :class:`~src.modules.mrp.engine.MrpDataSource`.

    ``structure`` maps a parent item to its consolidated components *per unit of
    the parent*, exactly as ``bom.engine.explode`` would return them once
    multiplied out. ``calls`` records round-trips and ``batch_sizes`` how many ids
    each call was given, which is what turns "no per-item query" into an
    assertion rather than an aspiration.
    """

    items: dict[uuid.UUID, PlanningItem]
    stock: dict[uuid.UUID, StockPosition] = field(default_factory=dict)
    structure: dict[uuid.UUID, list[tuple[PlanningItem, str, int]]] = field(default_factory=dict)
    default_bom: set[uuid.UUID] = field(default_factory=set)
    terms: dict[uuid.UUID, SupplyTerms] = field(default_factory=dict)
    incoming: dict[uuid.UUID, tuple[SupplyArrival, ...]] = field(default_factory=dict)
    in_production: dict[uuid.UUID, tuple[SupplyArrival, ...]] = field(default_factory=dict)
    incoming_available: bool = True
    production_available: bool = True
    calls: Counter[str] = field(default_factory=Counter)
    batch_sizes: dict[str, list[int]] = field(default_factory=dict)

    def _record(self, name: str, size: int) -> None:
        self.calls[name] += 1
        self.batch_sizes.setdefault(name, []).append(size)

    async def planning_items(self, item_ids: Any) -> dict[uuid.UUID, PlanningItem]:
        ids = list(item_ids)
        self._record("planning_items", len(ids))
        return {i: self.items[i] for i in ids if i in self.items}

    async def stock_positions(self, item_ids: Any) -> dict[uuid.UUID, StockPosition]:
        ids = list(item_ids)
        self._record("stock_positions", len(ids))
        return {i: self.stock[i] for i in ids if i in self.stock}

    async def bom_availability(self, item_ids: Any, as_of: date) -> dict[uuid.UUID, bool]:
        ids = list(item_ids)
        self._record("bom_availability", len(ids))
        return {i: (i in self.default_bom) for i in ids if i in self.structure}

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

    async def explode(
        self, item_id: uuid.UUID, quantity: Decimal, as_of: date
    ) -> list[ExplodedLine]:
        self._record("explode", 1)
        return [
            ExplodedLine(
                item_id=component.id,
                item_code=component.code,
                quantity=D(per_unit) * quantity,
                level=level,
            )
            for component, per_unit, level in self.structure.get(item_id, [])
        ]


def sales_line(
    item: PlanningItem, quantity: str, *, code: str, required: date = REQUIRED
) -> DemandLine:
    return DemandLine(
        item_id=item.id,
        quantity=D(quantity),
        required_date=required,
        source=MrpDemandSource.SALES_ORDER,
        source_id=uuid.uuid4(),
        source_line_id=uuid.uuid4(),
        source_code=code,
    )


async def run(source: FakeSource, demands: list[DemandLine], **kwargs: Any) -> engine.MrpPlan:
    return await engine.plan(
        source, demands=demands, as_of=AS_OF, horizon_end=HORIZON_END, **kwargs
    )


# -----------------------------------------------------------------------------
# THE behaviour: common parts merge across demands
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_sales_orders_for_one_product_produce_one_screw_requirement() -> None:
    """The single most important MRP behaviour in the master rules.

    Two orders for 60 and 40 kits, six screws per kit. The plan must contain
    exactly ONE screw requirement for 600 - not two rows a buyer has to add up,
    and not two purchase orders.
    """
    kit = make_product("LOCK-KIT")
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={kit.id: kit, screw.id: screw},
        structure={kit.id: [(screw, "6", 1)]},
        default_bom={kit.id},
    )

    plan = await run(
        source,
        [sales_line(kit, "60", code="SO/0001"), sales_line(kit, "40", code="SO/0002")],
    )

    screw_rows = [r for r in plan.requirements if r.item_id == screw.id]
    assert len(screw_rows) == 1, "the merge produced two rows for one part"
    assert screw_rows[0].gross_requirement == D("600.0000")
    assert screw_rows[0].shortage_quantity == D("600.0000")

    screw_suggestions = [s for s in plan.suggestions if s.item_id == screw.id]
    assert len(screw_suggestions) == 1
    assert screw_suggestions[0].suggested_quantity == D("600.0000")


@pytest.mark.asyncio
async def test_the_merge_is_performed_before_the_explosion() -> None:
    """Two orders for the same product explode its structure once, not twice.

    This is the difference between a plan that costs one explosion per *product*
    and one that costs an explosion per *order line* - the N+1 in planning form.
    """
    kit = make_product("LOCK-KIT")
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={kit.id: kit, screw.id: screw},
        structure={kit.id: [(screw, "6", 1)]},
        default_bom={kit.id},
    )

    await run(
        source,
        [sales_line(kit, str(n), code=f"SO/{n:04d}") for n in range(1, 21)],
    )

    assert source.calls["explode"] == 1


@pytest.mark.asyncio
async def test_a_screw_used_by_two_different_products_merges_into_one_row() -> None:
    """The merge crosses structures as well as orders.

    100 kits at 6 screws and 100 handle sets at 3 screws is one requirement for
    900, whichever branch each contribution arrived through.
    """
    kit = make_product("LOCK-KIT")
    handle_set = make_product("HANDLE-SET")
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={kit.id: kit, handle_set.id: handle_set, screw.id: screw},
        structure={kit.id: [(screw, "6", 1)], handle_set.id: [(screw, "3", 1)]},
        default_bom={kit.id, handle_set.id},
    )

    plan = await run(
        source,
        [
            sales_line(kit, "100", code="SO/0001"),
            sales_line(handle_set, "100", code="SO/0002"),
        ],
    )

    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.gross_requirement == D("900.0000")
    assert requirement.dependent_demand_quantity == D("900.0000")
    assert requirement.direct_demand_quantity == D("0.0000")


@pytest.mark.asyncio
async def test_every_contributing_demand_is_retained_on_the_merged_row() -> None:
    """The merge stays auditable: a planner can always ask where 900 came from."""
    kit = make_product("LOCK-KIT")
    handle_set = make_product("HANDLE-SET")
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={kit.id: kit, handle_set.id: handle_set, screw.id: screw},
        structure={kit.id: [(screw, "6", 1)], handle_set.id: [(screw, "3", 1)]},
        default_bom={kit.id, handle_set.id},
    )

    plan = await run(
        source,
        [
            sales_line(kit, "100", code="SO/0001"),
            sales_line(handle_set, "100", code="SO/0002"),
        ],
    )

    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.source_demand_count == 2
    via = sorted(entry["via_item_code"] for entry in requirement.source_demands)
    assert via == ["HANDLE-SET", "LOCK-KIT"]

    kit_requirement = plan.requirement_for(kit.id)
    assert kit_requirement is not None
    assert [entry["code"] for entry in kit_requirement.source_demands] == ["SO/0001"]


@pytest.mark.asyncio
async def test_the_merged_requirement_takes_the_earliest_date_of_its_demands() -> None:
    """A merged part cannot be half early and half late; the plan takes the worst."""
    kit = make_product("LOCK-KIT")
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={kit.id: kit, screw.id: screw},
        structure={kit.id: [(screw, "6", 1)]},
        default_bom={kit.id},
    )

    plan = await run(
        source,
        [
            sales_line(kit, "10", code="SO/LATE", required=date(2026, 8, 30)),
            sales_line(kit, "10", code="SO/EARLY", required=date(2026, 6, 20)),
        ],
    )

    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.earliest_required_date == date(2026, 6, 20)


# -----------------------------------------------------------------------------
# Sold AND used as a component
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_item_sold_and_consumed_nets_its_stock_only_once() -> None:
    """Master rules: one item, many roles - and one stock pool.

    ``ROSE-HANDLE`` is sold as a spare (100) and consumed by the kit (50 kits x 2
    = 100). 60 are in stock. The requirement is 200 and the shortage is 140.

    Netting the 60 against each demand separately gives 40 + 40 = 80 and
    under-orders by 60 - the defect this test exists to catch.
    """
    kit = make_product("LOCK-KIT")
    handle = make_item("ROSE-HANDLE")
    source = FakeSource(
        items={kit.id: kit, handle.id: handle},
        stock={handle.id: StockPosition(on_hand=D("60"))},
        structure={kit.id: [(handle, "2", 1)]},
        default_bom={kit.id},
    )

    plan = await run(
        source,
        [
            sales_line(handle, "100", code="SO/SPARE"),
            sales_line(kit, "50", code="SO/KIT"),
        ],
    )

    requirement = plan.requirement_for(handle.id)
    assert requirement is not None
    assert requirement.direct_demand_quantity == D("100.0000")
    assert requirement.dependent_demand_quantity == D("100.0000")
    assert requirement.gross_requirement == D("200.0000")
    assert requirement.available_quantity == D("60.0000")
    assert requirement.shortage_quantity == D("140.0000")


@pytest.mark.asyncio
async def test_a_directly_sold_component_is_reported_at_level_zero() -> None:
    """Direct demand wins the level, however deep the part also sits elsewhere.

    A planner recognises a part they sell as a product, not as somebody else's
    level-3 component.
    """
    kit = make_product("LOCK-KIT")
    handle = make_item("ROSE-HANDLE")
    source = FakeSource(
        items={kit.id: kit, handle.id: handle},
        structure={kit.id: [(handle, "2", 3)]},
        default_bom={kit.id},
    )

    plan = await run(
        source,
        [sales_line(handle, "10", code="SO/SPARE"), sales_line(kit, "10", code="SO/KIT")],
    )

    requirement = plan.requirement_for(handle.id)
    assert requirement is not None
    assert requirement.level == 0


@pytest.mark.asyncio
async def test_finished_goods_stock_reduces_what_is_exploded() -> None:
    """Section 14 explodes the *shortfall*, not the order.

    80 kits ordered with 30 already built needs components for 50, not 80.
    Exploding the order quantity would order half a plant's worth of parts that
    are already sitting on the shelf as finished kits.
    """
    kit = make_product("LOCK-KIT")
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={kit.id: kit, screw.id: screw},
        stock={kit.id: StockPosition(on_hand=D("30"))},
        structure={kit.id: [(screw, "6", 1)]},
        default_bom={kit.id},
    )

    plan = await run(source, [sales_line(kit, "80", code="SO/0001")])

    kit_requirement = plan.requirement_for(kit.id)
    screw_requirement = plan.requirement_for(screw.id)
    assert kit_requirement is not None and screw_requirement is not None
    assert kit_requirement.shortage_quantity == D("50.0000")
    assert screw_requirement.gross_requirement == D("300.0000")


@pytest.mark.asyncio
async def test_a_product_covered_by_stock_is_not_exploded_at_all() -> None:
    kit = make_product("LOCK-KIT")
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={kit.id: kit, screw.id: screw},
        stock={kit.id: StockPosition(on_hand=D("200"))},
        structure={kit.id: [(screw, "6", 1)]},
        default_bom={kit.id},
    )

    plan = await run(source, [sales_line(kit, "80", code="SO/0001")])

    assert source.calls["explode"] == 0
    assert plan.suggestions == ()
    assert plan.requirement_for(screw.id) is None


# -----------------------------------------------------------------------------
# Netting against supply the engine did not create
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirmed_incoming_orders_reduce_the_shortage() -> None:
    screw = make_item("SCREW-M4")
    source = FakeSource(items={screw.id: screw}, incoming={screw.id: arriving("400")})

    plan = await run(source, [sales_line(screw, "600", code="SO/0001")])

    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.net_requirement == D("600.0000")
    assert requirement.incoming_quantity == D("400.0000")
    assert requirement.shortage_quantity == D("200.0000")


@pytest.mark.asyncio
async def test_production_in_progress_reduces_the_shortage() -> None:
    kit = make_product("LOCK-KIT")
    source = FakeSource(items={kit.id: kit}, in_production={kit.id: arriving("25")})

    plan = await run(source, [sales_line(kit, "80", code="SO/0001")])

    requirement = plan.requirement_for(kit.id)
    assert requirement is not None
    assert requirement.in_production_quantity == D("25.0000")
    assert requirement.shortage_quantity == D("55.0000")


@pytest.mark.asyncio
async def test_a_netting_stage_that_could_not_run_is_reported_not_hidden() -> None:
    """A plan that skipped a netting stage over-orders, and must say so.

    Silently treating "the procurement module is not deployed" as "there is
    nothing on order" is the failure this warning exists to prevent.
    """
    screw = make_item("SCREW-M4")
    source = FakeSource(items={screw.id: screw}, incoming_available=False)

    plan = await run(source, [sales_line(screw, "600", code="SO/0001")])

    assert plan.stats["netted_against_incoming"] is False
    assert any("incoming purchase orders" in w for w in plan.warnings)


@pytest.mark.asyncio
async def test_a_make_item_with_no_structure_is_reported() -> None:
    """No BOM means no component demand was raised - a gap worth naming."""
    kit = make_product("LOCK-KIT")
    source = FakeSource(items={kit.id: kit})

    plan = await run(source, [sales_line(kit, "80", code="SO/0001")])

    assert source.calls["explode"] == 0
    assert any("no approved default bill of materials" in w for w in plan.warnings)


# -----------------------------------------------------------------------------
# Loading shape: the batching contract
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_engine_never_queries_per_item() -> None:
    """Three products, sixty order lines, six shared components.

    The statement count must not move with the number of order lines or the
    number of parts: two rounds of loading - item facts, stock, structures and
    both supply feeds for the demand items, then the same for the components the
    explosions discovered - and one explosion per short product.

    Supply is read per round rather than once at the end because a parent is
    netted against its own inbound orders before its structure is exploded. Two
    calls, not one per item: the number is fixed by the algorithm's shape, and
    that is what this test defends.
    """
    products = [make_product(f"PROD-{n}") for n in range(3)]
    components = [make_item(f"COMP-{n}") for n in range(6)]
    source = FakeSource(
        items={i.id: i for i in [*products, *components]},
        structure={p.id: [(c, "2", 1) for c in components] for p in products},
        default_bom={p.id for p in products},
    )
    demands = [
        sales_line(product, "10", code=f"SO/{index:04d}")
        for index, product in enumerate(products * 20)
    ]

    await run(source, demands)

    assert source.calls["planning_items"] == 2
    assert source.calls["stock_positions"] == 2
    assert source.calls["bom_availability"] == 2
    assert source.calls["incoming_supply"] == 2
    assert source.calls["in_production_supply"] == 2
    assert source.calls["supply_terms"] == 1
    assert source.calls["explode"] == len(products)

    # Every load was handed a batch, never a single id.
    assert source.batch_sizes["planning_items"] == [3, 6]
    assert source.batch_sizes["stock_positions"] == [3, 6]
    assert source.batch_sizes["incoming_supply"] == [3, 6]
    assert source.batch_sizes["in_production_supply"] == [3, 6]


@pytest.mark.asyncio
async def test_supply_terms_are_read_only_for_items_that_are_short() -> None:
    """A covered item never needs its supplier's pack size."""
    short = make_item("SCREW-M4")
    covered = make_item("BUSH-8")
    source = FakeSource(
        items={short.id: short, covered.id: covered},
        stock={covered.id: StockPosition(on_hand=D("10000"))},
    )

    await run(
        source,
        [sales_line(short, "100", code="SO/0001"), sales_line(covered, "100", code="SO/0002")],
    )

    assert source.batch_sizes["supply_terms"] == [1]


# -----------------------------------------------------------------------------
# Guard rails
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_demand_produces_an_empty_plan_rather_than_an_error() -> None:
    source = FakeSource(items={})
    plan = await run(source, [])

    assert plan.requirements == ()
    assert plan.suggestions == ()
    assert plan.stats["demand_line_count"] == 0
    assert plan.warnings


def test_more_demand_than_one_run_may_carry_is_refused() -> None:
    """A twenty-thousand-line plan is a data problem, not an order book."""
    item = make_item("SCREW-M4")
    lines = [sales_line(item, "1", code="SO/BULK")] * (MAX_DEMAND_LINES + 1)

    with pytest.raises(engine.MrpDemandTooLargeError) as caught:
        engine.merge_demand(lines)

    assert caught.value.details["limit"] == MAX_DEMAND_LINES


def test_zero_and_negative_demand_lines_are_dropped_not_subtracted() -> None:
    """A fully-shipped order line contributes nothing; it must not credit stock."""
    item = make_item("SCREW-M4")
    buckets = engine.merge_demand(
        [
            sales_line(item, "100", code="SO/0001"),
            DemandLine(
                item_id=item.id,
                quantity=D("0"),
                required_date=REQUIRED,
                source=MrpDemandSource.SALES_ORDER,
            ),
        ]
    )
    assert buckets[item.id].direct == D("100.0000")
    assert buckets[item.id].source_count == 1


@pytest.mark.asyncio
async def test_demand_for_an_unknown_item_is_skipped_and_reported() -> None:
    """A component the planner cannot read must not silently vanish from the plan."""
    ghost = make_item("GHOST")
    source = FakeSource(items={})

    plan = await run(source, [sales_line(ghost, "10", code="SO/0001")])

    assert plan.requirements == ()
    assert any("not readable in this company" in w for w in plan.warnings)


@pytest.mark.asyncio
async def test_manual_demand_is_planned_alongside_the_order_book() -> None:
    screw = make_item("SCREW-M4")
    source = FakeSource(items={screw.id: screw})

    plan = await run(
        source,
        [
            sales_line(screw, "100", code="SO/0001"),
            DemandLine(
                item_id=screw.id,
                quantity=D("250"),
                required_date=REQUIRED,
                source=MrpDemandSource.MANUAL,
                notes="Spares buffer",
            ),
        ],
    )

    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.gross_requirement == D("350.0000")
    assert requirement.source_demand_count == 2


@pytest.mark.asyncio
async def test_component_dates_are_pulled_forward_by_the_parents_lead_time() -> None:
    """A part is needed when its parent's build starts, not when the kit ships."""
    kit = make_product("LOCK-KIT", lead_time_days=10)
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={kit.id: kit, screw.id: screw},
        structure={kit.id: [(screw, "6", 1)]},
        default_bom={kit.id},
    )

    plan = await run(source, [sales_line(kit, "10", code="SO/0001", required=date(2026, 7, 15))])

    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.earliest_required_date == date(2026, 7, 5)


@pytest.mark.asyncio
async def test_suggestions_are_ordered_by_urgency() -> None:
    """The list is a work queue: what has to be ordered first comes first."""
    urgent = make_item("URGENT", lead_time_days=60)
    relaxed = make_item("RELAXED", lead_time_days=1)
    source = FakeSource(items={urgent.id: urgent, relaxed.id: relaxed})

    plan = await run(
        source,
        [
            sales_line(relaxed, "10", code="SO/0002"),
            sales_line(urgent, "10", code="SO/0001"),
        ],
    )

    assert [s.item_code for s in plan.suggestions] == ["URGENT", "RELAXED"]
    assert plan.suggestions[0].suggestion_type is MrpSuggestionType.PURCHASE
