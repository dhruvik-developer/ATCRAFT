"""Level-by-level planning: low-level codes, stock at every level, dates.

E3a replaced the date-settling fixed point with an ordering: every item is
planned exactly once, at its **low-level code** - the deepest level it appears
at across the structures in scope - so a parent is always netted and exploded
before the child that inherits its demand. These tests pin the properties that
ordering buys, each with the figure the old shape got wrong:

* the low-level code is the DEEPEST route, not the first one found;
* stock at a sub-assembly reduces the raw material demand beneath it, instead
  of the same stock being offered again through a second route;
* lead times offset one level at a time, from routing shop time where a routing
  exists and from the item master where it does not;
* expected availability is the date coverage completes - stock, dated supply,
  or the order the plan itself raises - and it rolls up through the levels;
* a cycle is refused rather than followed;
* a phantom is blown through without gaining a row of its own.

Everything is driven through the in-memory source the merge tests define, so
the batch shape is asserted beside the arithmetic rather than assumed.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.modules.mrp import engine
from src.modules.mrp.engine import StockPosition
from tests.unit.test_mrp_engine_merge import (
    AS_OF,
    HORIZON_END,
    REQUIRED,
    FakeSource,
    make_item,
    make_product,
    sales_line,
)

D = Decimal


def day(offset: int) -> date:
    """A date ``offset`` days into the run's horizon."""
    return AS_OF + timedelta(days=offset)


async def run(source: FakeSource, demands: list[engine.DemandLine]) -> engine.MrpPlan:
    return await engine.plan(source, demands=demands, as_of=AS_OF, horizon_end=HORIZON_END)


# -----------------------------------------------------------------------------
# Low-level codes: plan at the deepest place the item appears
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_low_level_code_is_the_deepest_level_an_item_appears_at() -> None:
    """SCREW-M4 is reached at level 1 (straight off the kit) and level 2 (through
    the sub-assembly). It must be planned at 2, or the demand through the
    sub-assembly arrives after the screw has already been netted and ordered.

    10 kits x 2 direct screws plus 10 sub-assemblies x 3 screws is one
    requirement for 50, not an order for 20 and a second one for 30.
    """
    kit = make_product("LOCK-KIT")
    sub = make_product("SUB-ASSY")
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={kit.id: kit, sub.id: sub, screw.id: screw},
        structure={kit.id: [(sub, "1"), (screw, "2")], sub.id: [(screw, "3")]},
        default_bom={kit.id, sub.id},
    )

    plan = await run(source, [sales_line(kit, "10", code="SO/0001")])

    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.level == 1, "the shallowest route is what a planner reads"
    assert requirement.low_level_code == 2, "the deepest route is where it is netted"
    assert requirement.dependent_demand_quantity == D("50.0000")
    assert requirement.gross_requirement == D("50.0000")

    # One row, planned once - and only at the deepest code.
    assert [r.item_code for r in plan.requirements if r.item_code == "SCREW-M4"] == ["SCREW-M4"]
    assert [r.item_code for r in plan.requirements if r.low_level_code == 2] == ["SCREW-M4"]


# -----------------------------------------------------------------------------
# Stock at every level
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stock_on_a_sub_assembly_reduces_the_components_beneath_it() -> None:
    """The spec's worked example, in miniature.

    100 kits each need one Rose Handle; 50 handles are in stock, so only 50
    handle bodies, 100 springs and 100 handle screws are raised - not double.
    The screws the kit consumes directly still merge with the handle's own, so
    the screw requirement is one row for 300.
    """
    kit = make_product("LOCK-KIT")
    handle = make_product("ROSE-HANDLE")
    body = make_item("HANDLE-BODY")
    spring = make_item("SPRING-8")
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={kit.id: kit, handle.id: handle, body.id: body, spring.id: spring, screw.id: screw},
        stock={handle.id: StockPosition(on_hand=D("50"))},
        structure={
            kit.id: [(handle, "1"), (screw, "2")],
            handle.id: [(body, "1"), (spring, "2"), (screw, "2")],
        },
        default_bom={kit.id, handle.id},
    )

    plan = await run(source, [sales_line(kit, "100", code="SO/0001")])

    handle_requirement = plan.requirement_for(handle.id)
    assert handle_requirement is not None
    assert handle_requirement.gross_requirement == D("100.0000")
    assert handle_requirement.shortage_quantity == D("50.0000"), "50 handles are on the shelf"

    assert plan.requirement_for(body.id).gross_requirement == D("50.0000")  # type: ignore[union-attr]
    assert plan.requirement_for(spring.id).gross_requirement == D("100.0000")  # type: ignore[union-attr]
    screw_requirement = plan.requirement_for(screw.id)
    assert screw_requirement is not None
    assert screw_requirement.gross_requirement == D("300.0000"), (
        "200 consumed by the kits plus 100 by the 50 handles actually built - "
        "not 500, which is what exploding the whole order twice would raise"
    )
    assert screw_requirement.low_level_code == 2


