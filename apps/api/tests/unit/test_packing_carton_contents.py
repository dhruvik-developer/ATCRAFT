"""Carton weight arithmetic and content validation.

The rules under test are the ones that keep a physical box, an order line and the
traceability record in agreement: a carton weighs its contents plus its own box,
a serialised item names one serial per unit, and nothing may be packed that the
order did not ask for.

All pure functions - no database, no session.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from src.core.errors import BusinessRuleError, NotFoundError, ValidationError
from src.modules.packing.repository import PackingItem
from src.modules.packing.schemas import CartonContentIn
from src.modules.packing.service import (
    LineFacts,
    PlannedContent,
    gross_weight_of,
    net_weight_of,
    plan_contents,
    recompute_packed_quantities,
)
from src.modules.products.models import ItemGroup

HANDLE = uuid.uuid4()
CYLINDER = uuid.uuid4()
LOCK_BODY = uuid.uuid4()
UOM = uuid.uuid4()


def item(
    item_id: uuid.UUID,
    code: str,
    *,
    batch: bool = False,
    serial: bool = False,
    active: bool = True,
    group: ItemGroup = ItemGroup.FINISHED_GOOD,
) -> PackingItem:
    return PackingItem(
        id=item_id,
        code=code,
        name=f"Item {code}",
        base_uom_id=UOM,
        item_group=group,
        is_batch_tracked=batch,
        is_serial_tracked=serial,
        is_active=active,
    )


def line(
    number: int,
    item_id: uuid.UUID,
    *,
    to_pack: str = "100",
    packed: str = "0",
    line_id: uuid.UUID | None = None,
) -> LineFacts:
    return LineFacts(
        id=line_id or uuid.uuid4(),
        line_number=number,
        item_id=item_id,
        quantity_to_pack=Decimal(to_pack),
        packed_quantity=Decimal(packed),
    )


def planned(line_facts: LineFacts, quantity: str, weight: str = "0") -> PlannedContent:
    return PlannedContent(
        line_id=line_facts.id,
        line_number=line_facts.line_number,
        item_id=line_facts.item_id,
        quantity=Decimal(quantity),
        net_weight_kg=Decimal(weight),
        batch_id=None,
        is_serial_tracked_item=False,
        serial_ids=(),
    )


# -----------------------------------------------------------------------------
# Weights
# -----------------------------------------------------------------------------


def test_net_weight_is_the_sum_of_the_contents() -> None:
    one = line(1, HANDLE)
    two = line(2, CYLINDER)
    assert net_weight_of([planned(one, "10", "2.5"), planned(two, "4", "1.25")]) == Decimal(
        "3.7500"
    )


def test_an_empty_carton_has_no_net_weight() -> None:
    assert net_weight_of([]) == Decimal("0")


def test_gross_weight_is_the_contents_plus_the_empty_box() -> None:
    assert gross_weight_of(
        net_weight_kg=Decimal("12.4"), tare_weight_kg=Decimal("0.85")
    ) == Decimal("13.2500")


def test_gross_weight_of_an_empty_carton_is_its_tare() -> None:
    # This is why an empty carton may exist but may not be sealed: it weighs
    # something, it just is not carrying anything.
    assert gross_weight_of(net_weight_kg=Decimal("0"), tare_weight_kg=Decimal("0.9")) == Decimal(
        "0.9000"
    )


def test_gross_weight_is_never_below_net_weight() -> None:
    # The database CHECK ``gross_covers_net`` relies on this: a tare is a
    # non-negative schema constraint, so the sum can only go one way.
    net = Decimal("40")
    for tare in ("0", "0.001", "5"):
        assert gross_weight_of(net_weight_kg=net, tare_weight_kg=Decimal(tare)) >= net


def test_weights_are_quantised_rather_than_accumulating_a_tail() -> None:
    # Four decimal places is the column's scale. A value with more would be
    # rounded by PostgreSQL on the way in, and the stored total would then
    # disagree with the one the API had already returned.
    total = net_weight_of(
        [planned(line(1, HANDLE), "1", "0.00005"), planned(line(2, CYLINDER), "1", "0.00005")]
    )
    assert total.as_tuple().exponent == -4


# -----------------------------------------------------------------------------
# Content validation
# -----------------------------------------------------------------------------


def test_a_plain_content_row_is_planned_from_its_line() -> None:
    handle_line = line(1, HANDLE)
    result = plan_contents(
        [CartonContentIn(line_number=1, quantity=Decimal("10"), net_weight_kg=Decimal("2"))],
        lines={1: handle_line},
        items={HANDLE: item(HANDLE, "HANDLE-01")},
    )
    assert len(result) == 1
    assert result[0].line_id == handle_line.id
    assert result[0].item_id == HANDLE
    assert result[0].quantity == Decimal("10")
    assert result[0].serial_ids == ()


def test_a_line_that_is_not_on_the_order_is_refused() -> None:
    with pytest.raises(ValidationError, match="not on this packing order"):
        plan_contents(
            [CartonContentIn(line_number=9, quantity=Decimal("1"))],
            lines={1: line(1, HANDLE)},
            items={HANDLE: item(HANDLE, "HANDLE-01")},
        )


def test_one_line_may_not_appear_twice_in_a_carton() -> None:
    # Two rows for one line would make the packed-quantity roll-up depend on
    # iteration order, and the second would silently overwrite nothing.
    handle_line = line(1, HANDLE)
    with pytest.raises(ValidationError, match="twice"):
        plan_contents(
            [
                CartonContentIn(line_number=1, quantity=Decimal("1")),
                CartonContentIn(line_number=1, quantity=Decimal("2")),
            ],
            lines={1: handle_line},
            items={HANDLE: item(HANDLE, "HANDLE-01")},
        )


def test_an_unknown_item_is_a_not_found_rather_than_a_silent_skip() -> None:
    with pytest.raises(NotFoundError):
        plan_contents(
            [CartonContentIn(line_number=1, quantity=Decimal("1"))],
            lines={1: line(1, HANDLE)},
            items={},
        )


def test_an_inactive_item_cannot_be_packed() -> None:
    with pytest.raises(BusinessRuleError, match="inactive"):
        plan_contents(
            [CartonContentIn(line_number=1, quantity=Decimal("1"))],
            lines={1: line(1, HANDLE)},
            items={HANDLE: item(HANDLE, "HANDLE-01", active=False)},
        )


# --- batches -----------------------------------------------------------------


def test_a_batch_tracked_item_must_name_its_batch() -> None:
    with pytest.raises(BusinessRuleError, match="batch tracked"):
        plan_contents(
            [CartonContentIn(line_number=1, quantity=Decimal("5"))],
            lines={1: line(1, CYLINDER)},
            items={CYLINDER: item(CYLINDER, "CYL-01", batch=True)},
        )


def test_a_non_batch_tracked_item_may_not_carry_a_batch() -> None:
    # A batch on an item that has none is either a mis-selected row or a batch
    # belonging to a different item; both would corrupt the recall trail.
    with pytest.raises(BusinessRuleError, match="not batch tracked"):
        plan_contents(
            [
                CartonContentIn(
                    line_number=1, quantity=Decimal("5"), batch_id=uuid.uuid4()
                )
            ],
            lines={1: line(1, CYLINDER)},
            items={CYLINDER: item(CYLINDER, "CYL-01")},
        )


def test_a_batch_tracked_item_with_its_batch_is_accepted() -> None:
    batch_id = uuid.uuid4()
    result = plan_contents(
        [CartonContentIn(line_number=1, quantity=Decimal("5"), batch_id=batch_id)],
        lines={1: line(1, CYLINDER)},
        items={CYLINDER: item(CYLINDER, "CYL-01", batch=True)},
    )
    assert result[0].batch_id == batch_id


# --- serials -----------------------------------------------------------------


def test_a_serialised_item_needs_one_serial_per_unit() -> None:
    with pytest.raises(BusinessRuleError, match="one serial per unit"):
        plan_contents(
            [
                CartonContentIn(
                    line_number=1,
                    quantity=Decimal("3"),
                    serial_ids=[uuid.uuid4(), uuid.uuid4()],
                )
            ],
            lines={1: line(1, LOCK_BODY)},
            items={LOCK_BODY: item(LOCK_BODY, "LOCK-01", serial=True)},
        )


def test_a_serialised_item_with_the_matching_serial_count_is_accepted() -> None:
    serials = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    result = plan_contents(
        [CartonContentIn(line_number=1, quantity=Decimal("3"), serial_ids=serials)],
        lines={1: line(1, LOCK_BODY)},
        items={LOCK_BODY: item(LOCK_BODY, "LOCK-01", serial=True)},
    )
    assert result[0].is_serial_tracked_item is True
    assert result[0].serial_ids == tuple(serials)


def test_a_non_serialised_item_may_not_carry_serials() -> None:
    with pytest.raises(BusinessRuleError, match="not serial tracked"):
        plan_contents(
            [
                CartonContentIn(
                    line_number=1, quantity=Decimal("1"), serial_ids=[uuid.uuid4()]
                )
            ],
            lines={1: line(1, HANDLE)},
            items={HANDLE: item(HANDLE, "HANDLE-01")},
        )


def test_the_same_serial_cannot_be_named_twice_in_one_content_row() -> None:
    duplicate = uuid.uuid4()
    with pytest.raises(ValueError, match="twice"):
        CartonContentIn(
            line_number=1, quantity=Decimal("2"), serial_ids=[duplicate, duplicate]
        )


# -----------------------------------------------------------------------------
# Packed-quantity roll-up
# -----------------------------------------------------------------------------


def test_adding_contents_raises_the_packed_quantity_of_its_line() -> None:
    handle_line = line(1, HANDLE, to_pack="100", packed="10")
    updated = recompute_packed_quantities(
        lines=[handle_line], released={}, added=[planned(handle_line, "25")]
    )
    assert updated == {handle_line.id: Decimal("35.0000")}


def test_a_line_nothing_touched_is_not_rewritten() -> None:
    # Only lines whose quantity actually moves are returned, so an edit to one
    # carton does not issue an UPDATE for every line of a 200-line order.
    touched = line(1, HANDLE)
    untouched = line(2, CYLINDER)
    updated = recompute_packed_quantities(
        lines=[touched, untouched], released={}, added=[planned(touched, "5")]
    )
    assert set(updated) == {touched.id}


def test_replacing_a_cartons_contents_nets_the_release_against_the_addition() -> None:
    handle_line = line(1, HANDLE, to_pack="100", packed="40")
    updated = recompute_packed_quantities(
        lines=[handle_line],
        released={handle_line.id: Decimal("40")},
        added=[planned(handle_line, "30")],
    )
    assert updated == {handle_line.id: Decimal("30.0000")}


def test_removing_a_carton_gives_its_quantity_back_to_the_line() -> None:
    handle_line = line(1, HANDLE, to_pack="100", packed="30")
    updated = recompute_packed_quantities(
        lines=[handle_line], released={handle_line.id: Decimal("30")}, added=[]
    )
    assert updated == {handle_line.id: Decimal("0.0000")}


def test_replacing_a_cartons_contents_with_the_same_quantity_writes_nothing() -> None:
    handle_line = line(1, HANDLE, to_pack="100", packed="40")
    updated = recompute_packed_quantities(
        lines=[handle_line],
        released={handle_line.id: Decimal("40")},
        added=[planned(handle_line, "40")],
    )
    assert updated == {}


def test_over_packing_is_refused_rather_than_clamped() -> None:
    # Clamping would ship the customer a quantity nobody decided on and leave the
    # order looking fully packed.
    handle_line = line(1, HANDLE, to_pack="100", packed="90")
    with pytest.raises(BusinessRuleError, match="over-packed"):
        recompute_packed_quantities(
            lines=[handle_line], released={}, added=[planned(handle_line, "20")]
        )


def test_packing_exactly_the_requested_quantity_is_allowed() -> None:
    handle_line = line(1, HANDLE, to_pack="100", packed="90")
    updated = recompute_packed_quantities(
        lines=[handle_line], released={}, added=[planned(handle_line, "10")]
    )
    assert updated == {handle_line.id: Decimal("100.0000")}


def test_a_negative_result_is_refused_loudly_rather_than_floored() -> None:
    # Impossible from correct data: it means the released set and the stored
    # quantity disagree, which is a bug somebody has to see.
    handle_line = line(1, HANDLE, to_pack="100", packed="5")
    with pytest.raises(BusinessRuleError, match="negative"):
        recompute_packed_quantities(
            lines=[handle_line], released={handle_line.id: Decimal("10")}, added=[]
        )


def test_two_content_rows_on_one_line_are_summed_before_the_bound_is_checked() -> None:
    # They cannot come from one carton, but two cartons in one order legitimately
    # both contribute to the same line.
    handle_line = line(1, HANDLE, to_pack="10", packed="0")
    with pytest.raises(BusinessRuleError, match="over-packed"):
        recompute_packed_quantities(
            lines=[handle_line],
            released={},
            added=[planned(handle_line, "6"), planned(handle_line, "6")],
        )
