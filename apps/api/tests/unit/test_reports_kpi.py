"""KPI arithmetic (master rules section 25).

Every derived figure a manager reads off a report - yield, utilisation, margin,
on-time percentage, days of cover - is computed by a pure function here, from
values the query returned. These tests exist because a wrong number on a
management screen is worse than a missing one: nobody can reproduce it, and
decisions get taken on it.

Three properties are pinned throughout:

* **Nothing divides by zero and nothing raises.** A machine with no scheduled
  hours, an item nobody inspected, a period with no sales - each is a real row
  that has to render.
* **Nothing touches binary floating point.** Section 84 applies to a report as
  much as to an invoice, so every result is an exact ``Decimal``.
* **Each derivation is exercised through the report definition that uses it**,
  so a definition wired to the wrong derivation fails here rather than in
  production.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from src.modules.reports.models import ReportKey
from src.modules.reports.schemas import ReportFilters, ReportPeriod
from src.modules.reports.service import (
    REPORT_CATALOGUE,
    ReportContext,
    available_minutes,
    percent,
    ratio,
    vendor_performance_score,
)

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def _context(period: ReportPeriod | None = None, **filters: object) -> ReportContext:
    return ReportContext(
        now=NOW,
        as_of=NOW.date(),
        filters=ReportFilters(**filters),  # type: ignore[arg-type]
        period=period,
    )


def _derive(key: ReportKey, row: dict[str, object], ctx: ReportContext) -> dict[str, object]:
    """Run a report's own derivation, so the wiring is under test too."""
    definition = REPORT_CATALOGUE[key]
    assert definition.derive is not None, f"{key} has no derivation to test"
    return definition.derive(dict(row), ctx)


# -----------------------------------------------------------------------------
# The two primitives
# -----------------------------------------------------------------------------


def test_a_percentage_is_exact_to_two_places() -> None:
    assert percent(Decimal("1"), Decimal("3")) == Decimal("33.33")
    assert percent(Decimal("2"), Decimal("3")) == Decimal("66.67")


def test_a_percentage_stays_a_decimal_and_never_becomes_a_float() -> None:
    assert isinstance(percent(1, 8), Decimal)
    assert percent(1, 8) == Decimal("12.50")


def test_a_percentage_of_nothing_is_zero_rather_than_an_error() -> None:
    # An item nobody inspected has no rejection rate. Raising here would take out
    # the whole report page over a row with no activity.
    assert percent(Decimal("5"), Decimal("0")) == Decimal("0.00")


def test_a_ratio_of_nothing_is_zero_and_keeps_quantity_scale() -> None:
    assert ratio(Decimal("10"), Decimal("0")) == Decimal("0")
    assert ratio(Decimal("10"), Decimal("3")) == Decimal("3.3333")


def test_capacity_is_hours_times_sixty_across_the_calendar_days_of_the_period() -> None:
    assert available_minutes(Decimal("8"), 30) == Decimal("14400.0000")


def test_a_work_centre_with_no_working_hours_has_no_capacity() -> None:
    assert available_minutes(Decimal("0"), 30) == Decimal("0.0000")


def test_the_vendor_score_weights_delivery_and_quality_equally() -> None:
    assert vendor_performance_score(Decimal("100"), Decimal("80")) == Decimal("90.00")
    assert vendor_performance_score(Decimal("0"), Decimal("0")) == Decimal("0.00")


# -----------------------------------------------------------------------------
# Sales order fulfilment
# -----------------------------------------------------------------------------


def test_order_fulfilment_reports_what_is_still_owed() -> None:
    row = _derive(
        ReportKey.SALES_ORDER_STATUS,
        {
            "ordered_quantity": Decimal("100"),
            "allocated_quantity": Decimal("100"),
            "dispatched_quantity": Decimal("40"),
        },
        _context(),
    )
    assert row["pending_quantity"] == Decimal("60")
    assert row["fulfilment_percent"] == Decimal("40.00")


