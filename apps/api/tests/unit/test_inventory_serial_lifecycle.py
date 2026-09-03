"""The lifecycle of one individually identified unit.

A serial number is the one place in the inventory engine where a *quantity* is
not the whole story. Stock is fungible: issuing 80 of something that has 100 is
legal, and the balance says so. A serialised unit is not - it is one physical
object, it is in exactly one state, and the second issue of it is either a
mis-scan or an object that has left the building twice. Both need a person.

These tests pin down :func:`serial_transition`, the pure half of that rule. The
write half - the row lock, the status assignment and the audit row - is
exercised against a real PostgreSQL in
``tests/integration/test_inventory_serials.py``, because the property that
matters there (two concurrent issues, exactly one winner) cannot be shown
without two genuine connections.
"""

from __future__ import annotations

import uuid

import pytest

from src.core.errors import BusinessRuleError, InvalidStateTransitionError
from src.modules.inventory.models import SerialStatus, StockState, TransactionType
from src.modules.inventory.service import (
    SERIAL_FLOW,
    SERIAL_IN_WAREHOUSE_STATUSES,
    SERIAL_MOVEMENT_TARGETS,
    SERIAL_RELOCATING_TYPES,
    assert_unit_is_where_the_movement_is,
    movement_relocates_unit,
    resolve_pools,
    serial_transition,
)

pytestmark = pytest.mark.unit

#: The one pair where a movement mapped to "no status change" does change one.
#: A QC release resolves the inspection a customer return goes through, and it
#: is the only movement that can put a returned unit back into normal stock
#: without adding a second unit's worth of quantity for one physical object.
_RETURN_RELEASE = (SerialStatus.RETURNED, TransactionType.QC_RELEASE)

#: Sentinel for "work the landing pool out from the transaction type", so a
#: genuine ``None`` - the movement takes the quantity out of the position
#: altogether - stays expressible.
_DEFAULT_POOL = object()


def _transition(
    status: SerialStatus,
    transaction_type: TransactionType,
    *,
    already_received: bool = False,
    landing_state: StockState | object | None = _DEFAULT_POOL,
) -> SerialStatus | None:
    """Drive :func:`serial_transition` for one movement.

    ``landing_state`` defaults to the pool the type lands in when the caller
    names no ``stock_state`` - the same answer :func:`resolve_pools` gives the
    engine for a plain ``post_movement(transaction_type=...)`` - so a case that
    is not about pools does not have to restate one. The cases that *are* about
    pools pass it explicitly.
    """
    pool = resolve_pools(transaction_type)[1] if landing_state is _DEFAULT_POOL else landing_state
    return serial_transition(
        serial_number="SN-0001",
        current=status,
        transaction_type=transaction_type,
        landing_state=pool,  # type: ignore[arg-type]
        already_received=already_received,
    )


class TestTheGraphIsStructurallySound:
    def test_no_unreachable_or_dead_end_states(self) -> None:
        SERIAL_FLOW.validate()

    def test_every_declared_status_appears_in_the_graph(self) -> None:
        """A status the machine has never heard of can only be set by a bug."""
        graph_states = {t.source for t in SERIAL_FLOW.transitions} | {
            t.target for t in SERIAL_FLOW.transitions
        }
        assert graph_states == set(SerialStatus)

    def test_scrapped_is_the_only_terminal_state(self) -> None:
        assert SERIAL_FLOW.terminal == frozenset({SerialStatus.SCRAPPED})

    def test_every_movement_type_is_mapped(self) -> None:
        """An unmapped type would be a ``KeyError`` on the posting path.

        The mapping is exhaustive on purpose: a new transaction type must be
        given a serial meaning when it is added, not the first time somebody
        posts one against a serialised item at two in the morning.
        """
        assert set(SERIAL_MOVEMENT_TARGETS) == set(TransactionType)

    def test_every_mapped_target_is_a_state_the_graph_knows(self) -> None:
        targets = {t for t in SERIAL_MOVEMENT_TARGETS.values() if t is not None}
        assert targets <= set(SerialStatus)


