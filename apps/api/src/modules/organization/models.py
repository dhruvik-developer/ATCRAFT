"""Organisation masters: company, branch, plant, department, cost centre.

Master rules section 9. ``companies`` is the tenant root - it is the one table
that does not carry ``company_id``, because it *is* the tenant. Everything else
in the schema hangs off it (section 40), so multi-company operation stays
possible even while the deployment serves a single company.

``cost_centres`` sits here, at the bottom of the dependency order, so every
module that carries one - routing, production, job work, inventory, finance -
may reference it without reaching upward (docs/architecture.md).
"""

from __future__ import annotations

import uuid
from datetime import date
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.constants import CURRENCY_CODE_LENGTH, DEFAULT_CURRENCY
from src.core.models import (
    Base,
    SoftDeleteMixin,
    TenantMasterEntity,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    sql_in_list,
)
from src.core.types import GUID


class CostCentreType(StrEnum):
    """How a cost centre is classified on a report. Nothing may branch on it.

    The same rule as ``items.item_group``: a reporting classification, never a
    switch. A rule that needs to know whether a centre is "production" reads
    the carrier it is attached to (a work centre, a production order), not this
    column - otherwise reclassifying a centre would silently change behaviour.
    """

    PRODUCTION = "PRODUCTION"
    SERVICE = "SERVICE"
    ADMINISTRATION = "ADMINISTRATION"
    SELLING = "SELLING"


class Company(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """The tenant boundary."""

    __tablename__ = "companies"

    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Indian statutory identifiers - the deployment context is a manufacturer
    # invoicing under GST.
    gstin: Mapped[str | None] = mapped_column(String(15), nullable=True)
    pan: Mapped[str | None] = mapped_column(String(10), nullable=True)
    cin: Mapped[str | None] = mapped_column(String(21), nullable=True)

    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)

    address_line1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address_line2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state_code: Mapped[str | None] = mapped_column(String(4), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    country: Mapped[str] = mapped_column(String(80), nullable=False, default="India")

    currency: Mapped[str] = mapped_column(
        String(CURRENCY_CODE_LENGTH), nullable=False, default=DEFAULT_CURRENCY
    )
    financial_year_start_month: Mapped[int] = mapped_column(nullable=False, default=4)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Kolkata")

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    logo_document_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    # --- bank details and terms printed on outgoing invoices -----------------
    # All optional: a company that has not entered them yet still invoices, the
    # document simply omits the block. Every change is audited with the account
    # number masked - redirecting where customers pay is the fraud these fields
    # invite, and the audit row is what catches it.
    bank_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    bank_account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: 34 characters covers an IBAN as well as an Indian account number.
    bank_account_number: Mapped[str | None] = mapped_column(String(34), nullable=True)
    bank_ifsc: Mapped[str | None] = mapped_column(String(11), nullable=True)
    bank_branch: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: Payment and delivery terms printed beneath the totals.
    invoice_terms: Mapped[str | None] = mapped_column(Text, nullable=True)

    branches: Mapped[list[Branch]] = relationship(back_populates="company", lazy="raise")

    __table_args__ = (Index("ix_companies_active", "is_active"),)


class Branch(TenantMasterEntity):
    """A selling/administrative location."""

    __tablename__ = "branches"

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    gstin: Mapped[str | None] = mapped_column(String(15), nullable=True)

    address_line1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state_code: Mapped[str | None] = mapped_column(String(4), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_head_office: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    company: Mapped[Company] = relationship(back_populates="branches", lazy="raise")

    __table_args__ = (
        # Codes are unique per tenant, never globally (section 40).
        UniqueConstraint("company_id", "code", name="uq_branches_company_id_code"),
        Index("ix_branches_company_active", "company_id", "is_active"),
    )


class Plant(TenantMasterEntity):
    """A manufacturing site. Production orders and work centres belong to one."""

    __tablename__ = "plants"

    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    address_line1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_plants_company_id_code"),
        Index("ix_plants_company_active", "company_id", "is_active"),
    )


class Department(TenantMasterEntity):
    """An organisational unit. Used for cost attribution and approval routing."""

    __tablename__ = "departments"

    plant_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("plants.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("departments.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    head_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: The cost centre a document raised by this department's people defaults
    #: to (requisitions, from WP B3). NULL means "no default" - the document
    #: is then unallocated unless somebody names one explicitly.
    cost_centre_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("cost_centres.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_departments_company_id_code"),
    )


class CostCentre(TenantMasterEntity):
    """A unit costs are collected against for management reporting.

    A tree, like a department, but a different question: a department says who
    people report to, a cost centre says where money is charged. They often
    line up, which is why a department may name a default cost centre, but they
    are not the same thing - a job-worked plating process is a cost centre with
    no department at all.

    Carried, nullable everywhere, by work centres, departments (as a default),
    production orders, job-work orders, stock adjustments, vendor invoices and
    their lines, and - the analytic source of truth - ``ledger_entries``. NULL
    on any of them means *unallocated*, never "the default centre".
    """

    __tablename__ = "cost_centres"

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: RESTRICT: a parent with children must not disappear from under them,
    #: and soft deletion is refused while live children exist.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("cost_centres.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    #: Reporting only - see :class:`CostCentreType`.
    cost_centre_type: Mapped[CostCentreType] = mapped_column(String(24), nullable=False)
    plant_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("plants.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    #: The person answerable for the centre's spend.
    head_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_cost_centres_company_id_code"),
        Index("ix_cost_centres_company_active", "company_id", "is_active"),
        # A centre cannot be its own parent. Deeper cycles are refused by the
        # service, which walks the chain - a CHECK cannot see beyond one row.
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="parent_not_self"),
        CheckConstraint(
            f"cost_centre_type IN ({sql_in_list(CostCentreType)})",
            name="cost_centre_type_known",
        ),
    )


class FinancialYear(TenantMasterEntity):
    """Named accounting period.

    Document numbering is scoped to it (``PO/2026-27/00001``), and closing a year
    is what stops back-dated postings into a period already reported.
    """

    __tablename__ = "financial_years"

    code: Mapped[str] = mapped_column(String(16), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_financial_years_company_id_code"),
        Index("ix_financial_years_range", "company_id", "start_date", "end_date"),
    )


__all__ = [
    "Branch",
    "Company",
    "CostCentre",
    "CostCentreType",
    "Department",
    "FinancialYear",
    "Plant",
]
