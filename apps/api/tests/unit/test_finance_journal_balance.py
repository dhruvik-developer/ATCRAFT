"""Balanced-journal enforcement - the rule that makes the ledger a ledger.

``assert_balanced`` is the only gate between a caller and ``ledger_entries``,
and there is no other write path, so everything it refuses is refused
everywhere. It is pure, so these tests exercise the real production function
rather than a re-implementation of it.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from src.core.errors import ValidationError
from src.modules.finance.models import MAX_JOURNAL_LINES, PartyType
from src.modules.finance.service import (
    JournalLine,
    UnbalancedJournalError,
    assert_balanced,
)

pytestmark = pytest.mark.unit


def _account() -> uuid.UUID:
    return uuid.uuid4()


def _debit(amount: str, **kw: object) -> JournalLine:
    return JournalLine(account_id=_account(), debit=Decimal(amount), **kw)  # type: ignore[arg-type]


def _credit(amount: str, **kw: object) -> JournalLine:
    return JournalLine(account_id=_account(), credit=Decimal(amount), **kw)  # type: ignore[arg-type]


class TestBalance:
    def test_a_matched_pair_balances(self) -> None:
        totals = assert_balanced([_debit("1000.00"), _credit("1000.00")])

        assert totals.is_balanced
        assert totals.debit == Decimal("1000.00")
        assert totals.credit == Decimal("1000.00")
        assert totals.line_count == 2

    def test_many_debits_against_one_credit_balance(self) -> None:
        # The shape every vendor-invoice posting takes: expense + tax against
        # one payable.
        totals = assert_balanced(
            [_debit("1000.00"), _debit("180.00"), _debit("0.40"), _credit("1180.40")]
        )

        assert totals.debit == Decimal("1180.40")
        assert totals.difference == Decimal("0")

    def test_a_one_paisa_difference_is_refused(self) -> None:
        # The whole point: a posting that is out by the smallest representable
        # amount is still an unbalanced posting.
        with pytest.raises(UnbalancedJournalError) as caught:
            assert_balanced([_debit("1000.00"), _credit("999.99")])

        assert caught.value.details["difference"] == "0.01"
        assert caught.value.details["total_debit"] == "1000.00"
        assert caught.value.details["total_credit"] == "999.99"

    def test_a_large_difference_is_refused(self) -> None:
        with pytest.raises(UnbalancedJournalError):
            assert_balanced([_debit("1000.00"), _credit("100.00")])

    def test_rounding_happens_per_leg_not_on_the_totals(self) -> None:
        # Two legs of 0.005 each round to 0.01 each, so the pair is 0.02 against
        # a single 0.02 credit. Summing raw and rounding afterwards would give
        # 0.01 and reject a posting that is correct at money scale.
        totals = assert_balanced(
            [
                JournalLine(account_id=_account(), debit=Decimal("0.005")),
                JournalLine(account_id=_account(), debit=Decimal("0.005")),
                JournalLine(account_id=_account(), credit=Decimal("0.02")),
            ]
        )

        assert totals.debit == Decimal("0.02")

    def test_a_leg_that_rounds_away_to_nothing_is_refused(self) -> None:
        # 0.004 is below the money quantum, so at money scale this leg posts
        # nothing. Refusing it is the point: silently dropping it would leave a
        # journal that balances only because a leg disappeared.
        with pytest.raises(ValidationError) as caught:
            assert_balanced(
                [
                    JournalLine(account_id=_account(), debit=Decimal("0.004")),
                    JournalLine(account_id=_account(), credit=Decimal("0.01")),
                ]
            )

        assert caught.value.details["line_number"] == 1


class TestLegShape:
    def test_a_leg_that_is_both_a_debit_and_a_credit_is_refused(self) -> None:
        with pytest.raises(ValidationError) as caught:
            assert_balanced(
                [
                    JournalLine(
                        account_id=_account(), debit=Decimal("100"), credit=Decimal("100")
                    ),
                    _credit("100"),
                ]
            )

        assert caught.value.details["line_number"] == 1

    def test_a_leg_with_neither_side_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            assert_balanced([JournalLine(account_id=_account()), _credit("100")])

    def test_a_negative_leg_is_refused(self) -> None:
        # A reduction is posted on the opposite side, never as a negative -
        # otherwise "total debits" stops meaning anything.
        with pytest.raises(ValidationError) as caught:
            assert_balanced([_debit("-100.00"), _credit("-100.00")])

        assert "negative" in str(caught.value).lower()

    def test_a_single_leg_can_never_balance_and_is_refused_early(self) -> None:
        with pytest.raises(ValidationError) as caught:
            assert_balanced([_debit("100.00")])

        assert caught.value.details["line_count"] == 1

    def test_an_empty_journal_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            assert_balanced([])

    def test_a_journal_beyond_the_leg_ceiling_is_refused(self) -> None:
        lines = [_debit("1.00") for _ in range(MAX_JOURNAL_LINES)]
        lines.append(_credit(str(MAX_JOURNAL_LINES)))

        with pytest.raises(ValidationError) as caught:
            assert_balanced(lines)

        assert caught.value.details["maximum"] == MAX_JOURNAL_LINES


class TestPartyLegs:
    def test_a_party_leg_must_name_its_party(self) -> None:
        with pytest.raises(ValidationError) as caught:
            assert_balanced(
                [
                    JournalLine(
                        account_id=_account(),
                        debit=Decimal("100"),
                        party_type=PartyType.VENDOR,
                    ),
                    _credit("100"),
                ]
            )

        assert caught.value.details["line_number"] == 1

    def test_a_leg_with_no_party_type_must_not_name_a_party(self) -> None:
        with pytest.raises(ValidationError):
            assert_balanced(
                [
                    JournalLine(
                        account_id=_account(), debit=Decimal("100"), party_id=uuid.uuid4()
                    ),
                    _credit("100"),
                ]
            )

    def test_a_correctly_formed_party_leg_is_accepted(self) -> None:
        totals = assert_balanced(
            [
                _debit("500.00"),
                JournalLine(
                    account_id=_account(),
                    credit=Decimal("500.00"),
                    party_type=PartyType.VENDOR,
                    party_id=uuid.uuid4(),
                ),
            ]
        )

        assert totals.is_balanced