class TestTheHappyPath:
    """One unit, from the goods-in bench to the customer and back."""

    def test_a_receipt_leaves_the_unit_in_stock(self) -> None:
        assert (
            _transition(SerialStatus.IN_STOCK, TransactionType.PURCHASE_RECEIPT)
            is SerialStatus.IN_STOCK
        )

    def test_an_issue_takes_the_unit_out_of_stock(self) -> None:
        assert (
            _transition(SerialStatus.IN_STOCK, TransactionType.MATERIAL_ISSUE)
            is SerialStatus.ISSUED
        )

    def test_an_issued_unit_can_come_back_to_the_store(self) -> None:
        assert (
            _transition(SerialStatus.ISSUED, TransactionType.MATERIAL_RETURN)
            is SerialStatus.IN_STOCK
        )

    def test_a_dispatch_sends_the_unit_to_the_customer(self) -> None:
        assert (
            _transition(SerialStatus.IN_STOCK, TransactionType.DISPATCH) is SerialStatus.DISPATCHED
        )

    def test_a_customer_return_is_distinguishable_from_new_stock(self) -> None:
        """RETURNED, not IN_STOCK: a unit that has been to a customer and come
        back is not the same thing as one that never left, and the difference is
        exactly what a warranty claim needs to know."""
        assert (
            _transition(SerialStatus.DISPATCHED, TransactionType.CUSTOMER_RETURN)
            is SerialStatus.RETURNED
        )

    def test_a_returned_unit_can_be_put_back_into_stock(self) -> None:
        assert (
            _transition(SerialStatus.RETURNED, TransactionType.ADJUSTMENT_INCREASE)
            is SerialStatus.IN_STOCK
        )

    def test_a_returned_unit_can_go_straight_out_again(self) -> None:
        assert (
            _transition(SerialStatus.RETURNED, TransactionType.DISPATCH) is SerialStatus.DISPATCHED
        )

    def test_a_unit_can_be_scrapped_from_stock(self) -> None:
        assert _transition(SerialStatus.IN_STOCK, TransactionType.SCRAP) is SerialStatus.SCRAPPED

    @pytest.mark.parametrize(
        ("status", "transaction_type", "expected"),
        sorted(
            (
                (status, transaction_type, target)
                for transaction_type, target in SERIAL_MOVEMENT_TARGETS.items()
                if target is not None
                for status in SerialStatus
                if SERIAL_FLOW.can_transition(status, target)
            ),
            key=lambda row: (str(row[0]), str(row[1])),
        ),
    )
    def test_every_edge_the_graph_declares_is_reachable_by_a_movement(
        self, status: SerialStatus, transaction_type: TransactionType, expected: SerialStatus
    ) -> None:
        """Exhaustive: every (state, movement) pair the graph permits is legal.

        Written as a sweep rather than as a list of examples so a transition
        added to :data:`SERIAL_FLOW` without a movement that can drive it - or a
        mapping that points at an edge the graph does not have - fails here
        instead of at a posting.
        """
        assert _transition(status, transaction_type) is expected


class TestMovementsThatDoNotChangeHands:
    """QC decisions and transfer legs. The unit stays ours and stays put."""

    @pytest.mark.parametrize(
        "transaction_type",
        sorted(
            (t for t, target in SERIAL_MOVEMENT_TARGETS.items() if target is None),
            key=str,
        ),
    )
    @pytest.mark.parametrize("status", sorted(SERIAL_IN_WAREHOUSE_STATUSES, key=str))
    def test_a_unit_in_a_warehouse_is_left_alone(
        self, transaction_type: TransactionType, status: SerialStatus
    ) -> None:
        if (status, transaction_type) == _RETURN_RELEASE:
            pytest.skip("A QC release resolves a customer return - see its own case.")
        assert _transition(status, transaction_type) is None

    @pytest.mark.parametrize(
        "transaction_type",
        sorted(
            (t for t, target in SERIAL_MOVEMENT_TARGETS.items() if target is None),
            key=str,
        ),
    )
    @pytest.mark.parametrize(
        "status",
        sorted(set(SerialStatus) - SERIAL_IN_WAREHOUSE_STATUSES, key=str),
    )
    def test_a_unit_that_is_not_here_cannot_be_handled(
        self, transaction_type: TransactionType, status: SerialStatus
    ) -> None:
        """A QC hold on a unit that is with a customer is a data-entry error.

        The stock quantity would happily allow it - the hold moves a quantity
        between two pools of a position that may well have other units in it -
        which is precisely why the unit's own state has to be consulted.
        """
        with pytest.raises(BusinessRuleError, match="not in a warehouse"):
            _transition(status, transaction_type)


