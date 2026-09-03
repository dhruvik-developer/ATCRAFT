"""Multi-level BOM explosion.

Everything here runs against an in-memory :class:`FakeSource` rather than a
database. That is not a shortcut - it is what makes the two properties the
engine exists for actually testable:

* the arithmetic (consolidation, scrap, yield division) can be asserted exactly,
* and the *shape of the loading* can be asserted at all. ``FakeSource`` counts
  its calls, so "loads one batch per level, never one query per node" is a
  failing test rather than a comment somebody has to keep honouring.

The worked structure is the one from the master rules:

    Premium Lock Kit
      Rose Handle   -> Handle Body, Bush, Screw (2)
      Lock Body     -> Lock Case, Spring, Pin, Screw (4)
      Cylinder
"""

from __future__ import annotations

import itertools
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, cast

import pytest

from src.core.errors import AppError, BomCycleError, BusinessRuleError, ErrorCode
from src.modules.bom import engine
from src.modules.bom.models import BomStatus, BomType
from src.modules.bom.repository import ItemInfo

AS_OF = date(2026, 8, 31)


# -----------------------------------------------------------------------------
# Test doubles
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeBom:
    """The BOM header fields the traversal reads.

    A plain dataclass rather than the ORM model, matching the convention the
    rest of the unit suite uses: the engine is pure logic over these attributes
    and must be testable without configuring a mapper registry.
    """

    id: uuid.UUID
    code: str
    item_id: uuid.UUID
    output_quantity: Decimal = Decimal("1")
    bom_type: str = BomType.PRODUCTION
    status: str = BomStatus.APPROVED
    version: int = 1
    is_default: bool = True


@dataclass(frozen=True)
class FakeLine:
    bom_id: uuid.UUID
    line_number: int
    component_item_id: uuid.UUID
    quantity_per: Decimal
    scrap_percentage: Decimal = Decimal("0")
    is_optional: bool = False
    substitute_for_line_id: uuid.UUID | None = None


@dataclass(frozen=True)
class FakeOperation:
    setup_time_minutes: Decimal = Decimal("0")
    run_time_minutes_per_unit: Decimal = Decimal("0")
    cost_per_hour: Decimal = Decimal("0")
    fixed_cost: Decimal = Decimal("0")


@dataclass
class FakeSource:
    """In-memory :class:`~src.modules.bom.engine.BomDataSource`.

    ``calls`` counts round-trips and ``batch_sizes`` records how many ids each
    call was given, which is what turns "no N+1" into an assertion rather than a
    comment somebody has to keep honouring.
    """

    boms_by_item: dict[uuid.UUID, FakeBom]
    lines_by_bom: dict[uuid.UUID, list[FakeLine]]
    item_info: dict[uuid.UUID, ItemInfo]
    operations_by_bom: dict[uuid.UUID, list[FakeOperation]] = field(default_factory=dict)
    prices: dict[uuid.UUID, Decimal] = field(default_factory=dict)
    calls: Counter[str] = field(default_factory=Counter)
    batch_sizes: dict[str, list[int]] = field(default_factory=dict)

    def _record(self, name: str, size: int) -> None:
        self.calls[name] += 1
        self.batch_sizes.setdefault(name, []).append(size)

    async def effective_boms(self, item_ids: Any, as_of: date) -> dict[uuid.UUID, Any]:
        ids = list(item_ids)
        self._record("effective_boms", len(ids))
        return {i: self.boms_by_item[i] for i in ids if i in self.boms_by_item}

    async def lines_for_boms(self, bom_ids: Any) -> dict[uuid.UUID, list[Any]]:
        ids = list(bom_ids)
        self._record("lines_for_boms", len(ids))
        return {i: list(self.lines_by_bom.get(i, [])) for i in ids}

    async def items(self, item_ids: Any) -> dict[uuid.UUID, ItemInfo]:
        ids = list(item_ids)
        self._record("items", len(ids))
        return {i: self.item_info[i] for i in ids if i in self.item_info}

    async def operations_for_boms(self, bom_ids: Any) -> dict[uuid.UUID, list[Any]]:
        ids = list(bom_ids)
        self._record("operations_for_boms", len(ids))
        return {i: list(self.operations_by_bom.get(i, [])) for i in ids}

    async def last_purchase_prices(self, item_ids: Any) -> dict[uuid.UUID, Decimal]:
        ids = list(item_ids)
        self._record("last_purchase_prices", len(ids))
        return {i: self.prices[i] for i in ids if i in self.prices}


