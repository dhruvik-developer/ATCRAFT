"""The mandatory, bounded date window on audit log queries.

``audit_logs`` is the largest table in the system and never shrinks, so an
unbounded query against it is a denial of service that any authenticated auditor
could trigger by accident. These tests pin the three properties that make that
impossible: a window always exists, it is never wider than the ceiling, and an
over-wide request is refused rather than quietly narrowed.

Pure logic - no database, no FastAPI.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.core.errors import ErrorCode, ValidationError
from src.modules.audit.query_service import (
    DEFAULT_WINDOW_DAYS,
    MAX_WINDOW_DAYS,
    normalise_entity_type,
    resolve_window,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


def test_no_dates_yields_the_default_trailing_window():
    window = resolve_window(None, None, now=NOW)

    assert window.end == NOW
    assert window.start == NOW - timedelta(days=DEFAULT_WINDOW_DAYS)
    assert window.days == pytest.approx(DEFAULT_WINDOW_DAYS)


def test_only_date_from_ends_at_now():
    start = NOW - timedelta(days=5)

    window = resolve_window(start, None, now=NOW)

    assert (window.start, window.end) == (start, NOW)


def test_only_date_to_backfills_the_default_window_before_it():
    end = datetime(2026, 3, 1, tzinfo=UTC)

    window = resolve_window(None, end, now=NOW)

    # Anchored to the supplied date_to, not to now: asking for "everything up to
    # 1 March" must not silently return three months of nothing.
    assert window.end == end
    assert window.start == end - timedelta(days=DEFAULT_WINDOW_DAYS)


def test_both_dates_are_used_verbatim():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 31, tzinfo=UTC)

    window = resolve_window(start, end, now=NOW)

    assert (window.start, window.end) == (start, end)


def test_naive_datetimes_are_read_as_utc():
    window = resolve_window(datetime(2026, 1, 1), datetime(2026, 1, 31), now=NOW)  # noqa: DTZ001

    assert window.start == datetime(2026, 1, 1, tzinfo=UTC)
    assert window.end == datetime(2026, 1, 31, tzinfo=UTC)


def test_offset_datetimes_are_converted_to_utc_not_stripped():
    from datetime import timezone

    ist = timezone(timedelta(hours=5, minutes=30))
    window = resolve_window(datetime(2026, 1, 1, 5, 30, tzinfo=ist), None, now=NOW)

    assert window.start == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def test_inverted_range_is_rejected():
    with pytest.raises(ValidationError) as excinfo:
        resolve_window(NOW, NOW - timedelta(days=1), now=NOW)

    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR
    assert "date_from" in excinfo.value.message


def test_date_from_in_the_future_is_rejected_because_date_to_defaults_to_now():
    with pytest.raises(ValidationError):
        resolve_window(NOW + timedelta(days=1), None, now=NOW)


def test_a_window_at_the_ceiling_is_allowed():
    start = NOW - timedelta(days=MAX_WINDOW_DAYS)

    window = resolve_window(start, NOW, now=NOW)

    assert window.days == pytest.approx(MAX_WINDOW_DAYS)


def test_a_window_past_the_ceiling_is_refused_not_clamped():
    start = NOW - timedelta(days=MAX_WINDOW_DAYS + 1)

    with pytest.raises(ValidationError) as excinfo:
        resolve_window(start, NOW, now=NOW)

    # Refused, not narrowed: a silently clamped window returns a page that looks
    # complete and is not, which in an audit is worse than an error.
    assert excinfo.value.details["max_days"] == MAX_WINDOW_DAYS
    assert excinfo.value.details["requested_days"] > MAX_WINDOW_DAYS


def test_an_open_ended_history_request_cannot_reach_the_beginning_of_time():
    """The guard is what stops "date_from=1970" from scanning the whole table."""
    with pytest.raises(ValidationError):
        resolve_window(datetime(1970, 1, 1, tzinfo=UTC), None, now=NOW)


def test_the_default_window_is_inside_the_ceiling():
    # A default wider than the maximum would make the bare endpoint unusable.
    assert DEFAULT_WINDOW_DAYS < MAX_WINDOW_DAYS


def test_entity_type_is_folded_to_the_form_recorders_write():
    assert normalise_entity_type("  Purchase_Order ") == "purchase_order"


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_entity_type_is_rejected(value):
    with pytest.raises(ValidationError):
        normalise_entity_type(value)


def test_over_long_entity_type_is_rejected_rather_than_truncated():
    # Truncating would produce a filter that matches nothing, which reads as
    # "this record has no history".
    with pytest.raises(ValidationError):
        normalise_entity_type("x" * 65)