class TestAReturnedUnitCanBeSoldAgain:
    """The way back from RETURNED, and why it has to be a QC release.

    Every movement type that targets IN_STOCK also *adds* a unit to on-hand, and
    the customer return that produced this status already added this one. So the
    graph declared a RETURNED -> IN_STOCK edge that no movement could drive
    without booking a second unit's worth of stock for one physical object, and
    a returned unit was unsellable for good. A QC release moves a quantity from
    the hold pool to the available one and leaves on-hand alone, which makes it
    both the movement that resolves the inspection and the only one that can
    resolve the status.
    """

    def test_a_qc_release_puts_a_returned_unit_back_into_stock(self) -> None:
        assert _transition(*_RETURN_RELEASE) is SerialStatus.IN_STOCK

    def test_a_qc_release_still_leaves_an_ordinary_unit_alone(self) -> None:
        """Releasing stock that was never at a customer is not a status change."""
        assert _transition(SerialStatus.IN_STOCK, TransactionType.QC_RELEASE) is None

    def test_the_edge_it_drives_is_one_the_graph_declares(self) -> None:
        assert SERIAL_FLOW.can_transition(SerialStatus.RETURNED, SerialStatus.IN_STOCK)

    def test_a_qc_release_cannot_reach_a_unit_that_is_not_here(self) -> None:
        with pytest.raises(BusinessRuleError, match="not in a warehouse"):
            _transition(SerialStatus.DISPATCHED, TransactionType.QC_RELEASE)


class TestAUnitCannotBeReceivedTwice:
    """The inbound half of the lifecycle, which the self edge left unguarded.

    ``IN_STOCK -> IN_STOCK`` exists because a serial row is registered from a
    packing list before the goods arrive, and the vocabulary has no "expected"
    state. Unqualified, it also accepted the second, third and hundredth receipt
    of a unit already on the shelf: a re-scanned goods receipt booked another
    unit's worth of stock against one physical object and the graph said
    nothing, because the unit was IN_STOCK before and IN_STOCK after.
    """

    @pytest.mark.parametrize(
        "transaction_type",
        sorted(
            (t for t, target in SERIAL_MOVEMENT_TARGETS.items() if target is SerialStatus.IN_STOCK),
            key=str,
        ),
    )
    def test_a_first_receipt_of_a_registered_unit_is_legal(
        self, transaction_type: TransactionType
    ) -> None:
        assert (
            _transition(SerialStatus.IN_STOCK, transaction_type, already_received=False)
            is SerialStatus.IN_STOCK
        )

    @pytest.mark.parametrize(
        "transaction_type",
        sorted(
            (t for t, target in SERIAL_MOVEMENT_TARGETS.items() if target is SerialStatus.IN_STOCK),
            key=str,
        ),
    )
    def test_a_second_receipt_of_the_same_unit_is_refused(
        self, transaction_type: TransactionType
    ) -> None:
        with pytest.raises(BusinessRuleError, match="already in stock"):
            _transition(SerialStatus.IN_STOCK, transaction_type, already_received=True)

    def test_a_unit_that_has_been_out_may_come_back(self) -> None:
        """The flag only qualifies the self edge. A unit that was issued has
        genuinely left, and its return is a real transition, not a duplicate."""
        assert (
            _transition(SerialStatus.ISSUED, TransactionType.MATERIAL_RETURN, already_received=True)
            is SerialStatus.IN_STOCK
        )

    def test_a_customer_return_of_a_received_unit_is_still_legal(self) -> None:
        assert (
            _transition(
                SerialStatus.DISPATCHED, TransactionType.CUSTOMER_RETURN, already_received=True
            )
            is SerialStatus.RETURNED
        )

    def test_the_flag_does_not_touch_an_outbound_movement(self) -> None:
        assert (
            _transition(
                SerialStatus.IN_STOCK, TransactionType.MATERIAL_ISSUE, already_received=True
            )
            is SerialStatus.ISSUED
        )