class Structure:
    """Fluent builder for a product structure under test."""

    def __init__(self) -> None:
        self.item_ids: dict[str, uuid.UUID] = {}
        self.boms_by_item: dict[uuid.UUID, FakeBom] = {}
        self.lines_by_bom: dict[uuid.UUID, list[FakeLine]] = {}
        self.item_info: dict[uuid.UUID, ItemInfo] = {}
        self.operations_by_bom: dict[uuid.UUID, list[FakeOperation]] = {}
        self.prices: dict[uuid.UUID, Decimal] = {}

    def item(
        self,
        code: str,
        *,
        purchased: bool = True,
        manufactured: bool = False,
        standard_cost: Decimal = Decimal("0"),
    ) -> uuid.UUID:
        item_id = self.item_ids.setdefault(code, uuid.uuid4())
        self.item_info[item_id] = ItemInfo(
            id=item_id,
            code=code,
            name=f"{code} name",
            base_uom_id=uuid.uuid4(),
            standard_cost=standard_cost,
            production_type="MAKE" if manufactured else "BUY",
            can_purchase=purchased,
            can_manufacture=manufactured,
            can_be_bom_component=True,
            is_active=True,
        )
        return item_id

    def bom(
        self,
        parent_code: str,
        components: dict[str, Decimal | tuple[Decimal, Decimal]],
        *,
        output_quantity: Decimal = Decimal("1"),
        bom_type: str = BomType.PRODUCTION,
        optional: frozenset[str] = frozenset(),
        substitutes: dict[str, str] | None = None,
        operations: list[FakeOperation] | None = None,
    ) -> FakeBom:
        """Define (or replace) the structure for ``parent_code``.

        ``components`` maps a component code to its per-run quantity, or to a
        ``(quantity, scrap_percentage)`` pair. ``substitutes`` maps a component
        code to the code of the line it may replace.
        """
        parent = self.item_ids.get(parent_code) or self.item(parent_code, manufactured=True)
        bom = FakeBom(
            id=uuid.uuid4(),
            code=f"BOM/{parent_code}",
            item_id=parent,
            output_quantity=output_quantity,
            bom_type=bom_type,
        )
        self.boms_by_item[parent] = bom

        substitutes = substitutes or {}
        line_id_by_code = {code: uuid.uuid4() for code in components}
        lines: list[FakeLine] = []
        for number, (code, spec) in enumerate(components.items(), start=1):
            quantity, scrap = spec if isinstance(spec, tuple) else (spec, Decimal("0"))
            component = self.item_ids.get(code) or self.item(code)
            replaces = substitutes.get(code)
            lines.append(
                FakeLine(
                    bom_id=bom.id,
                    line_number=number,
                    component_item_id=component,
                    quantity_per=quantity,
                    scrap_percentage=scrap,
                    is_optional=code in optional,
                    substitute_for_line_id=line_id_by_code[replaces] if replaces else None,
                )
            )
        self.lines_by_bom[bom.id] = lines
        if operations:
            self.operations_by_bom[bom.id] = operations
        return bom

    def code_of(self, item_id: uuid.UUID) -> str:
        return self.item_info[item_id].code

    def source(self) -> FakeSource:
        return FakeSource(
            boms_by_item=self.boms_by_item,
            lines_by_bom=self.lines_by_bom,
            item_info=self.item_info,
            operations_by_bom=self.operations_by_bom,
            prices=self.prices,
        )


