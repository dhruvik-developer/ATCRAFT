"""Validation of the statutory identifiers and payload rules - no database.

The GSTIN check digit and the GSTIN/PAN/state-code cross-checks are the tests
that earn their keep here: a mistyped GSTIN passes a naive length check, reaches
a GST return, and is only rejected weeks later by the portal. The check digit is
verified against a real, published GSTIN so the algorithm itself is pinned, not
just the code path.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from src.modules.vendors.models import VendorContactRole, VendorType
from src.modules.vendors.schemas import (
    TRACKING_PLACEHOLDER,
    TransporterCreate,
    VendorContactCreate,
    VendorCreate,
    VendorUpdate,
    assert_gstin_pan_agree,
    assert_gstin_state_agrees,
    gstin_check_digit,
    pan_from_gstin,
    state_code_from_gstin,
    validate_gstin,
    validate_pan,
)

pytestmark = pytest.mark.unit

# A structurally valid GSTIN whose check digit ('V') is correct.
VALID_GSTIN = "27AAPFU0939F1ZV"
VALID_PAN = "AAPFU0939F"


def _vendor(**overrides: object) -> VendorCreate:
    payload: dict[str, object] = {
        "code": "vend 001",
        "name": "  Sharma  Metals  ",
        "vendor_types": [VendorType.RAW_MATERIAL_SUPPLIER],
    }
    payload.update(overrides)
    return VendorCreate(**payload)  # type: ignore[arg-type]


class TestGstinCheckDigit:
    def test_known_gstin_validates(self) -> None:
        assert validate_gstin(VALID_GSTIN) == VALID_GSTIN

    def test_check_digit_matches_the_published_value(self) -> None:
        assert gstin_check_digit(VALID_GSTIN[:14]) == "V"

    def test_single_character_typo_is_caught(self) -> None:
        # Structurally perfect, check digit wrong - exactly the failure a regex
        # cannot see.
        with pytest.raises(ValueError, match="check digit"):
            validate_gstin("27AAPFU0939F1ZX")

    def test_transposed_digits_are_caught(self) -> None:
        transposed = "27AAPFU9039F1ZV"
        assert transposed != VALID_GSTIN
        with pytest.raises(ValueError, match="check digit"):
            validate_gstin(transposed)

    def test_lowercase_and_spacing_are_normalised_before_validation(self) -> None:
        assert validate_gstin(" 27aapfu0939f1zv ") == VALID_GSTIN

    @pytest.mark.parametrize(
        "candidate",
        [
            "27AAPFU0939F1Z",  # too short
            "27AAPFU0939F1ZVX",  # too long
            "AAAAPFU0939F1ZV",  # state code not numeric
            "27AAPFU0939F1AV",  # 14th character is not the mandatory 'Z'
        ],
    )
    def test_structurally_invalid_values_are_rejected(self, candidate: str) -> None:
        with pytest.raises(ValueError, match="GSTIN"):
            validate_gstin(candidate)


class TestPan:
    def test_valid_pan_is_upper_cased(self) -> None:
        assert validate_pan("aapfu0939f") == VALID_PAN

    @pytest.mark.parametrize("candidate", ["AAPFU0939", "AAPF0939FX", "12345U0939F"])
    def test_malformed_pan_is_rejected(self, candidate: str) -> None:
        with pytest.raises(ValueError, match="PAN"):
            validate_pan(candidate)

    def test_pan_and_state_code_are_extracted_from_the_gstin(self) -> None:
        assert pan_from_gstin(VALID_GSTIN) == VALID_PAN
        assert state_code_from_gstin(VALID_GSTIN) == "27"


class TestStatutoryConsistency:
    def test_matching_gstin_and_pan_pass(self) -> None:
        assert_gstin_pan_agree(VALID_GSTIN, VALID_PAN)

    def test_contradictory_pan_is_refused(self) -> None:
        with pytest.raises(ValueError, match="does not match"):
            assert_gstin_pan_agree(VALID_GSTIN, "ZZZZZ1111Z")

    def test_contradictory_state_code_is_refused(self) -> None:
        # 27 is Maharashtra; claiming 24 would put every purchase on the wrong
        # side of the CGST/SGST versus IGST decision.
        with pytest.raises(ValueError, match="state code"):
            assert_gstin_state_agrees(VALID_GSTIN, "24")

    def test_absent_values_are_not_an_error(self) -> None:
        assert_gstin_pan_agree(None, VALID_PAN)
        assert_gstin_pan_agree(VALID_GSTIN, None)
        assert_gstin_state_agrees(None, "27")

    def test_vendor_payload_enforces_consistency(self) -> None:
        with pytest.raises(PydanticValidationError, match="does not match"):
            _vendor(gstin=VALID_GSTIN, pan="ZZZZZ1111Z")

    def test_consistent_vendor_payload_is_accepted(self) -> None:
        vendor = _vendor(gstin=VALID_GSTIN, pan=VALID_PAN, state_code="27")
        assert vendor.gstin == VALID_GSTIN
        assert vendor.state_code == "27"


class TestVendorPayload:
    def test_code_is_normalised(self) -> None:
        assert _vendor().code == "VEND 001"

    def test_name_whitespace_is_collapsed(self) -> None:
        assert _vendor().name == "Sharma Metals"

    def test_vendor_types_are_deduplicated_and_ordered(self) -> None:
        vendor = _vendor(
            vendor_types=[
                VendorType.JOB_WORK,
                VendorType.COMPONENT_SUPPLIER,
                VendorType.JOB_WORK,
            ]
        )
        assert vendor.vendor_types == [VendorType.COMPONENT_SUPPLIER, VendorType.JOB_WORK]

    def test_at_least_one_vendor_type_is_required(self) -> None:
        with pytest.raises(PydanticValidationError):
            _vendor(vendor_types=[])

    def test_blank_optional_strings_become_null(self) -> None:
        vendor = _vendor(city="   ", address_line1="")
        assert vendor.city is None
        assert vendor.address_line1 is None

    def test_negative_credit_limit_is_refused(self) -> None:
        with pytest.raises(PydanticValidationError):
            _vendor(credit_limit="-1")

    def test_malformed_ifsc_is_refused(self) -> None:
        with pytest.raises(PydanticValidationError, match="IFSC"):
            _vendor(bank_ifsc="HDFC1234567")

    def test_valid_ifsc_is_upper_cased(self) -> None:
        assert _vendor(bank_ifsc="hdfc0001234").bank_ifsc == "HDFC0001234"

    def test_update_rejects_unknown_fields(self) -> None:
        """extra="forbid" is what stops a client writing is_approved by hand."""
        with pytest.raises(PydanticValidationError):
            VendorUpdate(is_approved=True)  # type: ignore[call-arg]

    def test_update_distinguishes_unset_from_null(self) -> None:
        # The service applies exclude_unset, so an omitted key must not appear.
        assert VendorUpdate(name="Acme").model_dump(exclude_unset=True) == {"name": "Acme"}
        assert "credit_limit" in VendorUpdate(credit_limit=None).model_dump(exclude_unset=True)


class TestContactPayload:
    def test_contact_needs_a_way_to_be_reached(self) -> None:
        with pytest.raises(PydanticValidationError, match="e-mail, a phone or a mobile"):
            VendorContactCreate(name="Rajesh Kumar", contact_role=VendorContactRole.DISPATCH)

    def test_a_mobile_number_alone_is_enough(self) -> None:
        contact = VendorContactCreate(
            name="Rajesh Kumar", contact_role=VendorContactRole.DISPATCH, mobile="+91 98200 00000"
        )
        assert contact.mobile == "+91 98200 00000"


class TestTrackingTemplate:
    def test_template_must_carry_the_placeholder(self) -> None:
        with pytest.raises(PydanticValidationError, match="placeholder"):
            TransporterCreate(
                code="TRN-01",
                name="Blue Freight",
                transporter_type="TRANSPORT",
                tracking_url_template="https://carrier.example/track",
            )

    def test_template_must_be_https(self) -> None:
        with pytest.raises(PydanticValidationError, match="https"):
            TransporterCreate(
                code="TRN-01",
                name="Blue Freight",
                transporter_type="TRANSPORT",
                tracking_url_template=f"http://carrier.example/t?awb={TRACKING_PLACEHOLDER}",
            )

    def test_valid_template_is_accepted(self) -> None:
        transporter = TransporterCreate(
            code="trn 01",
            name="Blue Freight",
            transporter_type="COURIER",
            tracking_url_template=f"https://carrier.example/t?awb={TRACKING_PLACEHOLDER}",
        )
        assert transporter.code == "TRN 01"
        assert TRACKING_PLACEHOLDER in (transporter.tracking_url_template or "")

    def test_template_is_optional(self) -> None:
        transporter = TransporterCreate(
            code="TRN-02", name="Local Cartage", transporter_type="TRANSPORT"
        )
        assert transporter.tracking_url_template is None
