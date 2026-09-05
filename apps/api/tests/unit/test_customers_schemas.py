"""Edge validation for the customer master payloads.

These rules exist so a malformed statutory identifier is rejected with a
field-level message at the API boundary, rather than reaching invoicing - where
the same mistake surfaces as a rejected e-invoice with an opaque portal error.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError as PydanticValidationError

from src.modules.customers.models import AddressType, CustomerType, PriceType
from src.modules.customers.schemas import (
    AddressCreate,
    CustomerCreate,
    CustomerUpdate,
    OutstandingAdjustment,
)

pytestmark = pytest.mark.unit


def _customer(**overrides: object) -> CustomerCreate:
    data: dict[str, object] = {"code": "ac-001", "name": "  Acme   Traders "}
    data.update(overrides)
    return CustomerCreate(**data)  # type: ignore[arg-type]


class TestCustomerCreate:
    def test_code_is_normalised_and_name_collapsed(self) -> None:
        payload = _customer()
        assert payload.code == "AC-001"
        assert payload.name == "Acme Traders"

    def test_defaults_are_the_conservative_ones(self) -> None:
        payload = _customer()
        assert payload.customer_type is CustomerType.CUSTOMER
        assert payload.price_list_type is PriceType.SALE
        # No limit agreed - not a limit of zero, and not unlimited by accident.
        assert payload.credit_limit is None
        assert payload.credit_days == 0

    @pytest.mark.parametrize("code", ["", "   ", "-BAD", "AC*001"])
    def test_unusable_codes_are_refused(self, code: str) -> None:
        with pytest.raises(PydanticValidationError):
            _customer(code=code)

    def test_gstin_is_upper_cased_and_format_checked(self) -> None:
        payload = _customer(gstin=" 27abcde1234f1z0 ")
        assert payload.gstin == "27ABCDE1234F1Z0"

    @pytest.mark.parametrize(
        "gstin",
        [
            "27ABCDE1234F1Z",  # 14 characters
            "AB",  # nonsense
            "27ABCDE1234F1X5",  # the 14th character must be Z
            "27ABCDE1234F1Z5",  # right shape, wrong check digit - a typo
            "27ABCDE1234F0Z0",  # entity number 0 is never issued
        ],
    )
    def test_malformed_gstin_is_refused(self, gstin: str) -> None:
        with pytest.raises(PydanticValidationError):
            _customer(gstin=gstin)

    def test_blank_optional_identifiers_become_null(self) -> None:
        # "" and NULL must not both mean "not supplied", or a report filtering
        # on gstin IS NULL misses half the unregistered customers.
        payload = _customer(gstin="   ", pan="  ", legal_name="  ")
        assert payload.gstin is None
        assert payload.pan is None
        assert payload.legal_name is None

    def test_pan_format_is_enforced(self) -> None:
        assert _customer(pan="abcde1234f").pan == "ABCDE1234F"
        with pytest.raises(PydanticValidationError):
            _customer(pan="ABCDE12345")

    def test_negative_credit_limit_is_refused(self) -> None:
        with pytest.raises(PydanticValidationError):
            _customer(credit_limit=Decimal("-1"))

    def test_zero_credit_limit_is_accepted(self) -> None:
        """Zero is a meaningful value in this module - strictly no credit - so
        it must survive validation rather than being coerced to None."""
        assert _customer(credit_limit=Decimal("0")).credit_limit == Decimal("0")


class TestCustomerUpdate:
    def test_unset_fields_are_distinguishable_from_nulls(self) -> None:
        payload = CustomerUpdate(name="Renamed")
        assert payload.model_dump(exclude_unset=True) == {"name": "Renamed"}

    def test_clear_flags_express_an_intentional_null(self) -> None:
        payload = CustomerUpdate(clear_credit_limit=True)
        changes = payload.model_dump(exclude_unset=True)
        assert changes == {"clear_credit_limit": True}

    def test_unknown_fields_are_rejected(self) -> None:
        # extra="forbid": a typo in a client payload must fail loudly rather
        # than silently not applying the change the caller intended.
        with pytest.raises(PydanticValidationError):
            CustomerUpdate(outstanding_amount=Decimal("100"))  # type: ignore[call-arg]

    def test_outstanding_balance_cannot_be_edited_through_the_master_payload(self) -> None:
        assert "outstanding_amount" not in CustomerUpdate.model_fields
        assert "is_blocked" not in CustomerUpdate.model_fields
        assert "code" not in CustomerUpdate.model_fields


class TestAddressAndAdjustment:
    def test_state_code_must_be_two_digits(self) -> None:
        with pytest.raises(PydanticValidationError):
            AddressCreate(
                address_type=AddressType.BILLING,
                address_line1="1 Industrial Estate",
                city="Pune",
                state_code="MH",
            )

    def test_valid_address_normalises_its_identifiers(self) -> None:
        address = AddressCreate(
            address_type=AddressType.SHIPPING,
            address_line1="1 Industrial Estate",
            city="Pune",
            state_code=" 27 ",
            gstin="27abcde1234f1z0",
            label="   ",
        )
        assert address.state_code == "27"
        assert address.gstin == "27ABCDE1234F1Z0"
        assert address.label is None

    def test_zero_outstanding_adjustment_is_refused(self) -> None:
        # It would write an audit row describing no change, in the one log a
        # reconciliation depends on.
        with pytest.raises(PydanticValidationError):
            OutstandingAdjustment(
                delta=Decimal("0"),
                reason="Nothing happened",
                reference_type="payment",
                reference_code="RCPT/2026-27/00012",
            )

    def test_negative_adjustment_is_allowed(self) -> None:
        """A receipt or credit note reduces the balance; the field is signed."""
        adjustment = OutstandingAdjustment(
            delta=Decimal("-500.25"),
            reason="Receipt 12",
            reference_type="payment",
            reference_code="RCPT/2026-27/00012",
        )
        assert adjustment.delta == Decimal("-500.25")
        assert adjustment.reference_id is None

    def test_adjustment_without_its_ledger_document_is_refused(self) -> None:
        """The cached balance only moves alongside the document that explains it.

        A delta with no reference could never be reconciled against the
        receivable ledger, so the schema refuses it before a service can apply
        it - the reference is how a reconciliation finds the journal.
        """
        with pytest.raises(PydanticValidationError) as exc:
            OutstandingAdjustment(delta=Decimal("100"), reason="Invoice")
        missing = {error["loc"][0] for error in exc.value.errors()}
        assert missing == {"reference_type", "reference_code"}

        with pytest.raises(PydanticValidationError):
            OutstandingAdjustment(
                delta=Decimal("100"), reason="Invoice", reference_type="", reference_code=""
            )
