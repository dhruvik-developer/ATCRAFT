"""Customer credit balances never compare or apply unlike denominations."""

import pytest

from src.core.currency import assert_customer_currency
from src.core.errors import BusinessRuleError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("currency, home", [("INR", "INR"), ("USD", "USD"), ("usd", "USD")])
def test_same_denomination_is_supported(currency, home):
    assert_customer_currency(currency, home)


@pytest.mark.parametrize("currency, home", [("USD", "INR"), ("INR", "USD"), ("EUR", "USD")])
def test_foreign_nominal_amount_is_refused_with_actionable_denominations(currency, home):
    with pytest.raises(BusinessRuleError) as error:
        assert_customer_currency(currency, home)
    assert error.value.details == {"currency": currency, "company_currency": home}