@pytest.mark.asyncio
async def test_semi_finished_stock_reduces_raw_material_demand() -> None:
    """Stock at *each* level is consumed once, at that level.

    100 kits need 200 sub-assemblies; 50 are stocked. The 150 that are built
    need 150 semi-finished units; 30 are stocked, so 120 are made, each from
    four raw pieces: 480 are bought, not 800.
    """
    kit = make_product("KIT")
    sub = make_product("SUB-ASSY")
    semi = make_product("SEMI-FINISHED")
    raw = make_item("RAW-PART")
    source = FakeSource(
        items={kit.id: kit, sub.id: sub, semi.id: semi, raw.id: raw},
        stock={
            sub.id: StockPosition(on_hand=D("50")),
            semi.id: StockPosition(on_hand=D("30")),
        },
        structure={kit.id: [(sub, "2")], sub.id: [(semi, "1")], semi.id: [(raw, "4")]},
        default_bom={kit.id, sub.id, semi.id},
    )

    plan = await run(source, [sales_line(kit, "100", code="SO/0001")])

    sub_requirement = plan.requirement_for(sub.id)
    semi_requirement = plan.requirement_for(semi.id)
    raw_requirement = plan.requirement_for(raw.id)
    assert sub_requirement is not None and semi_requirement is not None
    assert raw_requirement is not None

    assert sub_requirement.gross_requirement == D("200.0000")
    assert sub_requirement.shortage_quantity == D("150.0000")
    assert semi_requirement.gross_requirement == D("150.0000")
    assert semi_requirement.shortage_quantity == D("120.0000")
    assert raw_requirement.gross_requirement == D("480.0000")


# -----------------------------------------------------------------------------
# Per-level lead-time offsets
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lead_times_offset_one_level_at_a_time() -> None:
    """A ladder, not one subtraction from the root.

    TOP is needed on day 50 and takes 20 days, so MID is due on day 30. MID
    takes 5, so BASE is due on day 25 - not day 50 - 20 - 5 read twice from the
    root, and not day 50 - 20. BASE takes one day and puts RAW on day 24.
    """
    top = make_product("TOP", lead_time_days=20)
    mid = make_product("MID", lead_time_days=5)
    base = make_product("BASE", lead_time_days=1)
    raw = make_item("RAW")
    source = FakeSource(
        items={top.id: top, mid.id: mid, base.id: base, raw.id: raw},
        structure={top.id: [(mid, "1")], mid.id: [(base, "1")], base.id: [(raw, "1")]},
        default_bom={top.id, mid.id, base.id},
    )

    plan = await run(source, [sales_line(top, "4", code="SO/0001", required=day(50))])

    top_requirement = plan.requirement_for(top.id)
    mid_requirement = plan.requirement_for(mid.id)
    base_requirement = plan.requirement_for(base.id)
    raw_requirement = plan.requirement_for(raw.id)
    assert top_requirement is not None and mid_requirement is not None
    assert base_requirement is not None and raw_requirement is not None

    assert top_requirement.earliest_required_date == day(50)
    assert mid_requirement.earliest_required_date == day(30)
    assert base_requirement.earliest_required_date == day(25)
    assert raw_requirement.earliest_required_date == day(24)
    assert raw_requirement.source_demands[0]["required_date"] == day(24).isoformat()


