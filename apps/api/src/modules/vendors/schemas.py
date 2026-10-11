"""Request and response schemas for vendor and transporter master data.

Three conventions carry through this file:

* **Codes are normalised on the way in** (:func:`src.common.codes.normalise_code`)
  so ``vend 001``, ``VEND-001`` and ``vend001`` cannot become three vendors that
  a buyer reads as one.
* **Updates use explicit "unset" semantics.** Every field on an update schema is
  optional and the service applies only the keys actually sent
  (``model_dump(exclude_unset=True)``); otherwise a PATCH omitting
  ``credit_limit`` would silently clear it.
* **Statutory identifiers are validated, not merely length-checked.** A GSTIN
  carries a mod-36 check digit and embeds the PAN; verifying both here stops a
  transposed character from reaching a GST return, where it is expensive. The
  validators live in :mod:`src.common.tax_ids` and are the same ones the
  customer and organisation masters apply.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from src.common.banking import IFSC_PATTERN, validate_optional_ifsc
from src.common.codes import clean_name, strip_optional, validate_code
from src.common.tax_ids import (
    gstin_check_digit,
    pan_from_gstin,
    state_code_from_gstin,
    validate_gstin,
    validate_pan,
)
from src.core.constants import CURRENCY_CODE_LENGTH, DEFAULT_CURRENCY
from src.modules.vendors.models import (
    DEFAULT_RATING_WINDOW_DAYS,
    MAX_RATING_WINDOW_DAYS,
    MAX_VENDOR_RATING,
    MIN_RATING_WINDOW_DAYS,
    TransporterType,
    VendorContactRole,
    VendorType,
)

# ``IFSC_PATTERN`` lives in :mod:`src.common.banking`, shared with the company's
# own bank details, and stays importable from here.
SWIFT_PATTERN = r"^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$"


#: The literal a transporter's tracking template must contain. Kept as a
#: constant because the model's check constraint and the URL builder in the
#: service both have to agree with this exact spelling.
TRACKING_PLACEHOLDER = "{tracking_id}"

Code = Annotated[str, Field(min_length=1, max_length=32)]
Name = Annotated[str, Field(min_length=1, max_length=255)]
Rating = Annotated[Decimal, Field(ge=0, le=MAX_VENDOR_RATING, max_digits=3, decimal_places=2)]


def _clean_optional_name(value: str | None) -> str | None:
    return None if value is None else clean_name(value)


def _upper_optional(value: str | None) -> str | None:
    stripped = strip_optional(value)
    return stripped.upper().replace(" ", "") if stripped else None


def _optional_gstin(value: str | None) -> str | None:
    stripped = strip_optional(value)
    return validate_gstin(stripped) if stripped else None


def _optional_pan(value: str | None) -> str | None:
    stripped = strip_optional(value)
    return validate_pan(stripped) if stripped else None


def _optional_ifsc(value: str | None) -> str | None:
    return validate_optional_ifsc(value)


def _optional_swift(value: str | None) -> str | None:
    stripped = _upper_optional(value)
    if stripped and not re.match(SWIFT_PATTERN, stripped):
        raise ValueError("SWIFT/BIC must be 8 or 11 alphanumeric characters.")
    return stripped


def _normalise_vendor_types(values: list[VendorType]) -> list[VendorType]:
    """Deduplicate and order the type array.

    A stable order makes two vendors with the same roles compare equal in an
    audit diff, and deduplication keeps ``@>`` containment queries honest.
    """
    if not values:
        raise ValueError("A vendor must have at least one vendor type.")
    return sorted(set(values), key=str)


def _validate_tracking_template(value: str | None) -> str | None:
    """A tracking template must be an HTTPS URL carrying the placeholder.

    Without the placeholder every consignment resolves to the same link, which
    looks like it works and never does. HTTP is refused because the link is
    rendered for customers and an unencrypted carrier page is an easy downgrade
    target.
    """
    stripped = strip_optional(value)
    if stripped is None:
        return None
    if not stripped.lower().startswith("https://"):
        raise ValueError("Tracking URL template must start with https://")
    if TRACKING_PLACEHOLDER not in stripped:
        raise ValueError(
            f"Tracking URL template must contain the {TRACKING_PLACEHOLDER} placeholder."
        )
    return stripped


class _StatutoryConsistency(BaseModel):
    """Cross-field checks shared by every payload carrying a GSTIN.

    Kept in one place so create and update cannot drift apart: an update that
    changes only the PAN must be checked against the GSTIN already on file,
    which is why the service re-runs :func:`assert_gstin_pan_agree` after
    merging the changes.
    """

    @model_validator(mode="after")
    def _gstin_agrees_with_pan_and_state(self) -> _StatutoryConsistency:
        gstin = getattr(self, "gstin", None)
        if not gstin:
            return self
        assert_gstin_pan_agree(gstin, getattr(self, "pan", None))
        assert_gstin_state_agrees(gstin, getattr(self, "state_code", None))
        return self


def assert_gstin_pan_agree(gstin: str | None, pan: str | None) -> None:
    """Refuse a GSTIN and PAN that describe different entities."""
    if not gstin or not pan:
        return
    embedded = pan_from_gstin(gstin)
    if embedded != pan:
        raise ValueError(
            f"GSTIN contains PAN {embedded}, which does not match the PAN {pan} supplied."
        )


def assert_gstin_state_agrees(gstin: str | None, state_code: str | None) -> None:
    """Refuse a GST state code that contradicts the GSTIN it came with.

    The state code decides CGST+SGST versus IGST on every purchase from this
    vendor, so a disagreement here becomes a tax error on every invoice.
    """
    if not gstin or not state_code:
        return
    expected = state_code_from_gstin(gstin)
    if state_code.strip().zfill(2) != expected:
        raise ValueError(
            f"GSTIN begins with state code {expected}, which does not match "
            f"the state code {state_code} supplied."
        )


# -----------------------------------------------------------------------------
# Vendor
# -----------------------------------------------------------------------------


class VendorCreate(_StatutoryConsistency):
    """Payload for creating a vendor.

    ``company_id`` is absent by design: it comes from the authenticated
    principal and is never accepted from a client (section 40). So are
    ``is_approved``, ``approved_by_id``, ``approved_at`` and ``rating`` - a
    vendor is created unapproved and unrated, and both are changed only through
    their own permission-gated endpoints.
    """

    code: Code
    name: Name
    legal_name: str | None = Field(default=None, max_length=255)
    vendor_types: list[VendorType] = Field(min_length=1)

    gstin: str | None = Field(default=None, max_length=15)
    pan: str | None = Field(default=None, max_length=10)
    msme_number: str | None = Field(default=None, max_length=32)

    payment_terms_days: int = Field(default=0, ge=0, le=365)
    credit_limit: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    lead_time_days: int = Field(default=0, ge=0, le=999)
    currency: str = Field(
        default=DEFAULT_CURRENCY, min_length=CURRENCY_CODE_LENGTH, max_length=CURRENCY_CODE_LENGTH
    )

    bank_name: str | None = Field(default=None, max_length=120)
    bank_branch: str | None = Field(default=None, max_length=120)
    bank_account_name: str | None = Field(default=None, max_length=255)
    bank_account_number: str | None = Field(default=None, max_length=34)
    bank_ifsc: str | None = Field(default=None, max_length=11)
    bank_swift: str | None = Field(default=None, max_length=11)

    address_line1: str | None = Field(default=None, max_length=255)
    address_line2: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    state_code: str | None = Field(default=None, max_length=4, description="GST state code")
    postal_code: str | None = Field(default=None, max_length=16)
    country: str = Field(default="India", max_length=80)

    contact_person: str | None = Field(default=None, max_length=160)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    mobile: str | None = Field(default=None, max_length=32)
    website: str | None = Field(default=None, max_length=255)

    is_active: bool = True
    notes: str | None = None

    _code = field_validator("code")(validate_code)
    _name = field_validator("name")(clean_name)
    _legal_name = field_validator("legal_name")(_clean_optional_name)
    _types = field_validator("vendor_types")(_normalise_vendor_types)
    _gstin = field_validator("gstin")(_optional_gstin)
    _pan = field_validator("pan")(_optional_pan)
    _ifsc = field_validator("bank_ifsc")(_optional_ifsc)
    _swift = field_validator("bank_swift")(_optional_swift)
    _upper = field_validator("msme_number", "state_code", "currency")(_upper_optional)
    _strip = field_validator(
        "address_line1",
        "address_line2",
        "city",
        "state",
        "postal_code",
        "contact_person",
        "phone",
        "mobile",
        "website",
        "bank_name",
        "bank_branch",
        "bank_account_name",
        "bank_account_number",
    )(strip_optional)


class VendorUpdate(_StatutoryConsistency):
    """Partial update of a vendor.

    ``code`` is not updatable: it is quoted on purchase orders and vendor-portal
    URLs, so renaming it in place invalidates history that has already left the
    building. Retire the vendor and create the replacement instead.

    Approval and rating are absent for the same reason as on create - they have
    their own endpoints and their own permissions.
    """

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    legal_name: str | None = Field(default=None, max_length=255)
    vendor_types: list[VendorType] | None = Field(default=None, min_length=1)

    gstin: str | None = Field(default=None, max_length=15)
    pan: str | None = Field(default=None, max_length=10)
    msme_number: str | None = Field(default=None, max_length=32)

    payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    credit_limit: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    lead_time_days: int | None = Field(default=None, ge=0, le=999)
    currency: str | None = Field(default=None, min_length=3, max_length=3)

    bank_name: str | None = Field(default=None, max_length=120)
    bank_branch: str | None = Field(default=None, max_length=120)
    bank_account_name: str | None = Field(default=None, max_length=255)
    bank_account_number: str | None = Field(default=None, max_length=34)
    bank_ifsc: str | None = Field(default=None, max_length=11)
    bank_swift: str | None = Field(default=None, max_length=11)

    address_line1: str | None = Field(default=None, max_length=255)
    address_line2: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    state_code: str | None = Field(default=None, max_length=4)
    postal_code: str | None = Field(default=None, max_length=16)
    country: str | None = Field(default=None, max_length=80)

    contact_person: str | None = Field(default=None, max_length=160)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    mobile: str | None = Field(default=None, max_length=32)
    website: str | None = Field(default=None, max_length=255)

    notes: str | None = None

    _name = field_validator("name")(_clean_optional_name)
    _legal_name = field_validator("legal_name")(_clean_optional_name)
    _gstin = field_validator("gstin")(_optional_gstin)
    _pan = field_validator("pan")(_optional_pan)
    _ifsc = field_validator("bank_ifsc")(_optional_ifsc)
    _swift = field_validator("bank_swift")(_optional_swift)
    _upper = field_validator("msme_number", "state_code", "currency")(_upper_optional)
    _strip = field_validator(
        "address_line1",
        "address_line2",
        "city",
        "state",
        "postal_code",
        "contact_person",
        "phone",
        "mobile",
        "website",
        "bank_name",
        "bank_branch",
        "bank_account_name",
        "bank_account_number",
    )(strip_optional)

    @field_validator("vendor_types")
    @classmethod
    def _types(cls, value: list[VendorType] | None) -> list[VendorType] | None:
        return None if value is None else _normalise_vendor_types(value)


class VendorRead(BaseModel):
    """Full vendor representation.

    ``bank_account_number`` is returned masked. The full number is needed only
    by the payment run, which reads the row directly; every screen that merely
    displays a vendor gets the last four digits, so a shoulder-surfed browser
    tab is not a payment-diversion starting point.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    legal_name: str | None
    vendor_types: list[str]

    gstin: str | None
    pan: str | None
    msme_number: str | None

    payment_terms_days: int
    credit_limit: Decimal | None
    lead_time_days: int
    currency: str

    rating: Decimal | None
    rating_updated_at: datetime | None
    rating_sample_size: int

    is_approved: bool
    approved_by_id: uuid.UUID | None
    approved_at: datetime | None
    is_active: bool

    bank_name: str | None
    bank_branch: str | None
    bank_account_name: str | None
    bank_account_number_masked: str | None = None
    bank_ifsc: str | None
    bank_swift: str | None

    address_line1: str | None
    address_line2: str | None
    city: str | None
    state: str | None
    state_code: str | None
    postal_code: str | None
    country: str

    contact_person: str | None
    email: str | None
    phone: str | None
    mobile: str | None
    website: str | None
    notes: str | None

    #: Counts assembled for the whole page in one query each - never a per-row
    #: lookup (section 32).
    contact_count: int = 0
    category_count: int = 0

    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class VendorOption(BaseModel):
    """Slim shape for pickers.

    A separate projection rather than :class:`VendorRead`: the picker appears on
    every procurement screen and has no use for bank or address details.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    vendor_types: list[str]
    is_approved: bool
    rating: Decimal | None
    lead_time_days: int


# -----------------------------------------------------------------------------
# Vendor contacts
# -----------------------------------------------------------------------------


class VendorContactCreate(BaseModel):
    """A person at the vendor. ``vendor_id`` comes from the path, not the body."""

    name: Annotated[str, Field(min_length=1, max_length=160)]
    contact_role: VendorContactRole
    designation: str | None = Field(default=None, max_length=120)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    mobile: str | None = Field(default=None, max_length=32)
    is_primary: bool = False
    is_active: bool = True
    notes: str | None = None

    _name = field_validator("name")(clean_name)
    _strip = field_validator("designation", "phone", "mobile")(strip_optional)

    @model_validator(mode="after")
    def _reachable(self) -> VendorContactCreate:
        # Mirrors the check constraint, so the caller gets a field-level 422
        # rather than an opaque IntegrityError from the database.
        if not (self.email or self.phone or self.mobile):
            raise ValueError("A contact needs at least an e-mail, a phone or a mobile number.")
        return self


class VendorContactUpdate(BaseModel):
    """Partial update of a contact.

    ``is_primary`` is not here: promoting a contact demotes another one in the
    same transaction, which is an operation rather than a field edit, so it has
    its own endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    contact_role: VendorContactRole | None = None
    designation: str | None = Field(default=None, max_length=120)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    mobile: str | None = Field(default=None, max_length=32)
    is_active: bool | None = None
    notes: str | None = None

    _name = field_validator("name")(_clean_optional_name)
    _strip = field_validator("designation", "phone", "mobile")(strip_optional)


class VendorContactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    vendor_id: uuid.UUID
    name: str
    designation: str | None
    contact_role: VendorContactRole
    email: str | None
    phone: str | None
    mobile: str | None
    is_primary: bool
    is_active: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime


# -----------------------------------------------------------------------------
# Vendor categories
# -----------------------------------------------------------------------------


class VendorCategoryAssign(BaseModel):
    """Qualify a vendor for one item category."""

    item_category_id: uuid.UUID
    is_preferred: bool = False
    notes: str | None = None


class VendorCategoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_preferred: bool | None = None
    notes: str | None = None


class VendorCategoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    vendor_id: uuid.UUID
    item_category_id: uuid.UUID
    is_preferred: bool
    notes: str | None
    #: Filled from a single batched lookup so a list of qualifications does not
    #: issue one category query per row.
    item_category_code: str | None = None
    item_category_name: str | None = None
    created_at: datetime


# -----------------------------------------------------------------------------
# Vendor rating
# -----------------------------------------------------------------------------


class VendorRatingRead(BaseModel):
    """Outcome of a rating computation.

    ``rating`` is null when the window did not contain enough evidence; the
    stored rating is then left untouched, and ``reason`` says why.
    """

    model_config = ConfigDict(from_attributes=True)

    vendor_id: uuid.UUID
    rating: Decimal | None
    on_time_percentage: Decimal | None
    rejection_percentage: Decimal | None
    receipt_count: int
    window_days: int
    has_quality_evidence: bool
    is_sufficient: bool
    reason: str
    applied: bool = False


