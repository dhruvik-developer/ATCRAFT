"""Vendor and transporter master data (master rules sections 15, 16).

Four tables, each with a distinct job:

``vendors``            the supplying party. One row per legal entity we buy
                       from, job-work with, or pay for a service.
``vendor_contacts``    the people at that vendor, each with a portal role that
                       mirrors the vendor-portal role vocabulary of section 16.
``vendor_categories``  which item categories a vendor is qualified for. This is
                       what backs the "Manager 1 sees Lock Body, Manager 2 sees
                       Handles" restriction: a vendor principal is scoped to its
                       vendor *and* to a subset of that vendor's categories.
``transporters``       carriers and couriers. Deliberately not vendors: a
                       transporter is referenced by a dispatch, not by a
                       purchase order, and conflating the two would force every
                       dispatch screen to filter a vendor list by type.

Why ``vendor_types`` is an array
--------------------------------
The same firm routinely supplies raw material *and* does job work for us, and a
transport contractor may also invoice for services. A single ``vendor_type``
discriminator would force either a duplicate vendor row - which splits the
payment ledger and the rating for one legal entity - or a lie. The array is
constrained to the known vocabulary and to at least one element by check
constraints, so it stays a controlled classification rather than free text.

Nothing here stores a purchase figure. Spend, outstanding balance and purchase
history are derived from ``purchase_orders`` / ``vendor_invoices`` / ``payments``
and are never denormalised onto the master row, where they would silently
diverge from the ledger.
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
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.models import TenantEntity, TenantMasterEntity
from src.core.types import GUID, Money


class VendorType(StrEnum):
    """What we buy from a vendor. A vendor may hold several of these at once.

    A *classification*, not a permission: no rule reads it to decide whether an
    operation is legal. It drives the vendor picker on each document type (a
    job-work order proposes JOB_WORK vendors first) and the procurement spend
    reports.
    """

    RAW_MATERIAL_SUPPLIER = "RAW_MATERIAL_SUPPLIER"
    COMPONENT_SUPPLIER = "COMPONENT_SUPPLIER"
    PACKING_MATERIAL_SUPPLIER = "PACKING_MATERIAL_SUPPLIER"
    JOB_WORK = "JOB_WORK"
    TRANSPORT = "TRANSPORT"
    SERVICE = "SERVICE"


class VendorContactRole(StrEnum):
    """Portal role of a named contact at the vendor (section 16).

    Mirrors the ``VENDOR_*`` role presets in :mod:`src.core.permissions`, so the
    contact captured on the master record and the portal login eventually
    provisioned for that person describe the same job.
    """

    OWNER = "OWNER"
    MANAGER = "MANAGER"
    DISPATCH = "DISPATCH"
    QC = "QC"
    ACCOUNTS = "ACCOUNTS"


class TransporterType(StrEnum):
    """Carrier shape. Drives the default tracking behaviour on a dispatch."""

    TRANSPORT = "TRANSPORT"
    COURIER = "COURIER"


def _sql_array_literal(values: type[StrEnum]) -> str:
    """Render a StrEnum as a PostgreSQL array literal for a check constraint.

    Generated from the enum rather than typed out so the constraint cannot drift
    from the Python vocabulary the rest of the module validates against.
    """
    members = ",".join(f"'{member.value}'" for member in values)
    return f"ARRAY[{members}]::varchar[]"


#: Highest rating the 0-5 scale can express. Referenced by the check constraint
#: and by the rating engine so both agree on the ceiling.
MAX_VENDOR_RATING = 5


class Vendor(TenantMasterEntity):
    """A supplying party: material, components, job work, transport or service."""

    __tablename__ = "vendors"

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Registered name as it appears on the GST certificate. Differs from the
    #: trading name often enough that invoices are rejected without it.
    legal_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: Several roles per vendor - see the module docstring. Constrained to the
    #: VendorType vocabulary and to at least one element below.
    vendor_types: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), nullable=False, server_default=text("'{}'::varchar[]")
    )

    # --- statutory identifiers ---------------------------------------------
    gstin: Mapped[str | None] = mapped_column(String(15), nullable=True)
    pan: Mapped[str | None] = mapped_column(String(10), nullable=True)
    #: Udyam / MSME registration. Not decoration: the MSMED Act caps payment
    #: terms for a registered micro or small enterprise, and finance needs the
    #: number on file to defend that term in an audit.
    msme_number: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # --- commercial terms ---------------------------------------------------
    payment_terms_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: NULL means "no limit agreed", which is not the same as a limit of zero -
    #: the latter blocks every new order.
    credit_limit: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    #: Default procurement lead time. An item-vendor pair may override it via
    #: ``item_vendor_links.lead_time_days``; MRP prefers the more specific value.
    lead_time_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="INR", server_default="INR"
    )

    # --- performance --------------------------------------------------------
    #: 0.00-5.00, computed by the rating engine from on-time delivery and QC
    #: rejection. NULL means "never rated", which a report must not render as
    #: zero - an unrated vendor is unknown, not bad.
    rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), nullable=True)
    rating_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: How many receipts the stored rating was computed from. Stored so a UI can
    #: show "3.8 from 12 receipts" and a reader can judge how much to trust it.
    rating_sample_size: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # --- approval -----------------------------------------------------------
    #: A vendor must be approved before a purchase order may be raised on it.
    #: The flag lives here rather than being derived from an approval document
    #: because procurement reads it on every PO create, and a join per check
    #: would be paid on the hottest path in the module.
    is_approved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    # --- bank details -------------------------------------------------------
    # Payments are made against these. Every change is audited with the account
    # number masked (see the service): a diverted-payment fraud starts with a
    # quiet edit here, and the audit trail is what catches it.
    bank_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    bank_branch: Mapped[str | None] = mapped_column(String(120), nullable=True)
    bank_account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: 34 characters covers an IBAN as well as an Indian account number, so a
    #: future overseas vendor does not need a migration.
    bank_account_number: Mapped[str | None] = mapped_column(String(34), nullable=True)
    bank_ifsc: Mapped[str | None] = mapped_column(String(11), nullable=True)
    bank_swift: Mapped[str | None] = mapped_column(String(11), nullable=True)

    # --- address ------------------------------------------------------------
    address_line1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address_line2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: GST place-of-supply code. Determines whether a purchase attracts
    #: CGST+SGST or IGST, so it is not merely part of the postal address.
    state_code: Mapped[str | None] = mapped_column(String(4), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    country: Mapped[str] = mapped_column(
        String(80), nullable=False, default="India", server_default="India"
    )

    # --- primary contact ----------------------------------------------------
    # Denormalised onto the vendor so a picker and a PO header need no join.
    # The full list of people lives in vendor_contacts.
    contact_person: Mapped[str | None] = mapped_column(String(160), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mobile: Mapped[str | None] = mapped_column(String(32), nullable=True)
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # lazy="raise" everywhere: a vendor list that touched these per row would be
    # an N+1, and it must fail in development rather than in production (§32).
    contacts: Mapped[list[VendorContact]] = relationship(
        back_populates="vendor", lazy="raise", cascade="all, delete-orphan"
    )
    categories: Mapped[list[VendorCategory]] = relationship(
        back_populates="vendor", lazy="raise", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_vendors_company_id_code"),
        Index("ix_vendors_company_active", "company_id", "is_active"),
        # The procurement picker asks for "approved, active vendors" on every
        # PO screen; this index answers it without touching the table.
        Index("ix_vendors_company_approved", "company_id", "is_approved", "is_active"),
        # GIN over the type array: "which vendors do job work?" is a containment
        # query (vendor_types @> ARRAY['JOB_WORK']) and a btree cannot serve it.
        # company_id is not in the index because mixing btree and gin columns
        # needs the btree_gin extension; the tenant filter is applied as a cheap
        # recheck on the far smaller candidate set.
        Index("ix_vendors_types", "vendor_types", postgresql_using="gin"),
        # Two live vendors sharing a GSTIN is a duplicate-master error, and it
        # is the one that produces two payment trails for one legal entity.
        Index(
            "uq_vendors_company_gstin",
            "company_id",
            "gstin",
            unique=True,
            postgresql_where=text("gstin IS NOT NULL AND deleted_at IS NULL"),
        ),
        CheckConstraint("cardinality(vendor_types) >= 1", name="vendor_types_not_empty"),
        CheckConstraint(
            f"vendor_types <@ {_sql_array_literal(VendorType)}",
            name="vendor_types_known",
        ),
        CheckConstraint("payment_terms_days >= 0", name="payment_terms_non_negative"),
        CheckConstraint("lead_time_days >= 0", name="lead_time_non_negative"),
        CheckConstraint("credit_limit IS NULL OR credit_limit >= 0", name="credit_limit_positive"),
        CheckConstraint(
            f"rating IS NULL OR (rating >= 0 AND rating <= {MAX_VENDOR_RATING})",
            name="rating_in_range",
        ),
        CheckConstraint("rating_sample_size >= 0", name="rating_sample_non_negative"),
        # An approved vendor without a recorded approval time cannot be
        # defended in an audit, so the database refuses the combination outright
        # rather than trusting every write path to remember.
        CheckConstraint(
            "is_approved = false OR approved_at IS NOT NULL",
            name="approval_has_timestamp",
        ),
    )


class VendorContact(TenantMasterEntity):
    """A named person at a vendor, with the portal role they would be given."""

    __tablename__ = "vendor_contacts"

    # CASCADE, unlike most links in this codebase: a contact has no independent
    # existence and is never quoted on a historical document, so it may not
    # outlive its vendor. (Vendors are soft-deleted in practice, so this fires
    # only on a genuine hard delete.)
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    designation: Mapped[str | None] = mapped_column(String(120), nullable=True)
    contact_role: Mapped[VendorContactRole] = mapped_column(String(16), nullable=False)

    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mobile: Mapped[str | None] = mapped_column(String(32), nullable=True)

    #: Who a PO or a chase-up e-mail goes to by default. At most one per vendor,
    #: enforced by a partial unique index below.
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    vendor: Mapped[Vendor] = relationship(back_populates="contacts", lazy="raise")

    __table_args__ = (
        Index("ix_vendor_contacts_vendor_active", "company_id", "vendor_id", "is_active"),
        Index("ix_vendor_contacts_role", "company_id", "vendor_id", "contact_role"),
        # Exactly one primary contact per live vendor. In the database because
        # "we always clear the old one first" is a promise the application can
        # break under two concurrent requests, and this cannot.
        Index(
            "uq_vendor_contacts_one_primary",
            "company_id",
            "vendor_id",
            unique=True,
            postgresql_where=text("is_primary AND deleted_at IS NULL"),
        ),
        # A contact with no e-mail and no phone number is a name nobody can
        # reach, which defeats the point of recording it.
        CheckConstraint(
            "email IS NOT NULL OR phone IS NOT NULL OR mobile IS NOT NULL",
            name="contact_is_reachable",
        ),
        CheckConstraint(
            f"contact_role = ANY({_sql_array_literal(VendorContactRole)})",
            name="contact_role_known",
        ),
    )


class VendorCategory(TenantEntity):
    """Which item categories a vendor is qualified to supply.

    ``TenantEntity`` rather than ``TenantMasterEntity``: this is a pure
    association row that no document quotes, so revoking a qualification deletes
    it outright. The revocation is still recorded in the audit log, which is
    where the history of "who was allowed to supply what, when" belongs.

    Also the scoping table for the vendor portal (section 16): a vendor user
    carries ``allowed_category_ids``, and those must be a subset of the rows
    here, so a vendor manager restricted to Lock Body cannot read a Handles
    order.
    """

    __tablename__ = "vendor_categories"

    vendor_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # RESTRICT: a category that vendors are qualified against must not be
    # removable out from under them - the qualification would vanish silently.
    item_category_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("item_categories.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    #: The vendor procurement should approach first for this category. At most
    #: one per category - see the partial unique index.
    is_preferred: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    vendor: Mapped[Vendor] = relationship(back_populates="categories", lazy="raise")

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "vendor_id",
            "item_category_id",
            name="uq_vendor_categories_company_id_vendor_id_item_category_id",
        ),
        # "Who supplies this category?" - the query the sourcing screen and the
        # MRP purchase suggestion both run.
        Index("ix_vendor_categories_category", "company_id", "item_category_id"),
        # ASSUMPTION: at most one preferred vendor per item category, mirroring
        # the one-preferred-vendor-per-item rule already enforced on
        # item_vendor_links. Two "preferred" vendors for one category gives the
        # sourcing screen no basis to choose, so the ambiguity is refused here.
        Index(
            "uq_vendor_categories_one_preferred",
            "company_id",
            "item_category_id",
            unique=True,
            postgresql_where=text("is_preferred"),
        ),
    )


class Transporter(TenantMasterEntity):
    """A carrier or courier used on dispatches and inbound freight."""

    __tablename__ = "transporters"

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    transporter_type: Mapped[TransporterType] = mapped_column(String(16), nullable=False)

    gstin: Mapped[str | None] = mapped_column(String(15), nullable=True)
    pan: Mapped[str | None] = mapped_column(String(10), nullable=True)

    contact_person: Mapped[str | None] = mapped_column(String(160), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mobile: Mapped[str | None] = mapped_column(String(32), nullable=True)
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)

    address_line1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state_code: Mapped[str | None] = mapped_column(String(4), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    country: Mapped[str] = mapped_column(
        String(80), nullable=False, default="India", server_default="India"
    )

    #: URL with a literal ``{tracking_id}`` placeholder, e.g.
    #: ``https://carrier.example/track?awb={tracking_id}``. Storing the template
    #: rather than per-consignment links means a carrier changing its tracking
    #: URL is one master-data edit, not a rewrite of every historical dispatch.
    #: The check constraint refuses a template with no placeholder, because such
    #: a template silently produces the same useless link for every consignment.
    tracking_url_template: Mapped[str | None] = mapped_column(String(500), nullable=True)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_transporters_company_id_code"),
        Index("ix_transporters_company_active", "company_id", "is_active"),
        Index("ix_transporters_company_type", "company_id", "transporter_type"),
        # strpos rather than LIKE '%{tracking_id}%': SQLAlchemy escapes a literal
        # percent sign in a textual constraint for the driver's paramstyle, and
        # the doubled form can survive into the emitted DDL. This formulation
        # has no percent sign to escape and means exactly the same thing.
        CheckConstraint(
            "tracking_url_template IS NULL "
            "OR strpos(tracking_url_template, '{tracking_id}') > 0",
            name="tracking_template_has_placeholder",
        ),
        CheckConstraint(
            f"transporter_type = ANY({_sql_array_literal(TransporterType)})",
            name="transporter_type_known",
        ),
    )


__all__ = [
    "MAX_VENDOR_RATING",
    "Transporter",
    "TransporterType",
    "Vendor",
    "VendorCategory",
    "VendorContact",
    "VendorContactRole",
    "VendorType",
]
