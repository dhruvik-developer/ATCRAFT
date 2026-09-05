"""One GSTIN validator for every master that records one.

Three hand-copied validators had drifted: the customer master refused entity
number ``0`` (correctly) while the vendor and organisation masters accepted it,
and only the vendor master checked the check digit - so a mistyped GSTIN was
refused on a vendor, stored on a customer and printed on the company's own
invoices. These tests pin the shared behaviour *through each schema*, not just
through the helper, so a module that quietly grows a local copy again fails
here rather than at a GST filing.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from src.common import tax_ids
from src.common.tax_ids import (
    GSTIN_PATTERN,
    gstin_check_digit,
    validate_gstin,
    validate_optional_gstin,
    validate_optional_pan,
    validate_pan,
)
from src.modules.customers import schemas as customers
from src.modules.customers.schemas import CustomerCreate
from src.modules.organization import schemas as organization
from src.modules.organization.schemas import BranchCreate
from src.modules.vendors import schemas as vendors

pytestmark = pytest.mark.unit

VALID = "27AAPFU0939F1ZV"
WRONG_CHECK_DIGIT = "27AAPFU0939F1ZX"
ENTITY_ZERO = "27AAPFU0939F0Z" + gstin_check_digit("27AAPFU0939F0Z")


class TestTheHelper:
    def test_a_valid_gstin_is_normalised(self) -> None:
        assert validate_gstin(" 27aapfu0939f1zv ") == VALID

    def test_the_check_digit_is_verified(self) -> None:
        with pytest.raises(ValueError, match="check digit"):
            validate_gstin(WRONG_CHECK_DIGIT)

    def test_entity_number_zero_is_not_a_gstin(self) -> None:
        """Even with a correct check digit: the entity number starts at 1."""
        with pytest.raises(ValueError, match="15 characters"):
            validate_gstin(ENTITY_ZERO)

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_blank_optional_collapses_to_null(self, value: str | None) -> None:
        assert validate_optional_gstin(value) is None
        assert validate_optional_pan(value) is None

    def test_optional_still_validates_what_is_supplied(self) -> None:
        assert validate_optional_gstin(" 27aapfu0939f1zv ") == VALID
        with pytest.raises(ValueError):
            validate_optional_gstin(WRONG_CHECK_DIGIT)

    def test_pan_is_shape_checked_and_upper_cased(self) -> None:
        assert validate_pan("aapfu0939f") == "AAPFU0939F"
        with pytest.raises(ValueError):
            validate_pan("AAPFU09390")


class TestEveryMasterAppliesTheSameRule:
    """The schemas bind the shared callables, not private re-implementations."""

    def test_the_modules_share_one_implementation(self) -> None:
        assert customers.validate_optional_gstin is tax_ids.validate_optional_gstin
        assert customers.validate_optional_pan is tax_ids.validate_optional_pan
        assert organization.validate_optional_gstin is tax_ids.validate_optional_gstin
        assert organization.validate_optional_pan is tax_ids.validate_optional_pan
        assert vendors.validate_gstin is tax_ids.validate_gstin
        assert vendors.validate_pan is tax_ids.validate_pan

    def test_no_module_keeps_a_private_pattern(self) -> None:
        for module in (customers, organization, vendors):
            assert not hasattr(module, "GSTIN_PATTERN"), module.__name__
            assert not hasattr(module, "PAN_PATTERN"), module.__name__
        assert "[1-9A-Z]Z" in GSTIN_PATTERN

    @pytest.mark.parametrize("gstin", [WRONG_CHECK_DIGIT, ENTITY_ZERO])
    def test_a_customer_refuses_what_a_vendor_refuses(self, gstin: str) -> None:
        with pytest.raises(PydanticValidationError):
            CustomerCreate(code="AC-001", name="Acme Hardware", gstin=gstin)
        with pytest.raises(ValueError):
            vendors.validate_gstin(gstin)

    @pytest.mark.parametrize("gstin", [WRONG_CHECK_DIGIT, ENTITY_ZERO])
    def test_a_branch_refuses_what_a_vendor_refuses(self, gstin: str) -> None:
        with pytest.raises(PydanticValidationError):
            BranchCreate(code="HO", name="Head Office", gstin=gstin)

    def test_a_valid_gstin_passes_everywhere(self) -> None:
        assert CustomerCreate(code="AC-001", name="Acme Hardware", gstin=VALID).gstin == VALID
        assert BranchCreate(code="HO", name="Head Office", gstin=VALID).gstin == VALID
        assert vendors.validate_gstin(VALID) == VALID