def lock_kit() -> Structure:
    """The master-rules worked example."""
    structure = Structure()
    structure.item("KIT", manufactured=True, purchased=False)
    structure.bom(
        "KIT",
        {"ROSE-HANDLE": Decimal("1"), "LOCK-BODY": Decimal("1"), "CYLINDER": Decimal("1")},
    )
    structure.item("ROSE-HANDLE", manufactured=True, purchased=False)
    structure.bom(
        "ROSE-HANDLE",
        {"HANDLE-BODY": Decimal("1"), "BUSH": Decimal("2"), "SCREW-M4": Decimal("2")},
    )
    structure.item("LOCK-BODY", manufactured=True, purchased=False)
    structure.bom(
        "LOCK-BODY",
        {
            "LOCK-CASE": Decimal("1"),
            "SPRING": Decimal("1"),
            "PIN": Decimal("3"),
            "SCREW-M4": Decimal("4"),
        },
    )
    return structure


async def explode(
    structure: Structure,
    code: str,
    quantity: Decimal,
    *,
    source: FakeSource | None = None,
    **kwargs: Any,
) -> engine.BomExplosion:
    data = source or structure.source()
    return await engine.explode(
        cast(Any, None),
        cast(Any, None),
        structure.item_ids[code],
        quantity,
        as_of=AS_OF,
        source=cast(Any, data),
        **kwargs,
    )


def quantity_of(result: engine.BomExplosion, code: str) -> Decimal:
    component = next(c for c in result.components if c.item_code == code)
    return component.scrap_adjusted_quantity


# -----------------------------------------------------------------------------
# Consolidation - the behaviour the engine exists for
# -----------------------------------------------------------------------------


async def test_a_common_part_is_one_row_with_the_summed_quantity() -> None:
    """The example from database.md section 5, verbatim.

    SCREW-M4 is 2 per Rose Handle and 4 per Lock Body. 100 kits must produce
    ONE row for 600, not two rows a buyer has to add up.
    """
    result = await explode(lock_kit(), "KIT", Decimal("100"))

    screws = [c for c in result.components if c.item_code == "SCREW-M4"]
    assert len(screws) == 1
    assert screws[0].scrap_adjusted_quantity == Decimal("600")


async def test_the_merge_retains_the_branch_each_contribution_came_from() -> None:
    result = await explode(lock_kit(), "KIT", Decimal("100"))
    screw = next(c for c in result.components if c.item_code == "SCREW-M4")

    assert screw.source_path_count == 2
    assert set(screw.source_paths) == {
        ("KIT", "ROSE-HANDLE", "SCREW-M4"),
        ("KIT", "LOCK-BODY", "SCREW-M4"),
    }


async def test_every_part_of_the_worked_example_is_reported_once() -> None:
    result = await explode(lock_kit(), "KIT", Decimal("100"))

    codes = [c.item_code for c in result.components]
    assert sorted(codes) == [
        "BUSH",
        "CYLINDER",
        "HANDLE-BODY",
        "LOCK-BODY",
        "LOCK-CASE",
        "PIN",
        "ROSE-HANDLE",
        "SCREW-M4",
        "SPRING",
    ]
    # No duplicates anywhere, not only for the shared screw.
    assert len(codes) == len(set(codes))


async def test_sub_assemblies_are_requirements_in_their_own_right() -> None:
    """A sub-assembly is a real thing that must be made or stocked, so it is a
    line as well as a level."""
    result = await explode(lock_kit(), "KIT", Decimal("100"))

    rose = next(c for c in result.components if c.item_code == "ROSE-HANDLE")
    assert rose.level == 1
    assert rose.scrap_adjusted_quantity == Decimal("100")
    assert rose.is_manufactured is True
    assert next(c for c in result.components if c.item_code == "BUSH").level == 2


async def test_the_root_item_is_never_a_requirement_for_itself() -> None:
    result = await explode(lock_kit(), "KIT", Decimal("100"))
    assert all(c.item_code != "KIT" for c in result.components)


async def test_quantities_multiply_through_the_levels() -> None:
    structure = lock_kit()
    # Two rose handles per kit rather than one.
    structure.bom(
        "KIT",
        {"ROSE-HANDLE": Decimal("2"), "LOCK-BODY": Decimal("1"), "CYLINDER": Decimal("1")},
    )
    result = await explode(structure, "KIT", Decimal("100"))

    assert quantity_of(result, "ROSE-HANDLE") == Decimal("200")
    assert quantity_of(result, "BUSH") == Decimal("400")
    # 2 handles x 2 screws + 1 body x 4 screws, per kit.
    assert quantity_of(result, "SCREW-M4") == Decimal("800")


