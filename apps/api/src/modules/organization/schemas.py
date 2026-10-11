"""Request and response schemas for the organisation masters.

Conventions shared with the other master-data modules:

* **Codes are normalised on the way in** (``src.common.codes.normalise_code``),
  so ``hq``, ``HQ`` and ``H Q`` cannot become three branches that a clerk reads
  as one.
* **Updates use explicit "unset" semantics.** Every field on an update schema is
  optional and the service applies only the keys the client actually sent
  (``model_dump(exclude_unset=True)``). Without that, a PATCH omitting
  ``head_user_id`` would silently null it out.
* **No schema carries ``company_id``.** The tenant comes from the verified
  principal (master rules section 40); a client-supplied one is never read.

Statutory identifiers (GSTIN, PAN) are validated here rather than in the
service: they are printed on tax invoices, and a typo discovered at filing time
is expensive to unwind. The validators are the shared ones in
:mod:`src.common.tax_ids` - shape and GSTIN check digit - so a number the
vendor master refuses cannot be accepted on a branch. Only the GST portal can
say whether a well-formed GSTIN is actually registered.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from src.common.banking import validate_optional_ifsc
from src.common.codes import clean_name, strip_optional, validate_code
from src.common.tax_ids import validate_optional_gstin, validate_optional_pan
from src.modules.organization.models import CostCentreType

# ASSUMPTION: the IANA timezone name is validated semantically only when a tz
# database is actually present. A container built without `tzdata` would
# otherwise reject every timezone, including the one already stored - a
# deployment defect must not become a validation error on unrelated edits.
try:
    ZoneInfo("UTC")
except (ZoneInfoNotFoundError, KeyError, ValueError):  # pragma: no cover - host-dependent
    _TZDB_AVAILABLE = False
else:
    _TZDB_AVAILABLE = True

Code = Annotated[str, Field(min_length=1, max_length=32)]
Name = Annotated[str, Field(min_length=1, max_length=255)]


def _optional_code(value: str | None) -> str | None:
    return None if value is None else validate_code(value)


def _clean_optional_name(value: str | None) -> str | None:
    return None if value is None else clean_name(value)


def _upper_optional(value: str | None) -> str | None:
    stripped = strip_optional(value)
    return stripped.upper() if stripped else None


def _validate_timezone(value: str) -> str:
    name = value.strip()
    if not name:
        raise ValueError("Timezone cannot be blank.")
    if _TZDB_AVAILABLE:
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"'{name}' is not a known IANA timezone.") from exc
    return name


def _validate_currency(value: str) -> str:
    code = value.strip().upper()
    if not re.match(r"^[A-Z]{3}$", code):
        raise ValueError("Currency must be a three-letter ISO 4217 code, e.g. INR.")
    return code


# -----------------------------------------------------------------------------
# Company
# -----------------------------------------------------------------------------


class CompanyUpdate(BaseModel):
    """Partial update of the caller's own company.

    Two fields are deliberately absent:

    ``code``       identifies the tenant in exported data and integration
                   payloads; renaming it in place would orphan history that has
                   already left the building.
    ``is_active``  deactivating the tenant from inside the tenant would lock
                   every user out with no way back in. It is a platform
                   operation, not a self-service one.
    """

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    legal_name: str | None = Field(default=None, max_length=255)
    gstin: str | None = Field(default=None, max_length=15)
    pan: str | None = Field(default=None, max_length=10)
    cin: str | None = Field(default=None, max_length=21)

    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    website: str | None = Field(default=None, max_length=255)

    address_line1: str | None = Field(default=None, max_length=255)
    address_line2: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    state_code: str | None = Field(default=None, max_length=4, description="GST state code")
    postal_code: str | None = Field(default=None, max_length=16)
    country: str | None = Field(default=None, min_length=1, max_length=80)

    currency: str | None = Field(default=None, min_length=3, max_length=3)
    financial_year_start_month: int | None = Field(default=None, ge=1, le=12)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    logo_document_id: uuid.UUID | None = None

    # Printed on outgoing invoices. An explicit null (or blank) clears a field.
    bank_name: str | None = Field(default=None, max_length=120)
    bank_account_name: str | None = Field(default=None, max_length=255)
    bank_account_number: str | None = Field(default=None, max_length=34)
    bank_ifsc: str | None = Field(default=None, max_length=11)
    bank_branch: str | None = Field(default=None, max_length=120)
    invoice_terms: str | None = Field(default=None, max_length=4000)

    _name = field_validator("name")(_clean_optional_name)
    _gstin = field_validator("gstin")(validate_optional_gstin)
    _pan = field_validator("pan")(validate_optional_pan)
    _strip = field_validator(
        "legal_name",
        "cin",
        "phone",
        "website",
        "address_line1",
        "address_line2",
        "city",
        "state",
        "state_code",
        "postal_code",
        "bank_name",
        "bank_account_name",
        "bank_branch",
        "invoice_terms",
    )(strip_optional)

    # The two bank identifiers are normalised BEFORE the length check: an IFSC
    # copied off a cheque leaf as "HDFC 0001204" is twelve characters until its
    # space goes, and refusing it for length would blame the user for spacing.

    @field_validator("bank_ifsc", mode="before")
    @classmethod
    def _ifsc(cls, value: object) -> object:
        return validate_optional_ifsc(value) if isinstance(value, str) else value

    @field_validator("bank_account_number", mode="before")
    @classmethod
    def _account_number(cls, value: object) -> object:
        # Spaces are how a number is read aloud, not part of it; the bank's
        # copy has none, and a customer keying it from the invoice must not
        # be handed a different string from the one on the cheque leaf.
        if not isinstance(value, str):
            return value
        stripped = strip_optional(value)
        return stripped.replace(" ", "").upper() if stripped else None

    @field_validator("cin")
    @classmethod
    def _upper_cin(cls, value: str | None) -> str | None:
        return _upper_optional(value)

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str | None) -> str | None:
        return None if value is None else _validate_currency(value)

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str | None) -> str | None:
        return None if value is None else _validate_timezone(value)

    @field_validator("country")
    @classmethod
    def _country(cls, value: str | None) -> str | None:
        return None if value is None else clean_name(value)


class CompanyRead(BaseModel):
    """The caller's own company. There is no endpoint that reads another one."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    legal_name: str | None
    gstin: str | None
    pan: str | None
    cin: str | None

    email: str | None
    phone: str | None
    website: str | None

    address_line1: str | None
    address_line2: str | None
    city: str | None
    state: str | None
    state_code: str | None
    postal_code: str | None
    country: str

    currency: str
    financial_year_start_month: int
    timezone: str
    is_active: bool
    logo_document_id: uuid.UUID | None

    bank_name: str | None
    bank_account_name: str | None
    # ASSUMPTION: in full - these are the details printed on every invoice the
    # company sends, so anyone allowed to read the company profile may read
    # them. The audit trail is what is masked. docs/assumptions.md §B1-7.
    bank_account_number: str | None
    bank_ifsc: str | None
    bank_branch: str | None
    invoice_terms: str | None

    created_at: datetime
    updated_at: datetime


