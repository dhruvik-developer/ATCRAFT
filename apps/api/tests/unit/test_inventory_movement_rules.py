"""Movement arithmetic and master-data rules - no database involved.

These are the rules the whole ERP depends on being right, expressed as pure
functions precisely so they can be pinned down here rather than only observed
through a posting. Three things are under test:

* every transaction type carries the sign master rules section 83 gives it, and
  the caller cannot override it;
* every type changes exactly the balance columns it should, including the two
  that deliberately do *not* change ``on_hand_quantity``;
* an item that is batch or serial tracked cannot be *received* without its
  traceability. The two flags then diverge on the way out, and the divergence is
  deliberate: ``batch_id`` is part of ``BalanceGrain``, so a batch-less departure
  can only reach the batch-less position and a flag flipped over existing stock
  must not make that stock immovable. ``serial_id`` is not part of the grain, so
  an unnamed departure would take identified units and leave their rows saying
  IN_STOCK - the serial rule therefore still fires on any movement that changes
  whose hands hold the unit.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from src.core.errors import BusinessRuleError, ErrorCode, ValidationError
from src.modules.inventory.models import (
    DECREASE_TYPES,
    INCREASE_TYPES,
    ON_HAND_NEUTRAL_TYPES,
    TRANSACTION_SIGNS,
    StockState,
    TransactionType,
)
from src.modules.inventory.repository import ItemFacts, WarehouseFacts
from src.modules.inventory.service import (
    SERIAL_MOVEMENT_TARGETS,
    MovementRequest,
    movement_effect,
    signed_quantity,
    state_change_type,
    validate_movement,
)

pytestmark = pytest.mark.unit

#: Every movement that takes a quantity *out* of a position it already holds.
#: The BATCH rule must never fire on one of these - the grain confines it. The
#: SERIAL rule still does, for the ones that change whose hands hold the unit:
#: ``serial_id`` is not part of the grain, so an unnamed departure would take
#: identified units and leave their rows saying IN_STOCK.
OUTBOUND_TYPES = sorted(DECREASE_TYPES, key=str)


def _item(**overrides: object) -> ItemFacts:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "code": "RM-001",
        "name": "Brass rod",
        "is_batch_tracked": False,
        "is_serial_tracked": False,
        "is_active": True,
        "standard_cost": Decimal("100"),
        "reorder_level": Decimal("10"),
    }
    defaults.update(overrides)
    return ItemFacts(**defaults)  # type: ignore[arg-type]


def _warehouse(**overrides: object) -> WarehouseFacts:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "code": "RM-01",
        "name": "Raw material store",
        "is_active": True,
        "allow_negative_stock": False,
    }
    defaults.update(overrides)
    return WarehouseFacts(**defaults)  # type: ignore[arg-type]


def _movement(item: ItemFacts, warehouse: WarehouseFacts, **overrides: object) -> MovementRequest:
    defaults: dict[str, object] = {
        "item_id": item.id,
        "warehouse_id": warehouse.id,
        "quantity": Decimal("5"),
        "transaction_type": TransactionType.PURCHASE_RECEIPT,
        "reference_type": "goods_receipt",
        "reason": "GRN posting",
    }
    defaults.update(overrides)
    return MovementRequest(**defaults)  # type: ignore[arg-type]


class TestSignedQuantity:
    def test_every_transaction_type_has_a_sign(self) -> None:
        """A type without a declared sign would post an unsigned quantity."""
        assert set(TRANSACTION_SIGNS) == set(TransactionType)
        assert set(TransactionType) == INCREASE_TYPES | DECREASE_TYPES
        assert not INCREASE_TYPES & DECREASE_TYPES

    @pytest.mark.parametrize("transaction_type", sorted(INCREASE_TYPES))
    def test_increase_types_produce_a_positive_quantity(
        self, transaction_type: TransactionType
    ) -> None:
        assert signed_quantity(transaction_type, Decimal("12")) == Decimal("12")

    @pytest.mark.parametrize("transaction_type", sorted(DECREASE_TYPES))
    def test_decrease_types_produce_a_negative_quantity(
        self, transaction_type: TransactionType
    ) -> None:
        assert signed_quantity(transaction_type, Decimal("12")) == Decimal("-12")

    @pytest.mark.parametrize("transaction_type", sorted(DECREASE_TYPES))
    def test_an_already_negative_quantity_is_idempotent_on_a_decrease(
        self, transaction_type: TransactionType
    ) -> None:
        """Re-posting a ledger row's own signed quantity must not flip it back."""
        assert signed_quantity(transaction_type, Decimal("-12")) == Decimal("-12")

    @pytest.mark.parametrize("transaction_type", sorted(INCREASE_TYPES))
    def test_a_negative_receipt_is_refused(self, transaction_type: TransactionType) -> None:
        """A negative receipt is a keying error, never a compact way to issue."""
        with pytest.raises(ValidationError):
            signed_quantity(transaction_type, Decimal("-1"))

    def test_zero_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            signed_quantity(TransactionType.MATERIAL_ISSUE, Decimal("0"))

    def test_quantity_is_normalised_to_the_quantity_scale(self) -> None:
        # Quantity is NUMERIC(18,4); a value carrying more places would be
        # silently rounded by the database instead of by us.
        assert signed_quantity(TransactionType.PURCHASE_RECEIPT, Decimal("1.000049")) == Decimal(
            "1.0000"
        )