async def test_the_shallowest_level_wins_when_a_part_appears_twice() -> None:
    """A planner looks for a part where it is easiest to see it."""
    structure = lock_kit()
    structure.bom(
        "KIT",
        {
            "ROSE-HANDLE": Decimal("1"),
            "LOCK-BODY": Decimal("1"),
            "CYLINDER": Decimal("1"),
            "SCREW-M4": Decimal("1"),
        },
    )
    result = await explode(structure, "KIT", Decimal("10"))

    screw = next(c for c in result.components if c.item_code == "SCREW-M4")
    assert screw.level == 1
    assert screw.source_path_count == 3
    assert screw.scrap_adjusted_quantity == Decimal("70")  # 10 direct + 20 + 40


# -----------------------------------------------------------------------------
# Yield and scrap
# -----------------------------------------------------------------------------


async def test_output_quantity_divides_the_line_quantity() -> None:
    """A structure that yields 10 handles from 1 billet needs 0.1 billets each."""
    structure = Structure()
    structure.item("HANDLE", manufactured=True, purchased=False)
    structure.bom("HANDLE", {"BILLET": Decimal("1")}, output_quantity=Decimal("10"))

    result = await explode(structure, "HANDLE", Decimal("250"))
    assert quantity_of(result, "BILLET") == Decimal("25")


async def test_a_non_terminating_yield_does_not_drift() -> None:
    """3 pieces per run: 300 pieces is exactly 100 runs, not 99.99."""
    structure = Structure()
    structure.item("BRACKET", manufactured=True, purchased=False)
    structure.bom("BRACKET", {"BAR": Decimal("1")}, output_quantity=Decimal("3"))

    result = await explode(structure, "BRACKET", Decimal("300"))
    assert quantity_of(result, "BAR") == Decimal("100")


def test_apply_scrap_is_the_documented_formula() -> None:
    assert engine.apply_scrap(Decimal("100"), Decimal("5")) == Decimal("105")
    assert engine.apply_scrap(Decimal("100"), Decimal("2.5")) == Decimal("102.5")
    # Zero scrap is the identity, still quantised to the stock scale.
    assert engine.apply_scrap(Decimal("7"), Decimal("0")) == Decimal("7.0000")


def test_apply_scrap_refuses_a_negative_allowance() -> None:
    # A negative allowance would quietly under-plan every job that used it.
    with pytest.raises(ValueError, match="negative"):
        engine.apply_scrap(Decimal("100"), Decimal("-5"))


def test_line_requirement_refuses_a_zero_yield() -> None:
    with pytest.raises(ValueError, match="positive"):
        engine.line_requirement(Decimal("1"), Decimal("1"), Decimal("0"))


async def test_scrap_inflates_the_planned_quantity_but_not_the_net() -> None:
    structure = Structure()
    structure.item("PLATE", manufactured=True, purchased=False)
    structure.bom("PLATE", {"SHEET": (Decimal("2"), Decimal("5"))})

    result = await explode(structure, "PLATE", Decimal("100"))
    sheet = next(c for c in result.components if c.item_code == "SHEET")

    assert sheet.total_quantity == Decimal("200")
    assert sheet.scrap_adjusted_quantity == Decimal("210")


async def test_scrap_compounds_down_the_structure() -> None:
    """The scrapped sub-assemblies have to be built too, so their components
    scale with the inflated figure, not the net one."""
    structure = Structure()
    structure.item("ASSY", manufactured=True, purchased=False)
    structure.bom("ASSY", {"SUB": (Decimal("1"), Decimal("10"))})
    structure.item("SUB", manufactured=True, purchased=False)
    structure.bom("SUB", {"PART": (Decimal("1"), Decimal("10"))})

    result = await explode(structure, "ASSY", Decimal("100"))

    assert quantity_of(result, "SUB") == Decimal("110")
    assert quantity_of(result, "PART") == Decimal("121")


