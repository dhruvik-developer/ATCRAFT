"""The reporting contract: what may be asked for, and what may be cached.

These are the rules that make the module safe rather than merely functional, and
none of them is visible in a single report's code:

* a period report will not run without a bounded period (master rules section 25);
* nothing derived from stock on hand is ever served from cache (section 34);
* a report's spreadsheet columns are its API columns, because both come from one
  schema;
* two requests describing the same report share one snapshot, and two describing
  different reports do not.

A regression in any of them is silent - the report still renders, it is just
wrong or unsafe - which is exactly why they are asserted here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from src.core.deps import Principal
from src.core.errors import ValidationError
from src.core.permissions import Permission
from src.modules.reports.models import (
    DOCUMENT_ONLY_REPORT_KEYS,
    REPORT_JOB_LIFECYCLE,
    ReportJobStatus,
    ReportKey,
)
from src.modules.reports.schemas import (
    REPORT_MAX_RANGE_DAYS,
    ReportFilters,
    ReportPeriod,
)
from src.modules.reports.service import (
    REPORT_CATALOGUE,
    build_context,
    columns_for,
    definition_for,
    descriptors,
    parameters_hash,
    parameters_of,
)

NOW = datetime(2026, 9, 10, 9, 30, tzinfo=UTC)
PERIOD = ReportPeriod(date_from=date(2026, 8, 1), date_to=date(2026, 8, 31))


# -----------------------------------------------------------------------------
# The catalogue is complete and self-consistent
# -----------------------------------------------------------------------------


def test_every_report_in_the_vocabulary_has_a_definition() -> None:
    # Every key but the document-only ones: those are rendered by a dedicated
    # task and have a job row, never a query.
    assert set(REPORT_CATALOGUE) == set(ReportKey) - DOCUMENT_ONLY_REPORT_KEYS


def test_a_document_only_key_has_a_job_row_but_no_definition_and_no_descriptor() -> None:
    # The packing list is a PDF of one order, not a grid: giving it a definition
    # would let the catalogue runner count, page and cache rows it does not have,
    # and listing it in the picker would offer a report nobody can run.
    assert ReportKey.PACKING_LIST in DOCUMENT_ONLY_REPORT_KEYS
    offered = {descriptor.key for descriptor in descriptors()}
    for key in DOCUMENT_ONLY_REPORT_KEYS:
        assert key not in REPORT_CATALOGUE, f"{key} must not be a catalogue report"
        assert key not in offered, f"{key} must not appear in the report picker"


def test_the_purchase_return_debit_note_is_a_document_not_a_report() -> None:
    # It is a PDF of one posted return, rendered by its own task, so it must
    # have a job row and stay out of the catalogue and the picker.
    assert ReportKey.PURCHASE_RETURN_DEBIT_NOTE in DOCUMENT_ONLY_REPORT_KEYS


def test_every_definition_is_filed_under_its_own_key() -> None:
    # A copy-pasted definition left under the wrong key would serve one report's
    # rows from another report's query.
    for key, definition in REPORT_CATALOGUE.items():
        assert definition.key is key


def test_the_master_rules_reports_are_all_present() -> None:
    required = {
        "SALES_ORDER_STATUS",
        "STOCK_AVAILABILITY",
        "MRP_SHORTAGE",
        "PURCHASE_PENDING",
        "PRODUCTION_PENDING",
        "JOB_WORK_PENDING",
        "QC_REJECTION",
        "VENDOR_PERFORMANCE",
        "PRODUCT_PROFITABILITY",
        "FAST_MOVING",
        "SLOW_MOVING",
        "DEAD_STOCK",
        "ORDER_DELAY",
        "PRODUCTION_EFFICIENCY",
        "MACHINE_UTILISATION",
        "INVENTORY_VALUATION",
        "DISPATCH",
        "COURIER_TRACKING",
        "VENDOR_DISPATCH_HISTORY",
    }
    assert required <= {str(key) for key in REPORT_CATALOGUE}


def test_the_descriptor_list_carries_a_column_set_for_every_report() -> None:
    for descriptor in descriptors():
        assert descriptor.columns, f"{descriptor.key} exposes no columns"
        assert descriptor.title and descriptor.description


def test_production_cost_is_hidden_without_item_cost_permission() -> None:
    base = {
        "user_id": uuid.uuid4(),
        "company_id": uuid.uuid4(),
        "email": "reporter@example.test",
        "full_name": "Report User",
        "is_superuser": False,
    }
    report_reader = Principal(**base, permissions=frozenset({str(Permission.REPORT_VIEW)}))
    cost_reader = Principal(
        **base,
        permissions=frozenset({str(Permission.REPORT_VIEW), str(Permission.ITEM_COST_VIEW)}),
    )

    assert Permission.ITEM_COST_VIEW is definition_for(ReportKey.PRODUCTION_COST).permission
    assert ReportKey.PRODUCTION_COST not in {d.key for d in descriptors(report_reader)}
    assert ReportKey.PRODUCTION_COST in {d.key for d in descriptors(cost_reader)}


def test_a_reports_columns_are_exactly_its_row_schema() -> None:
    # The spreadsheet writer and the JSON response are driven from this one list,
    # so an artefact cannot show a different set of columns from the API.
    for definition in REPORT_CATALOGUE.values():
        names = [column.name for column in columns_for(definition.row_model)]
        assert names == list(definition.row_model.model_fields)


# -----------------------------------------------------------------------------
# Bounded periods
# -----------------------------------------------------------------------------


def test_a_period_report_refuses_to_run_without_a_period() -> None:
    definition = definition_for(ReportKey.SALES_ORDER_STATUS)
    assert definition.requires_period is True
    with pytest.raises(ValidationError) as raised:
        build_context(definition, None, ReportFilters(), now=NOW)
    assert "date_from" in str(raised.value)


def test_a_position_report_takes_no_period_at_all() -> None:
    # Stock availability reads a current position, not a span of history. A date
    # range on it would be a parameter that changes no row - and, worse, one that
    # would split the snapshot cache between requests with identical results.
    definition = definition_for(ReportKey.STOCK_AVAILABILITY)
    ctx = build_context(definition, PERIOD, ReportFilters(), now=NOW)
    assert ctx.period is None


def test_a_period_may_not_run_backwards() -> None:
    with pytest.raises(ValidationError):
        ReportPeriod(date_from=date(2026, 9, 30), date_to=date(2026, 9, 1))


def test_a_period_longer_than_the_maximum_is_refused_with_a_usable_message() -> None:
    with pytest.raises(ValidationError) as raised:
        ReportPeriod(date_from=date(2020, 1, 1), date_to=date(2026, 1, 1))
    message = str(raised.value)
    assert str(REPORT_MAX_RANGE_DAYS) in message
    assert "Narrow the range" in message


def test_the_longest_permitted_period_is_accepted() -> None:
    # The boundary itself must work, or "the whole financial year" is unrunnable.
    period = ReportPeriod(
        date_from=date(2026, 4, 1), date_to=date(2026, 4, 1) + _days(REPORT_MAX_RANGE_DAYS - 1)
    )
    assert period.days == REPORT_MAX_RANGE_DAYS


def _days(count: int):
    from datetime import timedelta

    return timedelta(days=count)


def test_the_report_clock_is_read_once_for_the_whole_run() -> None:
    # Every ageing and urgency column is derived per row. Reading the clock
    # inside that loop would let a report that crosses midnight bucket its first
    # rows one day differently from its last.
    ctx = build_context(
        definition_for(ReportKey.SALES_ORDER_STATUS), PERIOD, ReportFilters(), now=NOW
    )
    assert ctx.now == NOW
    assert ctx.as_of == NOW.date()


# -----------------------------------------------------------------------------
# What may be cached
# -----------------------------------------------------------------------------

#: Reports whose numbers derive from stock on hand. Section 34 forbids serving
#: any of them from cache, because a stale availability figure that lets two
#: orders reserve the same stock is a correctness bug, not a performance win.
STOCK_DERIVED_REPORTS = {
    ReportKey.STOCK_AVAILABILITY,
    ReportKey.INVENTORY_VALUATION,
    ReportKey.DEAD_STOCK,
    ReportKey.SLOW_MOVING,
    ReportKey.MRP_SHORTAGE,
}


def test_no_stock_derived_report_is_ever_cached() -> None:
    for key in STOCK_DERIVED_REPORTS:
        assert REPORT_CATALOGUE[key].cacheable is False, f"{key} must not be cached"


def test_the_live_tracking_board_is_not_cached_either() -> None:
    # A tracking board is watched while it changes; a five-minute-old event is
    # the one thing it exists to avoid showing.
    assert REPORT_CATALOGUE[ReportKey.COURIER_TRACKING].cacheable is False


def test_completed_historical_periods_are_cacheable() -> None:
    # If none of these were cacheable the cache policy would be decoration.
    assert REPORT_CATALOGUE[ReportKey.VENDOR_PERFORMANCE].cacheable is True
    assert REPORT_CATALOGUE[ReportKey.PRODUCT_PROFITABILITY].cacheable is True


# -----------------------------------------------------------------------------
# Snapshot identity
# -----------------------------------------------------------------------------


def _hash(key: ReportKey, period: ReportPeriod | None, filters: ReportFilters) -> str:
    definition = definition_for(key)
    ctx = build_context(definition, period, filters, now=NOW)
    return parameters_hash(parameters_of(definition, ctx))


def test_the_same_request_hashes_to_the_same_snapshot() -> None:
    first = _hash(ReportKey.SALES_ORDER_STATUS, PERIOD, ReportFilters(search="SO"))
    second = _hash(ReportKey.SALES_ORDER_STATUS, PERIOD, ReportFilters(search="SO"))
    assert first == second


def test_an_empty_filter_describes_the_same_report_as_an_omitted_one() -> None:
    # ``?vendor_id=`` and no vendor_id at all are the same question. Hashing them
    # apart would queue two identical exports for one request made twice.
    explicit = _hash(ReportKey.SALES_ORDER_STATUS, PERIOD, ReportFilters(vendor_id=None))
    omitted = _hash(ReportKey.SALES_ORDER_STATUS, PERIOD, ReportFilters())
    assert explicit == omitted


def test_a_different_period_is_a_different_report() -> None:
    other = ReportPeriod(date_from=date(2026, 7, 1), date_to=date(2026, 7, 31))
    assert _hash(ReportKey.SALES_ORDER_STATUS, PERIOD, ReportFilters()) != _hash(
        ReportKey.SALES_ORDER_STATUS, other, ReportFilters()
    )


def test_a_different_filter_is_a_different_report() -> None:
    assert _hash(ReportKey.SALES_ORDER_STATUS, PERIOD, ReportFilters()) != _hash(
        ReportKey.SALES_ORDER_STATUS, PERIOD, ReportFilters(search="SO")
    )


def test_two_reports_over_the_same_period_never_share_a_snapshot() -> None:
    assert _hash(ReportKey.DISPATCH, PERIOD, ReportFilters()) != _hash(
        ReportKey.VENDOR_DISPATCH_HISTORY, PERIOD, ReportFilters()
    )


def test_a_position_report_ignores_a_period_when_identifying_itself() -> None:
    # It ignores the period when producing rows, so it must ignore it here too;
    # otherwise the same result would be cached under unlimited distinct keys.
    assert _hash(ReportKey.STOCK_AVAILABILITY, PERIOD, ReportFilters()) == _hash(
        ReportKey.STOCK_AVAILABILITY, None, ReportFilters()
    )


def test_the_stored_parameters_describe_the_request_in_readable_form() -> None:
    # A hash alone is unreadable in a support conversation; the artefact has to
    # be able to say what it is a report of.
    definition = definition_for(ReportKey.SALES_ORDER_STATUS)
    ctx = build_context(definition, PERIOD, ReportFilters(search="SO"), now=NOW)
    parameters = parameters_of(definition, ctx)
    assert parameters["report_key"] == "SALES_ORDER_STATUS"
    assert parameters["period"] == {"date_from": "2026-08-01", "date_to": "2026-08-31"}
    assert parameters["filters"] == {"search": "SO"}


# -----------------------------------------------------------------------------
# The job state machine
# -----------------------------------------------------------------------------


def test_the_job_lifecycle_has_no_unreachable_or_dead_end_states() -> None:
    REPORT_JOB_LIFECYCLE.validate()


def test_a_queued_job_can_start_or_fail_but_cannot_complete_without_running() -> None:
    assert REPORT_JOB_LIFECYCLE.can_transition(ReportJobStatus.QUEUED, ReportJobStatus.RUNNING)
    assert REPORT_JOB_LIFECYCLE.can_transition(ReportJobStatus.QUEUED, ReportJobStatus.FAILED)
    assert not REPORT_JOB_LIFECYCLE.can_transition(
        ReportJobStatus.QUEUED, ReportJobStatus.COMPLETED
    )


def test_a_finished_job_never_moves_again() -> None:
    # This is what makes a re-delivered Celery task safe: the second delivery
    # cannot restart a job that has already produced its artefact.
    for terminal in (ReportJobStatus.COMPLETED, ReportJobStatus.FAILED):
        assert REPORT_JOB_LIFECYCLE.is_terminal(terminal)
        assert REPORT_JOB_LIFECYCLE.allowed_targets(terminal) == []
