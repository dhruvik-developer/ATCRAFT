"""Financial-year period rules and the two organisation state machines.

The period rules are pure functions on dates, so every boundary that matters -
a one-day year, a year sharing a single day with its neighbour, a year that runs
backwards - can be asserted exactly without a database.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from src.core.errors import BusinessRuleError, InvalidStateTransitionError
from src.core.permissions import Permission
from src.modules.organization.models import Branch, FinancialYear
from src.modules.organization.service import (
    BRANCH_LIFECYCLE,
    FINANCIAL_YEAR_MACHINE,
    MAX_FINANCIAL_YEAR_DAYS,
    FinancialYearState,
    MasterLifecycleState,
    assert_period_valid,
    default_financial_year_code,
    financial_year_state,
    lifecycle_state,
    ranges_overlap,
)

pytestmark = pytest.mark.unit

FY_2026 = (date(2026, 4, 1), date(2027, 3, 31))
FY_2027 = (date(2027, 4, 1), date(2028, 3, 31))


class TestPeriodShape:
    def test_accepts_a_standard_indian_financial_year(self) -> None:
        assert_period_valid(*FY_2026)

    def test_rejects_an_end_date_before_the_start(self) -> None:
        with pytest.raises(BusinessRuleError, match="must start before it ends"):
            assert_period_valid(date(2027, 3, 31), date(2026, 4, 1))

    def test_rejects_a_zero_length_period(self) -> None:
        """Start equal to end is not a one-day year, it is a mistyped range.

        Allowing it would let two "years" both claim the same single date and
        still pass the overlap check that runs on active years only.
        """
        with pytest.raises(BusinessRuleError, match="must start before it ends"):
            assert_period_valid(date(2026, 4, 1), date(2026, 4, 1))

    def test_accepts_a_period_exactly_at_the_length_limit(self) -> None:
        start = date(2026, 4, 1)
        end = start + timedelta(days=MAX_FINANCIAL_YEAR_DAYS - 1)
        assert_period_valid(start, end)

    def test_rejects_a_period_one_day_past_the_length_limit(self) -> None:
        start = date(2026, 4, 1)
        end = start + timedelta(days=MAX_FINANCIAL_YEAR_DAYS)
        with pytest.raises(BusinessRuleError) as exc:
            assert_period_valid(start, end)
        assert exc.value.details["days"] == MAX_FINANCIAL_YEAR_DAYS + 1
        assert exc.value.details["max_days"] == MAX_FINANCIAL_YEAR_DAYS


class TestRangeOverlap:
    """Both endpoints are inclusive, which is the whole subtlety."""

    def test_consecutive_years_do_not_overlap(self) -> None:
        assert not ranges_overlap(*FY_2026, *FY_2027)

    def test_a_single_shared_day_is_an_overlap(self) -> None:
        # 31 March 2027 would belong to two years, so a posting on it has no
        # single answer.
        assert ranges_overlap(*FY_2026, date(2027, 3, 31), date(2028, 3, 31))

    def test_a_fully_contained_period_overlaps(self) -> None:
        assert ranges_overlap(*FY_2026, date(2026, 7, 1), date(2026, 9, 30))

    def test_a_period_that_swallows_another_overlaps(self) -> None:
        assert ranges_overlap(date(2026, 7, 1), date(2026, 9, 30), *FY_2026)

    def test_overlap_is_symmetric(self) -> None:
        first = (date(2026, 4, 1), date(2026, 12, 31))
        second = (date(2026, 10, 1), date(2027, 3, 31))
        assert ranges_overlap(*first, *second) == ranges_overlap(*second, *first)

    def test_a_year_before_another_does_not_overlap(self) -> None:
        assert not ranges_overlap(*FY_2027, *FY_2026)


class TestFinancialYearMachine:
    def test_the_graph_is_structurally_sound(self) -> None:
        FINANCIAL_YEAR_MACHINE.validate()

    def test_an_open_year_can_be_closed(self) -> None:
        transition = FINANCIAL_YEAR_MACHINE.assert_transition(
            FinancialYearState.OPEN, FinancialYearState.CLOSED
        )
        # Marked irreversible so the UI demands confirmation - it is one-way for
        # everyone except a platform administrator.
        assert transition.is_irreversible
        assert transition.permission is Permission.SETTINGS_MANAGE

    def test_closing_an_already_closed_year_is_rejected(self) -> None:
        with pytest.raises(InvalidStateTransitionError) as exc:
            FINANCIAL_YEAR_MACHINE.assert_transition(
                FinancialYearState.CLOSED, FinancialYearState.CLOSED
            )
        assert exc.value.details["current_status"] == "CLOSED"
        assert exc.value.details["allowed"] == ["OPEN"]

    def test_the_reopen_edge_carries_no_permission(self) -> None:
        """Reopening is superuser-only, which is not expressible as a permission.

        A permission would be grantable through the ordinary role editor, so the
        edge deliberately declares none and the service checks ``is_superuser``.
        """
        transition = FINANCIAL_YEAR_MACHINE.assert_transition(
            FinancialYearState.CLOSED, FinancialYearState.OPEN
        )
        assert transition.permission is None

    def test_available_actions_hide_close_from_a_user_without_the_permission(self) -> None:
        actions = FINANCIAL_YEAR_MACHINE.available_actions(
            FinancialYearState.OPEN, held=[Permission.SETTINGS_VIEW]
        )
        assert actions == []

    def test_state_is_derived_from_the_flag(self) -> None:
        year = FinancialYear(code="2026-27", start_date=FY_2026[0], end_date=FY_2026[1])
        year.is_closed = False
        assert financial_year_state(year) is FinancialYearState.OPEN
        year.is_closed = True
        assert financial_year_state(year) is FinancialYearState.CLOSED


class TestMasterLifecycle:
    def test_the_graph_is_structurally_sound(self) -> None:
        BRANCH_LIFECYCLE.validate()

    def test_archived_is_terminal(self) -> None:
        assert BRANCH_LIFECYCLE.is_terminal(MasterLifecycleState.ARCHIVED)
        assert BRANCH_LIFECYCLE.allowed_targets(MasterLifecycleState.ARCHIVED) == []

    def test_a_retired_record_cannot_be_retired_again(self) -> None:
        with pytest.raises(InvalidStateTransitionError):
            BRANCH_LIFECYCLE.assert_transition(
                MasterLifecycleState.ARCHIVED, MasterLifecycleState.ARCHIVED
            )

    def test_deactivate_then_reactivate_is_legal(self) -> None:
        assert BRANCH_LIFECYCLE.can_transition(
            MasterLifecycleState.ACTIVE, MasterLifecycleState.INACTIVE
        )
        assert BRANCH_LIFECYCLE.can_transition(
            MasterLifecycleState.INACTIVE, MasterLifecycleState.ACTIVE
        )

    def test_lifecycle_state_reads_deleted_at_before_is_active(self) -> None:
        """A retired row reports ARCHIVED even if ``is_active`` was left true.

        The two flags can disagree after a direct SQL edit; ``deleted_at`` is the
        one that decides whether the record is still in play.
        """
        branch = Branch(code="HO", name="Head Office")
        branch.is_active = True
        branch.deleted_at = None
        assert lifecycle_state(branch) is MasterLifecycleState.ACTIVE

        branch.is_active = False
        assert lifecycle_state(branch) is MasterLifecycleState.INACTIVE

        branch.is_active = True
        branch.deleted_at = datetime(2026, 4, 1, tzinfo=UTC)
        assert lifecycle_state(branch) is MasterLifecycleState.ARCHIVED


class TestDefaultCode:
    def test_matches_the_document_numbering_period_key(self) -> None:
        """The FY code and the numbering period key must be the same string.

        ``FinancialYearRepository.document_numbers_issued`` joins them on that
        equality; if they drift, a year that has issued documents becomes
        deletable.
        """
        from src.common.codes import financial_year_key

        assert default_financial_year_code(date(2026, 6, 1)) == financial_year_key(date(2026, 6, 1))

    def test_a_date_before_april_belongs_to_the_previous_year(self) -> None:
        assert default_financial_year_code(date(2027, 3, 31)) == "2026-27"
        assert default_financial_year_code(date(2027, 4, 1)) == "2027-28"

    def test_a_january_start_month_shifts_the_label(self) -> None:
        assert default_financial_year_code(date(2027, 3, 31), start_month=1) == "2027-28"
