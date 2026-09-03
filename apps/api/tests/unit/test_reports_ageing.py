"""Ageing and bucketing (master rules section 25).

Every "how late is this?" column in the reporting module - purchase pending,
production pending, job-work pending, order delay, dead stock - is built from the
three functions tested here. That is the whole point of pinning them: four
reports disagreeing about when something becomes overdue is four screens telling
a production manager four different stories about the same order.

These tests pin the edges, because the edges are where a bucketing function is
wrong: the day something becomes late, the day it does not, the boundary between
one band and the next, and the two cases that are not a number of days at all -
no due date, and no movement ever.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from src.modules.reports.service import (
    CURRENT_BUCKET,
    NEVER_MOVED_BUCKET,
    OVER_AGE_BUCKET,
    URGENCY_LATER,
    URGENCY_OVERDUE,
    URGENCY_THIS_MONTH,
    URGENCY_THIS_WEEK,
    ageing_bucket,
    days_between,
    delivery_delay_days,
    overdue_days,
    urgency_bucket,
)

TODAY = date(2026, 9, 10)


# -----------------------------------------------------------------------------
# How late is it?
# -----------------------------------------------------------------------------


def test_a_document_due_today_is_not_yet_overdue() -> None:
    # "By the 10th" is a promise about the whole day. Flagging it at midnight
    # would put every one of the day's deliveries on the overdue list before
    # anybody had a chance to make them.
    assert overdue_days(TODAY, TODAY) == 0


def test_a_document_is_one_day_overdue_the_day_after_it_was_due() -> None:
    assert overdue_days(TODAY, date(2026, 9, 11)) == 1


def test_a_document_due_in_the_future_is_never_negatively_overdue() -> None:
    # Clamped rather than signed: a negative "days overdue" reads as an ageing
    # bucket in reverse everywhere it is displayed.
    assert overdue_days(date(2026, 9, 30), TODAY) == 0


def test_a_document_with_no_due_date_can_never_be_overdue() -> None:
    # A purchase order with no promised delivery date has no promise to break;
    # inventing one would put a permanent false positive on the buyer's list.
    assert overdue_days(None, TODAY) == 0


# -----------------------------------------------------------------------------
# Which band does it fall in?
# -----------------------------------------------------------------------------


def test_nothing_overdue_is_bucketed_as_current() -> None:
    assert ageing_bucket(0) == CURRENT_BUCKET


def test_the_first_band_starts_at_one_day_and_ends_at_thirty() -> None:
    assert ageing_bucket(1) == "1-30 days"
    assert ageing_bucket(30) == "1-30 days"


def test_the_band_boundary_is_exclusive_at_the_top_and_inclusive_at_the_bottom() -> None:
    # 30 and 31 must land in different bands, or a month-end report shows the
    # same document in two buckets depending on which screen renders it.
    assert ageing_bucket(30) != ageing_bucket(31)
    assert ageing_bucket(31) == "31-60 days"
    assert ageing_bucket(60) == "31-60 days"
    assert ageing_bucket(61) == "61-90 days"
    assert ageing_bucket(90) == "61-90 days"
    assert ageing_bucket(91) == "91-180 days"
    assert ageing_bucket(180) == "91-180 days"
    assert ageing_bucket(181) == "181-365 days"
    assert ageing_bucket(365) == "181-365 days"


def test_anything_past_a_year_falls_into_the_final_open_ended_band() -> None:
    assert ageing_bucket(366) == OVER_AGE_BUCKET
    assert ageing_bucket(5_000) == OVER_AGE_BUCKET


def test_never_having_moved_is_its_own_bucket_not_current() -> None:
    # A position that has never moved is the oldest dead stock there is. Reading
    # its missing date as "zero days old" would file it under Current and hide
    # exactly the rows the dead-stock report exists to surface.
    assert ageing_bucket(None) == NEVER_MOVED_BUCKET
    assert ageing_bucket(None) != CURRENT_BUCKET


def test_every_band_has_a_distinct_label() -> None:
    labels = {ageing_bucket(d) for d in (0, 1, 30, 31, 60, 61, 90, 91, 180, 181, 365, 366)}
    assert len(labels) == 7


# -----------------------------------------------------------------------------
# Urgency looks forward, not back
# -----------------------------------------------------------------------------


def test_a_shortage_already_past_its_date_is_overdue() -> None:
    assert urgency_bucket(-1) == URGENCY_OVERDUE


def test_a_shortage_needed_today_is_urgent_but_not_yet_overdue() -> None:
    assert urgency_bucket(0) == URGENCY_THIS_WEEK


def test_urgency_bands_break_at_a_week_and_a_month() -> None:
    assert urgency_bucket(7) == URGENCY_THIS_WEEK
    assert urgency_bucket(8) == URGENCY_THIS_MONTH
    assert urgency_bucket(30) == URGENCY_THIS_MONTH
    assert urgency_bucket(31) == URGENCY_LATER


# -----------------------------------------------------------------------------
# Delivery delay stops moving once the goods arrive
# -----------------------------------------------------------------------------


def test_an_undelivered_consignment_measures_its_delay_against_today() -> None:
    assert delivery_delay_days(date(2026, 9, 1), None, TODAY) == 9


def test_a_delivered_consignment_measures_its_delay_against_the_arrival() -> None:
    # The delay is a historical fact once the lorry is unloaded. Measuring it
    # against today would make a consignment delivered two days late look worse
    # every morning for the rest of the year.
    assert delivery_delay_days(date(2026, 9, 1), date(2026, 9, 3), TODAY) == 2


def test_an_early_delivery_has_no_delay() -> None:
    assert delivery_delay_days(date(2026, 9, 5), date(2026, 9, 1), TODAY) == 0


def test_a_consignment_with_no_promised_date_has_no_delay() -> None:
    assert delivery_delay_days(None, None, TODAY) == 0
    assert delivery_delay_days(None, date(2026, 9, 30), TODAY) == 0


# -----------------------------------------------------------------------------
# Idle days
# -----------------------------------------------------------------------------


def test_idle_days_counts_whole_days_since_the_last_movement() -> None:
    moved = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    assert days_between(moved, now) == 184


def test_a_position_that_has_never_moved_yields_no_number_at_all() -> None:
    # None, not zero: the caller has to decide what "never" means, and every
    # caller here buckets it as older than any real number of days.
    assert days_between(None, datetime(2026, 9, 1, tzinfo=UTC)) is None


def test_a_movement_in_the_future_does_not_produce_negative_idle_days() -> None:
    # Clock skew between the API and a worker can produce this. Reporting it as
    # negative would sort the row to the top of the dead-stock list.
    future = datetime(2026, 9, 2, tzinfo=UTC)
    assert days_between(future, datetime(2026, 9, 1, tzinfo=UTC)) == 0