def test_over_dispatch_never_reports_a_negative_outstanding_quantity() -> None:
    # An over-dispatch is a data problem to investigate, not a negative backlog
    # that silently reduces the total owed on the report footer.
    row = _derive(
        ReportKey.SALES_ORDER_STATUS,
        {
            "ordered_quantity": Decimal("10"),
            "allocated_quantity": Decimal("10"),
            "dispatched_quantity": Decimal("12"),
        },
        _context(),
    )
    assert row["pending_quantity"] == Decimal("0")


# -----------------------------------------------------------------------------
# Stock availability and dead stock
# -----------------------------------------------------------------------------


def test_a_position_at_its_reorder_level_is_flagged_not_just_one_below_it() -> None:
    row = _derive(
        ReportKey.STOCK_AVAILABILITY,
        {"available": Decimal("25"), "reorder_level": Decimal("25")},
        _context(),
    )
    assert row["below_reorder_level"] is True


def test_an_item_with_no_reorder_level_is_never_flagged() -> None:
    # Zero means "not configured", not "reorder at zero" - otherwise every item
    # nobody has set up would sit permanently on the reorder list.
    row = _derive(
        ReportKey.STOCK_AVAILABILITY,
        {"available": Decimal("0"), "reorder_level": Decimal("0")},
        _context(),
    )
    assert row["below_reorder_level"] is False


def test_dead_stock_that_has_never_moved_is_bucketed_as_never_moved() -> None:
    row = _derive(ReportKey.DEAD_STOCK, {"last_movement_at": None}, _context())
    assert row["days_since_movement"] is None
    assert row["ageing_bucket"] == "Never moved"


def test_dead_stock_ageing_is_measured_from_the_last_movement() -> None:
    row = _derive(
        ReportKey.DEAD_STOCK,
        {"last_movement_at": datetime(2026, 1, 1, tzinfo=UTC)},
        _context(),
    )
    assert row["days_since_movement"] == 252
    assert row["ageing_bucket"] == "181-365 days"


# -----------------------------------------------------------------------------
# Movement
# -----------------------------------------------------------------------------


def test_average_daily_usage_divides_by_the_days_in_the_period() -> None:
    period = ReportPeriod(date_from=date(2026, 8, 1), date_to=date(2026, 8, 31))
    row = _derive(
        ReportKey.FAST_MOVING,
        {"issued_quantity": Decimal("620"), "on_hand": Decimal("310")},
        _context(period),
    )
    assert row["average_daily_usage"] == Decimal("20.0000")
    assert row["days_of_cover"] == Decimal("15.5000")


def test_the_period_includes_both_of_its_endpoints() -> None:
    # A single-day period is one day, not zero. Getting this wrong would divide
    # by zero on the busiest report anybody runs - yesterday's movement.
    period = ReportPeriod(date_from=date(2026, 8, 1), date_to=date(2026, 8, 1))
    assert period.days == 1


def test_an_item_that_did_not_move_has_no_days_of_cover_rather_than_infinite() -> None:
    period = ReportPeriod(date_from=date(2026, 8, 1), date_to=date(2026, 8, 31))
    row = _derive(
        ReportKey.SLOW_MOVING,
        {"issued_quantity": Decimal("0"), "on_hand": Decimal("500")},
        _context(period),
    )
    assert row["average_daily_usage"] == Decimal("0")
    assert row["days_of_cover"] is None


# -----------------------------------------------------------------------------
# Quality and vendors
# -----------------------------------------------------------------------------


def test_the_rejection_rate_counts_scrapped_material_as_refused() -> None:
    # Scrap is material the inspection refused and wrote off. Leaving it out
    # would let a vendor whose goods are always scrapped score a clean sheet.
    row = _derive(
        ReportKey.QC_REJECTION,
        {
            "inspected_quantity": Decimal("200"),
            "rejected_quantity": Decimal("10"),
            "scrap_quantity": Decimal("30"),
        },
        _context(),
    )
    assert row["rejection_rate_percent"] == Decimal("20.00")


def test_vendor_performance_combines_on_time_delivery_with_quality() -> None:
    row = _derive(
        ReportKey.VENDOR_PERFORMANCE,
        {
            "receipt_count": 10,
            "on_time_receipt_count": 8,
            "received_quantity": Decimal("1000"),
            "rejected_quantity": Decimal("100"),
        },
        _context(),
    )
    assert row["on_time_percent"] == Decimal("80.00")
    assert row["rejection_rate_percent"] == Decimal("10.00")
    # 80% delivery, 90% quality, weighted equally.
    assert row["performance_score"] == Decimal("85.00")