# -----------------------------------------------------------------------------
# Branch
# -----------------------------------------------------------------------------


class BranchCreate(BaseModel):
    """Payload for creating a branch."""

    code: Code
    name: Name
    gstin: str | None = Field(default=None, max_length=15)

    address_line1: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    state_code: str | None = Field(default=None, max_length=4)
    postal_code: str | None = Field(default=None, max_length=16)
    phone: str | None = Field(default=None, max_length=32)
    email: EmailStr | None = None

    is_active: bool = True
    is_head_office: bool = False

    _code = field_validator("code")(validate_code)
    _name = field_validator("name")(clean_name)
    _gstin = field_validator("gstin")(validate_optional_gstin)
    _strip = field_validator(
        "address_line1", "city", "state", "state_code", "postal_code", "phone"
    )(strip_optional)


class BranchUpdate(BaseModel):
    """Partial update. ``code`` is immutable - see :class:`CompanyUpdate`."""

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    gstin: str | None = Field(default=None, max_length=15)
    address_line1: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    state_code: str | None = Field(default=None, max_length=4)
    postal_code: str | None = Field(default=None, max_length=16)
    phone: str | None = Field(default=None, max_length=32)
    email: EmailStr | None = None
    is_active: bool | None = None
    is_head_office: bool | None = None

    _name = field_validator("name")(_clean_optional_name)
    _gstin = field_validator("gstin")(validate_optional_gstin)
    _strip = field_validator(
        "address_line1", "city", "state", "state_code", "postal_code", "phone"
    )(strip_optional)


class BranchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    gstin: str | None
    address_line1: str | None
    city: str | None
    state: str | None
    state_code: str | None
    postal_code: str | None
    phone: str | None
    email: str | None
    is_active: bool
    is_head_office: bool
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


# -----------------------------------------------------------------------------
# Plant
# -----------------------------------------------------------------------------


class PlantCreate(BaseModel):
    code: Code
    name: Name
    branch_id: uuid.UUID | None = None
    address_line1: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    is_active: bool = True

    _code = field_validator("code")(validate_code)
    _name = field_validator("name")(clean_name)
    _strip = field_validator("address_line1", "city", "state")(strip_optional)


class PlantUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    branch_id: uuid.UUID | None = None
    address_line1: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    is_active: bool | None = None

    _name = field_validator("name")(_clean_optional_name)
    _strip = field_validator("address_line1", "city", "state")(strip_optional)


class PlantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    branch_id: uuid.UUID | None
    address_line1: str | None
    city: str | None
    state: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


# -----------------------------------------------------------------------------
# Department
# -----------------------------------------------------------------------------


class DepartmentCreate(BaseModel):
    code: Code
    name: Name
    plant_id: uuid.UUID | None = None
    parent_id: uuid.UUID | None = None
    head_user_id: uuid.UUID | None = None
    #: The cost centre documents raised by this department default to.
    cost_centre_id: uuid.UUID | None = None
    is_active: bool = True

    _code = field_validator("code")(validate_code)
    _name = field_validator("name")(clean_name)


class DepartmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    plant_id: uuid.UUID | None = None
    parent_id: uuid.UUID | None = None
    head_user_id: uuid.UUID | None = None
    #: An explicit null clears the default.
    cost_centre_id: uuid.UUID | None = None
    is_active: bool | None = None

    _name = field_validator("name")(_clean_optional_name)


class DepartmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    plant_id: uuid.UUID | None
    parent_id: uuid.UUID | None
    head_user_id: uuid.UUID | None
    cost_centre_id: uuid.UUID | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


# -----------------------------------------------------------------------------
# Cost centre
# -----------------------------------------------------------------------------


class CostCentreCreate(BaseModel):
    """Payload for opening a cost centre.

    ``cost_centre_type`` is required rather than defaulted: it is how the centre
    is grouped on every report, and a silent default would misclassify each one
    nobody remembered to change.
    """

    model_config = ConfigDict(extra="forbid")

    code: Code
    name: Name
    cost_centre_type: CostCentreType
    parent_id: uuid.UUID | None = None
    plant_id: uuid.UUID | None = None
    head_user_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=2000)
    is_active: bool = True

    _code = field_validator("code")(validate_code)
    _name = field_validator("name")(clean_name)
    _description = field_validator("description")(strip_optional)