# -----------------------------------------------------------------------------
# Optional lines and substitutes
# -----------------------------------------------------------------------------


async def test_an_optional_component_creates_no_demand() -> None:
    structure = Structure()
    structure.item("DOOR-SET", manufactured=True, purchased=False)
    structure.bom(
        "DOOR-SET",
        {"HINGE": Decimal("3"), "ENGRAVING-PLATE": Decimal("1")},
        optional=frozenset({"ENGRAVING-PLATE"}),
    )

    result = await explode(structure, "DOOR-SET", Decimal("50"))
    assert [c.item_code for c in result.components] == ["HINGE"]


async def test_a_substitute_line_is_not_counted_alongside_what_it_replaces() -> None:
    """Counting both would double the requirement for a part that is only ever
    consumed one way or the other."""
    structure = Structure()
    structure.item("LATCH", manufactured=True, purchased=False)
    structure.bom(
        "LATCH",
        {"SPRING-A": Decimal("1"), "SPRING-B": Decimal("1")},
        substitutes={"SPRING-B": "SPRING-A"},
    )

    result = await explode(structure, "LATCH", Decimal("10"))
    assert [c.item_code for c in result.components] == ["SPRING-A"]
    assert quantity_of(result, "SPRING-A") == Decimal("10")


# -----------------------------------------------------------------------------
# Phantoms
# -----------------------------------------------------------------------------


async def test_a_phantom_is_blown_through_and_raises_no_requirement_itself() -> None:
    structure = Structure()
    structure.item("KIT", manufactured=True, purchased=False)
    structure.bom("KIT", {"PHANTOM-GRP": Decimal("2"), "CYLINDER": Decimal("1")})
    structure.item("PHANTOM-GRP", manufactured=True, purchased=False)
    structure.bom(
        "PHANTOM-GRP",
        {"SCREW-M4": Decimal("3"), "WASHER": Decimal("3")},
        bom_type=BomType.PHANTOM,
    )

    result = await explode(structure, "KIT", Decimal("10"))

    codes = {c.item_code for c in result.components}
    assert "PHANTOM-GRP" not in codes
    assert result.phantom_item_codes == ("PHANTOM-GRP",)
    # Children still scale by the phantom's quantity: 10 kits x 2 groups x 3.
    assert quantity_of(result, "SCREW-M4") == Decimal("60")
    assert quantity_of(result, "WASHER") == Decimal("60")


async def test_a_phantoms_children_are_reported_below_it() -> None:
    structure = Structure()
    structure.item("KIT", manufactured=True, purchased=False)
    structure.bom("KIT", {"PHANTOM-GRP": Decimal("1")})
    structure.item("PHANTOM-GRP", manufactured=True, purchased=False)
    structure.bom("PHANTOM-GRP", {"SCREW-M4": Decimal("1")}, bom_type=BomType.PHANTOM)

    result = await explode(structure, "KIT", Decimal("1"))
    screw = next(c for c in result.components if c.item_code == "SCREW-M4")
    assert screw.level == 2
    assert screw.source_paths == (("KIT", "PHANTOM-GRP", "SCREW-M4"),)


# -----------------------------------------------------------------------------
# Cycles and depth
# -----------------------------------------------------------------------------


async def test_a_component_that_reaches_its_own_ancestor_raises_with_the_path() -> None:
    structure = Structure()
    structure.item("A", manufactured=True, purchased=False)
    structure.item("B", manufactured=True, purchased=False)
    structure.bom("A", {"B": Decimal("1")})
    structure.bom("B", {"A": Decimal("1")})

    with pytest.raises(BomCycleError) as caught:
        await explode(structure, "A", Decimal("1"))

    assert caught.value.details["cycle_path"] == ["A", "B", "A"]
    assert caught.value.code is ErrorCode.BOM_CYCLE_DETECTED


async def test_a_cycle_several_levels_down_is_still_caught() -> None:
    structure = Structure()
    for code in ("A", "B", "C"):
        structure.item(code, manufactured=True, purchased=False)
    structure.bom("A", {"B": Decimal("1")})
    structure.bom("B", {"C": Decimal("1")})
    structure.bom("C", {"B": Decimal("1")})

    with pytest.raises(BomCycleError) as caught:
        await explode(structure, "A", Decimal("1"))
    assert caught.value.details["cycle_path"] == ["A", "B", "C", "B"]


