"""An item's needed-by date must be final before any supply is netted against it.

The defect these tests pin down is not an arithmetic slip, it is an ordering
one, and it only appears for an item that is *both directly demanded and a
component of something else*:

    The engine's former ``_explode_shortfalls`` decided whether to explode a
    parent by splitting its supply at ``bucket.earliest_date`` - read while the
    same loop was still calling ``note_date`` on it. A parent visited later
    pulled the date
    earlier, and the final netting loop then split the *same item's* supply at
    the new, earlier date. One item, two dates: the explosion saw an inbound
    order as in time and raised no component demand, the requirement saw the
    same order as late and reported the full shortage, and the parts for that
    shortage were never planned. The run reports a clean plan and the floor runs
    out.

E3a removed the fixed point rather than hardening it. Every item is planned
once, at its low-level code - the deepest level it appears at - so a parent's
date, its explosion and its requirement are computed in one visit and cannot
disagree afterwards. A component of something directly demanded is netted after
that direct demand has already pulled the date earlier.

The reproductions are kept because a property that was once false is worth
asserting after the shape that made it true. Each one is driven through the
in-memory ``FakeSource``, so the arithmetic is asserted directly - the date each
contribution was raised for, and the fact that the requirement was netted at
that same date - rather than inferred from a total that could come out right by
accident.

Fixture note: the old fixtures consolidated a sub-assembly's components into the
parent's line ("1 bracket and 4 screws a kit ... consolidated across levels the
way the BOM engine reports them"). That shape no longer exists: the engine walks
single-level edges itself, so the kit's line is the kit's *own* two screws and
the bracket's line carries the bracket's two.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.modules.mrp import engine
from src.modules.mrp.engine import SupplyArrival, SupplySplit, supply_split
from tests.unit.test_mrp_engine_merge import (
    AS_OF,
    HORIZON_END,
    FakeSource,
    make_item,
    make_product,
    sales_line,
)

D = Decimal


def day(offset: int) -> date:
    """A date ``offset`` days into the run's horizon."""
    return AS_OF + timedelta(days=offset)


def arriving(when: date, quantity: str) -> tuple[SupplyArrival, ...]:
    return (SupplyArrival(due_date=when, quantity=D(quantity)),)


async def run(source: FakeSource, demands: list[engine.DemandLine]) -> engine.MrpPlan:
    return await engine.plan(source, demands=demands, as_of=AS_OF, horizon_end=HORIZON_END)


def bom_dates(requirement: engine.Requirement, *, via: str) -> list[str]:
    """The dates the explosions through ``via`` raised this component for.

    Reads the provenance the engine keeps on every contribution, which is the
    only place the date an explosion was *decided at* is visible from outside.
    """
    return [
        entry["required_date"]
        for entry in requirement.source_demands
        if entry.get("source") == "BOM" and entry.get("via_item_code") == via
    ]


# -----------------------------------------------------------------------------
# The split itself: one function, one answer
# -----------------------------------------------------------------------------


def test_the_split_phases_both_feeds_at_one_date() -> None:
    """Both stages read this object, so both stages read one date."""
    split = supply_split(
        day(20),
        incoming=[SupplyArrival(day(10), D("30")), SupplyArrival(day(30), D("70"))],
        in_production=[SupplyArrival(day(20), D("5")), SupplyArrival(day(21), D("15"))],
    )

    assert split == SupplySplit(
        needed_by=day(20),
        incoming=D("30.0000"),
        late_incoming=D("70.0000"),
        in_production=D("5.0000"),
        late_in_production=D("15.0000"),
        # Each feed's excluded tranche is dated from that feed alone: the
        # purchase order's 70 land on day 30 and the job's 15 on day 21. One
        # date for both would describe at most one of them.
        first_late_incoming=day(30),
        first_late_in_production=day(21),
        # Each feed has exactly ONE late tranche here, so its interval
        # degenerates to a point. With several, the closing date is what keeps
        # the summed quantity honest - the opening date alone attributes the
        # whole sum to the first tranche, which reads as "nearly here".
        last_late_incoming=day(30),
        last_late_in_production=day(21),
    )
    assert split.late_total == D("85.0000")
    # ...and the cross-feed earliest is derived from the pair, so it cannot be
    # set to something the pair does not support.
    assert split.first_late_arrival == day(21), "the soonest anything lands, from either feed"