class CostCentreUpdate(BaseModel):
    """Partial update. ``code`` is immutable: it is quoted on posted history.

    An explicit null clears ``parent_id`` (the centre becomes a root),
    ``plant_id``, ``head_user_id`` or ``description``; an omitted key is left
    alone.
    """

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    cost_centre_type: CostCentreType | None = None
    parent_id: uuid.UUID | None = None
    plant_id: uuid.UUID | None = None
    head_user_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=2000)
    is_active: bool | None = None

    _name = field_validator("name")(_clean_optional_name)
    _description = field_validator("description")(strip_optional)


class CostCentreRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    cost_centre_type: CostCentreType
    parent_id: uuid.UUID | None
    plant_id: uuid.UUID | None
    head_user_id: uuid.UUID | None
    description: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


# -----------------------------------------------------------------------------
# Financial year
# -----------------------------------------------------------------------------


class FinancialYearCreate(BaseModel):
    """Payload for opening a financial year.

    ``is_closed`` is absent by design: a year is closed through
    ``POST /financial-years/{id}/close``, which validates the transition and
    audits it. Accepting it here would let a create bypass both.
    """

    code: Annotated[str, Field(min_length=1, max_length=16)]
    start_date: date
    end_date: date
    is_active: bool = True
    notes: str | None = None

    _code = field_validator("code")(validate_code)
    _notes = field_validator("notes")(strip_optional)


class FinancialYearUpdate(BaseModel):
    """Partial update.

    Dates are updatable while the year is open because the first period of a new
    deployment is routinely entered before the exact cut-off is agreed. Once the
    year is closed the service refuses every edit - that is what closing means.
    """

    model_config = ConfigDict(extra="forbid")

    start_date: date | None = None
    end_date: date | None = None
    is_active: bool | None = None
    notes: str | None = None

    _notes = field_validator("notes")(strip_optional)


class FinancialYearRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    start_date: date
    end_date: date
    is_active: bool
    is_closed: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


# -----------------------------------------------------------------------------
# Filters and small command payloads
# -----------------------------------------------------------------------------


class BranchFilters(BaseModel):
    search: str | None = None
    is_active: bool | None = None
    is_head_office: bool | None = None
    include_deleted: bool = False


class PlantFilters(BaseModel):
    search: str | None = None
    branch_id: uuid.UUID | None = None
    is_active: bool | None = None
    include_deleted: bool = False


class DepartmentFilters(BaseModel):
    search: str | None = None
    plant_id: uuid.UUID | None = None
    parent_id: uuid.UUID | None = None
    head_user_id: uuid.UUID | None = None
    is_active: bool | None = None
    include_deleted: bool = False


class CostCentreFilters(BaseModel):
    search: str | None = None
    parent_id: uuid.UUID | None = None
    plant_id: uuid.UUID | None = None
    cost_centre_type: CostCentreType | None = None
    is_active: bool | None = None
    include_deleted: bool = False


class FinancialYearFilters(BaseModel):
    search: str | None = None
    is_active: bool | None = None
    is_closed: bool | None = None
    #: Return the year whose range contains this date - the question every
    #: posting screen actually asks.
    contains_date: date | None = None
    include_deleted: bool = False


class ReasonRequest(BaseModel):
    """Reason capture for a lifecycle change.

    Required, not optional: a state change with no recorded reason is what makes
    an audit trail useless six months later, when nobody remembers why a branch
    was switched off (section 83).
    """

    reason: str = Field(min_length=3, max_length=255)


__all__ = [
    "BranchCreate",
    "BranchFilters",
    "BranchRead",
    "BranchUpdate",
    "CompanyRead",
    "CompanyUpdate",
    "CostCentreCreate",
    "CostCentreFilters",
    "CostCentreRead",
    "CostCentreUpdate",
    "DepartmentCreate",
    "DepartmentFilters",
    "DepartmentRead",
    "DepartmentUpdate",
    "FinancialYearCreate",
    "FinancialYearFilters",
    "FinancialYearRead",
    "FinancialYearUpdate",
    "PlantCreate",
    "PlantFilters",
    "PlantRead",
    "PlantUpdate",
    "ReasonRequest",
]