async def test_a_cycle_is_detected_before_the_traversal_runs_away() -> None:
    """Detection happens on the edge, not after building a graph - so a cycle
    costs one level of work rather than max_depth levels."""
    structure = Structure()
    structure.item("A", manufactured=True, purchased=False)
    structure.item("B", manufactured=True, purchased=False)
    structure.bom("A", {"B": Decimal("1")})
    structure.bom("B", {"A": Decimal("1")})
    source = structure.source()

    with pytest.raises(BomCycleError):
        await explode(structure, "A", Decimal("1"), source=source, max_depth=20)

    assert source.calls["lines_for_boms"] <= 2


async def test_a_diamond_is_not_mistaken_for_a_cycle() -> None:
    """Two branches reaching the same part is normal; only reaching an *ancestor*
    is a loop."""
    structure = Structure()
    structure.item("TOP", manufactured=True, purchased=False)
    structure.bom("TOP", {"LEFT": Decimal("1"), "RIGHT": Decimal("1")})
    structure.item("LEFT", manufactured=True, purchased=False)
    structure.item("RIGHT", manufactured=True, purchased=False)
    structure.bom("LEFT", {"SHARED": Decimal("2")})
    structure.bom("RIGHT", {"SHARED": Decimal("3")})

    result = await explode(structure, "TOP", Decimal("10"))
    assert quantity_of(result, "SHARED") == Decimal("50")


async def test_beyond_max_depth_raises_bom_depth_exceeded() -> None:
    structure = Structure()
    chain = ["L0", "L1", "L2", "L3"]
    for code in chain:
        structure.item(code, manufactured=True, purchased=False)
    for parent, child in itertools.pairwise(chain):
        structure.bom(parent, {child: Decimal("1")})

    with pytest.raises(engine.BomDepthExceededError) as caught:
        await explode(structure, "L0", Decimal("1"), max_depth=2)

    assert caught.value.code is ErrorCode.BOM_DEPTH_EXCEEDED
    assert caught.value.details["max_depth"] == 2
    assert caught.value.details["path"][:2] == ["L0", "L1"]


async def test_a_structure_exactly_at_the_depth_limit_is_allowed() -> None:
    structure = Structure()
    chain = ["L0", "L1", "L2"]
    for code in chain:
        structure.item(code, manufactured=True, purchased=False)
    structure.bom("L0", {"L1": Decimal("1")})
    structure.bom("L1", {"L2": Decimal("1")})

    result = await explode(structure, "L0", Decimal("1"), max_depth=2)
    assert result.max_level_reached == 2


# -----------------------------------------------------------------------------
# Loading shape - the N+1 guard
# -----------------------------------------------------------------------------


def wide_structure(sub_assemblies: int, parts_each: int) -> Structure:
    """A root over ``sub_assemblies`` sub-assemblies of ``parts_each`` parts."""
    structure = Structure()
    structure.item("ROOT", manufactured=True, purchased=False)
    subs = {f"SUB-{i}": Decimal("1") for i in range(sub_assemblies)}
    structure.bom("ROOT", subs)
    for i in range(sub_assemblies):
        structure.item(f"SUB-{i}", manufactured=True, purchased=False)
        structure.bom(f"SUB-{i}", {f"PART-{i}-{j}": Decimal("1") for j in range(parts_each)})
    return structure


async def test_each_level_is_loaded_in_one_batch_not_one_query_per_node() -> None:
    structure = wide_structure(sub_assemblies=5, parts_each=10)
    source = structure.source()

    await explode(structure, "ROOT", Decimal("1"), source=source)

    # Three levels (root, subs, parts). One structure lookup per level plus the
    # root resolution; one line fetch per expandable level; one item fetch per
    # level that introduced new parts.
    assert source.calls["effective_boms"] == 3
    assert source.calls["lines_for_boms"] == 2
    assert source.calls["items"] == 3
    # The middle level asked for all five sub-assemblies at once.
    assert 5 in source.batch_sizes["effective_boms"]
    assert source.batch_sizes["lines_for_boms"] == [1, 5]


