"""The time dimension of MRP netting, without a database.

``split_supply`` is where the planner decides whether an inbound order is supply
for a demand or merely supply *eventually*. It is a pure function so the boundary
is a test rather than a claim, and so the rule can be stated once and asserted
directly:

    supply promised on or before the date the requirement is needed counts;
    everything else is reported and left out.

The defect this replaces netted every requirement against one summed figure for
the whole horizon. A line needing 100 bolts in two days was netted against a
purchase order landing in forty-four, the shortage came out at zero, and the run
reported success while the shop floor ran out. The two directions are not
symmetric: over-ordering raises a purchase order a buyer can see and cancel,
under-ordering raises nothing at all.

The engine-level half - that ``plan`` actually applies the split per requirement,
and that the BOM explosion nets the same way the parent's own suggestion does -
lives here too, driven by the same in-memory source the merge tests use.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.modules.mrp import engine
from src.modules.mrp.engine import (
    SupplyArrival,
    earliest_late_arrival,
    latest_late_arrival,
    split_supply,
    supply_split,
)
from src.modules.mrp.models import MrpDemandSource
from src.modules.products.models import ProductionType
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

EARLY = REQUIRED - timedelta(days=30)
ON_THE_DAY = REQUIRED
LATE = REQUIRED + timedelta(days=1)


def arrival(when: date, quantity: str) -> SupplyArrival:
    return SupplyArrival(due_date=when, quantity=D(quantity))


# -----------------------------------------------------------------------------
# The rule itself
# -----------------------------------------------------------------------------


def test_supply_arriving_before_the_demand_counts() -> None:
    in_time, late = split_supply([arrival(EARLY, "100")], REQUIRED)

    assert in_time == D("100.0000")
    assert late == D("0.0000")


def test_supply_arriving_on_the_day_counts() -> None:
    """The boundary is inclusive.

    A delivery promised for the day the line ships is supply for it. Excluding
    it would raise a duplicate order for stock already arriving - and the whole
    point of netting is not to order what is already coming.
    """
    in_time, late = split_supply([arrival(ON_THE_DAY, "100")], REQUIRED)

    assert in_time == D("100.0000")
    assert late == D("0.0000")


def test_supply_arriving_one_day_after_does_not_count() -> None:
    """The failing half of the boundary, and the whole defect in one assertion."""
    in_time, late = split_supply([arrival(LATE, "100")], REQUIRED)

    assert in_time == D("0.0000")
    assert late == D("100.0000"), "excluded, but never silently: the caller reports it"


def test_each_tranche_is_judged_on_its_own_date() -> None:
    """A part-early, part-late order is split, not accepted or rejected whole."""
    in_time, late = split_supply(
        [arrival(EARLY, "40"), arrival(ON_THE_DAY, "10"), arrival(LATE, "60")], REQUIRED
    )

    assert in_time == D("50.0000")
    assert late == D("60.0000")


def test_no_supply_at_all_is_zero_both_ways() -> None:
    assert split_supply(None, REQUIRED) == (D("0.0000"), D("0.0000"))
    assert split_supply([], REQUIRED) == (D("0.0000"), D("0.0000"))


def test_a_zero_tranche_contributes_nothing() -> None:
    """Defensive, and cheap: a zero row is noise whichever side of the date it is."""
    assert split_supply([arrival(EARLY, "0"), arrival(LATE, "0")], REQUIRED) == (
        D("0.0000"),
        D("0.0000"),
    )


def test_the_split_is_quantised_like_every_other_quantity() -> None:
    in_time, late = split_supply(
        [arrival(EARLY, "0.3333"), arrival(EARLY, "0.3333"), arrival(LATE, "1.5")], REQUIRED
    )

    assert in_time == D("0.6666")
    assert in_time.as_tuple().exponent == -4
    assert late.as_tuple().exponent == -4
    assert isinstance(in_time, Decimal) and not isinstance(in_time, float)


# -----------------------------------------------------------------------------
# The engine applying it
# -----------------------------------------------------------------------------


async def run(source: FakeSource, demands: list[engine.DemandLine]) -> engine.MrpPlan:
    return await engine.plan(source, demands=demands, as_of=AS_OF, horizon_end=HORIZON_END)


@pytest.mark.asyncio
async def test_an_order_landing_after_the_demand_does_not_erase_the_shortage() -> None:
    """The reproduction, at the level the arithmetic happens.

    100 needed on the 15th, 100 on order for the 16th. Before time phasing this
    reported a shortage of zero and raised nothing.
    """
    screw = make_item("SCREW-M4")
    source = FakeSource(items={screw.id: screw}, incoming={screw.id: (arrival(LATE, "100"),)})

    plan = await run(source, [sales_line(screw, "100", code="SO/0001")])

    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.incoming_quantity == D("0.0000")
    assert requirement.late_incoming_quantity == D("100.0000")
    assert requirement.shortage_quantity == D("100.0000")

    suggestion = plan.suggestion_for(screw.id)
    assert suggestion is not None
    assert suggestion.suggested_quantity == D("100.0000")


@pytest.mark.asyncio
async def test_the_supply_it_did_exclude_is_named_in_a_warning() -> None:
    """Quietly dropping a known inbound order is its own trap.

    The buyer can see the purchase order and the plan can see the shortage; if
    nothing connects the two, the next person to look widens the netting again.
    """
    screw = make_item("SCREW-M4")
    source = FakeSource(items={screw.id: screw}, incoming={screw.id: (arrival(LATE, "100"),)})

    plan = await run(source, [sales_line(screw, "100", code="SO/0001")])

    assert plan.stats["late_supply_item_count"] == 1
    late = [w for w in plan.warnings if "arrives AFTER it is needed" in w]
    assert late, plan.warnings
    assert "SCREW-M4" in late[0]
    assert LATE.isoformat() in late[0]
    assert REQUIRED.isoformat() in late[0]


@pytest.mark.asyncio
async def test_a_run_with_no_late_supply_raises_no_caveat() -> None:
    """The warning has to stay rare enough to be read."""
    screw = make_item("SCREW-M4")
    source = FakeSource(items={screw.id: screw}, incoming={screw.id: (arrival(EARLY, "40"),)})

    plan = await run(source, [sales_line(screw, "100", code="SO/0001")])

    assert plan.stats["late_supply_item_count"] == 0
    assert not [w for w in plan.warnings if "arrives AFTER" in w], plan.warnings
    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.incoming_quantity == D("40.0000")
    assert requirement.shortage_quantity == D("60.0000")


@pytest.mark.asyncio
async def test_a_component_is_phased_to_its_own_date_not_its_parents() -> None:
    """The dependent date is earlier than the parent's - and the netting follows it.

    The kit is needed on the 15th and takes ten days to build, so its screws are
    needed on the 5th. A screw delivery promised for the 10th is in time for the
    *kit's* date and much too late for the screws, and netting it against them
    would leave the build short on the day it starts.
    """
    kit = make_product("LOCK-KIT", lead_time_days=10)
    screw = make_item("SCREW-M4")
    component_date = REQUIRED - timedelta(days=10)
    source = FakeSource(
        items={kit.id: kit, screw.id: screw},
        structure={kit.id: [(screw, "6")]},
        default_bom={kit.id},
        incoming={screw.id: (arrival(component_date + timedelta(days=5), "600"),)},
    )

    plan = await run(source, [sales_line(kit, "100", code="SO/0001")])

    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.earliest_required_date == component_date
    assert requirement.incoming_quantity == D("0.0000")
    assert requirement.late_incoming_quantity == D("600.0000")
    assert requirement.shortage_quantity == D("600.0000")


# -----------------------------------------------------------------------------
# The explosion and the parent's own suggestion have to agree
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_parent_covered_by_incoming_supply_is_not_exploded() -> None:
    """Defect 2: the explosion used the shortfall from *before* supply netting.

    The parent's suggestion is computed after that netting, so a kit fully
    covered by an incoming order got no suggestion of its own and still raised
    demand for every screw in it - components for a build the plan had already
    decided not to raise.
    """
    kit = make_product("LOCK-KIT")
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={kit.id: kit, screw.id: screw},
        structure={kit.id: [(screw, "6")]},
        default_bom={kit.id},
        incoming={kit.id: (arrival(EARLY, "100"),)},
    )

    plan = await run(source, [sales_line(kit, "100", code="SO/0001")])

    assert plan.stats["explosion_count"] == 0, "a covered parent raises no components"
    assert plan.requirement_for(screw.id) is None
    assert plan.suggestion_for(kit.id) is None


@pytest.mark.asyncio
async def test_a_parent_covered_in_part_explodes_only_the_uncovered_half() -> None:
    """A purchase order for the parent removes the parts inside it, pro rata."""
    kit = make_product("LOCK-KIT")
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={kit.id: kit, screw.id: screw},
        structure={kit.id: [(screw, "6")]},
        default_bom={kit.id},
        incoming={kit.id: (arrival(EARLY, "60"),)},
    )

    plan = await run(source, [sales_line(kit, "100", code="SO/0001")])

    assert plan.stats["explosion_count"] == 1
    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.dependent_demand_quantity == D("240.0000"), "six screws for 40 kits"


@pytest.mark.asyncio
async def test_a_live_job_covers_the_parent_and_its_components() -> None:
    """E3a's binding change: production output is dated supply like any other.

    The old engine deliberately exploded the *whole* order when a live job
    covered the parent, on the argument that a released job may hold none of its
    material. E3a binds the other way - net against stock and in-time supply,
    production included, and explode only the net shortfall - so that a plan
    cannot raise a second set of components for work already on the floor. The
    job's own component demand belongs to the job, and a release that cannot
    reserve its material reports its own shortage; planning it twice here would
    buy the same screws twice.
    """
    kit = make_product("LOCK-KIT")
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={kit.id: kit, screw.id: screw},
        structure={kit.id: [(screw, "6")]},
        default_bom={kit.id},
        in_production={kit.id: (arrival(EARLY, "100"),)},
    )

    plan = await run(source, [sales_line(kit, "100", code="SO/0001")])

    kit_requirement = plan.requirement_for(kit.id)
    assert kit_requirement is not None
    assert kit_requirement.in_production_quantity == D("100.0000")
    assert kit_requirement.shortage_quantity == D("0.0000"), "the job covers the kits"
    assert plan.suggestion_for(kit.id) is None

    assert plan.stats["explosion_count"] == 0
    assert plan.requirement_for(screw.id) is None


@pytest.mark.asyncio
async def test_a_live_job_reduces_the_shortfall_and_so_the_explosion() -> None:
    """The same rule mid-range: only the kits the plan has to raise need parts.

    100 kits demanded, 10 already in stock, a live job for 60. The kit's
    shortage is 30 - stock and the job both count - so the plan buys screws for
    the 30 kits it is raising, not for the 60 the job is already building.
    """
    kit = make_product("LOCK-KIT")
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={kit.id: kit, screw.id: screw},
        stock={kit.id: engine.StockPosition(on_hand=D("10"))},
        structure={kit.id: [(screw, "6")]},
        default_bom={kit.id},
        in_production={kit.id: (arrival(EARLY, "60"),)},
    )

    plan = await run(source, [sales_line(kit, "100", code="SO/0001")])

    kit_requirement = plan.requirement_for(kit.id)
    assert kit_requirement is not None
    assert kit_requirement.shortage_quantity == D("30.0000")

    screw_requirement = plan.requirement_for(screw.id)
    assert screw_requirement is not None
    assert screw_requirement.dependent_demand_quantity == D("180.0000"), (
        "six screws for the 30 kits the plan raises; the live job supplies its own"
    )


@pytest.mark.asyncio
async def test_a_parent_covered_only_by_late_supply_is_exploded_in_full() -> None:
    """The other side of the consistency rule.

    Supply that cannot arrive in time does not cover the parent, so it does not
    suppress the components either. Both halves read the same schedule and reach
    the same answer, which is the property that was broken.
    """
    kit = make_product("LOCK-KIT")
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={kit.id: kit, screw.id: screw},
        structure={kit.id: [(screw, "6")]},
        default_bom={kit.id},
        incoming={kit.id: (arrival(LATE, "100"),)},
    )

    plan = await run(source, [sales_line(kit, "100", code="SO/0001")])

    assert plan.stats["explosion_count"] == 1
    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.dependent_demand_quantity == D("600.0000")
    assert plan.suggestion_for(kit.id) is not None


@pytest.mark.asyncio
async def test_supply_for_a_component_is_read_in_the_same_round_as_its_parent() -> None:
    """By the time supply is read the structure is known in full.

    Discovery walks the levels first, so the supply hook is handed the products
    *and* the components they introduced in one call - not one round for the
    parents and a second for whatever the explosions discovered. One call for
    the whole set is what keeps a run at a fixed number of statements however
    deep the structure goes.
    """
    kit = make_product("LOCK-KIT")
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={kit.id: kit, screw.id: screw},
        structure={kit.id: [(screw, "6")]},
        default_bom={kit.id},
        incoming={screw.id: (arrival(EARLY, "200"),)},
    )

    plan = await run(source, [sales_line(kit, "100", code="SO/0001")])

    assert source.calls["incoming_supply"] == 1
    assert source.batch_sizes["incoming_supply"] == [2], "the kit and the screw together"
    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.incoming_quantity == D("200.0000")
    assert requirement.shortage_quantity == D("400.0000")


@pytest.mark.asyncio
async def test_a_manual_demand_line_is_phased_by_its_own_date() -> None:
    """Nothing about the phasing depends on where the demand came from."""
    screw = make_item("SCREW-M4")
    wanted = date(2026, 6, 20)
    source = FakeSource(
        items={screw.id: screw},
        incoming={screw.id: (arrival(date(2026, 6, 25), "500"),)},
    )
    manual = engine.DemandLine(
        item_id=screw.id,
        quantity=D("500"),
        required_date=wanted,
        source=MrpDemandSource.MANUAL,
    )

    plan = await run(source, [manual])

    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.earliest_required_date == wanted
    assert requirement.shortage_quantity == D("500.0000")


@pytest.mark.asyncio
async def test_the_earliest_contributing_demand_sets_the_cut_off() -> None:
    """A merged requirement is planned to its earliest date, phasing included.

    Two orders for the same screw, one needed in June and one in August, are one
    requirement. Supply landing in July covers the August order but not the June
    one - and a merged row cannot be half early and half late, so the earliest
    date governs. That is the conservative reading: the alternative plans the
    June order short.
    """
    screw = make_item("SCREW-M4", production_type=ProductionType.BUY)
    early_demand = date(2026, 6, 10)
    late_demand = date(2026, 8, 10)
    source = FakeSource(
        items={screw.id: screw},
        incoming={screw.id: (arrival(date(2026, 7, 1), "100"),)},
    )

    plan = await run(
        source,
        [
            sales_line(screw, "100", code="SO/0001", required=early_demand),
            sales_line(screw, "100", code="SO/0002", required=late_demand),
        ],
    )

    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.earliest_required_date == early_demand
    assert requirement.gross_requirement == D("200.0000")
    assert requirement.incoming_quantity == D("0.0000")
    assert requirement.late_incoming_quantity == D("100.0000")
    assert requirement.shortage_quantity == D("200.0000")


@pytest.mark.asyncio
async def test_late_supply_behind_a_covered_item_raises_no_caveat() -> None:
    """The caveat is about a shortage that stands, not about every late order.

    Stock covers the demand outright, so the late purchase order changed
    nothing. Warning about it anyway is the noise that trains a planner to skip
    the warnings that matter.
    """
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={screw.id: screw},
        stock={screw.id: engine.StockPosition(on_hand=D("500"))},
        incoming={screw.id: (arrival(LATE, "100"),)},
    )

    plan = await run(source, [sales_line(screw, "100", code="SO/0001")])

    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.shortage_quantity == D("0.0000")
    # The figure is still on the row for anyone who wants it...
    assert requirement.late_incoming_quantity == D("100.0000")
    # ...but the run does not raise a caveat about a plan that is right.
    assert plan.stats["late_supply_item_count"] == 0
    assert not [w for w in plan.warnings if "arrives AFTER" in w], plan.warnings


# =============================================================================
# "Late" is only actionable with "how late" - the date on the requirement row
# =============================================================================
#
# The quantities above answer "is there an order behind this shortage?". They do
# not answer the question a planner asks next, which is whether to expedite that
# order or raise another one - and that turns entirely on when it lands. The
# engine already knew the date: it computed one to write the warning sentence
# and then discarded it. ``Requirement.first_late_supply_date`` is that date,
# kept, so a requirements table can render the row without parsing English out
# of ``MrpRunRead.warnings``.


def test_the_late_date_uses_the_same_boundary_as_the_quantity() -> None:
    """A date drawn from a different rule than the quantity beside it is a lie.

    On the day is in time, so there is no late date. One day later is late, and
    the date is that day.
    """
    assert earliest_late_arrival([arrival(ON_THE_DAY, "100")], REQUIRED) is None
    assert earliest_late_arrival([arrival(LATE, "100")], REQUIRED) == LATE


def test_the_earliest_late_tranche_is_the_one_reported() -> None:
    """Three deliveries, and the soonest relief is the one a planner needs."""
    later = LATE + timedelta(days=30)
    assert (
        earliest_late_arrival(
            [arrival(later, "10"), arrival(LATE, "10"), arrival(EARLY, "10")], REQUIRED
        )
        == LATE
    )


def test_a_summed_late_quantity_is_bounded_by_both_its_dates() -> None:
    """The defect two rounds of fixing relocated rather than removed.

    A late quantity is a SUM over that feed's tranches; its opening date is the
    MINIMUM over the same tranches. So one date beside the sum is a whole fact
    only for a single-tranche feed. With 10 landing in July and 5000 in
    December, "5010 arriving 2026-07-30" reads as nearly here - the direction
    that stops a line, because a planner defers the order.

    Splitting the fields per feed fixed the CROSS-feed case and left this one.
    The interval is what makes the sum honest.
    """
    far = LATE + timedelta(days=120)
    arrivals = [arrival(LATE, "10"), arrival(far, "5000")]

    _in_time, late = split_supply(arrivals, REQUIRED)
    assert late == Decimal("5010.0000")

    assert earliest_late_arrival(arrivals, REQUIRED) == LATE
    assert latest_late_arrival(arrivals, REQUIRED) == far

    split = supply_split(REQUIRED, incoming=arrivals)
    assert split.late_incoming == Decimal("5010.0000")
    assert split.first_late_incoming == LATE
    assert split.last_late_incoming == far, (
        "the whole late quantity must be bounded by the date it is COMPLETE by; "
        "reporting only the earliest date attributes all 5010 to the first "
        "tranche of 10"
    )


def test_a_single_late_tranche_opens_and_closes_on_one_date() -> None:
    """The interval degenerates to a point, which is the honest answer there."""
    split = supply_split(REQUIRED, incoming=[arrival(LATE, "40")])
    assert split.first_late_incoming == LATE
    assert split.last_late_incoming == LATE


def test_each_feed_keeps_its_own_interval() -> None:
    """The cross-feed half, still holding after the within-feed fix."""
    far = LATE + timedelta(days=120)
    split = supply_split(
        REQUIRED,
        incoming=[arrival(far, "5000")],
        in_production=[arrival(LATE, "0.0001")],
    )
    assert split.first_late_incoming == far
    assert split.last_late_incoming == far
    assert split.first_late_in_production == LATE
    assert split.last_late_in_production == LATE


def test_nothing_late_has_no_closing_date_either() -> None:
    assert latest_late_arrival(None, REQUIRED) is None
    assert latest_late_arrival([], REQUIRED) is None
    assert latest_late_arrival([arrival(EARLY, "100")], REQUIRED) is None


def test_nothing_late_has_no_date() -> None:
    assert earliest_late_arrival(None, REQUIRED) is None
    assert earliest_late_arrival([], REQUIRED) is None
    assert earliest_late_arrival([arrival(EARLY, "100")], REQUIRED) is None


def test_a_zero_tranche_does_not_supply_the_date_either() -> None:
    """The same rows the quantity ignores.

    Otherwise a row would carry a date with no quantity beside it, which is the
    half-fact this pair of columns exists to avoid.
    """
    assert earliest_late_arrival([arrival(LATE, "0")], REQUIRED) is None


@pytest.mark.asyncio
async def test_the_requirement_carries_the_date_the_late_order_lands() -> None:
    """The whole point: the row says 100 arriving, and says when."""
    screw = make_item("SCREW-M4")
    source = FakeSource(items={screw.id: screw}, incoming={screw.id: (arrival(LATE, "100"),)})

    plan = await run(source, [sales_line(screw, "100", code="SO/0001")])

    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.late_incoming_quantity == D("100.0000")
    assert requirement.first_late_supply_date == LATE
    assert requirement.first_late_supply_date > requirement.earliest_required_date


@pytest.mark.asyncio
async def test_supply_that_arrives_in_time_leaves_the_row_with_no_late_date() -> None:
    """Zero late, and no date - not a sentinel standing in for one."""
    screw = make_item("SCREW-M4")
    source = FakeSource(items={screw.id: screw}, incoming={screw.id: (arrival(EARLY, "40"),)})

    plan = await run(source, [sales_line(screw, "100", code="SO/0001")])

    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.incoming_quantity == D("40.0000")
    assert requirement.late_incoming_quantity == D("0.0000")
    assert requirement.late_in_production_quantity == D("0.0000")
    assert requirement.first_late_supply_date is None


@pytest.mark.asyncio
async def test_an_item_with_no_supply_at_all_has_no_late_date() -> None:
    """A shortage with nothing behind it is the ordinary case, and reads as one."""
    screw = make_item("SCREW-M4")
    source = FakeSource(items={screw.id: screw})

    plan = await run(source, [sales_line(screw, "100", code="SO/0001")])

    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.shortage_quantity == D("100.0000")
    assert requirement.first_late_supply_date is None


@pytest.mark.asyncio
async def test_the_date_is_the_earliest_across_both_supply_feeds() -> None:
    """A job finishing before the late purchase order lands is the earlier relief.

    Reporting the purchase order date because the incoming feed happened to be
    looked at first would tell a planner to wait three weeks longer than needed.
    """
    frame = make_item("FRAME")
    job_lands = LATE + timedelta(days=2)
    po_lands = LATE + timedelta(days=20)
    source = FakeSource(
        items={frame.id: frame},
        incoming={frame.id: (arrival(po_lands, "60"),)},
        in_production={frame.id: (arrival(job_lands, "10"),)},
    )

    plan = await run(source, [sales_line(frame, "100", code="SO/0001")])

    requirement = plan.requirement_for(frame.id)
    assert requirement is not None
    assert requirement.late_incoming_quantity == D("60.0000")
    assert requirement.late_in_production_quantity == D("10.0000")
    assert requirement.first_late_supply_date == job_lands


@pytest.mark.asyncio
async def test_a_component_dates_its_late_supply_against_its_own_needed_by() -> None:
    """The date is judged at the component date, not the parent one.

    The kit is needed on the 15th and takes ten days to build, so the screws are
    needed on the 5th. A screw delivery on the 10th is in time for the kit and
    five days late for the screws - and the row has to say the 10th, because
    that is when the screws land.
    """
    kit = make_product("LOCK-KIT", lead_time_days=10)
    screw = make_item("SCREW-M4")
    component_date = REQUIRED - timedelta(days=10)
    screws_land = component_date + timedelta(days=5)
    source = FakeSource(
        items={kit.id: kit, screw.id: screw},
        structure={kit.id: [(screw, "6")]},
        default_bom={kit.id},
        incoming={screw.id: (arrival(screws_land, "600"),)},
    )

    plan = await run(source, [sales_line(kit, "100", code="SO/0001")])

    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.earliest_required_date == component_date
    assert requirement.first_late_supply_date == screws_land


@pytest.mark.asyncio
async def test_the_row_figures_and_the_run_count_are_the_same_statement() -> None:
    """``late_supply_item_count`` is the *short* rows carrying late supply.

    Two items with a late order behind them, one of them covered by stock. The
    stat counts the one whose shortage stands; both rows keep their figures. A
    client deriving the count from the rows has to reach the same answer, or the
    API is telling a planner two different things.
    """
    short = make_item("SHORT-ITEM")
    covered = make_item("COVERED-ITEM")
    source = FakeSource(
        items={short.id: short, covered.id: covered},
        stock={covered.id: engine.StockPosition(on_hand=D("500"))},
        incoming={
            short.id: (arrival(LATE, "100"),),
            covered.id: (arrival(LATE, "100"),),
        },
    )

    plan = await run(
        source,
        [
            sales_line(short, "100", code="SO/0001"),
            sales_line(covered, "100", code="SO/0002"),
        ],
    )

    from_rows = [
        r
        for r in plan.requirements
        if r.shortage_quantity > 0 and r.late_incoming_quantity + r.late_in_production_quantity > 0
    ]
    assert plan.stats["late_supply_item_count"] == len(from_rows) == 1
    assert from_rows[0].item_code == "SHORT-ITEM"
    # ...and the covered row still carries its own dated figure.
    covered_row = plan.requirement_for(covered.id)
    assert covered_row is not None
    assert covered_row.late_incoming_quantity == D("100.0000")
    assert covered_row.first_late_supply_date == LATE


@pytest.mark.asyncio
async def test_the_warning_text_and_the_row_name_the_same_date() -> None:
    """Two statements of one fact that must not be able to drift apart.

    The warning is built from the same ``SupplySplit`` the row is, so this holds
    by construction rather than by two callers remembering the same rule.
    """
    screw = make_item("SCREW-M4")
    source = FakeSource(items={screw.id: screw}, incoming={screw.id: (arrival(LATE, "100"),)})

    plan = await run(source, [sales_line(screw, "100", code="SO/0001")])

    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.first_late_supply_date is not None
    late = [w for w in plan.warnings if "arrives AFTER it is needed" in w]
    assert late, plan.warnings
    assert requirement.first_late_supply_date.isoformat() in late[0]


# =============================================================================
# One date per FEED - a quantity and a date that describe different orders are
# not one fact
# =============================================================================
#
# The reproduction, in the reviewer's own figures: BOLT-M8 is needed on the
# 15th; a purchase order for 5 000 is due on 2026-11-12 and a released job for
# 0.0001 is due on the 16th. With one date per row the plan said
#
#     late_incoming_quantity      = 5000.0000
#     late_in_production_quantity =    0.0001
#     first_late_supply_date      = 2026-07-16
#
# and a table rendering the first and the third read "5000 arriving, one day
# late" for an order that lands four months later. That is worse than the
# warning prose the columns replaced, because it looks structured: the fields
# exist so a client does not have to parse English, so they have to be
# individually true.

FOUR_MONTHS_LATE = date(2026, 11, 12)
ONE_DAY_LATE = date(2026, 7, 16)


def test_each_feed_dates_its_own_excluded_tranche() -> None:
    """The split, where the pairing is made. Two feeds, two dates."""
    split = engine.supply_split(
        REQUIRED,
        incoming=[arrival(FOUR_MONTHS_LATE, "5000")],
        in_production=[arrival(ONE_DAY_LATE, "0.0001")],
    )

    assert split.late_incoming == D("5000.0000")
    assert split.first_late_incoming == FOUR_MONTHS_LATE
    assert split.late_in_production == D("0.0001")
    assert split.first_late_in_production == ONE_DAY_LATE
    # The cross-feed summary still exists and still means what it always did.
    assert split.first_late_arrival == ONE_DAY_LATE


def test_a_feed_with_nothing_late_has_no_date_of_its_own() -> None:
    """None means this feed excluded nothing, not that nobody looked."""
    split = engine.supply_split(
        REQUIRED,
        incoming=[arrival(EARLY, "40"), arrival(LATE, "60")],
        in_production=[arrival(ON_THE_DAY, "25")],
    )

    assert split.late_incoming == D("60.0000")
    assert split.first_late_incoming == LATE
    assert split.late_in_production == D("0.0000")
    assert split.first_late_in_production is None, "everything being made is in time"
    assert split.first_late_arrival == LATE


def test_the_summary_date_cannot_be_set_apart_from_the_two_it_summarises() -> None:
    """It is derived, so there is no way to state a third, disagreeing answer."""
    with pytest.raises(TypeError):
        engine.SupplySplit(needed_by=REQUIRED, first_late_arrival=LATE)  # type: ignore[call-arg]

    assert engine.SupplySplit(needed_by=REQUIRED).first_late_arrival is None


@pytest.mark.asyncio
async def test_the_reproduction_each_quantity_carries_its_own_date() -> None:
    """5 000 arriving 2026-11-12 and 0.0001 arriving 2026-07-16, as two facts."""
    bolt = make_item("BOLT-M8")
    source = FakeSource(
        items={bolt.id: bolt},
        incoming={bolt.id: (arrival(FOUR_MONTHS_LATE, "5000"),)},
        in_production={bolt.id: (arrival(ONE_DAY_LATE, "0.0001"),)},
    )

    plan = await run(source, [sales_line(bolt, "100", code="SO/0001")])

    requirement = plan.requirement_for(bolt.id)
    assert requirement is not None
    assert requirement.earliest_required_date == REQUIRED
    assert requirement.late_incoming_quantity == D("5000.0000")
    assert requirement.first_late_incoming_date == FOUR_MONTHS_LATE, (
        "the 5 000 land in November, whatever the job beside them does"
    )
    assert requirement.late_in_production_quantity == D("0.0001")
    assert requirement.first_late_in_production_date == ONE_DAY_LATE
    # The row-level summary is the earlier of the two and says only "the
    # soonest anything lands". It is not either quantity's date.
    assert requirement.first_late_supply_date == ONE_DAY_LATE
    # Both dates are still dates, and both are genuinely after the demand.
    for when in (
        requirement.first_late_incoming_date,
        requirement.first_late_in_production_date,
    ):
        assert when is not None and when > requirement.earliest_required_date


@pytest.mark.asyncio
async def test_a_row_whose_late_supply_is_all_on_order_dates_only_that_feed() -> None:
    """One feed late, one feed silent - and the silence reads as None."""
    screw = make_item("SCREW-M4")
    source = FakeSource(
        items={screw.id: screw},
        incoming={screw.id: (arrival(LATE, "100"),)},
        in_production={screw.id: (arrival(EARLY, "10"),)},
    )

    plan = await run(source, [sales_line(screw, "100", code="SO/0001")])

    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.in_production_quantity == D("10.0000"), "the job is in time"
    assert requirement.late_in_production_quantity == D("0.0000")
    assert requirement.first_late_in_production_date is None
    assert requirement.late_incoming_quantity == D("100.0000")
    assert requirement.first_late_incoming_date == LATE
    assert requirement.first_late_supply_date == LATE


@pytest.mark.asyncio
async def test_a_row_with_nothing_late_dates_neither_feed() -> None:
    screw = make_item("SCREW-M4")
    source = FakeSource(items={screw.id: screw}, incoming={screw.id: (arrival(EARLY, "40"),)})

    plan = await run(source, [sales_line(screw, "100", code="SO/0001")])

    requirement = plan.requirement_for(screw.id)
    assert requirement is not None
    assert requirement.first_late_incoming_date is None
    assert requirement.first_late_in_production_date is None
    assert requirement.first_late_supply_date is None


@pytest.mark.asyncio
async def test_the_warning_names_each_feed_with_its_own_date() -> None:
    """The run-level prose and the per-feed columns state the same two facts.

    Two descriptions of one fact that disagree is the defect this section
    exists for, one level up: the warning used to print the *sum* of both feeds
    beside the *earliest* date of either, which is the same false pairing the
    columns had.
    """
    bolt = make_item("BOLT-M8")
    source = FakeSource(
        items={bolt.id: bolt},
        incoming={bolt.id: (arrival(FOUR_MONTHS_LATE, "5000"),)},
        in_production={bolt.id: (arrival(ONE_DAY_LATE, "0.0001"),)},
    )

    plan = await run(source, [sales_line(bolt, "100", code="SO/0001")])

    requirement = plan.requirement_for(bolt.id)
    assert requirement is not None
    late = [w for w in plan.warnings if "arrives AFTER it is needed" in w]
    assert late, plan.warnings
    text = late[0]

    assert (
        f"{requirement.late_incoming_quantity} on order arriving "
        f"{FOUR_MONTHS_LATE.isoformat()}" in text
    )
    assert (
        f"{requirement.late_in_production_quantity} in production arriving "
        f"{ONE_DAY_LATE.isoformat()}" in text
    )
    assert REQUIRED.isoformat() in text, "...and when it was needed"
    # The pairing that was wrong: the big quantity beside the near date.
    assert f"5000.0000 on order arriving {ONE_DAY_LATE.isoformat()}" not in text
    assert "5000.0001" not in text, "the two feeds are not summed into one tranche"


@pytest.mark.asyncio
async def test_the_warning_names_only_the_feed_that_is_actually_late() -> None:
    """A feed with nothing excluded is not listed as arriving on any date."""
    screw = make_item("SCREW-M4")
    source = FakeSource(items={screw.id: screw}, incoming={screw.id: (arrival(LATE, "100"),)})

    plan = await run(source, [sales_line(screw, "100", code="SO/0001")])

    late = [w for w in plan.warnings if "arrives AFTER it is needed" in w]
    assert late, plan.warnings
    assert f"100.0000 on order arriving {LATE.isoformat()}" in late[0]
    assert "in production" not in late[0], "nothing is being made late; do not say so"