class TestBalanceEffects:
    def test_a_receipt_only_adds_to_on_hand(self) -> None:
        effect = movement_effect(TransactionType.PURCHASE_RECEIPT)
        assert effect.on_hand_quantity == 1
        assert effect.qc_hold_quantity == 0
        assert effect.damaged_quantity == 0

    def test_a_receipt_into_qc_hold_raises_both_columns(self) -> None:
        """Goods received pending inspection are present but not promisable."""
        effect = movement_effect(TransactionType.PURCHASE_RECEIPT, StockState.QC_HOLD)
        assert effect.on_hand_quantity == 1
        assert effect.qc_hold_quantity == 1

    def test_an_issue_only_reduces_on_hand(self) -> None:
        effect = movement_effect(TransactionType.MATERIAL_ISSUE)
        assert effect.on_hand_quantity == -1
        assert all(
            getattr(effect, column) == 0
            for column in (
                "qc_hold_quantity",
                "damaged_quantity",
                "in_transit_quantity",
                "job_work_quantity",
                "scrap_quantity",
            )
        )

    def test_writing_off_damaged_stock_clears_the_damaged_pool_too(self) -> None:
        """Otherwise damaged_quantity would survive the stock it describes."""
        effect = movement_effect(TransactionType.ADJUSTMENT_DECREASE, StockState.DAMAGED)
        assert effect.on_hand_quantity == -1
        assert effect.damaged_quantity == -1

    @pytest.mark.parametrize(
        "transaction_type", [TransactionType.QC_HOLD, TransactionType.QC_RELEASE]
    )
    def test_qc_movements_do_not_move_physical_stock(
        self, transaction_type: TransactionType
    ) -> None:
        """The generated available column requires qc_hold to be inside on-hand.

        If a hold reduced on_hand as well, ``on_hand - qc_hold`` would subtract
        the same quantity twice and available stock would read low by the amount
        held.
        """
        effect = movement_effect(transaction_type)
        assert effect.on_hand_quantity == 0
        assert effect.qc_hold_quantity == (1 if transaction_type is TransactionType.QC_HOLD else -1)

    def test_job_work_issue_moves_stock_out_of_hand_but_keeps_ownership(self) -> None:
        effect = movement_effect(TransactionType.JOB_WORK_ISSUE, StockState.JOB_WORK)
        assert effect.on_hand_quantity == -1
        assert effect.job_work_quantity == 1

    def test_scrapping_leaves_on_hand_and_lands_in_the_scrap_pool(self) -> None:
        effect = movement_effect(TransactionType.SCRAP, StockState.SCRAP)
        assert effect.on_hand_quantity == -1
        assert effect.scrap_quantity == 1

    def test_transfer_out_does_not_touch_in_transit(self) -> None:
        """In-transit is tracked at the destination by the transfer document.

        Keeping it out of the movement means a direct two-legged transfer and a
        document-driven one cannot disagree about where the quantity sits.
        """
        effect = movement_effect(TransactionType.TRANSFER_OUT)
        assert effect.on_hand_quantity == -1
        assert effect.in_transit_quantity == 0

    @pytest.mark.parametrize("transaction_type", sorted(set(TransactionType)))
    def test_on_hand_delta_matches_the_declared_sign(
        self, transaction_type: TransactionType
    ) -> None:
        """The reconciliation identity depends on this holding for every type.

        ``on_hand == SUM(quantity)`` over all non-neutral movements is only true
        if each of those types changes on-hand by exactly its signed quantity.
        """
        effect = movement_effect(transaction_type)
        expected = (
            0 if transaction_type in ON_HAND_NEUTRAL_TYPES else TRANSACTION_SIGNS[transaction_type]
        )
        assert effect.on_hand_quantity == expected

    def test_a_movement_that_describes_nothing_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            movement_effect(
                TransactionType.QC_HOLD,
                StockState.QC_HOLD,
                from_state=StockState.QC_HOLD,
            )


