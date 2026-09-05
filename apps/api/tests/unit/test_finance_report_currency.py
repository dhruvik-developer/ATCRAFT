"""How a finance report picks the denomination it answers in.

Orders, invoices and ledger entries each carry their own ``currency`` and this
schema has no exchange-rate table to convert between them - multi-currency is
still an open question for the business (``docs/assumptions.md``, open question
3), not a settled requirement. So no figure a report produces is ever summed
across two denominations.

Both halves of that are tested here, because getting the first right is what
makes it easy to get the second wrong:

* :func:`normalise_currency` stops a caller's *spelling* from selecting a
  different set of rows. ``inr`` matches no row of a column that stores ``INR``,
  so a company with a full payables book would be reported as owing nothing.
* :func:`primary_report_currency` stops the *default* from doing the same thing
  by a different route. Drawing every report in ``companies.currency`` is
  narrow, honest and - for a company whose invoices are all in dollars -
  completely empty.

A report that is confidently empty is worse than one that refuses.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.constants import CURRENCY_CODE_LENGTH, DEFAULT_CURRENCY
from src.core.errors import ValidationError
from src.modules.finance.repository import AGEING_TOTAL_COLUMNS, AgeingCurrencyTotal
from src.modules.finance.service import normalise_currency, primary_report_currency

pytestmark = pytest.mark.unit


class TestNormaliseCurrency:
    def test_no_currency_asked_for_stays_unasked(self) -> None:
        # None means "the caller did not choose", which the service resolves to
        # the company's own currency. It must not be turned into a code here.
        assert normalise_currency(None) is None

    @pytest.mark.parametrize("raw", ["inr", "InR", " inr ", "INR"])
    def test_a_currency_is_a_code_not_a_spelling(self, raw: str) -> None:
        assert normalise_currency(raw) == "INR"

    @pytest.mark.parametrize("raw", ["", "IN", "INRR", "rupees", "IN1", "12 ", "₹"])
    def test_anything_that_is_not_an_iso_code_is_refused(self, raw: str) -> None:
        # Refused rather than passed through: a code that matches no row makes a
        # company with debts look debt-free.
        with pytest.raises(ValidationError):
            normalise_currency(raw)

    def test_the_default_currency_is_itself_a_valid_code(self) -> None:
        # The fallback the reporting-currency lookup uses has to survive its own
        # guard, or a company with no readable company row would 400.
        assert normalise_currency(DEFAULT_CURRENCY) == DEFAULT_CURRENCY
        assert len(DEFAULT_CURRENCY) == CURRENCY_CODE_LENGTH


class TestPrimaryReportCurrency:
    """Which denomination an ageing report restates at its top level.

    The rule this pins down replaced one that was simpler and wrong: "always the
    company's own currency". That rule keeps every total honest - no sum ever
    spans two denominations - and then hands a company billed entirely in
    dollars a report saying it owes nothing, which is a worse lie than a
    mislabelled total. It is reachable with nothing more exotic than a company
    whose ``currency`` column says INR and an invoice that says USD, and no
    write path in the backend derives one from the other.

    Its replacement then had to be fixed too, and that is the second half of
    this class. "The denomination with the largest outstanding" ranks 118,000
    USD against 400,000 INR - the exact comparison the rest of the module
    refuses to make, because the schema holds no exchange rate with which to
    make it. The tie-break is a *count*: counts mean the same thing in every
    currency.
    """

    @staticmethod
    def _block(
        currency: str, *, outstanding: str = "0", parties: int = 1, documents: int = 1
    ) -> AgeingCurrencyTotal:
        return AgeingCurrencyTotal(
            currency=currency,
            party_count=parties,
            document_count=documents,
            totals={name: Decimal(outstanding) for name in AGEING_TOTAL_COLUMNS},
        )

    @classmethod
    def _present(cls, *pairs: tuple[str, str]) -> list[AgeingCurrencyTotal]:
        """Denominations with open items, one party and one document each."""
        return [cls._block(code, outstanding=amount) for code, amount in pairs]

    def test_an_explicit_choice_wins_outright(self) -> None:
        present = self._present(("INR", "50000"))
        assert primary_report_currency("usd", "INR", present) == "USD"

    def test_an_explicit_choice_survives_having_nothing_open_in_it(self) -> None:
        # "We owe nothing in euros" is a real answer, and the report still lists
        # the denominations that do have debt beside it.
        assert primary_report_currency("EUR", "INR", self._present(("INR", "50000"))) == "EUR"

    def test_the_companys_own_currency_wins_when_something_is_open_in_it(self) -> None:
        # Even though the dollar figure is the larger *number*. There is no rate
        # with which to call it the larger debt, so size does not decide here.
        present = self._present(("USD", "118000"), ("INR", "59000"))
        assert primary_report_currency(None, "INR", present) == "INR"

    def test_the_companys_own_currency_wins_on_one_document_against_many(self) -> None:
        # The count rule decides between *foreign* denominations only. A single
        # rupee invoice still outranks a hundred dollar ones, because the books
        # are kept in rupees and that is a stronger reason than either count.
        present = [
            self._block("USD", outstanding="118000", parties=40, documents=100),
            self._block("INR", outstanding="900", parties=1, documents=1),
        ]
        assert primary_report_currency(None, "INR", present) == "INR"

    def test_a_book_kept_entirely_elsewhere_is_never_reported_as_empty(self) -> None:
        # THE original regression. Returning "INR" here is a report that tells a
        # company owing 118,000 USD that it owes nothing.
        present = self._present(("USD", "118000"))
        assert primary_report_currency(None, "INR", present) == "USD"

    def test_the_larger_unconverted_number_does_not_win(self) -> None:
        """THE regression this rule was rewritten for.

        Three open bills in dirhams, one in dollars, nothing in the company's own
        rupees. 118,000 is the bigger number and 4,000 is the bigger book; which
        is the bigger *debt* is a question this schema cannot answer, so the
        report must not answer it. It leads with the denomination the company
        actually transacts in - the one with the most open documents.
        """
        present = [
            self._block("USD", outstanding="118000", parties=1, documents=1),
            self._block("AED", outstanding="4000", parties=3, documents=3),
        ]

        assert primary_report_currency(None, "INR", present) == "AED"
        # And the amounts are genuinely not consulted: multiply the dollar
        # figure by a thousand and the answer does not move.
        louder = [
            self._block("USD", outstanding="118000000", parties=1, documents=1),
            self._block("AED", outstanding="4000", parties=3, documents=3),
        ]
        assert primary_report_currency(None, "INR", louder) == "AED"

    def test_parties_break_a_tie_in_documents(self) -> None:
        present = [
            self._block("USD", outstanding="118000", parties=1, documents=4),
            self._block("AED", outstanding="4000", parties=3, documents=4),
        ]
        assert primary_report_currency(None, "INR", present) == "AED"

    def test_an_exact_tie_is_broken_by_the_code_not_by_the_amount(self) -> None:
        """Two denominations, one document each: the answer must still be fixed.

        A report that renames its own headline between two runs over unchanged
        data is a report nobody trusts, and "whichever the plan returned first"
        is what an unstable rule degrades to.
        """
        louder_first = [
            self._block("USD", outstanding="118000"),
            self._block("AED", outstanding="4000"),
        ]
        quieter_first = [
            self._block("AED", outstanding="4000"),
            self._block("USD", outstanding="118000"),
        ]

        assert primary_report_currency(None, "INR", louder_first) == "AED"
        assert primary_report_currency(None, "INR", quieter_first) == "AED"

    def test_the_argument_order_does_not_decide(self) -> None:
        """The rule is stated in the function, not inherited from an ORDER BY.

        The repository does order its blocks by the same key, but a caller that
        reordered them - or a future one that sorts for display first - must not
        change which denomination the report is drawn in.
        """
        blocks = [
            self._block("USD", outstanding="118000", parties=1, documents=1),
            self._block("AED", outstanding="4000", parties=3, documents=3),
        ]

        assert primary_report_currency(None, "INR", blocks) == "AED"
        assert primary_report_currency(None, "INR", list(reversed(blocks))) == "AED"

    def test_nothing_outstanding_at_all_reports_in_the_companys_own_currency(self) -> None:
        # A genuine zero, drawn in the denomination the books are kept in.
        assert primary_report_currency(None, "INR", []) == "INR"

    def test_the_spelling_of_an_explicit_choice_is_still_normalised(self) -> None:
        with pytest.raises(ValidationError):
            primary_report_currency("rupees", "INR", self._present(("INR", "1")))