def test_the_split_with_nothing_inbound_is_zero_everywhere() -> None:
    split = supply_split(day(20))

    assert split.needed_by == day(20)
    assert split.incoming == D("0.0000")
    assert split.late_in_production == D("0.0000")
    assert split.late_total == D("0.0000")
    assert split.first_late_arrival is None, "no supply at all is not late supply"


# =============================================================================
# Reproduction one: a bracket that is sold AND built into a lock kit
# =============================================================================
#
#   BRACKET   MAKE, lead 0,  BOM = 2 x SCREW-M4.  100 demanded for day 30.
#   LOCK-KIT  MAKE, lead 30, BOM = 1 x BRACKET + 2 x SCREW-M4 of its own (the
#             bracket adds its own two when it is exploded, one level down).
#             10 demanded for day 40, so its parts are needed on day 10.
#   One SENT purchase order for 100 BRACKET arriving on day 20.
#
# The kit's explosion pulls BRACKET back to day 10, where that order is twenty
# days too late, and because the bracket is planned after the kit - its
# low-level code is deeper - the requirement and the explosion see the same
# date by construction.


@dataclass(frozen=True)
class BracketCase:
    """The reproduction, built once and asserted from four angles."""

    source: FakeSource
    demands: list[engine.DemandLine]
    bracket: engine.PlanningItem
    kit: engine.PlanningItem
    screw: engine.PlanningItem


@pytest.fixture
def bracket_case() -> BracketCase:
    bracket = make_product("BRACKET", lead_time_days=0)
    kit = make_product("LOCK-KIT", lead_time_days=30)
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={bracket.id: bracket, kit.id: kit, screw.id: screw},
        structure={
            bracket.id: [(screw, "2")],
            # The kit's own two screws; the bracket's two arrive through the
            # bracket's line when the bracket is exploded one level down.
            kit.id: [(bracket, "1"), (screw, "2")],
        },
        default_bom={bracket.id, kit.id},
        incoming={bracket.id: arriving(day(20), "100")},
    )
    demands = [
        sales_line(bracket, "100", code="SO/0001", required=day(30)),
        sales_line(kit, "10", code="SO/0002", required=day(40)),
    ]
    return BracketCase(source=source, demands=demands, bracket=bracket, kit=kit, screw=screw)


@pytest.mark.asyncio
async def test_the_bracket_is_netted_at_one_date_not_two(bracket_case: BracketCase) -> None:
    """THE reproduction: the explosion and the requirement must agree.

    Before the fix the explosion split the bracket's supply on day 30 - where
    the purchase order is in time, the shortfall is nil and nothing is exploded
    - while the requirement split the same supply on day 10, where the order is
    late and the shortage is 110. The plan therefore said "make 110 brackets"
    and raised screws for none of them.
    """
    source, demands = bracket_case.source, bracket_case.demands
    bracket, screw = bracket_case.bracket, bracket_case.screw

    plan = await run(source, demands)

    bracket_requirement = plan.requirement_for(bracket.id)
    assert bracket_requirement is not None
    # The kit's thirty-day lead time pulls the bracket back to day 10, and that
    # is the date the whole row is planned to.
    assert bracket_requirement.earliest_required_date == day(10)
    assert bracket_requirement.incoming_quantity == D("0.0000")
    assert bracket_requirement.late_incoming_quantity == D("100.0000")
    assert bracket_requirement.gross_requirement == D("110.0000"), "100 sold, 10 in kits"
    assert bracket_requirement.shortage_quantity == D("110.0000")

    # ...and the explosion was decided at that same date, not at day 30.
    screw_requirement = plan.requirement_for(screw.id)
    assert screw_requirement is not None
    assert bom_dates(screw_requirement, via="BRACKET") == [day(10).isoformat()], (
        "the bracket's structure was exploded at a date the requirement then disagreed with"
    )

    # The number a stockout is made of: 220 screws for the 110 brackets the plan
    # says to build, plus 20 through the kits' own two each. (The old fixture
    # consolidated the bracket's screws into the kit's line and said 100 + 40;
    # with single-level edges the two contributions have to be counted where they
    # are actually consumed.)
    assert screw_requirement.dependent_demand_quantity == D("240.0000")
    assert screw_requirement.shortage_quantity == D("240.0000")

    suggestion = plan.suggestion_for(screw.id)
    assert suggestion is not None
    assert suggestion.suggested_quantity == D("240.0000")