class VendorRatingRequest(BaseModel):
    """Parameters for a rating recomputation.

    Deliberately carries no performance figures. The metrics are read from
    goods receipts and QC inspections on the server; accepting them from the
    client would let a buyer type their own vendor's score.
    """

    window_days: int = Field(
        default=DEFAULT_RATING_WINDOW_DAYS, ge=MIN_RATING_WINDOW_DAYS, le=MAX_RATING_WINDOW_DAYS
    )


# -----------------------------------------------------------------------------
# Transporters
# -----------------------------------------------------------------------------


class TransporterCreate(BaseModel):
    code: Code
    name: Name
    transporter_type: TransporterType
    gstin: str | None = Field(default=None, max_length=15)
    pan: str | None = Field(default=None, max_length=10)

    contact_person: str | None = Field(default=None, max_length=160)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    mobile: str | None = Field(default=None, max_length=32)
    website: str | None = Field(default=None, max_length=255)

    address_line1: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    state_code: str | None = Field(default=None, max_length=4)
    postal_code: str | None = Field(default=None, max_length=16)
    country: str = Field(default="India", max_length=80)

    tracking_url_template: str | None = Field(
        default=None,
        max_length=500,
        description=f"HTTPS URL containing the literal {TRACKING_PLACEHOLDER} placeholder",
    )
    is_active: bool = True
    notes: str | None = None

    _code = field_validator("code")(validate_code)
    _name = field_validator("name")(clean_name)
    _gstin = field_validator("gstin")(_optional_gstin)
    _pan = field_validator("pan")(_optional_pan)
    _upper = field_validator("state_code")(_upper_optional)
    _template = field_validator("tracking_url_template")(_validate_tracking_template)
    _strip = field_validator(
        "contact_person",
        "phone",
        "mobile",
        "website",
        "address_line1",
        "city",
        "state",
        "postal_code",
    )(strip_optional)

    @model_validator(mode="after")
    def _statutory(self) -> TransporterCreate:
        assert_gstin_pan_agree(self.gstin, self.pan)
        assert_gstin_state_agrees(self.gstin, self.state_code)
        return self