class TestStateChanges:
    @pytest.mark.parametrize(
        ("from_state", "to_state", "expected"),
        [
            (StockState.AVAILABLE, StockState.QC_HOLD, TransactionType.QC_HOLD),
            (StockState.AVAILABLE, StockState.DAMAGED, TransactionType.QC_HOLD),
            (StockState.QC_HOLD, StockState.AVAILABLE, TransactionType.QC_RELEASE),
            (StockState.DAMAGED, StockState.AVAILABLE, TransactionType.QC_RELEASE),
            (StockState.QC_HOLD, StockState.DAMAGED, TransactionType.QC_HOLD),
            (StockState.QC_HOLD, StockState.SCRAP, TransactionType.SCRAP),
            (StockState.AVAILABLE, StockState.JOB_WORK, TransactionType.JOB_WORK_ISSUE),
            (StockState.JOB_WORK, StockState.AVAILABLE, TransactionType.JOB_WORK_RECEIPT),
        ],
    )
    def test_pairs_map_to_the_right_ledger_vocabulary(
        self, from_state: StockState, to_state: StockState, expected: TransactionType
    ) -> None:
        assert state_change_type(from_state, to_state) is expected

    def test_qc_hold_to_damaged_keeps_available_unchanged(self) -> None:
        """Both pools are already outside availability, so nothing promisable moves."""
        effect = movement_effect(
            TransactionType.QC_HOLD, StockState.DAMAGED, from_state=StockState.QC_HOLD
        )
        assert effect.on_hand_quantity == 0
        assert effect.qc_hold_quantity == -1
        assert effect.damaged_quantity == 1

    def test_scrapping_held_stock_clears_the_hold(self) -> None:
        effect = movement_effect(
            TransactionType.SCRAP, StockState.SCRAP, from_state=StockState.QC_HOLD
        )
        assert effect.on_hand_quantity == -1
        assert effect.qc_hold_quantity == -1
        assert effect.scrap_quantity == 1

    def test_in_transit_is_not_reachable_by_a_state_change(self) -> None:
        with pytest.raises(BusinessRuleError):
            state_change_type(StockState.AVAILABLE, StockState.IN_TRANSIT)

    def test_scrap_is_not_quietly_returned_to_stock(self) -> None:
        """Reversing a scrap changes the balance sheet, so it needs a reason."""
        with pytest.raises(BusinessRuleError):
            state_change_type(StockState.SCRAP, StockState.AVAILABLE)

    def test_a_state_change_to_itself_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            state_change_type(StockState.QC_HOLD, StockState.QC_HOLD)