class TestWhichMovementsMoveTheUnit:
    """Which movements may rewrite a unit's recorded position.

    Spelled out rather than derived, so a change to the transition table that
    silently changes where units are recorded fails here. Assigning the
    movement's own warehouse and bin to every movement was how a QC hold keyed
    at warehouse level emptied the bin a unit was sitting in.
    """

    def test_only_movements_that_put_the_unit_somewhere_relocate_it(self) -> None:
        expected = {
            TransactionType.PURCHASE_RECEIPT,
            TransactionType.PRODUCTION_RECEIPT,
            TransactionType.ASSEMBLY_RECEIPT,
            TransactionType.JOB_WORK_RECEIPT,
            TransactionType.MATERIAL_RETURN,
            TransactionType.CUSTOMER_RETURN,
            TransactionType.OPENING_BALANCE,
            TransactionType.ADJUSTMENT_INCREASE,
            TransactionType.TRANSFER_IN,
        }
        assert set(SERIAL_RELOCATING_TYPES) == expected

    @pytest.mark.parametrize(
        "transaction_type",
        [
            TransactionType.QC_HOLD,
            TransactionType.QC_RELEASE,
            TransactionType.TRANSFER_OUT,
            TransactionType.MATERIAL_ISSUE,
            TransactionType.ASSEMBLY_CONSUME,
            TransactionType.JOB_WORK_ISSUE,
            TransactionType.DISPATCH,
            TransactionType.PURCHASE_RETURN,
            TransactionType.SCRAP,
            TransactionType.ADJUSTMENT_DECREASE,
        ],
    )
    def test_nothing_else_does(self, transaction_type: TransactionType) -> None:
        assert transaction_type not in SERIAL_RELOCATING_TYPES

    def test_the_outbound_leg_of_a_transfer_does_not_move_the_unit(self) -> None:
        """It is on a vehicle. The inbound leg is what places it."""
        assert TransactionType.TRANSFER_OUT not in SERIAL_RELOCATING_TYPES
        assert TransactionType.TRANSFER_IN in SERIAL_RELOCATING_TYPES

    def test_every_movement_type_is_decided_one_way_or_the_other(self) -> None:
        assert set(TransactionType) >= set(SERIAL_RELOCATING_TYPES)


class TestIllegalTransitions:
    def test_a_unit_cannot_be_issued_twice(self) -> None:
        """The defect this whole lifecycle exists to fix."""
        with pytest.raises(InvalidStateTransitionError) as excinfo:
            _transition(SerialStatus.ISSUED, TransactionType.MATERIAL_ISSUE)
        assert excinfo.value.details["current_status"] == "ISSUED"
        assert excinfo.value.details["requested_status"] == "ISSUED"
        assert excinfo.value.details["allowed"] == ["IN_STOCK"]

    def test_a_dispatched_unit_cannot_be_dispatched_again(self) -> None:
        with pytest.raises(InvalidStateTransitionError):
            _transition(SerialStatus.DISPATCHED, TransactionType.DISPATCH)

    def test_a_dispatched_unit_cannot_be_issued_to_the_shop_floor(self) -> None:
        """It is at the customer's site. Only a customer return brings it back."""
        with pytest.raises(InvalidStateTransitionError) as excinfo:
            _transition(SerialStatus.DISPATCHED, TransactionType.MATERIAL_ISSUE)
        assert excinfo.value.details["allowed"] == ["RETURNED"]

    def test_an_issued_unit_cannot_be_dispatched_to_a_customer(self) -> None:
        """It is on the shop floor. It comes back to the store first."""
        with pytest.raises(InvalidStateTransitionError):
            _transition(SerialStatus.ISSUED, TransactionType.DISPATCH)

    def test_an_issued_unit_cannot_be_taken_back_from_a_customer(self) -> None:
        """It never went to one. A customer return of a unit that is on the
        shop floor names the wrong serial."""
        with pytest.raises(InvalidStateTransitionError):
            _transition(SerialStatus.ISSUED, TransactionType.CUSTOMER_RETURN)

    def test_the_graph_is_keyed_on_the_target_not_on_the_document(self) -> None:
        """A known limit of the design, stated so it is a decision and not a
        surprise: two movement types that leave a unit in the same state are
        interchangeable to the graph. A purchase receipt against a unit that is
        out on the shop floor is therefore accepted as a return to store, because
        both land on IN_STOCK. Distinguishing them would need a per-document
        source restriction, which is a bigger rule than the defect being fixed
        here - and the ledger still records which document it actually was.
        """
        assert (
            _transition(SerialStatus.ISSUED, TransactionType.PURCHASE_RECEIPT)
            is SerialStatus.IN_STOCK
        )

    @pytest.mark.parametrize(
        "transaction_type",
        sorted(SERIAL_MOVEMENT_TARGETS, key=str),
    )
    def test_nothing_at_all_can_be_done_to_a_scrapped_unit(
        self, transaction_type: TransactionType
    ) -> None:
        """SCRAPPED is terminal, and a written-off unit that reappears in a
        movement is a discrepancy someone has to explain."""
        with pytest.raises((InvalidStateTransitionError, BusinessRuleError)):
            _transition(SerialStatus.SCRAPPED, transaction_type)

    def test_an_unknown_status_is_not_quietly_accepted(self) -> None:
        with pytest.raises(ValueError, match="NOT_A_STATUS"):
            serial_transition(
                serial_number="SN-0001",
                current="NOT_A_STATUS",  # type: ignore[arg-type]
                transaction_type=TransactionType.MATERIAL_ISSUE,
                landing_state=None,
            )


