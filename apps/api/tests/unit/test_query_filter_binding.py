"""A cross-field rule on a query filter answers 422, never 500, from any router.

``bind_query_filters`` is the boundary between a filter model's own validators
and the error envelope. Inventory had a private copy; finance and MRP built
their models by hand inside the dependency, so an inverted period raised a
pydantic error from the dependency body and reached the client as
INTERNAL_ERROR. All three now share the core helper, and each model reports the
rule against its upper-bound field so the frontend can attach the message to
the control that produced it.
"""

from __future__ import annotations

from datetime import date

import pytest

import src.modules.finance.router as finance_router
import src.modules.mrp.router as mrp_router
from src.core.errors import ErrorCode, ValidationError
from src.core.messages import FIELDS_NEED_ATTENTION
from src.core.pagination import bind_query_filters
from src.modules.finance.schemas import PeriodFilters, StatementFilters
from src.modules.mrp.schemas import MrpRunFilters

pytestmark = pytest.mark.unit

EARLIER = date(2026, 8, 1)
LATER = date(2026, 8, 31)


class TestTheHelper:
    def test_a_valid_window_is_bound(self) -> None:
        filters = bind_query_filters(StatementFilters, date_from=EARLIER, date_to=LATER)
        assert (filters.date_from, filters.date_to) == (EARLIER, LATER)

    def test_an_inverted_window_is_a_422_against_the_upper_bound(self) -> None:
        with pytest.raises(ValidationError) as exc:
            bind_query_filters(StatementFilters, date_from=LATER, date_to=EARLIER)

        assert exc.value.status_code == 422
        assert exc.value.code is ErrorCode.VALIDATION_ERROR
        assert exc.value.message == FIELDS_NEED_ATTENTION
        assert list(exc.value.details["fields"]) == ["query.date_to"]

    def test_the_message_is_the_one_the_validator_wrote(self) -> None:
        with pytest.raises(ValidationError) as exc:
            bind_query_filters(PeriodFilters, date_from=LATER, date_to=EARLIER)
        (message,) = exc.value.details["fields"]["query.date_to"]
        assert "The period end cannot be before the period start." in message

    def test_a_missing_required_field_names_the_field(self) -> None:
        with pytest.raises(ValidationError) as exc:
            bind_query_filters(PeriodFilters, date_from=EARLIER)
        assert "query.date_to" in exc.value.details["fields"]


class TestTheRouterDependencies:
    def test_the_finance_statement_window_is_bound_through_the_helper(self) -> None:
        with pytest.raises(ValidationError) as exc:
            finance_router.statement_filters(date_from=LATER, date_to=EARLIER)
        assert "query.date_to" in exc.value.details["fields"]

        filters = finance_router.statement_filters(date_from=EARLIER, date_to=LATER)
        assert filters.date_to == LATER

    def test_the_mrp_run_window_is_bound_through_the_helper(self) -> None:
        with pytest.raises(ValidationError) as exc:
            mrp_router.run_filters(
                status_filter=None, created_from=LATER, created_to=EARLIER, search=None
            )
        assert "query.created_to" in exc.value.details["fields"]

        filters = mrp_router.run_filters(
            status_filter=None, created_from=EARLIER, created_to=LATER, search="MRP"
        )
        assert isinstance(filters, MrpRunFilters)
        assert filters.search == "MRP"

    def test_an_open_window_is_accepted_by_every_model(self) -> None:
        assert StatementFilters(date_from=None, date_to=LATER).date_to == LATER
        assert StatementFilters(date_from=EARLIER, date_to=None).date_from == EARLIER
        assert MrpRunFilters(created_from=None, created_to=LATER).created_to == LATER