class TestTraceabilityRules:
    def test_a_batch_tracked_item_cannot_move_without_a_batch(self) -> None:
        item = _item(is_batch_tracked=True)
        warehouse = _warehouse()
        with pytest.raises(BusinessRuleError) as excinfo:
            validate_movement(
                _movement(item, warehouse),
                item=item,
                warehouse=warehouse,
                batch_owner=None,
                serial_owner=None,
            )
        assert excinfo.value.code is ErrorCode.BATCH_REQUIRED

    def test_a_serial_tracked_item_cannot_move_without_a_serial(self) -> None:
        item = _item(is_serial_tracked=True)
        warehouse = _warehouse()
        with pytest.raises(BusinessRuleError) as excinfo:
            validate_movement(
                _movement(item, warehouse),
                item=item,
                warehouse=warehouse,
                batch_owner=None,
                serial_owner=None,
            )
        assert excinfo.value.code is ErrorCode.SERIAL_REQUIRED

    def test_a_batch_from_another_item_is_refused(self) -> None:
        item = _item(is_batch_tracked=True)
        warehouse = _warehouse()
        with pytest.raises(BusinessRuleError, match="different item"):
            validate_movement(
                _movement(item, warehouse, batch_id=uuid.uuid4()),
                item=item,
                warehouse=warehouse,
                batch_owner=(uuid.uuid4(), "L-2026-07"),
                serial_owner=None,
            )

    def test_a_serial_movement_must_be_for_exactly_one_unit(self) -> None:
        item = _item(is_serial_tracked=True)
        warehouse = _warehouse()
        serial_id = uuid.uuid4()
        with pytest.raises(BusinessRuleError, match="single unit"):
            validate_movement(
                _movement(item, warehouse, serial_id=serial_id, quantity=Decimal("2")),
                item=item,
                warehouse=warehouse,
                batch_owner=None,
                serial_owner=(item.id, "SN-0001"),
            )

    def test_a_single_unit_serial_movement_passes(self) -> None:
        item = _item(is_serial_tracked=True)
        warehouse = _warehouse()
        validate_movement(
            _movement(item, warehouse, serial_id=uuid.uuid4(), quantity=Decimal("1")),
            item=item,
            warehouse=warehouse,
            batch_owner=None,
            serial_owner=(item.id, "SN-0001"),
        )

    def test_an_inactive_item_cannot_receive_stock(self) -> None:
        item = _item(is_active=False)
        warehouse = _warehouse()
        with pytest.raises(BusinessRuleError, match="inactive"):
            validate_movement(
                _movement(item, warehouse),
                item=item,
                warehouse=warehouse,
                batch_owner=None,
                serial_owner=None,
            )

    def test_an_inactive_item_can_still_give_stock_up(self) -> None:
        """Refusing outbound movements would strand stock behind a flag."""
        item = _item(is_active=False)
        warehouse = _warehouse(is_active=False)
        validate_movement(
            _movement(item, warehouse, transaction_type=TransactionType.MATERIAL_ISSUE),
            item=item,
            warehouse=warehouse,
            batch_owner=None,
            serial_owner=None,
        )


