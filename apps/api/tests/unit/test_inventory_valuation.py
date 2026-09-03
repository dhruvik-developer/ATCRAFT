"""FIFO cost-layer maths and the pre-flight stock checks.

Both are pure functions in the engine, and both are the kind of arithmetic that
is easy to get subtly wrong and expensive to discover later: a layer consumed
out of order makes every valuation report wrong in a way that only shows up
months later, and a shortfall rule that checks the wrong column lets a warehouse
promise stock it does not have.

``TestWeightedAverage`` used to live here. It has been replaced rather than
deleted: the cases that still describe true behaviour were carried into
``TestFifoLayerArithmetic`` and ``TestMovementValuation``, and the ones that
asserted a running average - an issue priced at the average, the average
surviving the issue unchanged - are gone on purpose, because that is exactly the
behaviour this change removes. ``TestFifoIsNotAnAverage`` is the proof that it
really is gone.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.core.errors import ValidationError
from src.modules.inventory.models import (
    InventoryCostLayer,
    TransactionType,
    fifo_sort_key,
)
from src.modules.inventory.repository import BalanceGrain
from src.modules.inventory.service import (
    FifoCostPreview,
    LayerCost,
    LayerDraw,
    LayerView,
    MovementEngine,
    MovementRequest,
    consume_layers,
    find_shortfall,
    open_layer_value,
    position_cost_basis,
    to_stock_value,
    value_movement,
)

pytestmark = pytest.mark.unit


def _buckets(
    on_hand: str = "0",
    qc_hold: str = "0",
    damaged: str = "0",
    in_transit: str = "0",
    job_work: str = "0",
    scrap: str = "0",
) -> dict[str, Decimal]:
    return {
        "on_hand_quantity": Decimal(on_hand),
        "qc_hold_quantity": Decimal(qc_hold),
        "damaged_quantity": Decimal(damaged),
        "in_transit_quantity": Decimal(in_transit),
        "job_work_quantity": Decimal(job_work),
        "scrap_quantity": Decimal(scrap),
    }


class TestFifoLayerArithmetic:
    """Consumption order, partial draws and exhaustion, with no database.

    This class replaces ``TestWeightedAverage``, which asserted the behaviour
    FIFO was brought in to remove: that an issue is priced at a running average
    and that the average survives the issue unchanged. Every case that still
    describes something true has been carried over rather than deleted - a pool
    move changes nothing, an unpriced receipt falls back to standard cost, a
    caller cannot price an outbound movement, exactness is not negotiable - and
    each of them is now stated against layers.
    """

    def test_a_single_layer_covers_the_whole_draw(self) -> None:
        result = consume_layers(
            [LayerView(Decimal("100"), Decimal("7.5"))],
            Decimal("40"),
            uncovered_unit_cost=Decimal("0"),
        )
        assert result.draws == (
            LayerDraw(index=0, quantity=Decimal("40.0000"), unit_cost=Decimal("7.500000")),
        )
        assert result.uncovered_quantity == 0
        assert result.value == Decimal("300.0000")

    def test_a_draw_spans_layers_and_takes_them_oldest_first(self) -> None:
        """The worked example: 30 against [20 @ 100, 50 @ 120] costs 3 200."""
        result = consume_layers(
            [
                LayerView(Decimal("20"), Decimal("100")),
                LayerView(Decimal("50"), Decimal("120")),
            ],
            Decimal("30"),
            uncovered_unit_cost=Decimal("0"),
        )
        assert [(d.index, d.quantity, d.unit_cost) for d in result.draws] == [
            (0, Decimal("20.0000"), Decimal("100.000000")),
            (1, Decimal("10.0000"), Decimal("120.000000")),
        ]
        assert result.value == Decimal("3200")
        assert result.covered_quantity == Decimal("30.0000")

    def test_a_draw_that_exactly_empties_a_layer_does_not_touch_the_next(self) -> None:
        result = consume_layers(
            [
                LayerView(Decimal("20"), Decimal("100")),
                LayerView(Decimal("50"), Decimal("120")),
            ],
            Decimal("20"),
            uncovered_unit_cost=Decimal("0"),
        )
        assert len(result.draws) == 1
        assert result.value == Decimal("2000")

    def test_an_exhausted_layer_is_skipped_rather_than_drawn_on(self) -> None:
        """A zero-remaining layer stays in the queue as history and contributes nothing."""
        result = consume_layers(
            [
                LayerView(Decimal("0"), Decimal("100")),
                LayerView(Decimal("5"), Decimal("120")),
            ],
            Decimal("5"),
            uncovered_unit_cost=Decimal("0"),
        )
        assert [d.index for d in result.draws] == [1]
        assert result.value == Decimal("600")

    def test_a_zero_cost_layer_is_still_consumed(self) -> None:
        """Free stock is stock. Skipping it would break the quantity invariant."""
        result = consume_layers(
            [
                LayerView(Decimal("10"), Decimal("0")),
                LayerView(Decimal("10"), Decimal("50")),
            ],
            Decimal("15"),
            uncovered_unit_cost=Decimal("999"),
        )
        assert [(d.index, d.quantity) for d in result.draws] == [
            (0, Decimal("10.0000")),
            (1, Decimal("5.0000")),
        ]
        assert result.uncovered_quantity == 0
        assert result.value == Decimal("250")

    def test_a_draw_the_layers_cannot_cover_reports_what_is_missing(self) -> None:
        """The floor beneath ``_reconcile_layers``, not a routine path."""
        result = consume_layers(
            [LayerView(Decimal("10"), Decimal("50"))],
            Decimal("25"),
            uncovered_unit_cost=Decimal("50"),
        )
        assert result.covered_quantity == Decimal("10.0000")
        assert result.uncovered_quantity == Decimal("15.0000")
        # Only the covered part comes out of layer value; the rest is priced.
        assert result.layer_value == Decimal("500")
        assert result.value == Decimal("1250")

    def test_the_cost_basis_is_value_over_quantity_not_an_average_of_prices(self) -> None:
        """10 @ 100 beside 990 @ 10 costs 10.9 a unit, not 55."""
        layers = [
            LayerView(Decimal("10"), Decimal("100")),
            LayerView(Decimal("990"), Decimal("10")),
        ]
        assert position_cost_basis(layers) == Decimal("10.900000")
        assert open_layer_value(layers) == Decimal("10900")

    def test_an_empty_position_has_no_cost_basis(self) -> None:
        assert position_cost_basis([]) == Decimal("0.000000")
        assert open_layer_value([]) == Decimal("0")


class TestFifoIsNotAnAverage:
    """The test that proves the method changed rather than the label.

    A rising-price series is where the two methods diverge, and every figure
    below is different under each. If this class ever passes with the weighted
    average restored, the implementation is average wearing a new name.
    """

    #: Buy 100 @ 10, then 100 @ 20. Weighted average: 200 units at 15.
    RISING = (
        LayerCost(quantity=Decimal("100"), unit_cost=Decimal("10")),
        LayerCost(quantity=Decimal("100"), unit_cost=Decimal("20")),
    )

    @staticmethod
    def _position(*costs: LayerCost) -> list[LayerView]:
        return [LayerView(remaining=c.quantity, unit_cost=c.unit_cost) for c in costs]

    def test_the_issue_costs_the_old_price_not_the_blended_one(self) -> None:
        result = value_movement(
            on_hand_after=Decimal("100"),
            magnitude=Decimal("100"),
            on_hand_delta=Decimal("-100"),
            layers=self._position(*self.RISING),
            average_before=Decimal("15"),
            movement_unit_cost=None,
            fallback_cost=Decimal("0"),
        )
        weighted_average_answer = Decimal("1500.00")
        assert result.total_cost == Decimal("1000.00")
        assert result.total_cost != weighted_average_answer
        assert result.unit_cost == Decimal("10.000000")

    def test_what_is_left_is_the_dear_stock(self) -> None:
        result = value_movement(
            on_hand_after=Decimal("100"),
            magnitude=Decimal("100"),
            on_hand_delta=Decimal("-100"),
            layers=self._position(*self.RISING),
            average_before=Decimal("15"),
            movement_unit_cost=None,
            fallback_cost=Decimal("0"),
        )
        weighted_average_answer = Decimal("1500.00")
        assert result.stock_value == Decimal("2000.00")
        assert result.stock_value != weighted_average_answer
        # The derived display figure follows the layers, not a carried average.
        assert result.average_cost == Decimal("20.000000")

    def test_a_second_receipt_does_not_re_price_the_first(self) -> None:
        """A weighted average would report 60 here; FIFO keeps the two layers."""
        result = value_movement(
            on_hand_after=Decimal("200"),
            magnitude=Decimal("100"),
            on_hand_delta=Decimal("100"),
            layers=self._position(LayerCost(Decimal("100"), Decimal("50"))),
            average_before=Decimal("50"),
            movement_unit_cost=Decimal("70"),
            fallback_cost=Decimal("0"),
        )
        assert result.opened == (
            LayerCost(quantity=Decimal("100.0000"), unit_cost=Decimal("70.000000")),
        )
        assert result.stock_value == Decimal("12000.00")
        # 12 000 / 200 does equal 60 - but as a quotient of the layers, and the
        # next issue proves it is not a cost basis: it draws at 50.
        assert result.average_cost == Decimal("60.000000")
        issue = value_movement(
            on_hand_after=Decimal("150"),
            magnitude=Decimal("50"),
            on_hand_delta=Decimal("-50"),
            layers=self._position(
                LayerCost(Decimal("100"), Decimal("50")), LayerCost(Decimal("100"), Decimal("70"))
            ),
            average_before=Decimal("60"),
            movement_unit_cost=None,
            fallback_cost=Decimal("0"),
        )
        assert issue.unit_cost == Decimal("50.000000")


class TestMovementValuation:
    """The whole-movement function, including the cases carried over from before."""

    def test_a_first_receipt_opens_one_layer_at_the_purchase_price(self) -> None:
        result = value_movement(
            on_hand_after=Decimal("100"),
            magnitude=Decimal("100"),
            on_hand_delta=Decimal("100"),
            layers=[],
            average_before=Decimal("0"),
            movement_unit_cost=Decimal("50"),
            fallback_cost=Decimal("999"),
        )
        assert result.unit_cost == Decimal("50")
        assert result.total_cost == Decimal("5000.00")
        assert result.stock_value == Decimal("5000.00")
        assert result.average_cost == Decimal("50")
        assert result.opened == (
            LayerCost(quantity=Decimal("100.0000"), unit_cost=Decimal("50.000000")),
        )

    def test_a_receipt_without_a_price_falls_back_to_standard_cost(self) -> None:
        result = value_movement(
            on_hand_after=Decimal("5"),
            magnitude=Decimal("5"),
            on_hand_delta=Decimal("5"),
            layers=[],
            average_before=Decimal("0"),
            movement_unit_cost=None,
            fallback_cost=Decimal("12.5"),
        )
        assert result.unit_cost == Decimal("12.5")
        assert result.stock_value == Decimal("62.50")

    def test_a_receipt_with_neither_price_nor_standard_cost_uses_the_position(self) -> None:
        """Last resorts, in order: what is on the shelf, then its last known cost."""
        result = value_movement(
            on_hand_after=Decimal("20"),
            magnitude=Decimal("10"),
            on_hand_delta=Decimal("10"),
            layers=[LayerView(Decimal("10"), Decimal("8"))],
            average_before=Decimal("42"),
            movement_unit_cost=None,
            fallback_cost=Decimal("0"),
        )
        assert result.unit_cost == Decimal("8")

        emptied = value_movement(
            on_hand_after=Decimal("10"),
            magnitude=Decimal("10"),
            on_hand_delta=Decimal("10"),
            layers=[],
            average_before=Decimal("42"),
            movement_unit_cost=None,
            fallback_cost=Decimal("0"),
        )
        assert emptied.unit_cost == Decimal("42")

    def test_an_issue_cannot_be_valued_below_the_layers_by_the_caller(self) -> None:
        """Otherwise a document could drain a position of value by quoting a price."""
        result = value_movement(
            on_hand_after=Decimal("90"),
            magnitude=Decimal("10"),
            on_hand_delta=Decimal("-10"),
            layers=[LayerView(Decimal("100"), Decimal("60"))],
            average_before=Decimal("60"),
            movement_unit_cost=Decimal("1"),
            fallback_cost=Decimal("0"),
        )
        assert result.unit_cost == Decimal("60")

    def test_a_pool_move_leaves_value_and_cost_untouched(self) -> None:
        """Putting stock on QC hold does not change what it is worth."""
        result = value_movement(
            on_hand_after=Decimal("100"),
            magnitude=Decimal("20"),
            on_hand_delta=Decimal("0"),
            layers=[LayerView(Decimal("100"), Decimal("7.5"))],
            average_before=Decimal("7.5"),
            movement_unit_cost=None,
            fallback_cost=Decimal("0"),
        )
        assert result.average_cost == Decimal("7.5")
        assert result.stock_value == Decimal("750.00")
        assert result.opened == ()
        assert result.draws == ()
        # The ledger row still carries a price, at the position's own cost.
        assert result.unit_cost == Decimal("7.5")
        assert result.total_cost == Decimal("150.00")

    def test_the_last_cost_survives_a_position_going_to_zero(self) -> None:
        """No layers left to derive from, so the previous figure stands.

        ``stock_value`` is zero either way, so nothing on the balance sheet
        turns on it - but an adjustment or a count re-booking stock into the
        emptied position uses it as the price.
        """
        result = value_movement(
            on_hand_after=Decimal("0"),
            magnitude=Decimal("10"),
            on_hand_delta=Decimal("-10"),
            layers=[LayerView(Decimal("10"), Decimal("42"))],
            average_before=Decimal("42"),
            movement_unit_cost=None,
            fallback_cost=Decimal("0"),
        )
        assert result.stock_value == Decimal("0.00")
        assert result.average_cost == Decimal("42")

    def test_an_arrival_can_carry_an_exact_cost_breakdown(self) -> None:
        """A transfer re-opens at its destination what its source gave up."""
        result = value_movement(
            on_hand_after=Decimal("30"),
            magnitude=Decimal("30"),
            on_hand_delta=Decimal("30"),
            layers=[],
            average_before=Decimal("0"),
            movement_unit_cost=Decimal("999"),
            fallback_cost=Decimal("999"),
            carried=(
                LayerCost(Decimal("20"), Decimal("100")),
                LayerCost(Decimal("10"), Decimal("120")),
            ),
        )
        assert result.opened == (
            LayerCost(quantity=Decimal("20.0000"), unit_cost=Decimal("100.000000")),
            LayerCost(quantity=Decimal("10.0000"), unit_cost=Decimal("120.000000")),
        )
        assert result.stock_value == Decimal("3200.00")
        # The ledger row blends; the layers do not.
        assert result.unit_cost == Decimal("106.666667")

    def test_a_short_carried_breakdown_is_topped_up_rather_than_refused(self) -> None:
        """Refusing would strand goods that are physically at the destination."""
        result = value_movement(
            on_hand_after=Decimal("30"),
            magnitude=Decimal("30"),
            on_hand_delta=Decimal("30"),
            layers=[],
            average_before=Decimal("0"),
            movement_unit_cost=Decimal("5"),
            fallback_cost=Decimal("0"),
            carried=(LayerCost(Decimal("20"), Decimal("100")),),
        )
        assert result.opened == (
            LayerCost(quantity=Decimal("20.0000"), unit_cost=Decimal("100.000000")),
            LayerCost(quantity=Decimal("10.0000"), unit_cost=Decimal("5.000000")),
        )
        assert result.stock_value == Decimal("2050.00")

    def test_a_carried_breakdown_larger_than_the_arrival_is_refused(self) -> None:
        """It would book value the movement never carried."""
        with pytest.raises(ValidationError):
            value_movement(
                on_hand_after=Decimal("10"),
                magnitude=Decimal("10"),
                on_hand_delta=Decimal("10"),
                layers=[],
                average_before=Decimal("0"),
                movement_unit_cost=Decimal("5"),
                fallback_cost=Decimal("0"),
                carried=(LayerCost(Decimal("20"), Decimal("100")),),
            )

    def test_decimals_are_exact_not_floating_point(self) -> None:
        """0.1 + 0.2 arithmetic must not leak into a stock value (section 84)."""
        result = value_movement(
            on_hand_after=Decimal("0.3"),
            magnitude=Decimal("0.2"),
            on_hand_delta=Decimal("0.2"),
            layers=[LayerView(Decimal("0.1"), Decimal("0.1"))],
            average_before=Decimal("0.1"),
            movement_unit_cost=Decimal("0.2"),
            fallback_cost=Decimal("0"),
        )
        # 0.1*0.1 + 0.2*0.2 = 0.05 exactly, and 0.05 / 0.3 = 0.166667.
        assert result.stock_value == Decimal("0.05")
        assert result.average_cost == Decimal("0.166667")

    def test_a_round_trip_at_the_issued_cost_neither_creates_nor_destroys_value(self) -> None:
        """A return that names what it was issued at leaves the position where it started."""
        layers = [LayerView(Decimal("100"), Decimal("12"))]
        issue = value_movement(
            on_hand_after=Decimal("60"),
            magnitude=Decimal("40"),
            on_hand_delta=Decimal("-40"),
            layers=layers,
            average_before=Decimal("12"),
            movement_unit_cost=None,
            fallback_cost=Decimal("0"),
        )
        assert issue.stock_value == Decimal("720.00")

        returned = value_movement(
            on_hand_after=Decimal("100"),
            magnitude=Decimal("40"),
            on_hand_delta=Decimal("40"),
            layers=[LayerView(Decimal("60"), Decimal("12"))],
            average_before=issue.average_cost,
            movement_unit_cost=issue.unit_cost,
            fallback_cost=Decimal("0"),
        )
        assert returned.stock_value == Decimal("1200.00")
        assert returned.average_cost == Decimal("12")


class TestFifoOrdering:
    """The tiebreaker, which is what makes the consumption order total.

    ``received_at`` is business time and several callers stamp it as a calendar
    date at midnight, so two layers sharing it exactly is ordinary rather than
    exotic. Ordering on the timestamp alone would hand them back in whatever
    order the database felt like - the trap CLAUDE.md records, and the one that
    bites FIFO hardest.
    """

    @staticmethod
    def _layer(received_at: datetime, layer_id: uuid.UUID) -> InventoryCostLayer:
        return InventoryCostLayer(id=layer_id, received_at=received_at)

    def test_business_time_decides_first(self) -> None:
        older = self._layer(datetime(2026, 3, 1, tzinfo=UTC), uuid.UUID(int=99))
        newer = self._layer(datetime(2026, 3, 2, tzinfo=UTC), uuid.UUID(int=1))
        assert sorted([newer, older], key=fifo_sort_key) == [older, newer]

    def test_the_primary_key_breaks_a_tie(self) -> None:
        """Two layers opened by one document share a timestamp to the microsecond."""
        stamp = datetime(2026, 3, 1, tzinfo=UTC)
        first = self._layer(stamp, uuid.UUID(int=1))
        second = self._layer(stamp, uuid.UUID(int=2))
        assert sorted([second, first], key=fifo_sort_key) == [first, second]

    def test_a_naive_timestamp_is_read_as_utc_rather_than_raising(self) -> None:
        """A sort in the middle of a posting is the wrong place to discover it."""
        naive = self._layer(datetime(2026, 3, 1), uuid.UUID(int=1))  # noqa: DTZ001
        aware = self._layer(datetime(2026, 3, 2, tzinfo=UTC), uuid.UUID(int=2))
        assert sorted([aware, naive], key=fifo_sort_key) == [naive, aware]


class TestCostCarrierValidation:
    """``carries_cost_of`` is a programming error when it is wrong, so it is loud.

    A transfer whose inbound leg silently failed to pick up its outbound leg's
    layers would re-cost the arrival and change the company's stock value by
    moving a box, with nothing anywhere saying so.
    """

    @staticmethod
    def _movement(transaction_type: TransactionType, **kwargs: object) -> MovementRequest:
        return MovementRequest(
            item_id=uuid.UUID(int=1),
            warehouse_id=uuid.UUID(int=2),
            quantity=Decimal("10"),
            transaction_type=transaction_type,
            reference_type="stock_transfer",
            reason="carry",
            **kwargs,  # type: ignore[arg-type]
        )

    def test_the_two_legs_of_a_transfer_are_accepted(self) -> None:
        MovementEngine._assert_cost_carriers(
            [
                self._movement(TransactionType.TRANSFER_OUT),
                self._movement(TransactionType.TRANSFER_IN, carries_cost_of=0),
            ]
        )

    def test_pointing_forwards_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            MovementEngine._assert_cost_carriers(
                [
                    self._movement(TransactionType.TRANSFER_IN, carries_cost_of=1),
                    self._movement(TransactionType.TRANSFER_OUT),
                ]
            )

    def test_pointing_at_itself_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            MovementEngine._assert_cost_carriers(
                [self._movement(TransactionType.TRANSFER_IN, carries_cost_of=0)]
            )

    def test_an_outflow_cannot_carry_a_cost(self) -> None:
        with pytest.raises(ValidationError):
            MovementEngine._assert_cost_carriers(
                [
                    self._movement(TransactionType.TRANSFER_OUT),
                    self._movement(TransactionType.MATERIAL_ISSUE, carries_cost_of=0),
                ]
            )

    def test_carrying_the_cost_of_an_inflow_is_refused(self) -> None:
        """There is no layer draw to carry - the donor opened layers, it spent none."""
        with pytest.raises(ValidationError):
            MovementEngine._assert_cost_carriers(
                [
                    self._movement(TransactionType.PURCHASE_RECEIPT),
                    self._movement(TransactionType.TRANSFER_IN, carries_cost_of=0),
                ]
            )


class TestShortfallRules:
    def test_a_movement_within_availability_passes(self) -> None:
        assert (
            find_shortfall(
                before=_buckets(on_hand="100"),
                after=_buckets(on_hand="20"),
                reserved=Decimal("0"),
                allow_negative_available=False,
            )
            is None
        )

    def test_taking_more_than_available_is_a_shortfall(self) -> None:
        shortfall = find_shortfall(
            before=_buckets(on_hand="100"),
            after=_buckets(on_hand="-30"),
            reserved=Decimal("0"),
            allow_negative_available=False,
        )
        assert shortfall is not None
        assert shortfall.rule == "on_hand"
        assert shortfall.available == Decimal("100")

    def test_reserved_stock_is_not_available_to_someone_else(self) -> None:
        """80 on hand with 60 reserved leaves 20 to promise, not 80."""
        shortfall = find_shortfall(
            before=_buckets(on_hand="80"),
            after=_buckets(on_hand="50"),
            reserved=Decimal("60"),
            allow_negative_available=False,
        )
        assert shortfall is not None
        assert shortfall.rule == "reserved"

    def test_qc_held_stock_is_not_available(self) -> None:
        shortfall = find_shortfall(
            before=_buckets(on_hand="100", qc_hold="90"),
            after=_buckets(on_hand="80", qc_hold="90"),
            reserved=Decimal("0"),
            allow_negative_available=False,
        )
        assert shortfall is not None
        assert shortfall.rule == "available"
        assert shortfall.available == Decimal("10")

    def test_allow_negative_stock_relaxes_availability_only(self) -> None:
        """The warehouse flag relaxes the pre-flight check, never the floor."""
        assert (
            find_shortfall(
                before=_buckets(on_hand="100", qc_hold="90"),
                after=_buckets(on_hand="80", qc_hold="90"),
                reserved=Decimal("0"),
                allow_negative_available=True,
            )
            is None
        )
        negative_on_hand = find_shortfall(
            before=_buckets(on_hand="10"),
            after=_buckets(on_hand="-5"),
            reserved=Decimal("0"),
            allow_negative_available=True,
        )
        assert negative_on_hand is not None
        assert negative_on_hand.rule == "on_hand"

    def test_releasing_more_than_is_held_is_refused(self) -> None:
        shortfall = find_shortfall(
            before=_buckets(on_hand="100", qc_hold="5"),
            after=_buckets(on_hand="100", qc_hold="-15"),
            reserved=Decimal("0"),
            allow_negative_available=False,
        )
        assert shortfall is not None
        assert shortfall.rule == "qc_hold_quantity"
        assert shortfall.available == Decimal("5")

    def test_receiving_more_job_work_than_was_issued_is_refused(self) -> None:
        shortfall = find_shortfall(
            before=_buckets(on_hand="0", job_work="10"),
            after=_buckets(on_hand="25", job_work="-15"),
            reserved=Decimal("0"),
            allow_negative_available=False,
        )
        assert shortfall is not None
        assert shortfall.rule == "job_work_quantity"

    def test_on_hand_is_reported_before_a_pool_underflow(self) -> None:
        """Negative physical stock is the more serious of the two failures."""
        shortfall = find_shortfall(
            before=_buckets(on_hand="1", qc_hold="1"),
            after=_buckets(on_hand="-1", qc_hold="-1"),
            reserved=Decimal("0"),
            allow_negative_available=False,
        )
        assert shortfall is not None
        assert shortfall.rule == "on_hand"


class _Position:
    """The three balance columns the preview reads. Not the ORM row.

    The preview takes a balance and a list of layers and does arithmetic on
    them; giving it a plain object keeps these cases free of a database and of
    an ORM identity map, which is the point of a unit test of a costing rule.
    """

    def __init__(self, on_hand: str, average_cost: str = "0") -> None:
        self.id = uuid.uuid4()
        self.on_hand_quantity = Decimal(on_hand)
        self.average_cost = Decimal(average_cost)


class _Layer:
    def __init__(self, remaining: str, unit_cost: str) -> None:
        self.remaining_quantity = Decimal(remaining)
        self.unit_cost = Decimal(unit_cost)


def _grain() -> BalanceGrain:
    return BalanceGrain(
        item_id=uuid.uuid4(), warehouse_id=uuid.uuid4(), location_id=None, batch_id=None
    )


def _preview(grain: BalanceGrain, position: _Position, *layers: _Layer) -> FifoCostPreview:
    return FifoCostPreview({grain: position}, {position.id: list(layers)})  # type: ignore[arg-type]


class TestStockValuePrecision:
    """``stock_value`` is rounded at the scale its column stores, not at two.

    ``Money`` is ``NUMERIC(18, 4)``; ``to_money`` quantizes to two places
    because that is the scale a *document total* is presented at. Using it for a
    position's value silently discarded two places the column was holding, once
    per position, so the same stock was worth a different amount depending on
    how many positions it was spread over.
    """

    def test_a_position_keeps_four_places(self) -> None:
        assert to_stock_value(Decimal("3.33333333")) == Decimal("3.3333")

    def test_it_rounds_half_up_like_every_other_money_helper(self) -> None:
        assert to_stock_value(Decimal("0.00005")) == Decimal("0.0001")

    def test_a_movement_values_the_position_at_four_places(self) -> None:
        valuation = value_movement(
            on_hand_after=Decimal("3"),
            magnitude=Decimal("3"),
            on_hand_delta=Decimal("3"),
            layers=[],
            average_before=Decimal("0"),
            movement_unit_cost=Decimal("1.111111"),
            fallback_cost=Decimal("0"),
        )
        assert valuation.stock_value == Decimal("3.3333")

    def test_splitting_a_position_loses_less_than_a_hundredth_of_a_paisa(self) -> None:
        """The reason the two-place rounding mattered, stated as a number.

        Seven units at 3.333333 are worth 23.333331. Rounded at four places each
        half is 23.3333 and the pair is 46.6666, against an exact 46.666662 - a
        residual under 0.0001. Rounded at two places each half was 23.33 and the
        pair 46.66, which is 0.0067 of company value created by nothing more
        than which warehouse the goods were in.
        """
        exact = Decimal("7") * Decimal("3.333333")
        halves = to_stock_value(exact) * 2
        assert abs(halves - exact * 2) <= Decimal("0.0001")


class TestThereIsNoPerItemValuationMethod:
    """The column is gone, and this is the guard that it stays gone.

    ``items.valuation_method`` existed as ``FIFO | WEIGHTED_AVERAGE | STANDARD``
    and defaulted to ``WEIGHTED_AVERAGE``, while the engine costed every item
    FIFO regardless. It described a choice nothing honoured, and for most rows it
    described the wrong one. Rather than implement dispatch for methods nobody
    asked for, the decision was FIFO for everything and the column was dropped
    (revision ``7e8eb4bdd0ed``).

    Re-adding it without the costing dispatch that reads it puts the lie back, so
    this fails if the column returns alone.
    """

    def test_the_item_model_declares_no_valuation_method(self) -> None:
        from src.modules.products.models import Item

        assert "valuation_method" not in Item.__table__.c, (
            "items.valuation_method is back. If per-item valuation is wanted, the "
            "column must land WITH the costing dispatch in inventory.service that "
            "reads it - a column the engine ignores is worse than no column, "
            "because a reader plans an audit around a valuation never applied."
        )

    def test_the_engine_exposes_no_per_item_costing_switch(self) -> None:
        from src.modules.inventory import service

        for name in ("resolve_costing_method", "DEFAULT_VALUATION_METHOD"):
            assert not hasattr(service, name), f"{name} is back without the column it dispatched on"


class TestFifoCostPreview:
    """What a document says it will cost has to be what the posting charges.

    The document services price their lines through this, and the engine posts
    through :func:`consume_layers`. The cases below are the ones where a
    quantity times an average gives a different answer - which is every position
    holding more than one price.
    """

    def test_a_write_off_is_priced_from_the_layers_it_will_consume(self) -> None:
        """The reproduction: 100 @ 500 then 900 @ 10, write off 60.

        The average is 59 and 60 x 59 is 3 540. The layers say the sixty that go
        are the oldest sixty, at 500 each: 30 000.
        """
        grain = _grain()
        preview = _preview(
            grain,
            _Position("1000", "59"),
            _Layer("100", "500"),
            _Layer("900", "10"),
        )
        cost = preview.line_cost(grain, Decimal("-60"), supplied=None, fallback_cost=Decimal("0"))
        assert cost.total_cost == Decimal("30000.00")
        assert cost.unit_cost == Decimal("500.000000")

    def test_a_draw_that_spans_two_layers_is_blended_only_for_display(self) -> None:
        grain = _grain()
        preview = _preview(grain, _Position("120", "0"), _Layer("100", "500"), _Layer("20", "10"))
        cost = preview.line_cost(grain, Decimal("-110"), supplied=None, fallback_cost=Decimal("0"))
        # 100 @ 500 + 10 @ 10.
        assert cost.total_cost == Decimal("50100.00")

    def test_two_lines_on_one_position_consume_in_sequence(self) -> None:
        """Not both off the same oldest layer, which would double-count it."""
        grain = _grain()
        preview = _preview(
            grain, _Position("1000", "59"), _Layer("100", "500"), _Layer("900", "10")
        )
        first = preview.line_cost(grain, Decimal("-100"), supplied=None, fallback_cost=Decimal("0"))
        second = preview.line_cost(
            grain, Decimal("-100"), supplied=None, fallback_cost=Decimal("0")
        )
        assert first.total_cost == Decimal("50000.00")
        assert second.total_cost == Decimal("1000.00")

    def test_an_increase_is_priced_at_the_cost_the_layer_will_open_at(self) -> None:
        """The engine takes an arrival's supplied cost first, so the preview must."""
        grain = _grain()
        preview = _preview(grain, _Position("10", "3"), _Layer("10", "3"))
        cost = preview.line_cost(
            grain, Decimal("5"), supplied=Decimal("40"), fallback_cost=Decimal("7")
        )
        assert cost.unit_cost == Decimal("40.000000")
        assert cost.total_cost == Decimal("200.00")

    def test_an_arrival_joins_the_back_of_the_queue(self) -> None:
        """A later line on the same position draws the old stock first."""
        grain = _grain()
        preview = _preview(grain, _Position("10", "3"), _Layer("10", "3"))
        preview.line_cost(grain, Decimal("10"), supplied=Decimal("99"), fallback_cost=Decimal("0"))
        drawn = preview.line_cost(grain, Decimal("-10"), supplied=None, fallback_cost=Decimal("0"))
        assert drawn.total_cost == Decimal("30.00")

    def test_a_position_short_of_layers_is_previewed_as_it_will_be_repaired(
        self,
    ) -> None:
        """The engine opens a repair layer at the recorded cost before it posts.

        A preview that ignored that would price the uncovered part at nothing
        and state a cost the posting is guaranteed not to charge.
        """
        grain = _grain()
        preview = _preview(grain, _Position("100", "7"))
        cost = preview.line_cost(grain, Decimal("-30"), supplied=None, fallback_cost=Decimal("0"))
        assert cost.total_cost == Decimal("210.00")

    def test_a_position_whose_layers_exceed_it_is_previewed_as_it_will_be_trimmed(
        self,
    ) -> None:
        """The other direction, and the same reason: match what will be posted.

        The position holds 10 but the layers claim 20 - a cheap lot the engine
        will trim away as the replay of the movement they missed. The next issue
        therefore costs 50, not 5.
        """
        grain = _grain()
        preview = _preview(grain, _Position("10", "50"), _Layer("10", "5"), _Layer("10", "50"))
        cost = preview.line_cost(grain, Decimal("-1"), supplied=None, fallback_cost=Decimal("0"))
        assert cost.unit_cost == Decimal("50.000000")

    def test_a_line_against_a_position_that_does_not_exist_falls_back(self) -> None:
        grain = _grain()
        preview = FifoCostPreview({}, {})
        assert preview.on_hand(grain) == Decimal("0")
        cost = preview.line_cost(grain, Decimal("4"), supplied=None, fallback_cost=Decimal("12.5"))
        assert cost.unit_cost == Decimal("12.500000")
        assert cost.total_cost == Decimal("50.00")

    def test_a_zero_line_costs_nothing_rather_than_raising(self) -> None:
        """A count line whose variance is zero is priced, not refused."""
        grain = _grain()
        preview = _preview(grain, _Position("10", "3"), _Layer("10", "3"))
        cost = preview.line_cost(grain, Decimal("0"), supplied=None, fallback_cost=Decimal("0"))
        assert cost.total_cost == Decimal("0.00")