@pytest.mark.asyncio
async def test_a_manufactured_item_is_paced_by_its_routings_shop_time() -> None:
    """The routing contract MRP was meant to call, called once per level.

    The item master says two days; the routing says ten. The plan must use the
    routing for both the suggestion and the components, and it must ask for the
    quantity it is actually going to make - shop time is dominated by run time.
    """
    kit = make_product("LOCK-KIT", lead_time_days=2)
    screw = make_item("SCREW-M4", lead_time_days=7)
    source = FakeSource(
        items={kit.id: kit, screw.id: screw},
        structure={kit.id: [(screw, "6")]},
        default_bom={kit.id},
        routing_leads={kit.id: 10},
    )

    plan = await run(source, [sales_line(kit, "10", code="SO/0001", required=day(30))])

    kit_suggestion = plan.suggestion_for(kit.id)
    assert kit_suggestion is not None
    assert kit_suggestion.lead_time_days == 10, "the routing, not the item master"
    assert kit_suggestion.suggested_order_date == day(20)
    assert kit_suggestion.expected_available_date == day(30)

    screw_requirement = plan.requirement_for(screw.id)
    assert screw_requirement is not None
    assert screw_requirement.earliest_required_date == day(20), "offset by the ten days"

    assert source.lead_time_requests == [{kit.id: D("10.0000")}]


@pytest.mark.asyncio
async def test_a_make_item_without_a_routing_falls_back_to_the_item_master() -> None:
    kit = make_product("LOCK-KIT", lead_time_days=2)
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={kit.id: kit, screw.id: screw},
        structure={kit.id: [(screw, "1")]},
        default_bom={kit.id},
    )

    plan = await run(source, [sales_line(kit, "1", code="SO/0001", required=day(30))])

    suggestion = plan.suggestion_for(kit.id)
    assert suggestion is not None
    assert suggestion.lead_time_days == 2
    screw_requirement = plan.requirement_for(screw.id)
    assert screw_requirement is not None
    assert screw_requirement.earliest_required_date == day(28)


# -----------------------------------------------------------------------------
# Expected availability
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_requirement_covered_by_stock_is_available_from_the_run_date() -> None:
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={screw.id: screw},
        stock={screw.id: StockPosition(on_hand=D("500"))},
    )

    plan = await run(source, [sales_line(screw, "100", code="SO/0001")])

    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.shortage_quantity == D("0.0000")
    assert requirement.expected_available_date == AS_OF


@pytest.mark.asyncio
async def test_expected_availability_is_the_day_supply_completes_coverage() -> None:
    """20 in stock, 40 arriving day 10, 50 arriving day 20 - covered on day 20."""
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={screw.id: screw},
        stock={screw.id: StockPosition(on_hand=D("20"))},
        incoming={
            screw.id: (
                engine.SupplyArrival(due_date=day(10), quantity=D("40")),
                engine.SupplyArrival(due_date=day(20), quantity=D("50")),
            )
        },
    )

    plan = await run(source, [sales_line(screw, "100", code="SO/0001")])

    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.shortage_quantity == D("0.0000")
    assert requirement.expected_available_date == day(20)


@pytest.mark.asyncio
async def test_a_short_item_is_available_when_its_own_order_lands() -> None:
    screw = make_item("SCREW-M4", lead_time_days=7)
    source = FakeSource(items={screw.id: screw})

    plan = await run(source, [sales_line(screw, "100", code="SO/0001", required=day(30))])

    suggestion = plan.suggestion_for(screw.id)
    requirement = plan.requirement_for(screw.id)
    assert suggestion is not None and requirement is not None
    assert suggestion.suggested_order_date == day(23)
    assert suggestion.expected_available_date == day(30)
    assert requirement.expected_available_date == day(30)


@pytest.mark.asyncio
async def test_a_late_suggestion_is_dated_from_the_run_date() -> None:
    """A late order cannot land in the past; it lands *lead time* from today.

    Needed on day 5 with a 30-day lead, the order date is 25 days ago. The
    honest answer is not day 5 - an impossible date - but day 30.
    """
    screw = make_item("SCREW-M4", lead_time_days=30)
    source = FakeSource(items={screw.id: screw})

    plan = await run(source, [sales_line(screw, "100", code="SO/0001", required=day(5))])

    suggestion = plan.suggestion_for(screw.id)
    requirement = plan.requirement_for(screw.id)
    assert suggestion is not None and requirement is not None
    assert suggestion.is_late is True
    assert suggestion.expected_available_date == day(30)
    assert requirement.expected_available_date == day(30)