class TestTraceabilityGatesArrivalsNotDepartures:
    """A batch or a serial is assigned when goods *arrive*.

    ``is_batch_tracked`` can be turned on over an item that already holds stock -
    "we are starting to lot-track this part" is an ordinary decision, and the
    stock on the shelf when it is taken carries no lot. Applied to outbound
    movements as well, the rule then closed both routes out of that position at
    once: a movement naming no batch was refused by the rule, and a movement
    naming one keys a different ``BalanceGrain`` - ``batch_id`` is part of the
    grain - which holds nothing. The quantity was owned, on the balance sheet
    and immovable, with no error that said why.

    So the rule is gated exactly like the three active flags beside it: it gates
    what may be *started into* a position, and never what may leave one. The
    grain is what keeps that safe - a batch-less issue can only ever draw on the
    batch-less position, which is precisely the stock that predates the flag.
    """

    def _validate(self, item: ItemFacts, warehouse: WarehouseFacts, **overrides: object) -> None:
        validate_movement(
            _movement(item, warehouse, **overrides),
            item=item,
            warehouse=warehouse,
            batch_owner=None,
            serial_owner=None,
        )

    @pytest.mark.parametrize("transaction_type", OUTBOUND_TYPES)
    def test_batch_tracked_stock_can_still_leave_without_a_batch(
        self, transaction_type: TransactionType
    ) -> None:
        item = _item(is_batch_tracked=True)
        warehouse = _warehouse()
        self._validate(item, warehouse, transaction_type=transaction_type)

    @pytest.mark.parametrize("transaction_type", OUTBOUND_TYPES)
    def test_serial_tracked_stock_may_not_leave_without_a_serial(
        self, transaction_type: TransactionType
    ) -> None:
        """The serial rule is NOT the batch rule, and the difference is the grain.

        Leaving the batch rule open outbound is safe because ``batch_id`` is part
        of ``BalanceGrain``: a batch-less issue can only ever reach the batch-less
        position, which is exactly the stock that predates the flag.

        ``serial_id`` is not part of the grain. A serial-less departure draws on
        the *whole* position, identified units included, and picks which ones by
        nobody's decision - so stock leaves while every ``stock_serials`` row
        still says IN_STOCK, and the unit can be issued a second time. That is
        the defect the serial lifecycle exists to prevent, so any movement that
        changes whose hands hold the unit has to name it.

        The gate is ``SERIAL_MOVEMENT_TARGETS`` - the same mapping that decides
        the unit's next status - so the rule and the lifecycle cannot drift
        apart. A movement that leaves the status alone (a QC hold or release)
        moves a quantity, not a unit, and is not held to it; a transfer changes
        status on both legs and therefore names its unit on both.
        """
        item = _item(is_serial_tracked=True)
        warehouse = _warehouse()
        if SERIAL_MOVEMENT_TARGETS.get(transaction_type) is None:
            self._validate(item, warehouse, transaction_type=transaction_type)
            return
        with pytest.raises(BusinessRuleError) as excinfo:
            self._validate(item, warehouse, transaction_type=transaction_type)
        assert excinfo.value.code is ErrorCode.SERIAL_REQUIRED

    def test_the_batch_and_serial_rules_disagree_on_purpose(self) -> None:
        """One movement, one flag each, opposite answers - the asymmetry pinned.

        If someone later 'tidies' these two rules into one gate, this fails.
        """
        warehouse = _warehouse()
        self._validate(
            _item(is_batch_tracked=True),
            warehouse,
            transaction_type=TransactionType.MATERIAL_ISSUE,
        )
        with pytest.raises(BusinessRuleError) as excinfo:
            self._validate(
                _item(is_serial_tracked=True),
                warehouse,
                transaction_type=TransactionType.MATERIAL_ISSUE,
            )
        assert excinfo.value.code is ErrorCode.SERIAL_REQUIRED

    @pytest.mark.parametrize("transaction_type", sorted(INCREASE_TYPES, key=str))
    def test_but_nothing_may_be_received_without_one(
        self, transaction_type: TransactionType
    ) -> None:
        """The half that is the point of the flag. Every type that starts stock
        into a position is still refused - a QC release is not one of them, it
        moves a quantity the position already holds between its own pools."""
        item = _item(is_batch_tracked=True, is_serial_tracked=True)
        warehouse = _warehouse()
        if transaction_type is TransactionType.QC_RELEASE:
            self._validate(item, warehouse, transaction_type=transaction_type)
            return
        with pytest.raises(BusinessRuleError) as excinfo:
            self._validate(item, warehouse, transaction_type=transaction_type)
        assert excinfo.value.code is ErrorCode.BATCH_REQUIRED

    def test_a_job_work_return_out_of_the_job_work_pool_is_not_an_arrival(self) -> None:
        """``change_stock_state(JOB_WORK -> AVAILABLE)`` posts a
        ``JOB_WORK_RECEIPT``, which is a +1 type - but it draws on this
        position's own job-work pool, so nothing arrives from outside. Gated on
        the type's sign, the flag trapped that pool exactly as it trapped
        on-hand."""
        warehouse = _warehouse()
        self._validate(
            _item(is_batch_tracked=True),
            warehouse,
            transaction_type=TransactionType.JOB_WORK_RECEIPT,
            stock_state=StockState.AVAILABLE,
            from_state=StockState.JOB_WORK,
        )
        # The serial flag still bites, and should: JOB_WORK_RECEIPT puts the unit
        # back in our hands (SERIAL_MOVEMENT_TARGETS maps it to IN_STOCK), so the
        # return has to say which unit came back. Nothing is trapped by that - the
        # unit that went out on a JOB_WORK_ISSUE was named on the way out.
        with pytest.raises(BusinessRuleError) as excinfo:
            self._validate(
                _item(is_serial_tracked=True),
                warehouse,
                transaction_type=TransactionType.JOB_WORK_RECEIPT,
                stock_state=StockState.AVAILABLE,
                from_state=StockState.JOB_WORK,
            )
        assert excinfo.value.code is ErrorCode.SERIAL_REQUIRED

    def test_a_plain_job_work_receipt_is_still_an_arrival(self) -> None:
        """A different item coming back from the vendor is a genuine arrival."""
        item = _item(is_batch_tracked=True)
        warehouse = _warehouse()
        with pytest.raises(BusinessRuleError) as excinfo:
            self._validate(item, warehouse, transaction_type=TransactionType.JOB_WORK_RECEIPT)
        assert excinfo.value.code is ErrorCode.BATCH_REQUIRED

    def test_the_refusal_still_names_the_item(self) -> None:
        item = _item(is_serial_tracked=True)
        warehouse = _warehouse()
        with pytest.raises(BusinessRuleError) as excinfo:
            self._validate(item, warehouse)
        assert excinfo.value.code is ErrorCode.SERIAL_REQUIRED
        assert excinfo.value.details["item_code"] == item.code