class TestTheStatusAgreesWithThePoolTheQuantityLandsIn:
    """A release is not one movement, and the unit's status has to say which.

    ``state_change_type`` maps several pool-to-pool moves onto ``QC_RELEASE``:
    QC hold to available, damaged to available, *and* QC hold back to the return
    pool. Keyed on the transaction type alone, the rule that resolves a customer
    return fired for all of them - so a release that put the quantity back in
    the return pool still declared the unit IN_STOCK, and the unit's status and
    its quantity's classification disagreed about the same physical object.

    :func:`resolve_pools` is the one place that answers "where did this movement
    put the goods", and it is the answer the status now follows.
    """

    def test_a_release_into_the_available_pool_resolves_the_return(self) -> None:
        assert (
            _transition(*_RETURN_RELEASE, landing_state=StockState.AVAILABLE)
            is SerialStatus.IN_STOCK
        )

    def test_a_release_back_into_the_return_pool_does_not(self) -> None:
        """The quantity is still classified as customer-return stock. The unit
        is too."""
        assert _transition(*_RETURN_RELEASE, landing_state=StockState.RETURN) is None

    def test_a_release_into_the_damaged_pool_does_not(self) -> None:
        assert _transition(*_RETURN_RELEASE, landing_state=StockState.DAMAGED) is None

    def test_the_default_release_lands_in_available(self) -> None:
        """The rule's own premise: with no ``stock_state`` named, a QC release
        puts the quantity in the available pool, which is why the plain call
        resolves the return."""
        assert resolve_pools(TransactionType.QC_RELEASE) == (
            StockState.QC_HOLD,
            StockState.AVAILABLE,
        )

    def test_an_ordinary_unit_is_still_left_alone_by_either_release(self) -> None:
        for pool in (StockState.AVAILABLE, StockState.RETURN):
            assert (
                _transition(
                    SerialStatus.IN_STOCK,
                    TransactionType.QC_RELEASE,
                    landing_state=pool,
                )
                is None
            )

    def test_a_release_cannot_reach_a_unit_that_is_not_here_whatever_the_pool(
        self,
    ) -> None:
        for pool in (StockState.AVAILABLE, StockState.RETURN):
            with pytest.raises(BusinessRuleError, match="not in a warehouse"):
                _transition(
                    SerialStatus.DISPATCHED,
                    TransactionType.QC_RELEASE,
                    landing_state=pool,
                )


