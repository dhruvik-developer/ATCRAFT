"""Currency boundaries for the company-denominated customer credit balance."""

from src.core.errors import BusinessRuleError


def assert_customer_currency(currency: str, company_currency: str) -> None:
    """Do not apply nominal foreign amounts to a company-currency credit cache."""
    if currency.upper() != company_currency.upper():
        raise BusinessRuleError(
            "Customer orders and receipts must use the company currency until "
            "currency conversion for customer credit is configured.",
            details={"currency": currency, "company_currency": company_currency},
        )