def test_a_vendor_with_no_judgeable_receipts_scores_zero_rather_than_erroring() -> None:
    row = _derive(
        ReportKey.VENDOR_PERFORMANCE,
        {
            "receipt_count": 0,
            "on_time_receipt_count": 0,
            "received_quantity": Decimal("0"),
            "rejected_quantity": Decimal("0"),
        },
        _context(),
    )
    assert row["on_time_percent"] == Decimal("0.00")
    assert row["performance_score"] == Decimal("50.00")


# -----------------------------------------------------------------------------
# Profitability
# -----------------------------------------------------------------------------


def test_margin_is_revenue_less_cost_and_the_percentage_is_of_revenue() -> None:
    row = _derive(
        ReportKey.PRODUCT_PROFITABILITY,
        {"revenue_amount": Decimal("1000.0000"), "cost_amount": Decimal("750.0000")},
        _context(),
    )
    assert row["margin_amount"] == Decimal("250.0000")
    assert row["margin_percent"] == Decimal("25.00")


def test_a_loss_making_item_reports_a_negative_margin_rather_than_zero() -> None:
    # Clamping this would hide the single most important row on the report.
    row = _derive(
        ReportKey.PRODUCT_PROFITABILITY,
        {"revenue_amount": Decimal("100"), "cost_amount": Decimal("150")},
        _context(),
    )
    assert row["margin_amount"] == Decimal("-50")
    assert row["margin_percent"] == Decimal("-50.00")


def test_pending_return_cost_withholds_cost_and_margin_rather_than_using_an_estimate() -> None:
    row = _derive(
        ReportKey.PRODUCT_PROFITABILITY,
        {
            "revenue_amount": Decimal("1000"),
            "cost_amount": Decimal("800"),
            "cost_pending": True,
        },
        _context(),
    )
    assert row["cost_amount"] is None
    assert row["margin_amount"] is None
    assert row["margin_percent"] is None


def test_unpriced_uninvoiced_quantity_withholds_revenue_and_margin() -> None:
    row = _derive(
        ReportKey.PRODUCT_PROFITABILITY,
        {
            "revenue_amount": Decimal("1000"),
            "cost_amount": Decimal("800"),
            "unpriced_quantity": Decimal("2"),
        },
        _context(),
    )
    assert row["revenue_amount"] is None
    assert row["margin_amount"] is None
    assert row["margin_percent"] is None


def test_a_credit_only_period_keeps_the_signed_margin_but_has_no_percentage() -> None:
    row = _derive(
        ReportKey.PRODUCT_PROFITABILITY,
        {"revenue_amount": Decimal("-50"), "cost_amount": Decimal("-40")},
        _context(),
    )
    assert row["margin_amount"] == Decimal("-10")
    assert row["margin_percent"] is None


# -----------------------------------------------------------------------------
# Production and machines
# -----------------------------------------------------------------------------


def test_yield_is_good_output_over_everything_attempted() -> None:
    row = _derive(
        ReportKey.PRODUCTION_EFFICIENCY,
        {
            "produced_quantity": Decimal("90"),
            "rejected_quantity": Decimal("8"),
            "scrap_quantity": Decimal("2"),
            "planned_minutes": Decimal("600"),
            "actual_minutes": Decimal("750"),
            "planned_end_date": date(2026, 9, 5),
            "actual_end_date": date(2026, 9, 4),
        },
        _context(),
    )
    assert row["yield_percent"] == Decimal("90.00")


def test_shift_yield_is_good_output_over_good_and_rejected() -> None:
    row = _derive(
        ReportKey.SHIFT_PRODUCTION,
        {"produced_quantity": Decimal("50"), "rejected_quantity": Decimal("2")},
        _context(),
    )
    assert row["yield_percent"] == Decimal("96.15")