async def test_the_query_count_does_not_grow_with_the_number_of_parts() -> None:
    """The property that separates a batched traversal from an N+1 one."""
    small = wide_structure(sub_assemblies=2, parts_each=3)
    large = wide_structure(sub_assemblies=20, parts_each=30)

    small_source, large_source = small.source(), large.source()
    await explode(small, "ROOT", Decimal("1"), source=small_source)
    await explode(large, "ROOT", Decimal("1"), source=large_source)

    assert small_source.calls == large_source.calls


async def test_an_item_seen_on_a_previous_level_is_not_fetched_again() -> None:
    structure = lock_kit()
    source = structure.source()
    await explode(structure, "KIT", Decimal("1"), source=source)

    # SCREW-M4 appears under both sub-assemblies but is introduced once, so the
    # second level asks only for the parts it has not seen.
    assert source.calls["items"] == 3


# -----------------------------------------------------------------------------
# Guards and edge cases
# -----------------------------------------------------------------------------


async def test_a_non_positive_quantity_is_refused() -> None:
    structure = lock_kit()
    with pytest.raises(BusinessRuleError):
        await explode(structure, "KIT", Decimal("0"))


async def test_an_item_with_no_effective_structure_reports_bom_not_found() -> None:
    structure = Structure()
    structure.item("ORPHAN", manufactured=True, purchased=False)

    with pytest.raises(AppError) as caught:
        await explode(structure, "ORPHAN", Decimal("1"))
    assert caught.value.code is ErrorCode.BOM_NOT_FOUND
    assert caught.value.status_code == 404


async def test_a_leaf_carries_the_capability_flags_planning_reads() -> None:
    """Master rules section 1: purchased and manufactured are independent
    booleans, never a single type discriminator."""
    structure = lock_kit()
    structure.item("SCREW-M4", purchased=True, manufactured=True)

    result = await explode(structure, "KIT", Decimal("1"))
    screw = next(c for c in result.components if c.item_code == "SCREW-M4")
    assert screw.is_purchased is True
    assert screw.is_manufactured is True


async def test_the_result_reports_what_it_was_computed_from() -> None:
    structure = lock_kit()
    result = await explode(structure, "KIT", Decimal("2.5"))

    assert result.root_item_code == "KIT"
    assert result.quantity == Decimal("2.5")
    assert result.as_of == AS_OF
    assert result.root_bom_status == BomStatus.APPROVED
    assert result.max_level_reached == 2
    assert result.component_for(structure.item_ids["BUSH"]) is not None


async def test_components_are_returned_in_a_stable_reading_order() -> None:
    result = await explode(lock_kit(), "KIT", Decimal("1"))
    ordering = [(c.level, c.item_code) for c in result.components]
    assert ordering == sorted(ordering)


# -----------------------------------------------------------------------------
# The node budget
# -----------------------------------------------------------------------------


async def test_the_node_budget_stops_the_level_that_breaches_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard has to bite while the frontier is being built, not one level on.

    A level of occurrences whose structures each carry hundreds of lines
    produces the whole cartesian product before control returns to the loop, so
    a budget checked only at the top of the next iteration is a budget the
    process can exhaust its memory before ever reaching. Here the budget is 20
    and the second level would be 100 nodes.
    """
    monkeypatch.setattr(engine, "MAX_EXPLOSION_NODES", 20)
    structure = wide_structure(sub_assemblies=10, parts_each=10)

    with pytest.raises(engine.BomTooLargeError) as caught:
        await explode(structure, "ROOT", Decimal("1"))

    assert caught.value.details["root_item_code"] == "ROOT"
    assert caught.value.details["node_budget"] == 20


async def test_a_structure_inside_the_budget_still_explodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard must not fire on a structure that fits."""
    monkeypatch.setattr(engine, "MAX_EXPLOSION_NODES", 200)
    structure = wide_structure(sub_assemblies=5, parts_each=10)

    result = await explode(structure, "ROOT", Decimal("1"))
    assert len(result.components) == 55
