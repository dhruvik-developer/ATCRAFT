"""The two pure rules cost centres add to the ledger.

* **Only income and expense legs may carry a cost centre.** A payable, a bank
  balance or a tax credit was not spent or earned anywhere, and a centre on one
  would put balance-sheet movement on a cost-centre statement.
* **An invoice's purchase expense is split per cost centre, exactly.** One leg
  per centre, in the order the lines name them, summing to the subtotal to the
  paisa - the journal has to balance against a total the invoice's own CHECK
  already fixed.

Both are module-level functions in ``finance.service`` so they are tested here
without a database, as :func:`assert_balanced` is.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from src.core.errors import ValidationError
from src.modules.finance.models import COST_CENTRE_ACCOUNT_TYPES, LedgerAccountType
from src.modules.finance.repository import PostingAccount
from src.modules.finance.service import (
    JournalLine,
    assert_balanced,
    assert_cost_centre_legs,
    split_by_cost_centre,
)

pytestmark = pytest.mark.unit

D = Decimal


def _account(account_type: LedgerAccountType, code: str = "9999") -> PostingAccount:
    return PostingAccount(id=uuid.uuid4(), code=code, account_type=account_type)


# -----------------------------------------------------------------------------
# Which legs may carry a cost centre
# -----------------------------------------------------------------------------


class TestCostCentreLegs:
    def test_the_permitted_classes_are_exactly_income_and_expense(self) -> None:
        assert (
            frozenset({LedgerAccountType.INCOME, LedgerAccountType.EXPENSE})
            == COST_CENTRE_ACCOUNT_TYPES
        )

    @pytest.mark.parametrize("account_type", [LedgerAccountType.EXPENSE, LedgerAccountType.INCOME])
    def test_income_and_expense_legs_may_carry_one(self, account_type: LedgerAccountType) -> None:
        account = _account(account_type)
        lines = [JournalLine(account_id=account.id, debit=D("10"), cost_centre_id=uuid.uuid4())]
        assert_cost_centre_legs(lines, {account.id: account})

    @pytest.mark.parametrize(
        "account_type",
        [LedgerAccountType.ASSET, LedgerAccountType.LIABILITY, LedgerAccountType.EQUITY],
    )
    def test_a_balance_sheet_leg_may_not_and_the_leg_is_named(
        self, account_type: LedgerAccountType
    ) -> None:
        expense = _account(LedgerAccountType.EXPENSE, "5100")
        other = _account(account_type, "2100")
        lines = [
            JournalLine(account_id=expense.id, debit=D("10"), cost_centre_id=uuid.uuid4()),
            JournalLine(account_id=other.id, credit=D("10"), cost_centre_id=uuid.uuid4()),
        ]
        with pytest.raises(ValidationError) as raised:
            assert_cost_centre_legs(lines, {expense.id: expense, other.id: other})

        assert "Leg 2" in raised.value.message
        assert "2100" in raised.value.message
        assert raised.value.details["line_number"] == 2
        assert raised.value.details["account_type"] == str(account_type)

    def test_a_leg_without_a_cost_centre_is_never_judged(self) -> None:
        payable = _account(LedgerAccountType.LIABILITY)
        assert_cost_centre_legs(
            [JournalLine(account_id=payable.id, credit=D("10"))], {payable.id: payable}
        )

    def test_an_unknown_account_is_left_to_the_not_found_check(self) -> None:
        """``post_journal`` raises NotFoundError for it first; this rule must not
        mask that with a misleading class error."""
        assert_cost_centre_legs(
            [JournalLine(account_id=uuid.uuid4(), debit=D("1"), cost_centre_id=uuid.uuid4())], {}
        )

    def test_a_journal_line_carries_none_unless_told(self) -> None:
        assert JournalLine(account_id=uuid.uuid4(), debit=D("1")).cost_centre_id is None


# -----------------------------------------------------------------------------
# Splitting the purchase expense per cost centre
# -----------------------------------------------------------------------------


class TestSplitByCostCentre:
    def test_an_invoice_with_no_centres_posts_the_one_leg_it_always_did(self) -> None:
        split = split_by_cost_centre([(None, D("100.00")), (None, D("50.00"))], total=D("150.00"))
        assert split == [(None, D("150.00"))]

    def test_one_leg_per_centre_in_the_order_the_lines_first_name_them(self) -> None:
        die, maint = uuid.uuid4(), uuid.uuid4()
        split = split_by_cost_centre(
            [(maint, D("18400.00")), (die, D("12500.00")), (maint, D("100.00")), (None, D("5"))],
            total=D("31005.00"),
        )
        assert split == [(maint, D("18500.00")), (die, D("12500.00")), (None, D("5.00"))]

    def test_the_legs_sum_to_the_subtotal_exactly(self) -> None:
        centres = [uuid.uuid4() for _ in range(3)]
        amounts = [
            (centres[0], D("33.33")),
            (centres[1], D("33.33")),
            (centres[2], D("33.34")),
            (centres[0], D("0.01")),
        ]
        total = sum((amount for _, amount in amounts), start=D("0"))
        split = split_by_cost_centre(amounts, total=total)
        assert sum((amount for _, amount in split), start=D("0")) == total

    def test_a_header_that_disagrees_with_its_lines_is_absorbed_by_the_largest_leg(self) -> None:
        """Never the case for an invoice built by the service; if it ever is, the
        posting still balances rather than refusing an approval over a paisa."""
        small, large = uuid.uuid4(), uuid.uuid4()
        split = split_by_cost_centre([(small, D("10.00")), (large, D("90.00"))], total=D("100.01"))
        assert split == [(small, D("10.00")), (large, D("90.01"))]

    def test_a_tie_goes_to_the_first_leg(self) -> None:
        first, second = uuid.uuid4(), uuid.uuid4()
        split = split_by_cost_centre([(first, D("50")), (second, D("50"))], total=D("99.99"))
        assert split == [(first, D("49.99")), (second, D("50.00"))]

    def test_a_centre_whose_lines_come_to_nothing_posts_no_leg(self) -> None:
        """A free-of-charge line is billed at zero; a ledger leg must carry money."""
        free, paid = uuid.uuid4(), uuid.uuid4()
        split = split_by_cost_centre([(free, D("0")), (paid, D("80.00"))], total=D("80.00"))
        assert split == [(paid, D("80.00"))]

    def test_amounts_are_money_rounded(self) -> None:
        centre = uuid.uuid4()
        assert split_by_cost_centre([(centre, D("10.004"))], total=D("10.00")) == [
            (centre, D("10.00"))
        ]

    def test_the_split_balances_against_the_rest_of_an_invoice_journal(self) -> None:
        """The whole point: the per-centre legs plus tax equal the payable."""
        expense, tax, payable = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        die, maint = uuid.uuid4(), uuid.uuid4()
        split = split_by_cost_centre(
            [(die, D("25000.00")), (maint, D("18400.00"))], total=D("43400")
        )
        lines = [
            JournalLine(account_id=expense, debit=amount, cost_centre_id=centre)
            for centre, amount in split
        ]
        lines.append(JournalLine(account_id=tax, debit=D("7812.00")))
        lines.append(JournalLine(account_id=payable, credit=D("51212.00")))
        totals = assert_balanced(lines)
        assert totals.is_balanced