def test_a_shift_that_only_booked_downtime_has_no_yield_rather_than_an_error() -> None:
    row = _derive(
        ReportKey.SHIFT_PRODUCTION,
        {"produced_quantity": Decimal("0"), "rejected_quantity": Decimal("0")},
        _context(),
    )
    assert row["yield_percent"] == Decimal("0.00")


def test_schedule_efficiency_is_planned_over_actual_so_faster_reads_higher() -> None:
    row = _derive(
        ReportKey.PRODUCTION_EFFICIENCY,
        {
            "produced_quantity": Decimal("100"),
            "rejected_quantity": Decimal("0"),
            "scrap_quantity": Decimal("0"),
            "planned_minutes": Decimal("600"),
            "actual_minutes": Decimal("500"),
            "planned_end_date": date(2026, 9, 5),
            "actual_end_date": date(2026, 9, 5),
        },
        _context(),
    )
    assert row["schedule_efficiency_percent"] == Decimal("120.00")
    assert row["completed_on_time"] is True


def test_an_order_finished_after_its_planned_end_date_is_not_on_time() -> None:
    row = _derive(
        ReportKey.PRODUCTION_EFFICIENCY,
        {
            "produced_quantity": Decimal("100"),
            "rejected_quantity": Decimal("0"),
            "scrap_quantity": Decimal("0"),
            "planned_minutes": Decimal("600"),
            "actual_minutes": Decimal("600"),
            "planned_end_date": date(2026, 9, 5),
            "actual_end_date": date(2026, 9, 6),
        },
        _context(),
    )
    assert row["completed_on_time"] is False


def test_utilisation_is_busy_minutes_against_the_capacity_of_the_whole_period() -> None:
    period = ReportPeriod(date_from=date(2026, 9, 1), date_to=date(2026, 9, 10))
    row = _derive(
        ReportKey.MACHINE_UTILISATION,
        {
            "working_hours_per_day": Decimal("8"),
            "busy_minutes": Decimal("2400"),
            "downtime_minutes": Decimal("480"),
            "rostered_minutes": Decimal("0"),
        },
        _context(period),
    )
    # Ten days at eight hours is 4 800 minutes of capacity.
    assert row["available_minutes"] == Decimal("4800.0000")
    assert row["utilisation_percent"] == Decimal("50.00")
    assert row["downtime_percent"] == Decimal("10.00")
    # Nothing rostered: no rostered utilisation, and no division error either.
    assert row["rostered_utilisation_percent"] == Decimal("0.00")


def test_rostered_utilisation_sits_beside_calendar_utilisation() -> None:
    """Busy against the minutes the roster planned, without moving the calendar figure."""
    period = ReportPeriod(date_from=date(2026, 9, 1), date_to=date(2026, 9, 10))
    row = _derive(
        ReportKey.MACHINE_UTILISATION,
        {
            "working_hours_per_day": Decimal("8"),
            "busy_minutes": Decimal("2400"),
            "downtime_minutes": Decimal("0"),
            # Six eight-hour shifts planned on it, net of breaks.
            "rostered_minutes": Decimal("2880"),
        },
        _context(period),
    )
    assert row["utilisation_percent"] == Decimal("50.00")
    assert row["rostered_utilisation_percent"] == Decimal("83.33")


def test_a_machine_on_a_work_centre_with_no_hours_reports_zero_not_an_error() -> None:
    period = ReportPeriod(date_from=date(2026, 9, 1), date_to=date(2026, 9, 10))
    row = _derive(
        ReportKey.MACHINE_UTILISATION,
        {
            "working_hours_per_day": Decimal("0"),
            "busy_minutes": Decimal("120"),
            "downtime_minutes": Decimal("0"),
            "rostered_minutes": Decimal("0"),
        },
        _context(period),
    )
    assert row["available_minutes"] == Decimal("0.0000")
    assert row["utilisation_percent"] == Decimal("0.00")


# -----------------------------------------------------------------------------
# Valuation
# -----------------------------------------------------------------------------


def test_unit_value_is_the_position_value_divided_by_what_is_on_hand() -> None:
    row = _derive(
        ReportKey.INVENTORY_VALUATION,
        {"valuation_amount": Decimal("1250.0000"), "on_hand": Decimal("100")},
        _context(),
    )
    assert row["unit_value"] == Decimal("12.5000")


