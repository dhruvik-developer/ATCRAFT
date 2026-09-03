"""Validation and normalisation on the organisation payloads.

Two properties are load-bearing and are asserted rather than assumed:

* a code is canonicalised before it ever reaches a uniqueness check, so
  ``ho``, ``HO`` and ``H  O`` cannot become three branches;
* an update schema distinguishes "not supplied" from "set to null", which is
  what stops a PATCH of one field from wiping the rest of the record.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from src.modules.organization.schemas import (
    BranchCreate,
    BranchUpdate,
    CompanyUpdate,
    DepartmentCreate,
    FinancialYearCreate,
    FinancialYearUpdate,
    PlantCreate,
)

pytestmark = pytest.mark.unit


class TestCodeNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("ho", "HO"),
            ("  ho  ", "HO"),
            ("plant  a", "PLANT A"),
            ("rm-01", "RM-01"),
            ("fy/2026-27", "FY/2026-27"),
        ],
    )
    def test_codes_are_upper_cased_and_space_collapsed(self, raw: str, expected: str) -> None:
        assert BranchCreate(code=raw, name="Branch").code == expected

    def test_two_spellings_of_one_code_normalise_to_the_same_value(self) -> None:
        """The uniqueness check downstream compares the normalised form only."""
        first = PlantCreate(code="plant  a", name="Plant A").code
        second = PlantCreate(code="  PLANT A ", name="Plant A").code
        assert first == second

    @pytest.mark.parametrize("raw", ["", "   ", "-HQ", "HQ@1", "HQ#"])
    def test_unusable_codes_are_rejected(self, raw: str) -> None:
        with pytest.raises(ValidationError):
            DepartmentCreate(code=raw, name="Quality")

    def test_names_have_internal_whitespace_collapsed(self) -> None:
        assert BranchCreate(code="HO", name="  Head    Office  ").name == "Head Office"

    def test_a_whitespace_only_name_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BranchCreate(code="HO", name="   ")


class TestStatutoryIdentifiers:
    def test_a_valid_gstin_is_accepted_and_upper_cased(self) -> None:
        branch = BranchCreate(code="HO", name="Head Office", gstin="24aaacc1206d1zm")
        assert branch.gstin == "24AAACC1206D1ZM"

    @pytest.mark.parametrize(
        "bad",
        [
            "24AAACC1206D1Z",  # 14 characters
            "24AAACC1206D1ZMX",  # 16 characters
            "24AAACC1206D1AM",  # 14th character must be Z
            "AAAAACC1206D1ZM",  # first two must be the numeric state code
        ],
    )
    def test_a_malformed_gstin_is_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            BranchCreate(code="HO", name="Head Office", gstin=bad)

    def test_a_valid_pan_is_accepted_and_upper_cased(self) -> None:
        assert CompanyUpdate(pan="aaacc1206d").pan == "AAACC1206D"

    @pytest.mark.parametrize("bad", ["AAACC1206", "AAACC12061", "12345ABCDE"])
    def test_a_malformed_pan_is_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            CompanyUpdate(pan=bad)

    def test_a_blank_identifier_becomes_null_rather_than_an_empty_string(self) -> None:
        """Otherwise ``gstin = ''`` and ``gstin IS NULL`` both mean "unregistered"."""
        assert BranchCreate(code="HO", name="Head Office", gstin="   ").gstin is None


class TestCompanyProfile:
    def test_currency_is_upper_cased(self) -> None:
        assert CompanyUpdate(currency="inr").currency == "INR"

    @pytest.mark.parametrize("bad", ["IN", "INRS", "1NR"])
    def test_a_non_iso_currency_is_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            CompanyUpdate(currency=bad)

    def test_a_known_timezone_is_accepted(self) -> None:
        assert CompanyUpdate(timezone="Asia/Kolkata").timezone == "Asia/Kolkata"

    def test_an_unknown_timezone_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CompanyUpdate(timezone="Asia/Bengaluru")

    @pytest.mark.parametrize("month", [0, 13, -1])
    def test_an_impossible_financial_year_start_month_is_rejected(self, month: int) -> None:
        with pytest.raises(ValidationError):
            CompanyUpdate(financial_year_start_month=month)

    def test_the_tenant_boundary_fields_cannot_be_sent(self) -> None:
        """``code`` and ``is_active`` are not editable through the tenant API.

        ``extra="forbid"`` turns an attempt into a 422 rather than a silently
        ignored field, so a client cannot believe it renamed the tenant.
        """
        with pytest.raises(ValidationError):
            CompanyUpdate(code="NEWCODE")
        with pytest.raises(ValidationError):
            CompanyUpdate(is_active=False)

    def test_company_id_is_not_accepted_from_a_client(self) -> None:
        with pytest.raises(ValidationError):
            CompanyUpdate(company_id="0198f000-0000-7000-8000-000000000000")


class TestUnsetSemantics:
    def test_an_omitted_field_is_absent_from_the_change_set(self) -> None:
        changes = BranchUpdate(name="New Name").model_dump(exclude_unset=True)
        assert changes == {"name": "New Name"}

    def test_an_explicit_null_is_present_in_the_change_set(self) -> None:
        """Clearing a field must be distinguishable from not mentioning it."""
        changes = BranchUpdate(gstin=None).model_dump(exclude_unset=True)
        assert changes == {"gstin": None}

    def test_an_empty_patch_produces_an_empty_change_set(self) -> None:
        # The service turns this into an explicit "nothing to update" error
        # rather than issuing a pointless UPDATE and an empty audit diff.
        assert BranchUpdate().model_dump(exclude_unset=True) == {}

    def test_unknown_fields_are_rejected_rather_than_ignored(self) -> None:
        with pytest.raises(ValidationError):
            BranchUpdate(is_default=True)


class TestFinancialYearPayloads:
    def test_the_code_is_normalised(self) -> None:
        year = FinancialYearCreate(
            code=" 2026-27 ", start_date=date(2026, 4, 1), end_date=date(2027, 3, 31)
        )
        assert year.code == "2026-27"

    def test_is_closed_cannot_be_set_on_create(self) -> None:
        """Closing is a transition with its own authority check and audit row.

        Accepting the flag here would let a create skip both.
        """
        payload = FinancialYearCreate(
            code="2026-27", start_date=date(2026, 4, 1), end_date=date(2027, 3, 31)
        )
        assert not hasattr(payload, "is_closed")

    def test_is_closed_cannot_be_set_on_update(self) -> None:
        with pytest.raises(ValidationError):
            FinancialYearUpdate(is_closed=True)

    def test_the_code_cannot_be_changed_on_update(self) -> None:
        """It is the document-numbering period key; renaming it orphans the series."""
        with pytest.raises(ValidationError):
            FinancialYearUpdate(code="2027-28")

    def test_notes_collapse_to_null_when_blank(self) -> None:
        year = FinancialYearCreate(
            code="2026-27",
            start_date=date(2026, 4, 1),
            end_date=date(2027, 3, 31),
            notes="  ",
        )
        assert year.notes is None