@pytest.mark.asyncio
async def test_the_bracket_case_is_planned_in_level_order(bracket_case: BracketCase) -> None:
    """The guarantee is structural, so the plan can report it.

    KIT is level 0, BRACKET is netted at level 1 (the kit's child) and the screw
    at level 2 (the bracket's child, and also a kit child). The run records the
    deepest code it planned at, and there is no settling verdict to report any
    more: nothing is revisited.
    """
    plan = await run(bracket_case.source, bracket_case.demands)

    kit_requirement = plan.requirement_for(bracket_case.kit.id)
    bracket_requirement = plan.requirement_for(bracket_case.bracket.id)
    screw_requirement = plan.requirement_for(bracket_case.screw.id)
    assert kit_requirement is not None and bracket_requirement is not None
    assert screw_requirement is not None

    assert kit_requirement.low_level_code == 0
    assert bracket_requirement.low_level_code == 1
    assert screw_requirement.low_level_code == 2
    assert plan.stats["low_level_code_max"] == 2
    assert "demand_dates_settled" not in plan.stats, (
        "there is no fixed point left to settle - the ordering is the guarantee"
    )


@pytest.mark.asyncio
async def test_each_level_costs_one_structure_batch(bracket_case: BracketCase) -> None:
    """The cost of the ordering, asserted rather than assumed.

    Discovery asks for the planning facts and the structures of each level once:
    two products at level 0, then the screw they introduced at level 1. The
    bracket is re-queued at level 1 when the kit raises it, but its structure was
    already read and is not read again. Supply is then one statement each over
    the whole discovered set.
    """
    source = bracket_case.source

    plan = await run(source, bracket_case.demands)

    assert plan.stats["explosion_count"] == 2, "the kit and the bracket, once each"
    assert source.batch_sizes["planning_items"] == [2, 1]
    assert source.batch_sizes["bom_structures"] == [2, 1]
    assert source.batch_sizes["stock_positions"] == [3]
    assert source.batch_sizes["incoming_supply"] == [3]
    assert source.batch_sizes["in_production_supply"] == [3]
    assert source.batch_sizes["supply_terms"] == [3]


@pytest.mark.asyncio
async def test_the_bracket_still_reports_the_order_it_could_not_count(
    bracket_case: BracketCase,
) -> None:
    """A hundred brackets are genuinely on order. The plan has to say so.

    Settling the date earlier is what makes that order late; quietly dropping it
    would leave a buyer looking at a shortage of 110 and a purchase order for
    100 with nothing connecting them.
    """
    plan = await run(bracket_case.source, bracket_case.demands)

    assert plan.stats["late_supply_item_count"] == 1
    late = [w for w in plan.warnings if "arrives AFTER it is needed" in w]
    assert late, plan.warnings
    assert "BRACKET" in late[0]
    assert "100.0000" in late[0]
    assert day(20).isoformat() in late[0], "when it arrives"
    assert day(10).isoformat() in late[0], "when it is needed"