class TestTheScrapPool:
    """``can_scrap``, and scrap leaving the company only through a disposal."""

    @pytest.mark.parametrize(
        "source",
        [StockState.AVAILABLE, StockState.QC_HOLD, StockState.DAMAGED, StockState.JOB_WORK],
    )
    def test_an_item_that_may_not_be_scrapped_is_refused_from_every_pool(
        self, source: StockState
    ) -> None:
        """A direct scrap, a QC scrap verdict and a vendor's scrap all land here."""
        item, warehouse = _item(can_scrap=False), _warehouse()
        with pytest.raises(BusinessRuleError, match="cannot be scrapped"):
            validate_movement(
                _movement(
                    item,
                    warehouse,
                    transaction_type=TransactionType.SCRAP,
                    from_state=source,
                    stock_state=StockState.SCRAP,
                ),
                item=item,
                warehouse=warehouse,
                batch_owner=None,
                serial_owner=None,
            )

    def test_the_default_scrap_movement_is_refused_too(self) -> None:
        item, warehouse = _item(can_scrap=False), _warehouse()
        with pytest.raises(BusinessRuleError, match="cannot be scrapped"):
            validate_movement(
                _movement(item, warehouse, transaction_type=TransactionType.SCRAP),
                item=item,
                warehouse=warehouse,
                batch_owner=None,
                serial_owner=None,
            )

    def test_scrap_the_item_already_holds_may_still_be_disposed_of(self) -> None:
        """The flag gates stock going in; it never traps the scrap that is there."""
        item, warehouse = _item(can_scrap=False), _warehouse()
        validate_movement(
            _movement(
                item,
                warehouse,
                transaction_type=TransactionType.SCRAP_DISPOSAL,
                stock_state=StockState.SCRAP,
            ),
            item=item,
            warehouse=warehouse,
            batch_owner=None,
            serial_owner=None,
        )

    def test_a_scrappable_item_passes(self) -> None:
        item, warehouse = _item(can_scrap=True), _warehouse()
        validate_movement(
            _movement(item, warehouse, transaction_type=TransactionType.SCRAP),
            item=item,
            warehouse=warehouse,
            batch_owner=None,
            serial_owner=None,
        )

    def test_a_disposal_takes_the_scrap_pool_and_leaves_on_hand_alone(self) -> None:
        effect = movement_effect(TransactionType.SCRAP_DISPOSAL)
        assert effect.scrap_quantity == -1
        assert effect.on_hand_quantity == 0
        assert effect.qc_hold_quantity == effect.damaged_quantity == 0

    def test_naming_the_scrap_pool_on_a_disposal_changes_nothing(self) -> None:
        assert movement_effect(TransactionType.SCRAP_DISPOSAL, StockState.SCRAP) == movement_effect(
            TransactionType.SCRAP_DISPOSAL
        )

    @pytest.mark.parametrize(
        "transaction_type", [TransactionType.QC_RELEASE, TransactionType.ADJUSTMENT_INCREASE]
    )
    def test_scrap_may_not_be_drawn_back_into_a_pool(
        self, transaction_type: TransactionType
    ) -> None:
        """Bringing scrap back is a write-up with a reason, never a reclassification."""
        item, warehouse = _item(), _warehouse()
        with pytest.raises(BusinessRuleError, match="only through a scrap disposal"):
            validate_movement(
                _movement(
                    item,
                    warehouse,
                    transaction_type=transaction_type,
                    from_state=StockState.SCRAP,
                ),
                item=item,
                warehouse=warehouse,
                batch_owner=None,
                serial_owner=None,
            )
