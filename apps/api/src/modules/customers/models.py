"""Customer, dealer and distributor master data.

Three tables:

``customers``           the buying party - a direct customer, a dealer, a
                        distributor or an OEM account. One row per legal entity
                        we invoice.
``customer_addresses``  billing and shipping addresses. Separate rows rather
                        than columns on the customer because a dealer routinely
                        bills to a head office and ships to several godowns, and
                        every one of those needs its own GST state code.
``customer_contacts``   the people at that customer.

Why the channel is a column and not a table
-------------------------------------------
A dealer is a customer with different commercial terms, not a different kind of
entity: it is invoiced, it has a credit limit, it receives dispatches, and its
sales orders flow through the same state machine. Splitting dealers into their
own table would duplicate every one of those relationships and force each sales
query into a UNION. ``customer_type`` therefore classifies the account, and
``price_list_type`` says which ``item_prices.price_type`` row applies to it -
so the price resolver needs no knowledge of the channel vocabulary at all.

Money on this table
-------------------
``outstanding_amount`` is the one denormalised financial figure here, and it is
owned by the finance module (see
:func:`src.modules.customers.service.apply_outstanding_delta`). It exists
because the credit check runs on every sales order, and summing the open
invoice ledger on that path would put an aggregate query in front of order
entry. Nothing else about a customer's financial history is stored here -
invoices, payments and ageing are derived from the ledger, which stays the
source of truth.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.models import TenantMasterEntity, sql_array_literal
from src.core.types import GUID, Money

# The price-list vocabulary is owned by the products module. Importing the enum
# rather than restating it means a customer can never be pointed at a channel
# the price resolver does not know about.
from src.modules.products.models import PriceType


class CustomerType(StrEnum):
    """Commercial channel the account belongs to.

    A classification for reporting, defaults and permission scoping - never a
    switch that decides whether an operation is legal. What the account is
    charged is decided by ``price_list_type``; whether it may order is decided
    by the credit check.
    """

    CUSTOMER = "CUSTOMER"
    DEALER = "DEALER"
    DISTRIBUTOR = "DISTRIBUTOR"
    OEM = "OEM"


class AddressType(StrEnum):
    """What an address is used for.

    Kept to the two that change a document: the billing address determines the
    GSTIN and place of supply on the invoice, the shipping address determines
    where the consignment physically goes and which state code the e-way bill
    carries. A "registered office" nobody ever raises a document against is a
    note, not an address type.
    """

    BILLING = "BILLING"
    SHIPPING = "SHIPPING"


#: Two-digit GST state code, as it appears in the first two characters of a
#: GSTIN. Used by the column constraint and by the service consistency check.
STATE_CODE_SQL_PATTERN = "^[0-9]{2}$"


class Customer(TenantMasterEntity):
    """A buying party: direct customer, dealer, distributor or OEM account."""

    __tablename__ = "customers"

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Registered name on the GST certificate. Printed on the invoice, and
    #: routinely different from the trading name the sales team uses.
    legal_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    customer_type: Mapped[CustomerType] = mapped_column(String(16), nullable=False)

    # --- statutory identifiers ---------------------------------------------
    gstin: Mapped[str | None] = mapped_column(String(15), nullable=True)
    pan: Mapped[str | None] = mapped_column(String(10), nullable=True)

    # --- commercial terms ---------------------------------------------------
    #: Which ``item_prices.price_type`` applies to this account. Stored on the
    #: customer rather than derived from ``customer_type`` because the two are
    #: genuinely independent: an OEM account may sit on dealer pricing, and a
    #: dealer being trialled on list price is a normal commercial decision.
    price_list_type: Mapped[PriceType] = mapped_column(
        String(16), nullable=False, default=PriceType.SALE, server_default=PriceType.SALE.value
    )
    #: NULL means no limit was agreed; 0 means strictly no credit. The two are
    #: not the same thing - see ``CREDIT_LIMIT_NULL_MEANS_UNLIMITED`` in the
    #: service, where the distinction is enforced.
    credit_limit: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    #: Agreed payment term in days. Drives invoice due dates and the ageing
    #: report; 0 means payment against delivery.
    credit_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: Maintained exclusively by the finance module through
    #: ``apply_outstanding_delta``. Deliberately carries no ``>= 0`` constraint:
    #: an advance receipt or a credit note legitimately pushes the balance below
    #: zero, and a constraint here would reject a correct posting.
    outstanding_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    # --- status -------------------------------------------------------------
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    #: A credit hold, distinct from being inactive: a blocked customer still has
    #: open orders, open invoices and a history, it simply may not take on new
    #: exposure. The check constraints below refuse a block with no recorded
    #: reason or timestamp, because "why is this account on hold?" is the first
    #: question asked and the last thing anyone remembers.
    is_blocked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    block_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    blocked_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # --- ownership ----------------------------------------------------------
    #: The salesperson who owns the account. SET NULL rather than RESTRICT: a
    #: person leaving must not block their own deactivation, and an unassigned
    #: account is a visible gap for a manager to fill.
    assigned_sales_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    #: Default carrier for this customer's dispatches.
    #:
    #: Intentionally a bare UUID with NO foreign key to ``transporters``. That
    #: table is owned by the vendors module, and a database-level reference
    #: would make the customers table uncreatable in any environment where the
    #: vendors module has not been migrated yet - the two are independent master
    #: modules with no ordering guarantee between them, and a hard link would
    #: force one to be deployed before the other forever. Referential integrity
    #: is enforced in the service instead
    #: (``CustomerService._assert_transporter_exists``), which probes for the
    #: table before validating rather than failing a customer edit over a module
    #: that is not present.
    default_transporter_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # lazy="raise" (section 32): a customer list that touched either collection
    # per row is an N+1, and it must fail loudly in development rather than
    # quietly issue 200 queries in production. The detail endpoint loads them
    # with an explicit second query instead.
    addresses: Mapped[list[CustomerAddress]] = relationship(
        back_populates="customer", lazy="raise", cascade="all, delete-orphan"
    )
    contacts: Mapped[list[CustomerContact]] = relationship(
        back_populates="customer", lazy="raise", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_customers_company_id_code"),
        Index("ix_customers_company_active", "company_id", "is_active"),
        # The order-entry customer picker filters by channel and active state.
        Index("ix_customers_company_type", "company_id", "customer_type", "is_active"),
        # "Which accounts does this salesperson own?" - the sales dashboard's
        # primary query. Partial, because most rows in a small company are
        # unassigned and indexing those NULLs buys nothing.
        Index(
            "ix_customers_sales_user",
            "company_id",
            "assigned_sales_user_id",
            postgresql_where=text("assigned_sales_user_id IS NOT NULL AND deleted_at IS NULL"),
        ),
        # The credit-hold worklist. Tiny partial index: blocked accounts are a
        # handful of rows out of thousands.
        Index(
            "ix_customers_blocked",
            "company_id",
            postgresql_where=text("is_blocked AND deleted_at IS NULL"),
        ),
        # Two live customers sharing a GSTIN is a duplicate master, and it is
        # the duplicate that splits one legal entity's outstanding balance
        # across two credit limits - which makes the credit check on either row
        # wrong.
        Index(
            "uq_customers_company_gstin",
            "company_id",
            "gstin",
            unique=True,
            postgresql_where=text("gstin IS NOT NULL AND deleted_at IS NULL"),
        ),
        CheckConstraint(
            f"customer_type = ANY({sql_array_literal(CustomerType)})",
            name="customer_type_known",
        ),
        CheckConstraint(
            f"price_list_type = ANY({sql_array_literal(PriceType)})",
            name="price_list_type_known",
        ),
        CheckConstraint("credit_days >= 0", name="credit_days_non_negative"),
        CheckConstraint(
            "credit_limit IS NULL OR credit_limit >= 0", name="credit_limit_non_negative"
        ),
        CheckConstraint("is_blocked = false OR block_reason IS NOT NULL", name="block_has_reason"),
        CheckConstraint("is_blocked = false OR blocked_at IS NOT NULL", name="block_has_timestamp"),
    )


class CustomerAddress(TenantMasterEntity):
    """One billing or shipping address for a customer.

    ``state_code`` is the load-bearing column: GST is charged as CGST+SGST when
    the place of supply matches the seller's state and as IGST when it does not,
    so an invoice cannot be raised correctly without it. It is therefore
    mandatory for a domestic address (see ``domestic_address_has_state_code``)
    and optional for an export, where the concept does not apply.
    """

    __tablename__ = "customer_addresses"

    # CASCADE: an address has no independent existence. Historical documents
    # snapshot the address text they were raised against rather than pointing
    # here, precisely so a customer's address may be corrected without
    # rewriting last year's invoices. (Customers are soft-deleted in practice,
    # so this fires only on a genuine hard delete.)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )

    address_type: Mapped[AddressType] = mapped_column(String(16), nullable=False)
    #: What the warehouse team calls this address - "Bhiwandi godown", "Head
    #: office". Shown in the ship-to picker, where three addresses in the same
    #: city are otherwise indistinguishable.
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)

    address_line1: Mapped[str] = mapped_column(String(255), nullable=False)
    address_line2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    landmark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str] = mapped_column(String(120), nullable=False)
    state: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state_code: Mapped[str | None] = mapped_column(String(4), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    country: Mapped[str] = mapped_column(
        String(80), nullable=False, default="India", server_default="India"
    )

    #: A branch registered separately under GST has its own GSTIN, and the
    #: invoice must carry the one belonging to the place of supply - not the
    #: head-office number on the customer row.
    gstin: Mapped[str | None] = mapped_column(String(15), nullable=True)

    contact_person: Mapped[str | None] = mapped_column(String(160), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: The address pre-selected on a new document of this type. At most one per
    #: (customer, address_type) - enforced by the partial unique index below,
    #: not by application discipline.
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    customer: Mapped[Customer] = relationship(back_populates="addresses", lazy="raise")

    __table_args__ = (
        Index(
            "ix_customer_addresses_customer_type",
            "company_id",
            "customer_id",
            "address_type",
            "is_active",
        ),
        # Exactly one default per (customer, address_type). In the database
        # because "the service always clears the old default first" is a promise
        # two concurrent requests can break, and this cannot. The service
        # guarantees the other half - that a customer with addresses of a type
        # always has one of them flagged.
        Index(
            "uq_customer_addresses_one_default",
            "company_id",
            "customer_id",
            "address_type",
            unique=True,
            postgresql_where=text("is_default AND deleted_at IS NULL"),
        ),
        CheckConstraint(
            f"address_type = ANY({sql_array_literal(AddressType)})",
            name="address_type_known",
        ),
        CheckConstraint(
            f"state_code IS NULL OR state_code ~ '{STATE_CODE_SQL_PATTERN}'",
            name="state_code_is_two_digits",
        ),
        # An Indian address with no state code cannot be taxed correctly, so it
        # is refused at the column rather than discovered at invoicing time.
        CheckConstraint(
            "country <> 'India' OR state_code IS NOT NULL",
            name="domestic_address_has_state_code",
        ),
    )


class CustomerContact(TenantMasterEntity):
    """A named person at a customer."""

    __tablename__ = "customer_contacts"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    designation: Mapped[str | None] = mapped_column(String(120), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)

    #: Who an order confirmation or a payment chase goes to by default. At most
    #: one per live customer.
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    customer: Mapped[Customer] = relationship(back_populates="contacts", lazy="raise")

    __table_args__ = (
        Index("ix_customer_contacts_customer", "company_id", "customer_id", "is_active"),
        Index(
            "uq_customer_contacts_one_primary",
            "company_id",
            "customer_id",
            unique=True,
            postgresql_where=text("is_primary AND deleted_at IS NULL"),
        ),
        # A contact with neither an e-mail nor a phone number is a name nobody
        # can reach, which defeats the point of recording it.
        CheckConstraint(
            "email IS NOT NULL OR phone IS NOT NULL",
            name="contact_is_reachable",
        ),
    )


__all__ = [
    "STATE_CODE_SQL_PATTERN",
    "AddressType",
    "Customer",
    "CustomerAddress",
    "CustomerContact",
    "CustomerType",
    "PriceType",
]