# =============================================================================
# Reproduction two: a sub-assembly sold as a spare
# =============================================================================
#
#   SUBASSY-X  MAKE, lead 10, BOM = 3 x PIN.  40 sold as spares for day 25.
#   KIT-A      MAKE, lead 40, BOM = 1 x SUBASSY-X (three pins each, one level
#              down). 50 demanded for day 60, so its parts are needed on day 20.
#   One purchase order for 40 SUBASSY-X arriving on day 22.
#
# The spare demand (day 25) is later than the component need (day 20), and the
# purchase order lands between the two. Netted at day 20 - the date the kit pulls
# it to, and the date it is planned at because it is shallower than the pins -
# the sub-assembly is short 90, and only the 90 actually built raise pins.


@pytest.mark.asyncio
async def test_a_sub_assembly_sold_as_a_spare_is_netted_at_its_low_level_date() -> None:
    subassy = make_product("SUBASSY-X", lead_time_days=10)
    kit = make_product("KIT-A", lead_time_days=40)
    pin = make_item("PIN-3")
    source = FakeSource(
        items={subassy.id: subassy, kit.id: kit, pin.id: pin},
        structure={
            subassy.id: [(pin, "3")],
            kit.id: [(subassy, "1")],
        },
        default_bom={subassy.id, kit.id},
        incoming={subassy.id: arriving(day(22), "40")},
    )
    demands = [
        sales_line(subassy, "40", code="SO/SPARE", required=day(25)),
        sales_line(kit, "50", code="SO/KIT", required=day(60)),
    ]

    plan = await run(source, demands)

    sub_requirement = plan.requirement_for(subassy.id)
    assert sub_requirement is not None
    assert sub_requirement.earliest_required_date == day(20), "the kit's lead time governs"
    assert sub_requirement.incoming_quantity == D("0.0000")
    assert sub_requirement.late_incoming_quantity == D("40.0000")
    assert sub_requirement.gross_requirement == D("90.0000"), "40 spares, 50 in kits"
    assert sub_requirement.shortage_quantity == D("90.0000")

    pin_requirement = plan.requirement_for(pin.id)
    assert pin_requirement is not None
    # 270 for the 90 sub-assemblies the plan says to build, at three pins each.
    assert pin_requirement.dependent_demand_quantity == D("270.0000")
    # The sub-assembly's own ten-day lead time offsets its pins again, from the
    # date it was netted at - day 20 - not from the spare order's day 25.
    assert bom_dates(pin_requirement, via="SUBASSY-X") == [day(10).isoformat()]
    assert pin_requirement.earliest_required_date == day(10)

    assert sub_requirement.low_level_code == 1
    assert pin_requirement.low_level_code == 2


# =============================================================================
# A plan with nothing to re-decide costs no extra round trips
# =============================================================================


@pytest.mark.asyncio
async def test_a_product_that_is_nobodys_component_is_decided_once() -> None:
    """No cross-dependency, no second look: one walk, one explosion."""
    kit = make_product("LOCK-KIT", lead_time_days=10)
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={kit.id: kit, screw.id: screw},
        structure={kit.id: [(screw, "6")]},
        default_bom={kit.id},
        incoming={kit.id: arriving(day(5), "40")},
    )

    plan = await run(source, [sales_line(kit, "100", code="SO/0001", required=day(30))])

    assert plan.stats["explosion_count"] == 1
    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.dependent_demand_quantity == D("360.0000"), "six screws for 60 kits"
    assert bom_dates(requirement, via="LOCK-KIT") == [day(20).isoformat()]


@pytest.mark.asyncio
async def test_a_covered_product_is_still_never_exploded() -> None:
    """Stock on the parent raises no component demand, and no suggestion."""
    kit = make_product("LOCK-KIT")
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={kit.id: kit, screw.id: screw},
        stock={kit.id: engine.StockPosition(on_hand=D("200"))},
        structure={kit.id: [(screw, "6")]},
        default_bom={kit.id},
    )

    plan = await run(source, [sales_line(kit, "80", code="SO/0001")])

    assert plan.requirement_for(screw.id) is None
    assert plan.suggestions == ()