@pytest.mark.asyncio
async def test_a_parent_cannot_be_available_before_its_components() -> None:
    """One late part moves the assembly it feeds.

    The kit's own order could land on day 44, but its screws can only be here on
    day 60, and the kit needs ten days after that: day 70. Reporting day 44
    would promise an assembly whose parts do not exist.
    """
    kit = make_product("LOCK-KIT", lead_time_days=10)
    screw = make_item("SCREW-M4", lead_time_days=60)
    source = FakeSource(
        items={kit.id: kit, screw.id: screw},
        structure={kit.id: [(screw, "6")]},
        default_bom={kit.id},
    )

    plan = await run(source, [sales_line(kit, "10", code="SO/0001", required=REQUIRED)])

    kit_requirement = plan.requirement_for(kit.id)
    screw_requirement = plan.requirement_for(screw.id)
    kit_suggestion = plan.suggestion_for(kit.id)
    assert kit_requirement is not None and screw_requirement is not None
    assert kit_suggestion is not None

    assert screw_requirement.expected_available_date == day(60)
    assert kit_suggestion.expected_available_date == REQUIRED, (
        "the suggestion says when its own order lands"
    )
    assert kit_requirement.expected_available_date == day(70), (
        "the requirement rolls the screw's slippage up through the kit's lead time"
    )


# -----------------------------------------------------------------------------
# Structures the planner must refuse or pass through
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cycle_is_refused_rather_than_followed() -> None:
    """A cycle has no low-level code: levelling it never terminates.

    Following it is not a slow answer, it is no answer - every pass raises one
    of the two items a level deeper. The run refuses and names the structure.
    """
    a = make_product("ASSY-A")
    b = make_product("ASSY-B")
    source = FakeSource(
        items={a.id: a, b.id: b},
        structure={a.id: [(b, "1")], b.id: [(a, "1")]},
        default_bom={a.id, b.id},
    )

    with pytest.raises(engine.MrpStructureTooDeepError) as caught:
        await run(source, [sales_line(a, "1", code="SO/LOOP")])

    assert caught.value.details["max_structure_levels"] == engine.MAX_STRUCTURE_LEVELS
    assert "almost always means a cycle" in caught.value.message
    listed = set(caught.value.details["item_codes"])
    assert listed and listed <= {"ASSY-A", "ASSY-B"}


@pytest.mark.asyncio
async def test_a_cycle_outside_the_structure_in_scope_does_not_stop_the_run() -> None:
    """Only reachable items are levelled; an unrelated bad structure is not this
    run's business and must not block a plan that never touches it."""
    kit = make_product("LOCK-KIT")
    screw = make_item("SCREW-M4")
    a = make_product("ASSY-A")
    b = make_product("ASSY-B")
    source = FakeSource(
        items={kit.id: kit, screw.id: screw, a.id: a, b.id: b},
        structure={
            kit.id: [(screw, "1")],
            a.id: [(b, "1")],
            b.id: [(a, "1")],
        },
        default_bom={kit.id, a.id, b.id},
    )

    plan = await run(source, [sales_line(kit, "10", code="SO/0001")])

    screw_requirement = plan.requirement_for(screw.id)
    assert screw_requirement is not None
    assert screw_requirement.gross_requirement == D("10.0000")
    assert plan.requirement_for(a.id) is None


@pytest.mark.asyncio
async def test_a_phantom_is_blown_through_without_a_row_of_its_own() -> None:
    """A phantom is a grouping on a drawing, not a stocked or built part.

    KIT-GROUPING is demanded as two per kit and supplies three screws each. It
    is exploded like its children's structure belongs to the kit, but nothing
    is ever bought or built under its name, so it gets no requirement row and no
    suggestion.
    """
    kit = make_product("LOCK-KIT")
    grouping = make_item("KIT-GROUPING")
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={kit.id: kit, grouping.id: grouping, screw.id: screw},
        structure={kit.id: [(grouping, "2")], grouping.id: [(screw, "3")]},
        default_bom={kit.id, grouping.id},
        phantom={grouping.id},
    )

    plan = await run(source, [sales_line(kit, "10", code="SO/0001")])

    assert plan.requirement_for(grouping.id) is None
    assert plan.suggestion_for(grouping.id) is None
    screw_requirement = plan.requirement_for(screw.id)
    assert screw_requirement is not None
    assert screw_requirement.gross_requirement == D("60.0000")
    assert screw_requirement.low_level_code == 2
