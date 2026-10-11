"""The cost-centre statement's place in the report catalogue.

The query itself is exercised against PostgreSQL in
``tests/integration/test_cost_centres_service.py``. What is pinned here is the
contract around it: it is a period report that refuses to run unbounded, it is
never served from cache, its columns are its row schema, and its route is the
kebab-case of its key - which is how the frontend finds it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from src.core.errors import ValidationError
from src.modules.reports.models import ReportKey
from src.modules.reports.router import router
from src.modules.reports.schemas import CostCentreExpenseRow, ReportFilters, ReportPeriod
from src.modules.reports.service import (
    REPORT_CATALOGUE,
    build_context,
    columns_for,
    definition_for,
    descriptors,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)


def test_it_is_in_the_catalogue_and_the_picker() -> None:
    assert ReportKey.COST_CENTRE_EXPENSE in REPORT_CATALOGUE
    offered = {descriptor.key for descriptor in descriptors()}
    assert ReportKey.COST_CENTRE_EXPENSE in offered


def test_it_is_called_a_statement() -> None:
    assert definition_for(ReportKey.COST_CENTRE_EXPENSE).title == "Cost centre statement"


def test_it_will_not_run_without_a_period() -> None:
    definition = definition_for(ReportKey.COST_CENTRE_EXPENSE)
    assert definition.requires_period is True
    with pytest.raises(ValidationError):
        build_context(definition, None, ReportFilters(), now=NOW)


def test_it_is_never_cached() -> None:
    """An open period still takes postings; a lagging statement would disagree
    with the account statement for the same day."""
    assert definition_for(ReportKey.COST_CENTRE_EXPENSE).cacheable is False


def test_its_columns_are_its_row_schema() -> None:
    names = [column.name for column in columns_for(CostCentreExpenseRow)]
    assert names == [
        "cost_centre_id",
        "cost_centre_code",
        "cost_centre_name",
        "account_id",
        "account_code",
        "account_name",
        "account_type",
        "debit_amount",
        "credit_amount",
        "net_amount",
    ]


def test_it_is_served_at_the_kebab_case_of_its_key() -> None:
    paths = {getattr(route, "path", "") for route in router.routes}
    assert "/reports/cost-centre-expense" in paths


def test_a_full_financial_year_is_a_valid_period() -> None:
    period = ReportPeriod(date_from=date(2026, 4, 1), date_to=date(2027, 3, 31))
    ctx = build_context(
        definition_for(ReportKey.COST_CENTRE_EXPENSE), period, ReportFilters(), now=NOW
    )
    assert ctx.period == period
