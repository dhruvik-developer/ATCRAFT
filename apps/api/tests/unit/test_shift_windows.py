"""Shift windows: wall-clock times in the company timezone become UTC instants.

The roster stores each planned window as two instants, computed once from the
shift's local times and the date the shift *starts* on. Everything that decides
an overlap reads those instants, so the arithmetic here is the whole of the
correctness argument for the EXCLUDE constraints - a night shift placed on the
wrong day, or back-to-back shifts treated as overlapping, would be refused or
admitted by the database for a reason nobody could see.

India is the case that matters: IST is UTC+05:30, so the UTC date is still
yesterday until half past five in the morning, and a shift starting at 02:00
local is a shift whose UTC start falls on the previous calendar day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from src.core.errors import BusinessRuleError
from src.modules.routing.models import MINUTES_PER_DAY, shift_crosses_midnight, shift_minutes
from src.modules.routing.service import (
    local_date,
    net_shift_minutes,
    resolve_zone,
    shift_window,
    windows_overlap,
)

pytestmark = pytest.mark.unit

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 10, 11)
NEXT_DAY = date(2026, 10, 12)


@dataclass(frozen=True)
class _Shift:
    start_time: time
    end_time: time
    break_minutes: int = 0


A = _Shift(time(6), time(14))
B = _Shift(time(14), time(22))
C = _Shift(time(22), time(6))
GENERAL = _Shift(time(9), time(17, 30), break_minutes=30)
FULL_DAY = _Shift(time(6), time(6))


def utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


# --- lengths -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("shift", "minutes"),
    [(A, 480), (B, 480), (C, 480), (GENERAL, 510), (FULL_DAY, MINUTES_PER_DAY)],
)
def test_a_shift_is_as_long_as_its_clock_says(shift: _Shift, minutes: int) -> None:
    assert shift_minutes(shift.start_time, shift.end_time) == minutes


def test_the_break_comes_off_the_working_minutes_not_the_window() -> None:
    assert net_shift_minutes(GENERAL) == 480
    start, end = shift_window(DAY, GENERAL, IST)
    assert (end - start).total_seconds() == 510 * 60


@pytest.mark.parametrize(
    ("start", "end", "crosses"),
    [
        (time(6), time(14), False),
        (time(22), time(6), True),
        # An end equal to the start is the one way to say twenty-four hours.
        (time(6), time(6), True),
        (time(0), time(23, 59), False),
    ],
)
def test_crossing_midnight_is_derived_from_the_times(start: time, end: time, crosses: bool) -> None:
    assert shift_crosses_midnight(start, end) is crosses


# --- windows -----------------------------------------------------------------


def test_the_night_shift_belongs_to_the_date_it_starts_and_ends_the_next_morning() -> None:
    assert shift_window(DAY, C, IST) == (utc(11, 16, 30), utc(12, 0, 30))


def test_a_twenty_four_hour_shift_ends_at_the_same_clock_time_the_next_day() -> None:
    start, end = shift_window(DAY, FULL_DAY, IST)
    assert (start, end) == (utc(11, 0, 30), utc(12, 0, 30))


def test_the_zone_may_be_named_or_resolved() -> None:
    assert shift_window(DAY, A, "Asia/Kolkata") == shift_window(DAY, A, IST)


def test_an_unknown_company_timezone_is_a_refusal_not_a_crash() -> None:
    with pytest.raises(BusinessRuleError, match="not known to this server"):
        resolve_zone("Mars/Olympus_Mons")


# --- overlaps ----------------------------------------------------------------


def test_back_to_back_shifts_do_not_overlap() -> None:
    assert not windows_overlap(shift_window(DAY, A, IST), shift_window(DAY, B, IST))
    assert not windows_overlap(shift_window(DAY, B, IST), shift_window(DAY, C, IST))


def test_the_general_shift_overlaps_both_day_shifts() -> None:
    general = shift_window(DAY, GENERAL, IST)
    assert windows_overlap(general, shift_window(DAY, A, IST))
    assert windows_overlap(general, shift_window(DAY, B, IST))
    assert not windows_overlap(general, shift_window(DAY, C, IST))


def test_the_night_shift_on_one_day_meets_the_morning_shift_of_the_next() -> None:
    night = shift_window(DAY, C, IST)
    assert not windows_overlap(night, shift_window(NEXT_DAY, A, IST))
    # ...but an earlier start on the next day does land inside it.
    early = _Shift(time(5), time(13))
    assert windows_overlap(night, shift_window(NEXT_DAY, early, IST))


def test_the_same_shift_on_consecutive_days_never_overlaps() -> None:
    assert not windows_overlap(shift_window(DAY, C, IST), shift_window(NEXT_DAY, C, IST))
    assert not windows_overlap(
        shift_window(DAY, FULL_DAY, IST), shift_window(NEXT_DAY, FULL_DAY, IST)
    )


def test_overlap_is_symmetric_and_half_open() -> None:
    first = (utc(11, 0), utc(11, 8))
    touching = (utc(11, 8), utc(11, 16))
    inside = (utc(11, 2), utc(11, 4))
    assert not windows_overlap(first, touching)
    assert not windows_overlap(touching, first)
    assert windows_overlap(first, inside)
    assert windows_overlap(inside, first)


# --- the local date against the UTC date -------------------------------------


def test_an_early_morning_shift_starts_on_the_previous_utc_day() -> None:
    """The roster date is local. A 02:00 IST start is 20:30 UTC the day before."""
    small_hours = _Shift(time(2), time(10))
    start, _ = shift_window(NEXT_DAY, small_hours, IST)
    assert start == utc(11, 20, 30)
    assert start.date() == DAY
    assert local_date(start, IST) == NEXT_DAY


def test_today_where_the_company_is_runs_ahead_of_utc_after_midnight_local() -> None:
    # 00:15 IST on the 12th is still 18:45 UTC on the 11th.
    moment = utc(11, 18, 45)
    assert moment.date() == DAY
    assert local_date(moment, IST) == NEXT_DAY
