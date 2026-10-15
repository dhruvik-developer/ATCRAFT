"""Multi-level cost roll-up.

The properties asserted here are the ones a wrong answer quietly corrupts a
standard cost with: a sub-assembly priced from a stale stored cost instead of
its own structure, setup time charged per piece instead of per batch, scrap left
out of the material cost, or a missing price silently treated as free.

Everything runs on an in-memory data source, so the arithmetic is exact and the
suite needs no database.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Any, cast

import pytest

from src.modules.bom import engine
from src.modules.bom.models import BomStatus, BomType
from src.modules.bom.repository import ItemInfo

AS_OF = date(2026, 8, 31)


# -----------------------------------------------------------------------------
# Test doubles
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeBom:
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
    is_outsourced: bool = False


@dataclass
class FakeSource:
    boms_by_item: dict[uuid.UUID, FakeBom]
    lines_by_bom: dict[uuid.UUID, list[FakeLine]]
    item_info: dict[uuid.UUID, ItemInfo]
    operations_by_bom: dict[uuid.UUID, list[FakeOperation]] = field(default_factory=dict)
    prices: dict[uuid.UUID, Decimal] = field(default_factory=dict)
    calls: Counter[str] = field(default_factory=Counter)

    async def effective_boms(self, item_ids: Any, as_of: date) -> dict[uuid.UUID, Any]:
        self.calls["effective_boms"] += 1
        return {i: self.boms_by_item[i] for i in item_ids if i in self.boms_by_item}

    async def lines_for_boms(self, bom_ids: Any) -> dict[uuid.UUID, list[Any]]:
        self.calls["lines_for_boms"] += 1
        return {i: list(self.lines_by_bom.get(i, [])) for i in bom_ids}

    async def items(self, item_ids: Any) -> dict[uuid.UUID, ItemInfo]:
        self.calls["items"] += 1
        return {i: self.item_info[i] for i in item_ids if i in self.item_info}

    async def operations_for_boms(self, bom_ids: Any) -> dict[uuid.UUID, list[Any]]:
        self.calls["operations_for_boms"] += 1
        return {i: list(self.operations_by_bom.get(i, [])) for i in bom_ids}

    async def last_purchase_prices(self, item_ids: Any) -> dict[uuid.UUID, Decimal]:
        self.calls["last_purchase_prices"] += 1
        return {i: self.prices[i] for i in item_ids if i in self.prices}


class Costed:
    """Builder for a costed structure."""

    def __init__(self) -> None:
        self.ids: dict[str, uuid.UUID] = {}
        self.boms_by_item: dict[uuid.UUID, FakeBom] = {}
        self.lines_by_bom: dict[uuid.UUID, list[FakeLine]] = {}
        self.info: dict[uuid.UUID, ItemInfo] = {}
        self.operations_by_bom: dict[uuid.UUID, list[FakeOperation]] = {}
        self.prices: dict[uuid.UUID, Decimal] = {}

    def item(
        self,
        code: str,
        *,
        standard_cost: Decimal = Decimal("0"),
        purchase_price: Decimal | None = None,
    ) -> uuid.UUID:
        item_id = self.ids.setdefault(code, uuid.uuid4())
        self.info[item_id] = ItemInfo(
            id=item_id,
            code=code,
            name=f"{code} name",
            base_uom_id=uuid.uuid4(),
            standard_cost=standard_cost,
            production_type="BUY",
            can_purchase=True,
            can_manufacture=False,
            can_be_bom_component=True,
            is_active=True,
        )
        if purchase_price is not None:
            self.prices[item_id] = purchase_price
        return item_id

    def bom(
        self,
        parent: str,
        components: dict[str, Decimal | tuple[Decimal, Decimal]],
        *,
        output_quantity: Decimal = Decimal("1"),
        operations: list[FakeOperation] | None = None,
        optional: frozenset[str] = frozenset(),
        substitutes: dict[str, str] | None = None,
    ) -> FakeBom:
        parent_id = self.ids.get(parent) or self.item(parent)
        bom = FakeBom(
            id=uuid.uuid4(),
            code=f"BOM/{parent}",
            item_id=parent_id,
            output_quantity=output_quantity,
        )
        self.boms_by_item[parent_id] = bom
        substitutes = substitutes or {}
        line_ids = {code: uuid.uuid4() for code in components}
        self.lines_by_bom[bom.id] = [
            FakeLine(
                bom_id=bom.id,
                line_number=number,
                component_item_id=self.ids.get(code) or self.item(code),
                quantity_per=(spec[0] if isinstance(spec, tuple) else spec),
                scrap_percentage=(spec[1] if isinstance(spec, tuple) else Decimal("0")),
                is_optional=code in optional,
                substitute_for_line_id=(
                    line_ids[substitutes[code]] if code in substitutes else None
                ),
            )
            for number, (code, spec) in enumerate(components.items(), start=1)
        ]
        if operations:
            self.operations_by_bom[bom.id] = operations
        return bom

    def source(self) -> FakeSource:
        return FakeSource(
            boms_by_item=self.boms_by_item,
            lines_by_bom=self.lines_by_bom,
            item_info=self.info,
            operations_by_bom=self.operations_by_bom,
            prices=self.prices,
        )


async def roll_up(
    structure: Costed, code: str, *, source: FakeSource | None = None
) -> engine.BomCost:
    return await engine.cost_roll_up(
        cast(Any, None),
        cast(Any, None),
        structure.ids[code],
        as_of=AS_OF,
        source=cast(Any, source or structure.source()),
    )


# -----------------------------------------------------------------------------
# Operation costing - pure function
# -----------------------------------------------------------------------------


def test_setup_is_charged_once_per_run_and_run_time_per_unit() -> None:
    operations = [
        FakeOperation(
            setup_time_minutes=Decimal("30"),
            run_time_minutes_per_unit=Decimal("6"),
            cost_per_hour=Decimal("60"),
            fixed_cost=Decimal("5"),
        )
    ]
    # 5 fixed + 30 min setup at 60/hr + 10 units x 6 min at 60/hr.
    assert engine.operation_cost_per_run(cast(Any, operations), Decimal("10")) == Decimal("95")


def test_setup_amortises_over_a_larger_batch() -> None:
    """The whole reason setup is per-run: a hundred-piece batch must not be
    priced as if the machine were set up a hundred times."""
    operations = [FakeOperation(setup_time_minutes=Decimal("60"), cost_per_hour=Decimal("100"))]
    single = engine.operation_cost_per_run(cast(Any, operations), Decimal("1"))
    hundred = engine.operation_cost_per_run(cast(Any, operations), Decimal("100"))
    assert single == hundred == Decimal("100")


def test_operations_accumulate() -> None:
    operations = [
        FakeOperation(fixed_cost=Decimal("10")),
        FakeOperation(run_time_minutes_per_unit=Decimal("30"), cost_per_hour=Decimal("40")),
    ]
    assert engine.operation_cost_per_run(cast(Any, operations), Decimal("2")) == Decimal("50")


def test_outsourced_operation_cost_is_reported_as_a_component_of_operation_cost() -> None:
    operations = [
        FakeOperation(run_time_minutes_per_unit=Decimal("30"), cost_per_hour=Decimal("40")),
        FakeOperation(fixed_cost=Decimal("15"), is_outsourced=True),
    ]
    assert engine.operation_cost_per_run(cast(Any, operations), Decimal("2")) == Decimal("55")
    subcontract = engine.subcontract_operation_cost_per_run(cast(Any, operations), Decimal("2"))
    assert subcontract == Decimal("15")


def test_no_operations_costs_nothing() -> None:
    assert engine.operation_cost_per_run([], Decimal("5")) == Decimal("0")


def test_operation_costing_refuses_a_zero_yield() -> None:
    with pytest.raises(ValueError, match="positive"):
        engine.operation_cost_per_run([], Decimal("0"))


# -----------------------------------------------------------------------------
# Material roll-up
# -----------------------------------------------------------------------------


async def test_a_single_level_structure_costs_its_components() -> None:
    structure = Costed()
    structure.item("PLATE", standard_cost=Decimal("10"))
    structure.item("SCREW", standard_cost=Decimal("0.50"))
    structure.bom("WIDGET", {"PLATE": Decimal("2"), "SCREW": Decimal("4")})

    cost = await roll_up(structure, "WIDGET")
    assert cost.material_cost == Decimal("22")
    assert cost.operation_cost == Decimal("0")
    assert cost.total_cost == Decimal("22")
    assert cost.unit_cost == Decimal("22")


async def test_a_sub_assembly_is_costed_from_its_own_structure_not_its_stored_cost() -> None:
    """The point of a roll-up: a stale standard cost on the sub-assembly must
    not hide the drift the roll-up exists to surface."""
    structure = Costed()
    structure.item("PIN", standard_cost=Decimal("2.50"))
    structure.bom("SUBASSY", {"PIN": Decimal("4")})
    # Deliberately wrong stored cost on the sub-assembly.
    subassy_id = structure.ids["SUBASSY"]
    structure.info[subassy_id] = replace(structure.info[subassy_id], standard_cost=Decimal("999"))
    structure.item("PLATE", standard_cost=Decimal("10"))
    structure.bom("WIDGET", {"PLATE": Decimal("2"), "SUBASSY": Decimal("1")})

    cost = await roll_up(structure, "WIDGET")
    # 2 plates at 10 + one sub-assembly rolled up at 4 x 2.50.
    assert cost.material_cost == Decimal("30")


async def test_the_roll_up_reaches_every_level() -> None:
    structure = Costed()
    structure.item("WIRE", standard_cost=Decimal("1"))
    structure.bom("SPRING", {"WIRE": Decimal("3")})
    structure.bom("LOCK-BODY", {"SPRING": Decimal("2")})
    structure.bom("KIT", {"LOCK-BODY": Decimal("2")})

    cost = await roll_up(structure, "KIT")
    # 2 bodies x (2 springs x (3 wire x 1)).
    assert cost.unit_cost == Decimal("12")


async def test_scrap_is_included_in_the_material_cost() -> None:
    """The scrapped material is bought and consumed, so it is part of the cost."""
    structure = Costed()
    structure.item("SHEET", standard_cost=Decimal("100"))
    structure.bom("PANEL", {"SHEET": (Decimal("1"), Decimal("5"))})

    cost = await roll_up(structure, "PANEL")
    assert cost.material_cost == Decimal("105")


async def test_output_quantity_divides_the_unit_cost() -> None:
    structure = Costed()
    structure.item("BILLET", standard_cost=Decimal("200"))
    structure.bom(
        "HANDLE",
        {"BILLET": Decimal("1")},
        output_quantity=Decimal("10"),
        operations=[FakeOperation(fixed_cost=Decimal("50"))],
    )

    cost = await roll_up(structure, "HANDLE")
    assert cost.material_cost == Decimal("200")
    assert cost.operation_cost == Decimal("50")
    assert cost.total_cost == Decimal("250")
    assert cost.unit_cost == Decimal("25")


async def test_operations_are_added_to_the_material_cost() -> None:
    structure = Costed()
    structure.item("PLATE", standard_cost=Decimal("10"))
    structure.bom(
        "WIDGET",
        {"PLATE": Decimal("2")},
        operations=[
            FakeOperation(
                setup_time_minutes=Decimal("30"),
                run_time_minutes_per_unit=Decimal("6"),
                cost_per_hour=Decimal("60"),
                fixed_cost=Decimal("5"),
            )
        ],
    )

    cost = await roll_up(structure, "WIDGET")
    assert cost.material_cost == Decimal("20")
    assert cost.operation_cost == Decimal("41")
    assert cost.total_cost == Decimal("61")


async def test_a_sub_assemblys_operations_roll_into_the_parent() -> None:
    structure = Costed()
    structure.item("WIRE", standard_cost=Decimal("1"))
    structure.bom(
        "SPRING",
        {"WIRE": Decimal("1")},
        operations=[FakeOperation(fixed_cost=Decimal("2"))],
    )
    structure.bom("LATCH", {"SPRING": Decimal("3")})

    cost = await roll_up(structure, "LATCH")
    # Each spring is 1 of wire plus 2 of forming.
    assert cost.material_cost == Decimal("9")


# -----------------------------------------------------------------------------
# Cost sources and gaps
# -----------------------------------------------------------------------------


async def test_a_leaf_with_no_standard_cost_falls_back_to_the_purchase_price() -> None:
    structure = Costed()
    structure.item("BUSH", standard_cost=Decimal("0"), purchase_price=Decimal("3.25"))
    structure.bom("HANDLE", {"BUSH": Decimal("2")})

    cost = await roll_up(structure, "HANDLE")
    assert cost.material_cost == Decimal("6.50")
    bush = next(c for c in cost.components if c.item_code == "BUSH")
    assert bush.cost_source == engine.COST_SOURCE_PURCHASE


async def test_a_standard_cost_wins_over_the_purchase_price() -> None:
    structure = Costed()
    structure.item("BUSH", standard_cost=Decimal("4"), purchase_price=Decimal("3.25"))
    structure.bom("HANDLE", {"BUSH": Decimal("1")})

    cost = await roll_up(structure, "HANDLE")
    bush = next(c for c in cost.components if c.item_code == "BUSH")
    assert bush.unit_cost == Decimal("4")
    assert bush.cost_source == engine.COST_SOURCE_STANDARD


async def test_an_item_with_no_cost_anywhere_is_named_rather_than_assumed_free() -> None:
    """A roll-up that quietly understates is worse than one that says what it
    does not know."""
    structure = Costed()
    structure.item("PLATE", standard_cost=Decimal("10"))
    structure.item("GASKET", standard_cost=Decimal("0"))
    structure.bom("WIDGET", {"PLATE": Decimal("1"), "GASKET": Decimal("1")})

    cost = await roll_up(structure, "WIDGET")
    assert cost.items_without_cost == ("GASKET",)
    assert cost.material_cost == Decimal("10")
    gasket = next(c for c in cost.components if c.item_code == "GASKET")
    assert gasket.cost_source == engine.COST_SOURCE_MISSING


async def test_a_missing_cost_deep_in_the_structure_is_still_reported() -> None:
    structure = Costed()
    structure.item("WIRE", standard_cost=Decimal("0"))
    structure.bom("SPRING", {"WIRE": Decimal("1")})
    structure.bom("LATCH", {"SPRING": Decimal("1")})

    cost = await roll_up(structure, "LATCH")
    assert cost.items_without_cost == ("WIRE",)


async def test_a_rolled_up_component_is_labelled_as_such() -> None:
    structure = Costed()
    structure.item("WIRE", standard_cost=Decimal("1"))
    structure.bom("SPRING", {"WIRE": Decimal("2")})
    structure.bom("LATCH", {"SPRING": Decimal("1")})

    cost = await roll_up(structure, "LATCH")
    spring = next(c for c in cost.components if c.item_code == "SPRING")
    assert spring.cost_source == engine.COST_SOURCE_ROLLED_UP
    assert spring.unit_cost == Decimal("2")


# -----------------------------------------------------------------------------
# Line selection and reporting
# -----------------------------------------------------------------------------


async def test_optional_and_substitute_lines_are_left_out_of_the_cost() -> None:
    structure = Costed()
    structure.item("SPRING-A", standard_cost=Decimal("5"))
    structure.item("SPRING-B", standard_cost=Decimal("9"))
    structure.item("BADGE", standard_cost=Decimal("50"))
    structure.bom(
        "LATCH",
        {"SPRING-A": Decimal("1"), "SPRING-B": Decimal("1"), "BADGE": Decimal("1")},
        optional=frozenset({"BADGE"}),
        substitutes={"SPRING-B": "SPRING-A"},
    )

    cost = await roll_up(structure, "LATCH")
    assert cost.material_cost == Decimal("5")
    assert [c.item_code for c in cost.components] == ["SPRING-A"]


async def test_components_report_the_quantity_per_finished_unit() -> None:
    structure = Costed()
    structure.item("BILLET", standard_cost=Decimal("30"))
    structure.bom("HANDLE", {"BILLET": Decimal("2")}, output_quantity=Decimal("4"))

    cost = await roll_up(structure, "HANDLE")
    billet = next(c for c in cost.components if c.item_code == "BILLET")
    assert billet.quantity_per_unit == Decimal("0.5")
    # extended_cost is for one run, matching material_cost.
    assert billet.extended_cost == Decimal("60")
    assert cost.material_cost == Decimal("60")
    assert cost.unit_cost == Decimal("15")


async def test_every_figure_is_a_decimal() -> None:
    """Master rules section 84: a float anywhere near a cost is a defect."""
    structure = Costed()
    structure.item("PLATE", standard_cost=Decimal("10.005"))
    structure.bom(
        "WIDGET",
        {"PLATE": Decimal("3")},
        operations=[FakeOperation(fixed_cost=Decimal("1.005"))],
    )

    cost = await roll_up(structure, "WIDGET")
    for value in (
        cost.material_cost,
        cost.operation_cost,
        cost.total_cost,
        cost.unit_cost,
        cost.components[0].unit_cost,
        cost.components[0].extended_cost,
        cost.components[0].quantity_per_unit,
    ):
        assert isinstance(value, Decimal)


async def test_the_operations_of_every_level_are_loaded_in_one_query() -> None:
    """The roll-up walks the structure once and fetches operations for the whole
    tree in a single statement - not one per sub-assembly."""
    structure = Costed()
    structure.item("WIRE", standard_cost=Decimal("1"))
    springs = ("SPRING-1", "SPRING-2", "SPRING-3")
    for sub in springs:
        structure.bom(
            sub, {"WIRE": Decimal("1")}, operations=[FakeOperation(fixed_cost=Decimal("1"))]
        )
    structure.bom("LATCH", dict.fromkeys(springs, Decimal("1")))

    source = structure.source()
    cost = await roll_up(structure, "LATCH", source=source)

    assert source.calls["operations_for_boms"] == 1
    assert source.calls["last_purchase_prices"] == 1
    assert cost.material_cost == Decimal("6")


# -----------------------------------------------------------------------------
# The costed lines must add up to the costed total
# -----------------------------------------------------------------------------


async def test_the_component_lines_sum_to_the_material_cost() -> None:
    """A costing screen whose column does not add up to its own footer.

    Two lines of 1.05 (1 plus 5% scrap) at 0.25 each are 0.2625 apiece. Rounding
    each printed line gives 0.26 + 0.26 = 0.52, while rounding the sum of the
    unrounded products separately gives 0.53. Reporting both numbers in one
    payload leaves a buyer unable to reconcile the total against the evidence
    stored beside it, and ``boms.total_material_cost`` is written from that same
    total at approval.
    """
    structure = Costed()
    structure.item("GASKET-A", standard_cost=Decimal("0.25"))
    structure.item("SEAL-B", standard_cost=Decimal("0.25"))
    structure.bom(
        "PANEL",
        {"GASKET-A": (Decimal("1"), Decimal("5")), "SEAL-B": (Decimal("1"), Decimal("5"))},
    )

    cost = await roll_up(structure, "PANEL")

    assert [c.extended_cost for c in cost.components] == [Decimal("0.26"), Decimal("0.26")]
    assert cost.material_cost == Decimal("0.52")
    assert sum(c.extended_cost for c in cost.components) == cost.material_cost


async def test_the_lines_sum_to_the_total_however_many_there_are() -> None:
    """The drift grows with the line count, which is where nobody re-adds it."""
    structure = Costed()
    components: dict[str, Decimal | tuple[Decimal, Decimal]] = {}
    for n in range(40):
        code = f"PART-{n:02d}"
        structure.item(code, standard_cost=Decimal("0.25"))
        components[code] = (Decimal("1"), Decimal("5"))
    structure.bom("PANEL", components)

    cost = await roll_up(structure, "PANEL")

    assert sum(c.extended_cost for c in cost.components) == cost.material_cost
    assert cost.total_cost == cost.material_cost + cost.operation_cost


async def test_a_sub_assemblys_own_roll_up_agrees_with_its_lines_too() -> None:
    """The invariant holds at every level, not only at the root."""
    structure = Costed()
    structure.item("WASHER", standard_cost=Decimal("0.25"))
    structure.item("SHIM", standard_cost=Decimal("0.25"))
    structure.bom(
        "SUBASSY",
        {"WASHER": (Decimal("1"), Decimal("5")), "SHIM": (Decimal("1"), Decimal("5"))},
    )

    cost = await roll_up(structure, "SUBASSY")
    assert sum(c.extended_cost for c in cost.components) == cost.material_cost
