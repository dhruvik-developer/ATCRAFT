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

Statutory identifiers (GSTIN, PAN) are format-checked here rather than in the
service: they are printed on tax invoices, and a typo discovered at filing time
is expensive to unwind. The checks are shape-only - only the GST portal can say
whether a well-formed GSTIN is actually registered.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from src.common.codes import normalise_code

# A code appears in document references (``PO/2026-27/00001`` is scoped by the
# financial year code) and on printed stationery; punctuation beyond dot, dash,
# slash and underscore makes those ambiguous to read and to parse.
CODE_PATTERN = r"^[A-Z0-9][A-Z0-9._\-/ ]*$"

# Standard Indian formats. Both are fixed-width and checksum-bearing, so a
# length-only check would let obvious transpositions through.
PAN_PATTERN = r"^[A-Z]{5}[0-9]{4}[A-Z]$"
GSTIN_PATTERN = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$"

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


def _normalise_code(value: str) -> str:
    normalised = normalise_code(value)
    if not normalised:
        raise ValueError("Code cannot be blank.")
    if not re.match(CODE_PATTERN, normalised):
        raise ValueError(
            "Code may contain only letters, digits, dot, dash, slash, underscore and spaces, "
            "and must start with a letter or digit."
        )
    return normalised


def _optional_code(value: str | None) -> str | None:
    return None if value is None else _normalise_code(value)


def _clean_name(value: str) -> str:
    cleaned = " ".join(value.split())
    if not cleaned:
        raise ValueError("Name cannot be blank.")
    return cleaned


def _clean_optional_name(value: str | None) -> str | None:
    return None if value is None else _clean_name(value)


def _strip_optional(value: str | None) -> str | None:
    """Collapse a whitespace-only optional field to NULL.

    An empty string and NULL must not both mean "not supplied", or a report
    filtering on ``city IS NULL`` misses half the rows.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _upper_optional(value: str | None) -> str | None:
    stripped = _strip_optional(value)
    return stripped.upper() if stripped else None


def _validate_gstin(value: str | None) -> str | None:
    upper = _upper_optional(value)
    if upper is not None and not re.match(GSTIN_PATTERN, upper):
        raise ValueError("GSTIN must be 15 characters in the standard GST format.")
    return upper


def _validate_pan(value: str | None) -> str | None:
    upper = _upper_optional(value)
    if upper is not None and not re.match(PAN_PATTERN, upper):
        raise ValueError("PAN must be 10 characters, e.g. ABCDE1234F.")
    return upper


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

    _name = field_validator("name")(_clean_optional_name)
    _gstin = field_validator("gstin")(_validate_gstin)
    _pan = field_validator("pan")(_validate_pan)
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
    )(_strip_optional)

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
        return None if value is None else _clean_name(value)


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

    _code = field_validator("code")(_normalise_code)
    _name = field_validator("name")(_clean_name)
    _gstin = field_validator("gstin")(_validate_gstin)
    _strip = field_validator(
        "address_line1", "city", "state", "state_code", "postal_code", "phone"
    )(_strip_optional)


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
    _gstin = field_validator("gstin")(_validate_gstin)
    _strip = field_validator(
        "address_line1", "city", "state", "state_code", "postal_code", "phone"
    )(_strip_optional)


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

    _code = field_validator("code")(_normalise_code)
    _name = field_validator("name")(_clean_name)
    _strip = field_validator("address_line1", "city", "state")(_strip_optional)


class PlantUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    branch_id: uuid.UUID | None = None
    address_line1: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    is_active: bool | None = None

    _name = field_validator("name")(_clean_optional_name)
    _strip = field_validator("address_line1", "city", "state")(_strip_optional)


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
    is_active: bool = True

    _code = field_validator("code")(_normalise_code)
    _name = field_validator("name")(_clean_name)


class DepartmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    plant_id: uuid.UUID | None = None
    parent_id: uuid.UUID | None = None
    head_user_id: uuid.UUID | None = None
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

    _code = field_validator("code")(_normalise_code)
    _notes = field_validator("notes")(_strip_optional)


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

    _notes = field_validator("notes")(_strip_optional)


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
    "CODE_PATTERN",
    "GSTIN_PATTERN",
    "PAN_PATTERN",
    "BranchCreate",
    "BranchFilters",
    "BranchRead",
    "BranchUpdate",
    "CompanyRead",
    "CompanyUpdate",
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