class TestAMovementThatDoesNotMoveTheUnitMustBeRaisedWhereItIs:
    """The other half of the position rule.

    Only a relocating movement may rewrite ``warehouse_id``. That stops a
    mis-keyed QC hold from teleporting a unit, but on its own it left the
    posting itself accepted: the quantity moved at a warehouse the unit is not
    in, and the unit's own status was decided by a movement that happened
    somewhere else. Combined with the release rule above, a QC release raised
    against the wrong store put a returned unit "back into stock" at a store it
    had never been in.
    """

    HERE = uuid.uuid4()
    ELSEWHERE = uuid.uuid4()

    def _check(
        self,
        transaction_type: TransactionType,
        *,
        recorded: uuid.UUID | None,
        movement: uuid.UUID,
        relocates: bool | None = None,
        recorded_bin: uuid.UUID | None = None,
        movement_bin: uuid.UUID | None = None,
    ) -> None:
        """Drive the rule for one movement.

        ``relocates`` defaults to the answer the type's *default* transition
        gives, which is what a plain ``post_movement`` produces. A case that
        overrides the source pool - ``change_stock_state`` does - states it.
        """
        assert_unit_is_where_the_movement_is(
            serial_number="SN-0001",
            transaction_type=transaction_type,
            relocates=(
                transaction_type in SERIAL_RELOCATING_TYPES if relocates is None else relocates
            ),
            recorded_warehouse_id=recorded,
            movement_warehouse_id=movement,
            recorded_location_id=recorded_bin,
            movement_location_id=movement_bin,
        )

    @pytest.mark.parametrize(
        "transaction_type",
        sorted(set(TransactionType) - set(SERIAL_RELOCATING_TYPES), key=str),
    )
    def test_a_non_relocating_movement_at_another_warehouse_is_refused(
        self, transaction_type: TransactionType
    ) -> None:
        with pytest.raises(BusinessRuleError, match="held at another warehouse"):
            self._check(transaction_type, recorded=self.HERE, movement=self.ELSEWHERE)

    @pytest.mark.parametrize(
        "transaction_type",
        sorted(set(TransactionType) - set(SERIAL_RELOCATING_TYPES), key=str),
    )
    def test_the_same_movement_at_the_unit_s_own_warehouse_passes(
        self, transaction_type: TransactionType
    ) -> None:
        self._check(transaction_type, recorded=self.HERE, movement=self.HERE)

    @pytest.mark.parametrize("transaction_type", sorted(SERIAL_RELOCATING_TYPES, key=str))
    def test_a_relocating_movement_may_name_any_warehouse(
        self, transaction_type: TransactionType
    ) -> None:
        """It is what *puts* the unit somewhere - the inbound leg of a transfer
        names the destination by definition."""
        self._check(transaction_type, recorded=self.HERE, movement=self.ELSEWHERE)

    @pytest.mark.parametrize("transaction_type", sorted(TransactionType, key=str))
    def test_a_unit_that_has_never_been_positioned_is_accepted(
        self, transaction_type: TransactionType
    ) -> None:
        """NULL means "registered from a packing list, never put anywhere", so
        there is nothing for the movement to disagree with. Whether a unit may
        leave before it arrived is answered from the ledger, not from a column
        that was never written."""
        self._check(transaction_type, recorded=None, movement=self.ELSEWHERE)

    def test_the_refusal_names_both_warehouses(self) -> None:
        """A storekeeper needs to be told which store the unit is actually in,
        not just that the posting was wrong."""
        with pytest.raises(BusinessRuleError) as excinfo:
            self._check(TransactionType.QC_RELEASE, recorded=self.HERE, movement=self.ELSEWHERE)
        assert excinfo.value.details["serial_warehouse_id"] == str(self.HERE)
        assert excinfo.value.details["movement_warehouse_id"] == str(self.ELSEWHERE)


