"""Partial release and consume arithmetic, and the reservation state graph.

No database. These are the rules that decide how much stock goes back to
availability and what a reservation's status becomes, and they are pure
functions precisely so they can be pinned down here rather than only observed
through a posting.

What is being defended:

* a release or consume for more than is outstanding is *refused*, never clamped -
  a caller that has miscounted has to find out, and silently doing 30 when 50 was
  asked for hides the mistake behind a 200 response;
* the status after a drawdown is derived from what is left, so ``reserved_quantity``
  on the balance and the reservation's own status can never tell different
  stories;
* the state graph has no way back out of a terminal state, because issued stock
  cannot be un-issued by editing a promise.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.errors import BusinessRuleError, ErrorCode
from src.modules.allocation.models import (
    DEFAULT_RESERVATION_PRIORITY,
    OPEN_RESERVATION_STATUSES,
    ReservationStatus,
)
from src.modules.allocation.service import (
    REALLOCATION_FLOW,
    RESERVATION_FLOW,
    _parse_priority,
    plan_consume,
    plan_release,
    remaining_of,
)

pytestmark = pytest.mark.unit


def _release(
    quantity: str,
    consumed: str = "0",
    released: str = "0",
    *,
    status: ReservationStatus = ReservationStatus.ACTIVE,
    requested: str | None = None,
):
    return plan_release(
        quantity=Decimal(quantity),
        consumed_quantity=Decimal(consumed),
        released_quantity=Decimal(released),
        status=status,
        requested=None if requested is None else Decimal(requested),
    )


def _consume(
    quantity: str,
    consumed: str = "0",
    released: str = "0",
    *,
    status: ReservationStatus = ReservationStatus.ACTIVE,
    requested: str,
):
    return plan_consume(
        quantity=Decimal(quantity),
        consumed_quantity=Decimal(consumed),
        released_quantity=Decimal(released),
        status=status,
        requested=Decimal(requested),
    )


class TestRemaining:
    def test_remaining_is_the_promise_less_what_has_been_drawn(self) -> None:
        assert remaining_of(Decimal("100"), Decimal("30"), Decimal("20")) == Decimal("50")

    def test_remaining_keeps_quantity_scale_without_float_drift(self) -> None:
        """Four decimal places, exactly - a reservation that drifted by 1e-16
        would eventually refuse a release for "more than remains"."""
        assert remaining_of(
            Decimal("10.0001"), Decimal("3.3333"), Decimal("3.3333")
        ) == Decimal("3.3335")


class TestPartialRelease:
    def test_releasing_everything_left_closes_the_reservation(self) -> None:
        plan = _release("100")
        assert plan.applied_quantity == Decimal("100")
        assert plan.remaining_after == Decimal("0")
        assert plan.next_status is ReservationStatus.RELEASED
        assert plan.closes_reservation is True

    def test_an_omitted_quantity_means_whatever_is_outstanding(self) -> None:
        """What cancelling a demand document does - and it must account for the
        part already issued rather than releasing the original promise."""
        plan = _release("100", consumed="40", status=ReservationStatus.PARTIALLY_CONSUMED)
        assert plan.applied_quantity == Decimal("60")
        assert plan.next_status is ReservationStatus.RELEASED

    def test_a_partial_release_leaves_the_status_where_it_was(self) -> None:
        plan = _release("100", requested="30")
        assert plan.applied_quantity == Decimal("30")
        assert plan.remaining_after == Decimal("70")
        assert plan.next_status is ReservationStatus.ACTIVE
        assert plan.closes_reservation is False

    def test_a_partial_release_of_a_part_issued_reservation_stays_part_issued(self) -> None:
        plan = _release(
            "100", consumed="40", status=ReservationStatus.PARTIALLY_CONSUMED, requested="10"
        )
        assert plan.remaining_after == Decimal("50")
        assert plan.next_status is ReservationStatus.PARTIALLY_CONSUMED

    def test_releasing_the_last_of_a_part_issued_reservation_closes_it(self) -> None:
        plan = _release(
            "100", consumed="40", released="50", status=ReservationStatus.PARTIALLY_CONSUMED
        )
        assert plan.applied_quantity == Decimal("10")
        assert plan.next_status is ReservationStatus.RELEASED

    def test_releasing_more_than_remains_is_refused_not_clamped(self) -> None:
        with pytest.raises(BusinessRuleError) as exc:
            _release("100", consumed="40", released="30", requested="40")
        assert exc.value.details["remaining"] == "30.0000"
        assert exc.value.details["requested"] == "40.0000"

    def test_releasing_more_than_the_original_promise_is_refused(self) -> None:
        with pytest.raises(BusinessRuleError):
            _release("100", requested="101")

    @pytest.mark.parametrize("requested", ["0", "-5"])
    def test_a_non_positive_release_is_refused(self, requested: str) -> None:
        with pytest.raises(BusinessRuleError, match="positive quantity"):
            _release("100", requested=requested)

    @pytest.mark.parametrize(
        "status",
        [ReservationStatus.CONSUMED, ReservationStatus.RELEASED, ReservationStatus.EXPIRED],
    )
    def test_a_closed_reservation_cannot_be_released_again(
        self, status: ReservationStatus
    ) -> None:
        with pytest.raises(BusinessRuleError) as exc:
            _release("100", released="100", status=status)
        assert exc.value.details["status"] == str(status)

    def test_successive_partial_releases_add_up_to_exactly_the_promise(self) -> None:
        """The arithmetic has to survive being applied repeatedly, which is what
        a demand released line by line actually does."""
        released = Decimal("0")
        status = ReservationStatus.ACTIVE
        for chunk in ("10.5", "0.25", "89.25"):
            plan = plan_release(
                quantity=Decimal("100"),
                consumed_quantity=Decimal("0"),
                released_quantity=released,
                status=status,
                requested=Decimal(chunk),
            )
            released += plan.applied_quantity
            status = plan.next_status
        assert released == Decimal("100.0000")
        assert status is ReservationStatus.RELEASED


class TestConsume:
    def test_consuming_part_moves_the_reservation_to_partially_consumed(self) -> None:
        plan = _consume("100", requested="30")
        assert plan.remaining_after == Decimal("70")
        assert plan.next_status is ReservationStatus.PARTIALLY_CONSUMED

    def test_consuming_the_remainder_closes_the_reservation(self) -> None:
        plan = _consume(
            "100", consumed="70", status=ReservationStatus.PARTIALLY_CONSUMED, requested="30"
        )
        assert plan.remaining_after == Decimal("0")
        assert plan.next_status is ReservationStatus.CONSUMED

    def test_a_reservation_partly_released_then_fully_issued_reads_as_consumed(self) -> None:
        """The status only has to say whether anything is outstanding; the
        individual events stay in the audit log."""
        plan = _consume("100", consumed="0", released="40", requested="60")
        assert plan.next_status is ReservationStatus.CONSUMED

    def test_consuming_more_than_remains_is_refused(self) -> None:
        with pytest.raises(BusinessRuleError) as exc:
            _consume("100", consumed="80", requested="30")
        assert exc.value.code is ErrorCode.RESERVATION_ALREADY_CONSUMED
        assert exc.value.details["remaining"] == "20.0000"

    def test_a_fully_consumed_reservation_cannot_be_consumed_again(self) -> None:
        with pytest.raises(BusinessRuleError) as exc:
            _consume("100", consumed="100", status=ReservationStatus.CONSUMED, requested="1")
        assert exc.value.code is ErrorCode.RESERVATION_ALREADY_CONSUMED

    @pytest.mark.parametrize("requested", ["0", "-1"])
    def test_a_non_positive_consumption_is_refused(self, requested: str) -> None:
        with pytest.raises(BusinessRuleError, match="positive quantity"):
            _consume("100", requested=requested)

    def test_a_mixed_life_adds_up_to_exactly_the_promise(self) -> None:
        """Issue 30, give back 20, issue the last 50: consumed + released is the
        original quantity and nothing is left outstanding."""
        first = _consume("100", requested="30")
        assert first.next_status is ReservationStatus.PARTIALLY_CONSUMED

        given_back = plan_release(
            quantity=Decimal("100"),
            consumed_quantity=Decimal("30"),
            released_quantity=Decimal("0"),
            status=first.next_status,
            requested=Decimal("20"),
        )
        assert given_back.next_status is ReservationStatus.PARTIALLY_CONSUMED
        assert given_back.remaining_after == Decimal("50")

        last = plan_consume(
            quantity=Decimal("100"),
            consumed_quantity=Decimal("30"),
            released_quantity=Decimal("20"),
            status=given_back.next_status,
            requested=Decimal("50"),
        )
        assert last.next_status is ReservationStatus.CONSUMED
        assert last.remaining_after == Decimal("0")


class TestStateGraph:
    def test_the_reservation_graph_is_structurally_sound(self) -> None:
        """No unreachable state, and no non-terminal state a reservation can
        never leave."""
        RESERVATION_FLOW.validate()

    def test_the_reallocation_graph_is_structurally_sound(self) -> None:
        REALLOCATION_FLOW.validate()

    def test_every_status_a_drawdown_can_produce_is_a_legal_transition(self) -> None:
        """The plan functions and the state machine must agree: a plan that
        produced an illegal status would fail at assert_transition after the
        arithmetic had already been accepted."""
        cases = [
            (_release("100", requested="30"), ReservationStatus.ACTIVE),
            (_release("100"), ReservationStatus.ACTIVE),
            (_consume("100", requested="30"), ReservationStatus.ACTIVE),
            (_consume("100", requested="100"), ReservationStatus.ACTIVE),
            (
                _release(
                    "100",
                    consumed="40",
                    status=ReservationStatus.PARTIALLY_CONSUMED,
                    requested="10",
                ),
                ReservationStatus.PARTIALLY_CONSUMED,
            ),
            (
                _consume(
                    "100",
                    consumed="40",
                    status=ReservationStatus.PARTIALLY_CONSUMED,
                    requested="60",
                ),
                ReservationStatus.PARTIALLY_CONSUMED,
            ),
        ]
        for plan, source in cases:
            assert RESERVATION_FLOW.can_transition(source, plan.next_status), (
                f"{source} -> {plan.next_status} is not a declared transition"
            )

    def test_expiry_is_reachable_from_both_open_states(self) -> None:
        """The housekeeping sweep has to be able to close either kind of open
        reservation, or the EXPIRED status would be unreachable for half of them."""
        for status in OPEN_RESERVATION_STATUSES:
            assert RESERVATION_FLOW.can_transition(status, ReservationStatus.EXPIRED)

    @pytest.mark.parametrize(
        "terminal",
        [ReservationStatus.CONSUMED, ReservationStatus.RELEASED, ReservationStatus.EXPIRED],
    )
    def test_nothing_leaves_a_terminal_reservation_state(
        self, terminal: ReservationStatus
    ) -> None:
        """Issued stock is not un-issued by editing a promise; the correction is
        a new movement."""
        assert RESERVATION_FLOW.is_terminal(terminal)
        assert RESERVATION_FLOW.allowed_targets(terminal) == []

    def test_a_reallocation_cannot_execute_without_being_approved_first(self) -> None:
        """Approval and execution happen in one transaction, but they are two
        edges: an audit trail that cannot show the approval preceding the effect
        is not an approval trail."""
        from src.modules.allocation.models import ReallocationStatus

        assert not REALLOCATION_FLOW.can_transition(
            ReallocationStatus.PENDING, ReallocationStatus.EXECUTED
        )
        assert REALLOCATION_FLOW.can_transition(
            ReallocationStatus.PENDING, ReallocationStatus.APPROVED
        )
        assert REALLOCATION_FLOW.can_transition(
            ReallocationStatus.APPROVED, ReallocationStatus.EXECUTED
        )

    def test_a_rejected_reallocation_is_final(self) -> None:
        from src.modules.allocation.models import ReallocationStatus

        assert REALLOCATION_FLOW.allowed_targets(ReallocationStatus.REJECTED) == []


class TestRequesterPriorityRoundTrip:
    """Priority survives the request's JSONB context intact.

    An executed reallocation rebuilds the granted reservation's urgency from
    ``impact_summary["requester"]``. ``0`` is a legal priority and the *most*
    urgent one, so a truthiness fallback silently demoted the rush order the
    reallocation was approved for to the default - where any demand at priority
    99 or better could take it straight back, without a second approval.
    """

    def test_the_most_urgent_priority_is_not_mistaken_for_a_missing_one(self) -> None:
        assert _parse_priority(0) == 0

    @pytest.mark.parametrize("value", [10, "10"])
    def test_a_stated_priority_is_kept(self, value: object) -> None:
        assert _parse_priority(value) == 10

    @pytest.mark.parametrize("value", [None, "", "not-a-number", True, -1, 3.5])
    def test_an_absent_or_unusable_priority_falls_back_to_the_default(
        self, value: object
    ) -> None:
        """Never aborts an approved reallocation, and never writes a value the
        ``priority >= 0`` check constraint would reject."""
        assert _parse_priority(value) == DEFAULT_RESERVATION_PRIORITY