@pytest.mark.asyncio
async def test_the_provenance_of_an_explosion_is_not_doubled() -> None:
    """One contribution per parent, however many times the levels meet.

    The screw is a child of both the kit and the bracket, and the bracket is a
    child of the kit. Each contribution is added exactly once - a merged
    quantity counted twice is the failure this module exists to prevent.
    """
    bracket = make_product("BRACKET", lead_time_days=0)
    kit = make_product("LOCK-KIT", lead_time_days=30)
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={bracket.id: bracket, kit.id: kit, screw.id: screw},
        structure={
            bracket.id: [(screw, "2")],
            kit.id: [(bracket, "1"), (screw, "2")],
        },
        default_bom={bracket.id, kit.id},
        incoming={bracket.id: arriving(day(20), "100")},
    )

    plan = await run(
        source,
        [
            sales_line(bracket, "100", code="SO/0001", required=day(30)),
            sales_line(kit, "10", code="SO/0002", required=day(40)),
        ],
    )

    screw_requirement = plan.requirement_for(screw.id)
    assert screw_requirement is not None
    assert len(bom_dates(screw_requirement, via="BRACKET")) == 1
    assert len(bom_dates(screw_requirement, via="LOCK-KIT")) == 1
    assert screw_requirement.source_demand_count == 2

    bracket_requirement = plan.requirement_for(bracket.id)
    assert bracket_requirement is not None
    assert bracket_requirement.direct_demand_quantity == D("100.0000")
    assert bracket_requirement.dependent_demand_quantity == D("10.0000"), (
        "one contribution from the kit, not one per visit"
    )
    assert bracket_requirement.source_demand_count == 2, "one sales order, one BOM"


@pytest.mark.asyncio
async def test_a_date_survives_a_deeper_chain() -> None:
    """Three products in a chain, and the date has to travel the whole way.

    TOP (lead 20) contains MID; MID (lead 5) contains BASE; BASE is also sold on
    its own. The deepest code is PIN at three levels, so BASE is netted after MID
    has already pulled it to day 25 - the pull that a single pass in demand-date
    order used to miss.
    """
    base = make_product("BASE", lead_time_days=0)
    mid = make_product("MID", lead_time_days=5)
    top = make_product("TOP", lead_time_days=20)
    pin = make_item("PIN-3")
    source = FakeSource(
        items={base.id: base, mid.id: mid, top.id: top, pin.id: pin},
        structure={
            base.id: [(pin, "1")],
            mid.id: [(base, "1")],
            top.id: [(mid, "1")],
        },
        default_bom={base.id, mid.id, top.id},
    )

    plan = await run(
        source,
        [
            sales_line(base, "1", code="SO/BASE", required=day(30)),
            sales_line(mid, "1", code="SO/MID", required=day(40)),
            sales_line(top, "1", code="SO/TOP", required=day(50)),
        ],
    )

    top_requirement = plan.requirement_for(top.id)
    mid_requirement = plan.requirement_for(mid.id)
    base_requirement = plan.requirement_for(base.id)
    pin_requirement = plan.requirement_for(pin.id)
    assert top_requirement is not None
    assert mid_requirement is not None
    assert base_requirement is not None
    assert pin_requirement is not None

    assert top_requirement.earliest_required_date == day(50)
    # TOP's twenty-day lead time puts MID on day 30, which is earlier than MID's
    # own order...
    assert mid_requirement.earliest_required_date == day(30)
    # ...and MID's five days then put BASE on day 25, which BASE inherits even
    # though its own sales order is for day 30.
    assert base_requirement.earliest_required_date == day(25)
    assert plan.stats["low_level_code_max"] == 3
    assert (top_requirement.low_level_code, mid_requirement.low_level_code) == (0, 1)
    assert (base_requirement.low_level_code, pin_requirement.low_level_code) == (2, 3)

    # Every explosion is dated from the date the item was netted at.
    assert bom_dates(base_requirement, via="MID") == [day(25).isoformat()]