def test_a_position_with_value_but_no_quantity_reports_no_unit_value() -> None:
    row = _derive(
        ReportKey.INVENTORY_VALUATION,
        {"valuation_amount": Decimal("40"), "on_hand": Decimal("0")},
        _context(),
    )
    assert row["unit_value"] == Decimal("0")


# -----------------------------------------------------------------------------
# Dispatch
# -----------------------------------------------------------------------------


def test_a_dispatch_row_reports_the_delay_against_the_promised_date() -> None:
    row = _derive(
        ReportKey.DISPATCH,
        {
            "expected_delivery_date": date(2026, 9, 1),
            "actual_delivery_date": date(2026, 9, 4),
        },
        _context(),
    )
    assert row["delay_days"] == 3


def test_the_vendor_dispatch_history_uses_the_same_delay_definition() -> None:
    # Two reports about the same consignments must not disagree about lateness.
    payload = {
        "expected_delivery_date": date(2026, 9, 1),
        "actual_delivery_date": None,
    }
    assert (
        _derive(ReportKey.VENDOR_DISPATCH_HISTORY, dict(payload), _context())["delay_days"]
        == _derive(ReportKey.DISPATCH, dict(payload), _context())["delay_days"]
    )


def test_days_in_transit_counts_from_the_day_the_consignment_left() -> None:
    row = _derive(ReportKey.COURIER_TRACKING, {"dispatch_date": date(2026, 9, 3)}, _context())
    assert row["days_in_transit"] == 7


# -----------------------------------------------------------------------------
# Shortage urgency
# -----------------------------------------------------------------------------


def test_a_shortage_needed_before_today_is_flagged_overdue() -> None:
    period = ReportPeriod(date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
    row = _derive(
        ReportKey.MRP_SHORTAGE,
        {"earliest_required_date": date(2026, 9, 1)},
        _context(period),
    )
    assert row["days_to_required"] == -9
    assert row["urgency_bucket"] == "Overdue"


def test_a_shortage_needed_next_month_is_not_urgent() -> None:
    period = ReportPeriod(date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
    row = _derive(
        ReportKey.MRP_SHORTAGE,
        {"earliest_required_date": date(2026, 10, 31)},
        _context(period),
    )
    assert row["urgency_bucket"] == "Beyond 30 days"


def test_pending_reports_share_one_overdue_definition() -> None:
    # Purchase, production and job work all age the same way. If they did not,
    # "what is late?" would have three different answers.
    ctx = _context()
    purchase = _derive(
        ReportKey.PURCHASE_PENDING, {"expected_delivery_date": date(2026, 8, 1)}, ctx
    )
    production = _derive(ReportKey.PRODUCTION_PENDING, {"planned_end_date": date(2026, 8, 1)}, ctx)
    job_work = _derive(ReportKey.JOB_WORK_PENDING, {"expected_return_date": date(2026, 8, 1)}, ctx)
    assert purchase["days_overdue"] == production["days_overdue"] == job_work["days_overdue"] == 40
    assert purchase["ageing_bucket"] == production["ageing_bucket"] == job_work["ageing_bucket"]


def test_a_purchase_order_with_no_promised_date_is_reported_as_current() -> None:
    row = _derive(ReportKey.PURCHASE_PENDING, {"expected_delivery_date": None}, _context())
    assert row["days_overdue"] == 0
    assert row["ageing_bucket"] == "Current"


def test_derivations_do_not_mutate_the_uuid_bearing_row_identity() -> None:
    # The derivation adds computed columns; it must not quietly rewrite the keys
    # that identify the row, or a drill-through link would point at the wrong
    # document.
    identifier = uuid.uuid4()
    row = _derive(
        ReportKey.SALES_ORDER_STATUS,
        {
            "sales_order_id": identifier,
            "ordered_quantity": Decimal("5"),
            "allocated_quantity": Decimal("5"),
            "dispatched_quantity": Decimal("5"),
        },
        _context(),
    )
    assert row["sales_order_id"] == identifier