class TransporterUpdate(BaseModel):
    """Partial update. ``code`` is immutable, as for vendors."""

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    transporter_type: TransporterType | None = None
    gstin: str | None = Field(default=None, max_length=15)
    pan: str | None = Field(default=None, max_length=10)

    contact_person: str | None = Field(default=None, max_length=160)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    mobile: str | None = Field(default=None, max_length=32)
    website: str | None = Field(default=None, max_length=255)

    address_line1: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    state_code: str | None = Field(default=None, max_length=4)
    postal_code: str | None = Field(default=None, max_length=16)
    country: str | None = Field(default=None, max_length=80)

    tracking_url_template: str | None = Field(default=None, max_length=500)
    notes: str | None = None

    _name = field_validator("name")(_clean_optional_name)
    _gstin = field_validator("gstin")(_optional_gstin)
    _pan = field_validator("pan")(_optional_pan)
    _upper = field_validator("state_code")(_upper_optional)
    _template = field_validator("tracking_url_template")(_validate_tracking_template)
    _strip = field_validator(
        "contact_person",
        "phone",
        "mobile",
        "website",
        "address_line1",
        "city",
        "state",
        "postal_code",
    )(strip_optional)

    @model_validator(mode="after")
    def _statutory(self) -> TransporterUpdate:
        assert_gstin_pan_agree(self.gstin, self.pan)
        assert_gstin_state_agrees(self.gstin, self.state_code)
        return self


class TransporterRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    transporter_type: TransporterType
    gstin: str | None
    pan: str | None
    contact_person: str | None
    email: str | None
    phone: str | None
    mobile: str | None
    website: str | None
    address_line1: str | None
    city: str | None
    state: str | None
    state_code: str | None
    postal_code: str | None
    country: str
    tracking_url_template: str | None
    is_active: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class TransporterOption(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    transporter_type: TransporterType
    has_tracking: bool = False


class TrackingLinkRead(BaseModel):
    """A tracking id resolved against a transporter's template."""

    transporter_id: uuid.UUID
    tracking_id: str
    url: str | None
    #: False when the transporter has no template on file, so the UI shows the
    #: raw consignment number instead of a dead link.
    is_resolvable: bool


# -----------------------------------------------------------------------------
# Filters and small command payloads
# -----------------------------------------------------------------------------


class VendorFilters(BaseModel):
    """List filters, validated here so the repository never sees free text."""

    search: str | None = None
    vendor_type: VendorType | None = None
    item_category_id: uuid.UUID | None = None
    is_active: bool | None = None
    is_approved: bool | None = None
    state_code: str | None = None
    min_rating: Rating | None = None
    include_deleted: bool = False


class VendorContactFilters(BaseModel):
    vendor_id: uuid.UUID | None = None
    contact_role: VendorContactRole | None = None
    is_active: bool | None = None
    search: str | None = None
    include_deleted: bool = False


class VendorCategoryFilters(BaseModel):
    vendor_id: uuid.UUID | None = None
    item_category_id: uuid.UUID | None = None
    is_preferred: bool | None = None


class TransporterFilters(BaseModel):
    search: str | None = None
    transporter_type: TransporterType | None = None
    is_active: bool | None = None
    include_deleted: bool = False


class StatusChangeRequest(BaseModel):
    """Reason capture for a lifecycle change.

    Required, not optional: a state change with no recorded reason is what makes
    an audit trail useless six months later, when nobody remembers why a vendor
    was blocked (section 83).
    """

    reason: str = Field(min_length=3, max_length=255)


class ApprovalRequest(BaseModel):
    """Reason and optional note captured when a vendor is approved or blocked."""

    reason: str = Field(min_length=3, max_length=255)


__all__ = [
    "IFSC_PATTERN",
    "TRACKING_PLACEHOLDER",
    "ApprovalRequest",
    "StatusChangeRequest",
    "TrackingLinkRead",
    "TransporterCreate",
    "TransporterFilters",
    "TransporterOption",
    "TransporterRead",
    "TransporterUpdate",
    "VendorCategoryAssign",
    "VendorCategoryFilters",
    "VendorCategoryRead",
    "VendorCategoryUpdate",
    "VendorContactCreate",
    "VendorContactFilters",
    "VendorContactRead",
    "VendorContactUpdate",
    "VendorCreate",
    "VendorFilters",
    "VendorOption",
    "VendorRatingRead",
    "VendorRatingRequest",
    "VendorRead",
    "VendorUpdate",
    "assert_gstin_pan_agree",
    "assert_gstin_state_agrees",
    "gstin_check_digit",
    "pan_from_gstin",
    "state_code_from_gstin",
    "validate_gstin",
    "validate_pan",
]
