"""Queries for vendors, their contacts, their category qualifications and
transporters.

Everything that touches the database lives here; the service decides *whether*
an operation is allowed, this file decides *how* it is read or written.

Three things are worth reading twice:

* :meth:`VendorRepository._scoped` narrows to the caller's own vendor when the
  principal is a vendor-portal user, on top of the tenant filter. A vendor user
  asking for another vendor gets a 404, not a 200 (sections 16, 39).
* The counts and category lookups are **batched for a whole page** rather than
  issued per row - the difference between one query and fifty on a list screen.
* :func:`open_purchase_order_exists` reaches into ``purchase_orders`` with raw
  SQL guarded by ``to_regclass`` instead of importing the procurement module.
  Master data sits *below* procurement in the dependency order, and the table
  does not exist yet in a partially migrated database.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, and_, func, literal, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from src.common.repository import BaseRepository
from src.core.database import affected_rows
from src.core.pagination import SortParams

# Read-only references to other modules' master tables. Vendors validate that a
# category or a user belongs to the same tenant before pointing at it; they
# never write to those tables.
from src.modules.products.models import ItemCategory
from src.modules.users.models import User
from src.modules.vendors.models import (
    Transporter,
    Vendor,
    VendorCategory,
    VendorContact,
)
from src.modules.vendors.schemas import (
    TransporterFilters,
    VendorCategoryFilters,
    VendorContactFilters,
    VendorFilters,
)

#: Purchase-order statuses that still need the vendor. Anything past CLOSED or
#: CANCELLED is history and does not block retiring the vendor record.
#: Vocabulary from the purchase-order state machine in the database contract §7.
#: VENDOR_DECLINED is open: the buyer still has to cancel the order or send it
#: back to this vendor (docs/assumptions.md §C1-10).
OPEN_PURCHASE_ORDER_STATUSES = (
    "DRAFT",
    "PENDING_APPROVAL",
    "APPROVED",
    "SENT",
    "VENDOR_DECLINED",
    "ACKNOWLEDGED",
    "PARTIALLY_RECEIVED",
    "RECEIVED",
    "QC_PENDING",
    "QC_COMPLETED",
    "GRN_COMPLETED",
    "INVOICED",
)


async def table_exists(session: AsyncSession, table_name: str) -> bool:
    """Is ``table_name`` present in the ``public`` schema of this database?

    ``to_regclass`` returns NULL rather than raising for an unknown relation,
    which is the only way to ask without aborting the surrounding transaction.
    It must be its own round trip: a statement that *references* a missing table
    fails at parse time, so the probe cannot be folded into the query it guards.
    """
    found = await session.execute(
        text("SELECT to_regclass(:qualified)"), {"qualified": f"public.{table_name}"}
    )
    return found.scalar() is not None


async def columns_present(session: AsyncSession, table_name: str, required: Sequence[str]) -> bool:
    """Does ``table_name`` carry every column in ``required``?

    The rating adapter reads tables owned by modules that are still being
    built. Probing the columns as well as the table means a schema that has
    moved on degrades to "insufficient data" instead of raising an
    UndefinedColumn error at request time.
    """
    if not required:
        return True
    found = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :table_name"
        ),
        {"table_name": table_name},
    )
    available = {row[0] for row in found.all()}
    return set(required).issubset(available)


class VendorRepository(BaseRepository[Vendor]):
    """Persistence for :class:`~src.modules.vendors.models.Vendor`."""

    model = Vendor
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": Vendor.code,
        "name": Vendor.name,
        "rating": Vendor.rating,
        "lead_time_days": Vendor.lead_time_days,
        "payment_terms_days": Vendor.payment_terms_days,
        "is_active": Vendor.is_active,
        "is_approved": Vendor.is_approved,
        "created_at": Vendor.created_at,
        "updated_at": Vendor.updated_at,
    }
    searchable_fields = ("code", "name", "legal_name", "gstin", "city")

    def _scoped(self, *, include_deleted: bool = False) -> Select[tuple[Vendor]]:
        """Tenant filter plus the vendor-portal restriction.

        A principal with ``vendor_id`` set is a vendor's own staff and may see
        exactly one vendor row: its own. Applied here rather than in each
        endpoint so no caller can forget it (section 16).
        """
        stmt = super()._scoped(include_deleted=include_deleted)
        if self.tenant.vendor_id is not None:
            stmt = stmt.where(Vendor.id == self.tenant.vendor_id)
        return stmt

    # --- reads --------------------------------------------------------------

    def build_list_stmt(self, filters: VendorFilters, sort: SortParams) -> Select[Any]:
        stmt = self._scoped(include_deleted=filters.include_deleted)
        stmt = self._apply_search(stmt, filters.search)

        if filters.vendor_type is not None:
            # Array containment, which the GIN index on vendor_types serves.
            # ``==`` on an array column would demand an exact match of the whole
            # set, which is never what "show me the job workers" means.
            stmt = stmt.where(Vendor.vendor_types.contains([str(filters.vendor_type)]))
        if filters.is_active is not None:
            stmt = stmt.where(Vendor.is_active == filters.is_active)
        if filters.is_approved is not None:
            stmt = stmt.where(Vendor.is_approved == filters.is_approved)
        if filters.state_code is not None:
            stmt = stmt.where(Vendor.state_code == filters.state_code)
        if filters.min_rating is not None:
            # An unrated vendor is excluded rather than treated as zero: NULL
            # means "not yet measured", which is not the same as "bad".
            stmt = stmt.where(Vendor.rating.is_not(None), Vendor.rating >= filters.min_rating)
        if filters.item_category_id is not None:
            # EXISTS rather than a join: a vendor qualified for several
            # categories would otherwise appear once per matching row.
            stmt = stmt.where(
                select(literal(1))
                .select_from(VendorCategory)
                .where(
                    VendorCategory.vendor_id == Vendor.id,
                    VendorCategory.company_id == self.tenant.company_id,
                    VendorCategory.item_category_id == filters.item_category_id,
                )
                .exists()
            )

        default_order = sort.resolve(self.sortable_fields, Vendor.code.asc())
        return stmt.order_by(default_order, Vendor.id.asc())

    async def code_taken(self, code: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        """Is this code used in the tenant, including by a retired vendor?

        Retired rows count: their code is still quoted on historical purchase
        orders, so reusing it would make two different vendors read as one in
        the archive.
        """
        stmt = (
            select(literal(1))
            .select_from(Vendor)
            .where(Vendor.company_id == self.tenant.company_id, Vendor.code == code)
        )
        if exclude_id is not None:
            stmt = stmt.where(Vendor.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def gstin_taken(self, gstin: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        """Is another *live* vendor already registered under this GSTIN?

        Scoped to live rows only, matching the partial unique index: a retired
        vendor's GSTIN may legitimately be reused when the same legal entity is
        re-onboarded.
        """
        stmt = (
            select(literal(1))
            .select_from(Vendor)
            .where(
                Vendor.company_id == self.tenant.company_id,
                Vendor.gstin == gstin,
                Vendor.deleted_at.is_(None),
            )
        )
        if exclude_id is not None:
            stmt = stmt.where(Vendor.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def list_options(
        self,
        *,
        vendor_type: str | None = None,
        approved_only: bool = True,
        limit: int = 500,
    ) -> list[Vendor]:
        """Active vendors for a picker.

        Bounded and index-served, so it is returned whole rather than paginated:
        a paginated dropdown is worse for the user and no cheaper for the
        database. The hard ``limit`` is the safety net that keeps "whole" from
        meaning "unbounded" on a tenant with an unusually long vendor list.
        """
        stmt = self._scoped().where(Vendor.is_active.is_(True))
        if approved_only:
            stmt = stmt.where(Vendor.is_approved.is_(True))
        if vendor_type is not None:
            stmt = stmt.where(Vendor.vendor_types.contains([vendor_type]))
        stmt = stmt.order_by(Vendor.name.asc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def contact_counts(self, vendor_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        """Live contact count per vendor, for a whole page in one query."""
        if not vendor_ids:
            return {}
        stmt = (
            select(VendorContact.vendor_id, func.count())
            .where(
                VendorContact.company_id == self.tenant.company_id,
                VendorContact.vendor_id.in_(list(vendor_ids)),
                VendorContact.deleted_at.is_(None),
            )
            .group_by(VendorContact.vendor_id)
        )
        return {row[0]: int(row[1]) for row in (await self.session.execute(stmt)).all()}

    async def category_counts(self, vendor_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        """Category-qualification count per vendor, for a whole page in one query."""
        if not vendor_ids:
            return {}
        stmt = (
            select(VendorCategory.vendor_id, func.count())
            .where(
                VendorCategory.company_id == self.tenant.company_id,
                VendorCategory.vendor_id.in_(list(vendor_ids)),
            )
            .group_by(VendorCategory.vendor_id)
        )
        return {row[0]: int(row[1]) for row in (await self.session.execute(stmt)).all()}

    async def category_exists(self, item_category_id: uuid.UUID) -> bool:
        """Tenant-scoped existence probe against ``item_categories``.

        Scoped on purpose: a foreign key only proves the row exists *somewhere*,
        and pointing a vendor qualification at another company's category would
        be a tenant leak the database would happily accept.
        """
        stmt = (
            select(literal(1))
            .select_from(ItemCategory)
            .where(
                ItemCategory.company_id == self.tenant.company_id,
                ItemCategory.id == item_category_id,
                ItemCategory.deleted_at.is_(None),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def has_active_portal_users(self, vendor_id: uuid.UUID) -> bool:
        """Are there live vendor-portal logins bound to this vendor?

        Retiring a vendor whose staff can still sign in would leave accounts
        with a live session against a record the buyer believes is closed.
        """
        stmt = (
            select(literal(1))
            .select_from(User)
            .where(
                User.company_id == self.tenant.company_id,
                User.vendor_id == vendor_id,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def has_item_links(self, vendor_id: uuid.UUID) -> bool:
        """Is this vendor still named as a source on any item?

        ``item_vendor_links`` is owned by the products module and is imported
        lazily so that a deployment without the products tables (there is none
        today, but the dependency direction should stay one-way) does not fail
        at import time.
        """
        from src.modules.products.models import ItemVendorLink

        stmt = (
            select(literal(1))
            .select_from(ItemVendorLink)
            .where(
                ItemVendorLink.company_id == self.tenant.company_id,
                ItemVendorLink.vendor_id == vendor_id,
                ItemVendorLink.deleted_at.is_(None),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def has_open_purchase_orders(self, vendor_id: uuid.UUID) -> bool:
        """Does this vendor still owe us, or we them, on an open order?

        Deliberately not an import of the procurement module: master data sits
        below procurement in the dependency order, and inverting that would make
        the two impossible to build or test independently. Returns False when
        ``purchase_orders`` does not exist yet, which is correct - a table that
        has never been created cannot hold an open order.
        """
        if not await table_exists(self.session, "purchase_orders"):
            return False
        if not await columns_present(
            self.session, "purchase_orders", ("company_id", "vendor_id", "status")
        ):
            return False

        found = await self.session.execute(
            text(
                "SELECT 1 FROM purchase_orders "
                "WHERE company_id = :company_id AND vendor_id = :vendor_id "
                "AND status = ANY(:statuses) LIMIT 1"
            ),
            {
                "company_id": self.tenant.company_id,
                "vendor_id": vendor_id,
                "statuses": list(OPEN_PURCHASE_ORDER_STATUSES),
            },
        )
        return found.first() is not None


class VendorContactRepository(BaseRepository[VendorContact]):
    """Persistence for vendor contacts."""

    model = VendorContact
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "name": VendorContact.name,
        "contact_role": VendorContact.contact_role,
        "is_primary": VendorContact.is_primary,
        "is_active": VendorContact.is_active,
        "created_at": VendorContact.created_at,
    }
    searchable_fields = ("name", "email", "phone", "mobile")

    def _scoped(self, *, include_deleted: bool = False) -> Select[tuple[VendorContact]]:
        stmt = super()._scoped(include_deleted=include_deleted)
        if self.tenant.vendor_id is not None:
            stmt = stmt.where(VendorContact.vendor_id == self.tenant.vendor_id)
        return stmt

    def build_list_stmt(self, filters: VendorContactFilters, sort: SortParams) -> Select[Any]:
        stmt = self._scoped(include_deleted=filters.include_deleted)
        stmt = self._apply_search(stmt, filters.search)

        if filters.vendor_id is not None:
            stmt = stmt.where(VendorContact.vendor_id == filters.vendor_id)
        if filters.contact_role is not None:
            stmt = stmt.where(VendorContact.contact_role == filters.contact_role)
        if filters.is_active is not None:
            stmt = stmt.where(VendorContact.is_active == filters.is_active)

        default_order = sort.resolve(self.sortable_fields, VendorContact.is_primary.desc())
        return stmt.order_by(default_order, VendorContact.name.asc(), VendorContact.id.asc())

    async def clear_primary(self, vendor_id: uuid.UUID, *, keep_id: uuid.UUID | None = None) -> int:
        """Demote every other primary contact of this vendor.

        One UPDATE rather than a read-then-write loop: the partial unique index
        would reject a second primary anyway, and a single statement means the
        window in which two rows are flagged never exists.
        """
        stmt = (
            update(VendorContact)
            .where(
                VendorContact.company_id == self.tenant.company_id,
                VendorContact.vendor_id == vendor_id,
                VendorContact.is_primary.is_(True),
                VendorContact.deleted_at.is_(None),
            )
            .values(is_primary=False, updated_by_id=self.tenant.user_id)
            .execution_options(synchronize_session=False)
        )
        if keep_id is not None:
            stmt = stmt.where(VendorContact.id != keep_id)
        return affected_rows(await self.session.execute(stmt))

    async def role_taken(
        self,
        vendor_id: uuid.UUID,
        name: str,
        contact_role: str,
        *,
        exclude_id: uuid.UUID | None = None,
    ) -> bool:
        """Guard against the same person being entered twice in the same role.

        Case-insensitive on the name because "Rajesh Kumar" and "rajesh kumar"
        are one person, and two rows for them mean two copies of every e-mail.
        """
        stmt = (
            select(literal(1))
            .select_from(VendorContact)
            .where(
                VendorContact.company_id == self.tenant.company_id,
                VendorContact.vendor_id == vendor_id,
                VendorContact.contact_role == contact_role,
                func.lower(VendorContact.name) == name.lower(),
                VendorContact.deleted_at.is_(None),
            )
        )
        if exclude_id is not None:
            stmt = stmt.where(VendorContact.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None


class VendorCategoryRepository(BaseRepository[VendorCategory]):
    """Persistence for vendor category qualifications."""

    model = VendorCategory
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "is_preferred": VendorCategory.is_preferred,
        "created_at": VendorCategory.created_at,
    }

    def _scoped(self, *, include_deleted: bool = False) -> Select[tuple[VendorCategory]]:
        stmt = super()._scoped(include_deleted=include_deleted)
        if self.tenant.vendor_id is not None:
            stmt = stmt.where(VendorCategory.vendor_id == self.tenant.vendor_id)
            # A vendor user restricted to specific categories sees only those.
            # This is the query that makes "Manager 1 -> Lock Body, Manager 2 ->
            # Handles" real rather than a UI convention (section 16).
            if self.tenant.allowed_category_ids:
                stmt = stmt.where(
                    VendorCategory.item_category_id.in_(list(self.tenant.allowed_category_ids))
                )
        return stmt

    def build_list_stmt(self, filters: VendorCategoryFilters, sort: SortParams) -> Select[Any]:
        stmt = self._scoped()
        if filters.vendor_id is not None:
            stmt = stmt.where(VendorCategory.vendor_id == filters.vendor_id)
        if filters.item_category_id is not None:
            stmt = stmt.where(VendorCategory.item_category_id == filters.item_category_id)
        if filters.is_preferred is not None:
            stmt = stmt.where(VendorCategory.is_preferred == filters.is_preferred)

        default_order = sort.resolve(self.sortable_fields, VendorCategory.is_preferred.desc())
        return stmt.order_by(default_order, VendorCategory.id.asc())

    async def get_link(
        self, vendor_id: uuid.UUID, item_category_id: uuid.UUID
    ) -> VendorCategory | None:
        stmt = self._scoped().where(
            VendorCategory.vendor_id == vendor_id,
            VendorCategory.item_category_id == item_category_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def category_labels(
        self, category_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[str, str]]:
        """Code and name per category id, for a whole page in one query.

        Only the two columns the response needs are selected: the category row
        carries a materialised path that no caller of this method reads.
        """
        if not category_ids:
            return {}
        stmt = select(ItemCategory.id, ItemCategory.code, ItemCategory.name).where(
            ItemCategory.company_id == self.tenant.company_id,
            ItemCategory.id.in_(list(category_ids)),
        )
        rows = (await self.session.execute(stmt)).all()
        return {row[0]: (row[1], row[2]) for row in rows}

    async def clear_preferred(
        self, item_category_id: uuid.UUID, *, keep_id: uuid.UUID | None = None
    ) -> int:
        """Demote whichever vendor is currently preferred for this category."""
        stmt = (
            update(VendorCategory)
            .where(
                VendorCategory.company_id == self.tenant.company_id,
                VendorCategory.item_category_id == item_category_id,
                VendorCategory.is_preferred.is_(True),
            )
            .values(is_preferred=False, updated_by_id=self.tenant.user_id)
            .execution_options(synchronize_session=False)
        )
        if keep_id is not None:
            stmt = stmt.where(VendorCategory.id != keep_id)
        return affected_rows(await self.session.execute(stmt))

    async def delete_for_vendor(self, vendor_id: uuid.UUID) -> int:
        """Drop every qualification of a vendor in one statement.

        Called when the vendor is retired: a qualification that outlives its
        vendor would keep the vendor visible in category-based sourcing.
        """
        from sqlalchemy import delete

        stmt = delete(VendorCategory).where(
            VendorCategory.company_id == self.tenant.company_id,
            VendorCategory.vendor_id == vendor_id,
        )
        return affected_rows(await self.session.execute(stmt))


class TransporterRepository(BaseRepository[Transporter]):
    """Persistence for :class:`~src.modules.vendors.models.Transporter`."""

    model = Transporter
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": Transporter.code,
        "name": Transporter.name,
        "transporter_type": Transporter.transporter_type,
        "is_active": Transporter.is_active,
        "created_at": Transporter.created_at,
        "updated_at": Transporter.updated_at,
    }
    searchable_fields = ("code", "name", "gstin", "city")

    def build_list_stmt(self, filters: TransporterFilters, sort: SortParams) -> Select[Any]:
        stmt = self._scoped(include_deleted=filters.include_deleted)
        stmt = self._apply_search(stmt, filters.search)

        if filters.transporter_type is not None:
            stmt = stmt.where(Transporter.transporter_type == filters.transporter_type)
        if filters.is_active is not None:
            stmt = stmt.where(Transporter.is_active == filters.is_active)

        default_order = sort.resolve(self.sortable_fields, Transporter.code.asc())
        return stmt.order_by(default_order, Transporter.id.asc())

    async def code_taken(self, code: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        stmt = (
            select(literal(1))
            .select_from(Transporter)
            .where(Transporter.company_id == self.tenant.company_id, Transporter.code == code)
        )
        if exclude_id is not None:
            stmt = stmt.where(Transporter.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def list_options(
        self, *, transporter_type: str | None = None, limit: int = 500
    ) -> list[Transporter]:
        stmt = self._scoped().where(Transporter.is_active.is_(True))
        if transporter_type is not None:
            stmt = stmt.where(Transporter.transporter_type == transporter_type)
        stmt = stmt.order_by(Transporter.name.asc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def has_open_dispatches(self, transporter_id: uuid.UUID) -> bool:
        """Is this carrier still named on a consignment that has not arrived?

        Same reasoning as :meth:`VendorRepository.has_open_purchase_orders`:
        raw SQL behind a ``to_regclass`` probe, because ``dispatches`` belongs
        to a module that sits above master data and may not exist yet.
        """
        if not await table_exists(self.session, "dispatches"):
            return False
        if not await columns_present(
            self.session, "dispatches", ("company_id", "transporter_id", "status")
        ):
            return False

        found = await self.session.execute(
            text(
                "SELECT 1 FROM dispatches "
                "WHERE company_id = :company_id AND transporter_id = :transporter_id "
                "AND status = ANY(:statuses) LIMIT 1"
            ),
            {
                "company_id": self.tenant.company_id,
                "transporter_id": transporter_id,
                # Everything before DELIVERED: the goods are still with the
                # carrier, so the master row is still needed operationally.
                "statuses": [
                    "NOT_DISPATCHED",
                    "READY_FOR_DISPATCH",
                    "DISPATCHED",
                    "IN_TRANSIT",
                ],
            },
        )
        return found.first() is not None


def vendor_type_predicate(*vendor_types: str) -> Any:
    """Reusable "holds any of these types" predicate.

    Exposed for other modules (procurement's vendor picker, MRP's purchase
    suggestion) so the array-containment form - the one the GIN index can serve
    - is written once rather than re-derived, occasionally wrongly, per caller.
    """
    if not vendor_types:
        raise ValueError("vendor_type_predicate needs at least one vendor type")
    clauses = [Vendor.vendor_types.contains([vendor_type]) for vendor_type in vendor_types]
    return clauses[0] if len(clauses) == 1 else or_(*clauses)


def approved_and_active() -> Any:
    """The predicate procurement must apply before raising an order on a vendor."""
    return and_(Vendor.is_approved.is_(True), Vendor.is_active.is_(True))


__all__ = [
    "OPEN_PURCHASE_ORDER_STATUSES",
    "TransporterRepository",
    "VendorCategoryRepository",
    "VendorContactRepository",
    "VendorRepository",
    "approved_and_active",
    "columns_present",
    "table_exists",
    "vendor_type_predicate",
]
