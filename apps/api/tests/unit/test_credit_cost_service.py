"""Return-credit FIFO planning never invents or double-charges a cost.

The service under test is the last place a customer credit meets inventory
history. Three properties matter more than any single branch:

* **All or pending.** A credit line either receives complete, provable pieces
  or no allocation at all; a half-known cost would make the profitability
  report look complete when it is not.
* **Evidence, not a blend.** Missing original layers cannot be replaced by a
  receipt's blended average when the accepted quantity is only part of that
  receipt. A serial names its own unit, so it may keep its own price.
* **Frozen money.** Allocation values come from the receipt's booked total,
  scaled across its FIFO prefix and rounded once, so adjacent pieces telescope
  and cancellation/reissue cannot drift a paisa.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.core.types import ZERO
from src.modules.finance.credit_cost_service import (
    ReceiptCostSource,
    free_receipt_ranges,
    plan_cost_pieces,
    receipt_cost_source,
)
from src.modules.inventory.service import LayerCost


def _layers(*values: tuple[str, str]) -> tuple[LayerCost, ...]:
    return tuple(
        LayerCost(quantity=Decimal(quantity), unit_cost=Decimal(unit_cost))
        for quantity, unit_cost in values
    )


def _source(
    *,
    quantity: str = "5",
    eligible: str | None = None,
    total: str = "50",
    layers: tuple[LayerCost, ...] | None = None,
    divisible: bool = True,
) -> ReceiptCostSource:
    quantity_value = Decimal(quantity)
    return ReceiptCostSource(
        transaction_id=uuid.uuid4(),
        quantity=quantity_value,
        eligible_quantity=quantity_value if eligible is None else Decimal(eligible),
        total_cost=Decimal(total),
        layers=(
            _layers((quantity, str(Decimal(total) / quantity_value))) if layers is None else layers
        ),
        divisible=divisible,
    )


def _movement(
    *,
    quantity: str = "10",
    total: str = "100",
    serial_id: uuid.UUID | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        quantity=Decimal(quantity),
        total_cost=Decimal(total),
        serial_id=serial_id,
    )


def _model_layers(*values: tuple[str, str]) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(original_quantity=Decimal(quantity), unit_cost=Decimal(unit_cost))
        for quantity, unit_cost in values
    ]


# -----------------------------------------------------------------------------
# Free ranges
# -----------------------------------------------------------------------------


def test_a_receipt_with_no_history_offers_its_whole_quantity() -> None:
    assert free_receipt_ranges(Decimal("10"), ()) == ((ZERO, Decimal("10")),)


def test_occupied_pieces_leave_only_the_gaps_between_them() -> None:
    assert free_receipt_ranges(Decimal("10"), [(Decimal("2"), Decimal("3"))]) == (
        (ZERO, Decimal("2")),
        (Decimal("5"), Decimal("5")),
    )


def test_adjacent_occupied_pieces_leave_no_spurious_gap() -> None:
    assert (
        free_receipt_ranges(
            Decimal("10"),
            [(ZERO, Decimal("4")), (Decimal("4"), Decimal("6"))],
        )
        == ()
    )


def test_fully_occupied_receipt_has_no_free_range() -> None:
    assert free_receipt_ranges(Decimal("5"), [(ZERO, Decimal("5"))]) == ()


def test_ranges_are_sorted_before_gaps_are_measured() -> None:
    assert free_receipt_ranges(
        Decimal("10"),
        [(Decimal("5"), Decimal("5")), (ZERO, Decimal("2"))],
    ) == ((Decimal("2"), Decimal("3")),)


@pytest.mark.parametrize(
    "occupied",
    [
        [(Decimal("2"), Decimal("4")), (Decimal("3"), Decimal("2"))],
        [(Decimal("-1"), Decimal("1"))],
        [(Decimal("2"), ZERO)],
        [(Decimal("8"), Decimal("3"))],
    ],
    ids=["overlap", "negative-offset", "zero-quantity", "past-the-end"],
)
def test_corrupt_history_is_rejected_rather_than_repriced(
    occupied: list[tuple[Decimal, Decimal]],
) -> None:
    assert free_receipt_ranges(Decimal("10"), occupied) is None


def test_quantity_scale_gaps_keep_four_places() -> None:
    assert free_receipt_ranges(
        Decimal("3.5000"),
        [(Decimal("1.2500"), Decimal("0.5000"))],
    ) == (
        (ZERO, Decimal("1.2500")),
        (Decimal("1.7500"), Decimal("1.7500")),
    )


# -----------------------------------------------------------------------------
# Prefix valuation
# -----------------------------------------------------------------------------


def test_a_prefix_is_valued_from_its_fifo_layers_and_scaled_to_the_receipt_total() -> None:
    source = _source(total="10", layers=_layers(("1", "1"), ("2", "2")))

    assert source.prefix_cost(Decimal("1")) == Decimal("2.0000")
    assert source.prefix_cost(Decimal("2")) == Decimal("6.0000")
    assert source.prefix_cost(Decimal("3")) == Decimal("10")


def test_the_booked_total_is_returned_exactly_for_the_whole_receipt() -> None:
    source = _source(total="10.0001", layers=_layers(("3", "1")))

    assert source.prefix_cost(Decimal("3")) == Decimal("10.0001")


def test_zero_quantity_and_zero_value_receipts_cost_nothing() -> None:
    zero_total = _source(total="0", layers=_layers(("5", "0")))

    assert zero_total.prefix_cost(Decimal("2")) == ZERO
    assert zero_total.prefix_cost(ZERO) == ZERO
    assert zero_total.prefix_cost(Decimal("5")) == ZERO


def test_adjacent_prefix_pieces_telescope_to_the_receipt_total() -> None:
    source = _source(total="10", layers=_layers(("3", "1")))
    pieces = [
        source.prefix_cost(Decimal("1")),
        source.prefix_cost(Decimal("2")) - source.prefix_cost(Decimal("1")),
        source.prefix_cost(Decimal("3")) - source.prefix_cost(Decimal("2")),
    ]

    assert sum(pieces, ZERO) == source.total_cost
    assert pieces == [Decimal("3.3333"), Decimal("3.3334"), Decimal("3.3333")]


def test_a_zero_cost_layer_before_a_priced_layer_keeps_the_prefix_free() -> None:
    source = _source(total="10", layers=_layers(("1", "0"), ("1", "10")))

    assert source.prefix_cost(Decimal("1")) == ZERO
    assert source.prefix_cost(Decimal("2")) == Decimal("10")


# -----------------------------------------------------------------------------
# Planning
# -----------------------------------------------------------------------------


def test_a_non_positive_credit_plans_nothing() -> None:
    source = _source()

    assert plan_cost_pieces([source], {}, ZERO) == ()
    assert plan_cost_pieces([source], {}, Decimal("-1")) == ()


def test_a_whole_credit_on_one_receipt_becomes_one_frozen_piece() -> None:
    source = _source()

    pieces = plan_cost_pieces([source], {}, Decimal("2"))

    assert len(pieces) == 1
    assert pieces[0].transaction_id == source.transaction_id
    assert pieces[0].offset == ZERO
    assert pieces[0].quantity == Decimal("2")
    assert pieces[0].cost_amount == Decimal("20")


def test_a_prefix_shared_with_an_earlier_credit_uses_only_the_free_gap() -> None:
    source = _source()

    pieces = plan_cost_pieces(
        [source],
        {source.transaction_id: [(ZERO, Decimal("1"))]},
        Decimal("2"),
    )

    assert len(pieces) == 1
    assert pieces[0].offset == Decimal("1")
    assert pieces[0].quantity == Decimal("2")
    assert pieces[0].cost_amount == Decimal("20")


def test_a_credit_spanning_receipts_keeps_one_piece_per_receipt() -> None:
    first = _source(quantity="2", total="20")
    second = _source(quantity="3", total="30")

    pieces = plan_cost_pieces(
        [first, second],
        {first.transaction_id: [(ZERO, Decimal("1"))]},
        Decimal("3"),
    )

    assert [(piece.transaction_id, piece.offset, piece.quantity) for piece in pieces] == [
        (first.transaction_id, Decimal("1"), Decimal("1")),
        (second.transaction_id, ZERO, Decimal("2")),
    ]
    assert [piece.cost_amount for piece in pieces] == [Decimal("10"), Decimal("20")]


def test_insufficient_free_cost_leaves_the_whole_line_pending() -> None:
    first = _source(quantity="2")
    second = _source(quantity="1")

    assert plan_cost_pieces([first, second], {}, Decimal("4")) == ()


def test_corrupt_history_pends_the_line_instead_of_using_later_receipts() -> None:
    first = _source()
    second = _source()

    assert (
        plan_cost_pieces(
            [first, second],
            {first.transaction_id: [(Decimal("2"), Decimal("2")), (Decimal("3"), Decimal("2"))]},
            Decimal("1"),
        )
        == ()
    )


def test_an_indivisible_receipt_can_only_be_taken_whole() -> None:
    source = _source(quantity="5", total="50", divisible=False)

    assert plan_cost_pieces([source], {}, Decimal("5"))[0].cost_amount == Decimal("50")
    assert plan_cost_pieces([source], {}, Decimal("2")) == ()


def test_an_indivisible_receipt_already_partly_used_cannot_be_reused() -> None:
    source = _source(quantity="5", total="50", divisible=False)

    assert (
        plan_cost_pieces(
            [source],
            {source.transaction_id: [(ZERO, Decimal("1"))]},
            Decimal("4"),
        )
        == ()
    )


def test_a_zero_cost_receipt_produces_a_zero_cost_piece() -> None:
    source = _source(quantity="2", total="0", layers=_layers(("2", "0")))

    pieces = plan_cost_pieces([source], {}, Decimal("1"))

    assert pieces[0].cost_amount == ZERO


# -----------------------------------------------------------------------------
# Reading a receipt movement as a cost source
# -----------------------------------------------------------------------------


def test_a_complete_layer_history_is_an_ordinary_source() -> None:
    movement = _movement(quantity="10", total="100")

    source = receipt_cost_source(
        movement,
        _model_layers(("10", "10")),
        eligible_quantity=Decimal("10"),
    )

    assert source is not None
    assert source.transaction_id == movement.id
    assert source.quantity == Decimal("10")
    assert source.eligible_quantity == Decimal("10")
    assert source.total_cost == Decimal("100")
    assert source.divisible is True


def test_a_partial_ordinary_receipt_with_one_price_is_provable() -> None:
    movement = _movement(quantity="10", total="100")

    source = receipt_cost_source(
        movement,
        _model_layers(("10", "10")),
        eligible_quantity=Decimal("4"),
    )

    assert source is not None
    assert source.prefix_cost(Decimal("4")) == Decimal("40")


def test_a_partial_ordinary_receipt_with_mixed_prices_stays_pending() -> None:
    movement = _movement(quantity="10", total="300")

    source = receipt_cost_source(
        movement,
        _model_layers(("5", "10"), ("5", "50")),
        eligible_quantity=Decimal("4"),
    )

    assert source is None


def test_a_named_serial_may_keep_its_identity_through_partial_acceptance() -> None:
    movement = _movement(quantity="10", total="300", serial_id=uuid.uuid4())

    source = receipt_cost_source(
        movement,
        _model_layers(("5", "10"), ("5", "50")),
        eligible_quantity=Decimal("4"),
    )

    assert source is not None
    assert source.divisible is True


@pytest.mark.parametrize(
    ("movement", "eligible"),
    [
        (_movement(quantity="0"), Decimal("1")),
        (_movement(quantity="-1"), Decimal("1")),
        (_movement(total="-1"), Decimal("1")),
        (_movement(), ZERO),
        (_movement(), Decimal("11")),
    ],
    ids=["zero-receipt", "negative-receipt", "negative-value", "zero-eligible", "over-eligible"],
)
def test_an_impossible_receipt_or_eligible_quantity_is_not_a_source(
    movement: SimpleNamespace, eligible: Decimal
) -> None:
    assert (
        receipt_cost_source(movement, _model_layers(("10", "10")), eligible_quantity=eligible)
        is None
    )


def test_missing_legacy_layers_value_a_whole_receipt_at_its_booked_price() -> None:
    movement = _movement(quantity="10", total="100")

    source = receipt_cost_source(movement, [], eligible_quantity=Decimal("10"))

    assert source is not None
    assert source.layers == _layers(("10", "10"))
    assert source.divisible is False


def test_missing_legacy_layers_cannot_price_only_part_of_an_ordinary_receipt() -> None:
    movement = _movement(quantity="10", total="100")

    assert receipt_cost_source(movement, [], eligible_quantity=Decimal("4")) is None


def test_missing_legacy_layers_can_price_part_of_a_named_serial() -> None:
    movement = _movement(quantity="10", total="100", serial_id=uuid.uuid4())

    source = receipt_cost_source(movement, [], eligible_quantity=Decimal("4"))

    assert source is not None
    assert source.divisible is True


def test_a_zero_value_receipt_without_layers_needs_no_invented_price() -> None:
    movement = _movement(quantity="10", total="0")

    source = receipt_cost_source(movement, [], eligible_quantity=Decimal("4"))

    assert source is not None
    assert source.prefix_cost(Decimal("4")) == ZERO


def test_layer_history_that_does_not_cover_the_receipt_is_not_rescaled() -> None:
    movement = _movement(quantity="10", total="100")

    assert (
        receipt_cost_source(
            movement,
            _model_layers(("9", "10")),
            eligible_quantity=Decimal("10"),
        )
        is None
    )


@pytest.mark.parametrize(
    "layers",
    [
        [("0", "10")],
        [("-1", "10")],
        [("10", "-1")],
    ],
    ids=["zero-quantity", "negative-quantity", "negative-price"],
)
def test_invalid_layer_evidence_is_refused(
    layers: list[tuple[str, str]],
) -> None:
    movement = _movement(quantity="10", total="100")

    assert (
        receipt_cost_source(
            movement,
            _model_layers(*layers),
            eligible_quantity=Decimal("10"),
        )
        is None
    )


def test_layers_worth_nothing_cannot_explain_a_booked_receipt_value() -> None:
    movement = _movement(quantity="10", total="100")

    assert (
        receipt_cost_source(
            movement,
            _model_layers(("10", "0")),
            eligible_quantity=Decimal("10"),
        )
        is None
    )
