"""Delay detection (master rules section 23).

A consignment is late when it is on the road past the date we promised. The
definition has to be exactly one thing, because four separate places read it -
the list filter, the tracking board, the delayed feed and the hourly sweep - and
two of them disagreeing means the screen and the alert tell different stories.

These tests pin the edges: the day it becomes late, the day it does not, and
every reason a consignment can never be late at all.
"""

from __future__ import annotations

from datetime import date

from src.modules.dispatch.models import DELIVERED_STATUSES, IN_FLIGHT_STATUSES, DispatchStatus
from src.modules.dispatch.repository import GRN_OUTSTANDING_STATUSES
from src.modules.dispatch.service import (
    arrived_late,
    delay_days,
    grn_is_pending,
    is_delayed,
    qc_is_pending,
)

PROMISED = date(2026, 9, 10)


# -----------------------------------------------------------------------------
# Is it late?
# -----------------------------------------------------------------------------


def test_a_consignment_is_late_the_day_after_its_promised_date() -> None:
    assert (
        is_delayed(
            status=DispatchStatus.IN_TRANSIT,
            expected_delivery_date=PROMISED,
            as_of=date(2026, 9, 11),
        )
        is True
    )


def test_a_consignment_is_not_late_on_the_promised_date_itself() -> None:
    # The promise is "by the 10th", not "before the 10th". Flagging it at
    # midnight on the day would put the whole day's deliveries on the board.
    assert (
        is_delayed(
            status=DispatchStatus.IN_TRANSIT,
            expected_delivery_date=PROMISED,
            as_of=PROMISED,
        )
        is False
    )


def test_a_consignment_with_no_promised_date_is_never_late() -> None:
    # A walk-in collection has no delivery commitment, and inventing one would
    # put a permanent false positive on the board.
    assert (
        is_delayed(
            status=DispatchStatus.DISPATCHED,
            expected_delivery_date=None,
            as_of=date(2030, 1, 1),
        )
        is False
    )


def test_a_delivered_consignment_cannot_become_late_afterwards() -> None:
    for status in DELIVERED_STATUSES:
        assert (
            is_delayed(
                status=status, expected_delivery_date=PROMISED, as_of=date(2026, 12, 31)
            )
            is False
        ), status


def test_a_consignment_that_has_not_left_is_not_late() -> None:
    # It may be behind schedule, but it is not a *delivery* delay: nothing is on
    # a lorry to chase. The board would otherwise mix two different problems.
    for status in (
        DispatchStatus.NOT_DISPATCHED,
        DispatchStatus.READY_FOR_DISPATCH,
        DispatchStatus.CANCELLED,
    ):
        assert (
            is_delayed(status=status, expected_delivery_date=PROMISED, as_of=date(2026, 10, 1))
            is False
        ), status


def test_exactly_the_in_flight_statuses_can_be_late() -> None:
    late_statuses = {
        status
        for status in DispatchStatus
        if is_delayed(
            status=status, expected_delivery_date=PROMISED, as_of=date(2026, 10, 1)
        )
    }
    assert late_statuses == set(IN_FLIGHT_STATUSES)


# -----------------------------------------------------------------------------
# How late?
# -----------------------------------------------------------------------------


def test_delay_days_counts_whole_days_past_the_promise() -> None:
    assert delay_days(PROMISED, date(2026, 9, 11)) == 1
    assert delay_days(PROMISED, date(2026, 9, 24)) == 14


def test_delay_days_is_never_negative() -> None:
    # An early consignment is not "minus three days late"; the board sorts on
    # this number and a negative would put the earliest promise at the top.
    assert delay_days(PROMISED, date(2026, 9, 7)) == 0
    assert delay_days(PROMISED, PROMISED) == 0
    assert delay_days(None, date(2026, 9, 30)) == 0


# -----------------------------------------------------------------------------
# Did it arrive late? (asked at confirmation, not on the road)
# -----------------------------------------------------------------------------


def test_arriving_after_the_promise_is_late_and_needs_an_explanation() -> None:
    assert (
        arrived_late(expected_delivery_date=PROMISED, actual_delivery_date=date(2026, 9, 12))
        is True
    )


def test_arriving_on_or_before_the_promise_is_on_time() -> None:
    assert (
        arrived_late(expected_delivery_date=PROMISED, actual_delivery_date=PROMISED) is False
    )
    assert (
        arrived_late(expected_delivery_date=PROMISED, actual_delivery_date=date(2026, 9, 8))
        is False
    )


def test_with_no_promise_an_arrival_is_never_late() -> None:
    assert (
        arrived_late(expected_delivery_date=None, actual_delivery_date=date(2026, 9, 30))
        is False
    )


# -----------------------------------------------------------------------------
# The board's QC and GRN columns
# -----------------------------------------------------------------------------


def test_qc_is_pending_only_while_the_receiver_is_inspecting() -> None:
    pending = {status for status in DispatchStatus if qc_is_pending(status)}
    assert pending == {DispatchStatus.QC_PENDING}


def test_a_grn_is_outstanding_from_arrival_until_it_is_booked_in() -> None:
    outstanding = {status for status in DispatchStatus if grn_is_pending(status)}
    assert outstanding == set(GRN_OUTSTANDING_STATUSES)
    assert DispatchStatus.DELIVERED in outstanding
    assert DispatchStatus.GRN_COMPLETED not in outstanding


def test_nothing_that_has_not_arrived_owes_a_goods_receipt() -> None:
    for status in (*IN_FLIGHT_STATUSES, DispatchStatus.NOT_DISPATCHED):
        assert grn_is_pending(status) is False, status
