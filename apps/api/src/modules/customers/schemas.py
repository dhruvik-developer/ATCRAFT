"""Request and response contracts for the customer master.

Conventions applied throughout, each for a reason:

* **Codes are normalised on the way in** (``src.common.codes.normalise_code``)
  so ``ac 001``, ``AC-001`` and ``ac001`` cannot become three customers that a
  salesperson reads as one.
* **Updates carry explicit "unset" semantics.** Every field on an update schema
  is optional and the service applies only the keys the client actually sent
  (``model_dump(exclude_unset=True)``). Without that, a PATCH omitting
  ``assigned_sales_user_id`` would silently unassign the account.
* **Statutory identifiers are validated here, check digit included**
  (:mod:`src.common.tax_ids`, shared with the vendor and organisation masters
  so the same GSTIN cannot pass on one and fail on another). A malformed or
  mistyped GSTIN is rejected at the edge with a field-level message rather
  than surviving to invoicing time, where it fails an e-invoice submission
  with a numeric error code nobody can act on.
* **``company_id`` appears on no request model.** It comes from the
  authenticated principal and is never accepted from a client (section 40).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from src.common.codes import clean_name, strip_optional, validate_code
from src.common.tax_ids import validate_optional_gstin, validate_optional_pan
from src.modules.customers.models import AddressType, CustomerType, PriceType

MoneyField = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
#: Signed money, for a finance adjustment that may reduce the balance.
SignedMoneyField = Annotated[Decimal, Field(max_digits=18, decimal_places=4)]

Code = Annotated[str, Field(min_length=1, max_length=32)]
Name = Annotated[str, Field(min_length=1, max_length=255)]
Reason = Annotated[str, Field(min_length=3, max_length=255)]


def _validate_state_code(value: str | None) -> str | None:
    cleaned = strip_optional(value)
    if cleaned is None:
        return None
    if not re.match(r"^[0-9]{2}$", cleaned):
        raise ValueError("State code must be the two-digit GST state code, for example 27.")
    return cleaned


class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True)


# -----------------------------------------------------------------------------
# Customer
# -----------------------------------------------------------------------------


class CustomerCreate(_Base):
    """Payload for creating a customer, dealer, distributor or OEM account."""

    code: Code
    name: Name
    legal_name: str | None = Field(default=None, max_length=255)
    customer_type: CustomerType = CustomerType.CUSTOMER

    gstin: str | None = Field(default=None, max_length=15)
    pan: str | None = Field(default=None, max_length=10)

    price_list_type: PriceType = Field(
        default=PriceType.SALE,
        description="Which item_prices.price_type applies to this account.",
    )
    credit_limit: MoneyField | None = Field(
        default=None,
        description=(
            "Maximum exposure allowed. Omit or send null for an unlimited limit; "
            "send 0 to forbid credit entirely."
        ),
    )
    credit_days: Annotated[int, Field(ge=0, le=365)] = 0

    assigned_sales_user_id: uuid.UUID | None = None
    default_transporter_id: uuid.UUID | None = None

    is_active: bool = True
    notes: str | None = None

    _code = field_validator("code")(validate_code)
    _name = field_validator("name")(clean_name)
    _gstin = field_validator("gstin")(validate_optional_gstin)
    _pan = field_validator("pan")(validate_optional_pan)
    _strip = field_validator("legal_name")(strip_optional)


class CustomerUpdate(BaseModel):
    """Partial update.

    ``code`` is not updatable: it is quoted on orders, invoices and customer
    statements that have already left the building, so renaming it in place
    would make that history ambiguous. Retire the account and create the
    replacement instead.

    ``is_blocked`` is absent as well - a credit hold is a state change with a
    mandatory reason, and it goes through the block/unblock endpoints so it
    cannot be flipped as a side effect of an unrelated edit.
    """

    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True, extra="forbid")

    name: Name | None = None
    legal_name: str | None = Field(default=None, max_length=255)
    customer_type: CustomerType | None = None
    gstin: str | None = Field(default=None, max_length=15)
    pan: str | None = Field(default=None, max_length=10)
    price_list_type: PriceType | None = None
    credit_limit: MoneyField | None = None
    credit_days: Annotated[int, Field(ge=0, le=365)] | None = None
    assigned_sales_user_id: uuid.UUID | None = None
    default_transporter_id: uuid.UUID | None = None
    notes: str | None = None

    #: ``credit_limit=None`` is ambiguous - "no change" or "make it unlimited"?
    #: This flag disambiguates without a sentinel amount, which is the same
    #: problem the "0 means unlimited" convention creates and that this module
    #: deliberately refuses (see the service).
    clear_credit_limit: bool = False
    #: Same ambiguity, same solution, for the two nullable references.
    clear_assigned_sales_user: bool = False
    clear_default_transporter: bool = False

    _name = field_validator("name")(clean_name)
    _gstin = field_validator("gstin")(validate_optional_gstin)
    _pan = field_validator("pan")(validate_optional_pan)
    _strip = field_validator("legal_name")(strip_optional)


class CustomerSummary(_Base):
    """List row. Deliberately narrower than the detail model - a customer grid
    renders none of the address or contact data, and loading it would cost a
    join per page for nothing."""

    id: uuid.UUID
    code: str
    name: str
    legal_name: str | None
    customer_type: CustomerType
    price_list_type: PriceType
    gstin: str | None
    credit_limit: Decimal | None
    credit_days: int
    outstanding_amount: Decimal
    is_active: bool
    is_blocked: bool
    assigned_sales_user_id: uuid.UUID | None
    created_at: datetime


class CustomerDetail(CustomerSummary):
    """Single-customer view, including its addresses and contacts."""

    pan: str | None
    block_reason: str | None
    blocked_at: datetime | None
    blocked_by_id: uuid.UUID | None
    default_transporter_id: uuid.UUID | None
    notes: str | None
    updated_at: datetime

    addresses: list[AddressRead] = Field(default_factory=list)
    contacts: list[ContactRead] = Field(default_factory=list)
    #: Derived, never stored: NULL limit means unlimited, so headroom is
    #: undefined rather than infinite.
    credit_headroom: Decimal | None = None


class CustomerFilters(BaseModel):
    """List filters. Validated here so the repository never sees free text."""

    search: str | None = None
    customer_type: CustomerType | None = None
    price_list_type: PriceType | None = None
    assigned_sales_user_id: uuid.UUID | None = None
    is_active: bool | None = None
    is_blocked: bool | None = None
    #: "Show me accounts that have used up their limit" - the credit
    #: controller's worklist, computed in SQL rather than by filtering a page in
    #: Python, which would return the wrong rows for every page but the first.
    over_credit_limit: bool = False
    state_code: str | None = None
    include_deleted: bool = False

    _state_code = field_validator("state_code")(_validate_state_code)


class StatusChangeRequest(BaseModel):
    """Reason capture for a lifecycle change.

    Required, not optional: section 83 is explicit that a state change with no
    recorded reason is what makes an audit trail useless six months later, when
    nobody remembers why an account was switched off.
    """

    reason: Reason


class BlockRequest(BaseModel):
    """Put an account on credit hold."""

    reason: Reason


class OutstandingAdjustment(BaseModel):
    """A finance-owned change to the cached outstanding balance.

    Signed: an invoice increases the balance, a receipt or credit note reduces
    it. The reference names the ledger document that caused the change, so the
    cached figure can always be reconciled against the ledger - which is why
    ``reference_type`` and ``reference_code`` are required rather than optional.
    There is no HTTP route that takes this payload: the balance only ever moves
    inside the finance transaction that posts the document it describes.
    """

    delta: SignedMoneyField
    reason: Reason
    reference_type: str = Field(min_length=1, max_length=48)
    #: Optional because the document number is what a person reconciling the
    #: balance searches by; some callers post before the id is known.
    reference_id: uuid.UUID | None = None
    reference_code: str = Field(min_length=1, max_length=64)

    @field_validator("delta")
    @classmethod
    def _non_zero(cls, value: Decimal) -> Decimal:
        # A zero adjustment writes an audit row describing no change, which is
        # noise in the one log a reconciliation depends on.
        if value == 0:
            raise ValueError("A zero adjustment is not a change; send a non-zero delta.")
        return value


class CreditStatus(_Base):
    """The answer to "can this account take another order of X?"."""

    customer_id: uuid.UUID
    customer_code: str
    is_active: bool
    is_blocked: bool
    block_reason: str | None
    credit_limit: Decimal | None
    credit_days: int
    outstanding_amount: Decimal
    #: The order value the check was run against; zero for a plain status read.
    order_value: Decimal
    #: ``outstanding_amount + order_value``.
    exposure: Decimal
    #: ``credit_limit - exposure``. NULL when the limit is unlimited.
    headroom: Decimal | None
    is_unlimited: bool
    within_limit: bool


# -----------------------------------------------------------------------------
# Addresses
# -----------------------------------------------------------------------------


class AddressCreate(_Base):
    """A billing or shipping address.

    ``state_code`` is optional in the schema but mandatory in the database for a
    domestic address; the service reports that as a business-rule error naming
    the field rather than letting a constraint violation surface as a 500.
    """

    address_type: AddressType
    label: str | None = Field(default=None, max_length=120)
    address_line1: Annotated[str, Field(min_length=1, max_length=255)]
    address_line2: str | None = Field(default=None, max_length=255)
    landmark: str | None = Field(default=None, max_length=255)
    city: Annotated[str, Field(min_length=1, max_length=120)]
    state: str | None = Field(default=None, max_length=120)
    state_code: str | None = Field(default=None, max_length=4, description="GST state code")
    postal_code: str | None = Field(default=None, max_length=16)
    country: str = Field(default="India", max_length=80)
    gstin: str | None = Field(default=None, max_length=15)
    contact_person: str | None = Field(default=None, max_length=160)
    phone: str | None = Field(default=None, max_length=32)
    email: EmailStr | None = None
    is_default: bool = False
    is_active: bool = True
    notes: str | None = None

    _gstin = field_validator("gstin")(validate_optional_gstin)
    _state_code = field_validator("state_code")(_validate_state_code)
    _strip = field_validator(
        "label", "address_line2", "landmark", "state", "postal_code", "contact_person", "phone"
    )(strip_optional)


class AddressUpdate(BaseModel):
    """Partial update of an address.

    ``address_type`` cannot be changed: a billing address that becomes a
    shipping address would have to renegotiate the single-default rule for two
    types at once, and the operation is indistinguishable from creating the
    address you actually meant.
    """

    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True, extra="forbid")

    label: str | None = Field(default=None, max_length=120)
    address_line1: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    address_line2: str | None = Field(default=None, max_length=255)
    landmark: str | None = Field(default=None, max_length=255)
    city: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    state: str | None = Field(default=None, max_length=120)
    state_code: str | None = Field(default=None, max_length=4)
    postal_code: str | None = Field(default=None, max_length=16)
    country: str | None = Field(default=None, max_length=80)
    gstin: str | None = Field(default=None, max_length=15)
    contact_person: str | None = Field(default=None, max_length=160)
    phone: str | None = Field(default=None, max_length=32)
    email: EmailStr | None = None
    is_active: bool | None = None
    notes: str | None = None

    _gstin = field_validator("gstin")(validate_optional_gstin)
    _state_code = field_validator("state_code")(_validate_state_code)


class AddressRead(_Base):
    id: uuid.UUID
    customer_id: uuid.UUID
    address_type: AddressType
    label: str | None
    address_line1: str
    address_line2: str | None
    landmark: str | None
    city: str
    state: str | None
    state_code: str | None
    postal_code: str | None
    country: str
    gstin: str | None
    contact_person: str | None
    phone: str | None
    email: str | None
    is_default: bool
    is_active: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime


# -----------------------------------------------------------------------------
# Contacts
# -----------------------------------------------------------------------------


class ContactCreate(_Base):
    name: Annotated[str, Field(min_length=1, max_length=160)]
    designation: str | None = Field(default=None, max_length=120)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    is_primary: bool = False
    is_active: bool = True
    notes: str | None = None

    _name = field_validator("name")(clean_name)
    _strip = field_validator("designation", "phone")(strip_optional)

    @field_validator("phone")
    @classmethod
    def _reachable_hint(cls, value: str | None) -> str | None:
        # The email-or-phone rule spans two fields, so it is enforced in the
        # service (and by a check constraint); this validator only normalises.
        return value


class ContactUpdate(BaseModel):
    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True, extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    designation: str | None = Field(default=None, max_length=120)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    is_active: bool | None = None
    notes: str | None = None

    _name = field_validator("name")(clean_name)


class ContactRead(_Base):
    id: uuid.UUID
    customer_id: uuid.UUID
    name: str
    designation: str | None
    email: str | None
    phone: str | None
    is_primary: bool
    is_active: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime


# ``CustomerDetail`` refers to the address and contact models declared below it.
CustomerDetail.model_rebuild()


__all__ = [
    "AddressCreate",
    "AddressRead",
    "AddressUpdate",
    "BlockRequest",
    "ContactCreate",
    "ContactRead",
    "ContactUpdate",
    "CreditStatus",
    "CustomerCreate",
    "CustomerDetail",
    "CustomerFilters",
    "CustomerSummary",
    "CustomerUpdate",
    "OutstandingAdjustment",
    "StatusChangeRequest",
]