class TestTheBinHalfOfTheSameRule:
    """The wrong-warehouse case was closed; the wrong-*bin* case was identical.

    The rule waived the bin comparison entirely, on the argument that assembly,
    dispatch and QC decisions post at warehouse level and refusing them would
    enforce a precision the callers do not carry. True for a movement that names
    no bin - and it was applied to movements that name one too, which is a
    different statement: the posting has said where it thinks the unit is, the
    quantity moves on that shelf, and the unit's status is decided by something
    that happened one aisle over. The waiver belongs to the unstated case only.
    """

    HERE = uuid.uuid4()
    BIN_A = uuid.uuid4()
    BIN_B = uuid.uuid4()

    def _check(
        self,
        *,
        recorded_bin: uuid.UUID | None,
        movement_bin: uuid.UUID | None,
        transaction_type: TransactionType = TransactionType.QC_HOLD,
    ) -> None:
        assert_unit_is_where_the_movement_is(
            serial_number="SN-0001",
            transaction_type=transaction_type,
            relocates=transaction_type in SERIAL_RELOCATING_TYPES,
            recorded_warehouse_id=self.HERE,
            movement_warehouse_id=self.HERE,
            recorded_location_id=recorded_bin,
            movement_location_id=movement_bin,
        )

    def test_a_non_relocating_movement_naming_another_bin_is_refused(self) -> None:
        with pytest.raises(BusinessRuleError, match="another storage location") as excinfo:
            self._check(recorded_bin=self.BIN_A, movement_bin=self.BIN_B)
        assert excinfo.value.details["serial_location_id"] == str(self.BIN_A)
        assert excinfo.value.details["movement_location_id"] == str(self.BIN_B)

    def test_the_same_bin_passes(self) -> None:
        self._check(recorded_bin=self.BIN_A, movement_bin=self.BIN_A)

    def test_a_movement_that_names_no_bin_is_still_accepted(self) -> None:
        """The documented reason the comparison is by warehouse: a QC decision
        raised at warehouse level says nothing about the shelf, so there is
        nothing to disagree with."""
        self._check(recorded_bin=self.BIN_A, movement_bin=None)

    def test_a_unit_with_no_recorded_bin_is_accepted(self) -> None:
        """NULL is "positioned at warehouse level, or never positioned"."""
        self._check(recorded_bin=None, movement_bin=self.BIN_B)

    @pytest.mark.parametrize("transaction_type", sorted(SERIAL_RELOCATING_TYPES, key=str))
    def test_a_relocating_movement_may_name_any_bin(
        self, transaction_type: TransactionType
    ) -> None:
        """It is what puts the unit on the shelf it names."""
        self._check(
            recorded_bin=self.BIN_A, movement_bin=self.BIN_B, transaction_type=transaction_type
        )


class TestRelocationIsDecidedByThePoolsTheMovementActuallyResolvedTo:
    """A movement relocates a unit when it brings stock in from *outside* the
    position - and the transaction type alone does not answer that.

    ``SERIAL_RELOCATING_TYPES`` reads the type's *default* transition, which is
    right for a plain posting and wrong for every caller that overrides the
    source. ``change_stock_state(JOB_WORK -> AVAILABLE)`` posts a
    ``JOB_WORK_RECEIPT`` - default source ``None``, so "relocating" - while
    actually drawing on this position's own job-work pool. Nothing arrives from
    outside, nothing is relocated, and treating it as a relocation waived the
    "raise it where the unit is" check *and* let a return keyed at the wrong
    store rewrite the unit's position to that store.
    """

    HERE = uuid.uuid4()
    ELSEWHERE = uuid.uuid4()

    def test_a_job_work_return_out_of_the_job_work_pool_does_not_relocate(self) -> None:
        source, _ = resolve_pools(
            TransactionType.JOB_WORK_RECEIPT,
            StockState.AVAILABLE,
            StockState.JOB_WORK,
        )
        assert movement_relocates_unit(source) is False

    def test_and_so_it_must_be_raised_where_the_unit_is(self) -> None:
        source, _ = resolve_pools(
            TransactionType.JOB_WORK_RECEIPT,
            StockState.AVAILABLE,
            StockState.JOB_WORK,
        )
        with pytest.raises(BusinessRuleError, match="held at another warehouse"):
            assert_unit_is_where_the_movement_is(
                serial_number="SN-0001",
                transaction_type=TransactionType.JOB_WORK_RECEIPT,
                relocates=movement_relocates_unit(source),
                recorded_warehouse_id=self.HERE,
                movement_warehouse_id=self.ELSEWHERE,
            )

    def test_the_plain_job_work_receipt_still_relocates(self) -> None:
        """A *different* item coming back from the vendor is a genuine arrival,
        and it is the default this type declares."""
        source, _ = resolve_pools(TransactionType.JOB_WORK_RECEIPT)
        assert movement_relocates_unit(source) is True
        assert TransactionType.JOB_WORK_RECEIPT in SERIAL_RELOCATING_TYPES

    def test_the_default_set_still_agrees_with_the_predicate(self) -> None:
        """The set is the predicate applied to every default transition, so the
        two can never drift."""
        for transaction_type in TransactionType:
            source, _ = resolve_pools(transaction_type)
            assert movement_relocates_unit(source) is (transaction_type in SERIAL_RELOCATING_TYPES)
