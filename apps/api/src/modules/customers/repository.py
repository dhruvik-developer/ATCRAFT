"""Queries for the customer master.

Everything that touches the database lives here; the service decides *whether*
an operation is allowed, this file decides *how* it is read or written.

Three choices are worth reading twice:

* **List rows load an explicit column set.** ``load_only(..., raiseload=True)``
  means the SELECT names the columns the grid actually renders - notes and the
  block narrative are not among them - and touching an unloaded attribute
  raises instead of silently issuing another query per row (section 30).
* **Address and contact lists are fetched by customer id in one statement.**
  The detail endpoint asks for both once; nothing iterates rows issuing a query
  each (section 32).
* **The transporter check is a raw-SQL probe, not an import.** ``transporters``
  belongs to the vendors module, and ``customers`` deliberately holds no
  foreign key to it (see ``Customer.default_transporter_id``). Probing with
  ``to_regclass`` first means a database where that module has not been
  migrated yet answers "cannot verify" instead of aborting the transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, exists, select, text, update
from sqlalchemy.orm import InstrumentedAttribute, load_only

from src.common.repository import BaseRepository
from src.core.clock import utcnow
from src.core.database import affected_rows
from src.core.pagination import SortParams
from src.modules.customers.models import (
    AddressType,
    Customer,
    CustomerAddress,
    CustomerContact,
)
from src.modules.customers.schemas import CustomerFilters

# Read-only reference to another module's master table: a customer may be
# assigned to a salesperson, but the customers module never writes to users.
from src.modules.users.models import User

#: Columns a customer list row renders. ``notes`` and the block narrative are
#: absent on purpose - they are detail-view fields and both are unbounded text.
CUSTOMER_SUMMARY_COLUMNS: tuple[InstrumentedAttribute[Any], ...] = (
    Customer.code,
    Customer.name,
    Customer.legal_name,
    Customer.customer_type,
    Customer.price_list_type,
    Customer.gstin,
    Customer.credit_limit,
    Customer.credit_days,
    Customer.outstanding_amount,
    Customer.is_active,
    Customer.is_blocked,
    Customer.assigned_sales_user_id,
    # The default list order and the pagination tiebreaker read this.
    Customer.created_at,
)


class CustomerRepository(BaseRepository[Customer]):
    """Persistence for :class:`~src.modules.customers.models.Customer`."""

    model = Customer
    searchable_fields = ("code", "name", "legal_name", "gstin")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": Customer.code,
        "name": Customer.name,
        "customer_type": Customer.customer_type,
        "credit_limit": Customer.credit_limit,
        "outstanding_amount": Customer.outstanding_amount,
        "created_at": Customer.created_at,
        "updated_at": Customer.updated_at,
    }

    # --- reads --------------------------------------------------------------

    def build_list_stmt(
        self,
        filters: CustomerFilters,
        sort: SortParams,
        *,
        channels: Sequence[Any] | None = None,
    ) -> Select[Any]:
        """The customer list query.

        ``channels`` restricts the result to a set of ``customer_type`` values
        and is how the dealer sub-resource is scoped: the restriction belongs in
        the SQL, not in a filter applied to an already-paginated page.
        """
        stmt = self._scoped(include_deleted=filters.include_deleted)
        stmt = self._apply_search(stmt, filters.search)

        if channels:
            stmt = stmt.where(Customer.customer_type.in_([str(c) for c in channels]))
        if filters.customer_type is not None:
            stmt = stmt.where(Customer.customer_type == filters.customer_type)
        if filters.price_list_type is not None:
            stmt = stmt.where(Customer.price_list_type == filters.price_list_type)
        if filters.assigned_sales_user_id is not None:
            stmt = stmt.where(Customer.assigned_sales_user_id == filters.assigned_sales_user_id)
        if filters.is_active is not None:
            stmt = stmt.where(Customer.is_active == filters.is_active)
        if filters.is_blocked is not None:
            stmt = stmt.where(Customer.is_blocked == filters.is_blocked)

        if filters.over_credit_limit:
            # An unlimited (NULL) limit can never be exceeded, so those rows are
            # excluded by the IS NOT NULL rather than by comparing against NULL,
            # which would evaluate to NULL and drop them anyway - stated
            # explicitly so the intent survives a later edit.
            stmt = stmt.where(
                Customer.credit_limit.is_not(None),
                Customer.outstanding_amount > Customer.credit_limit,
            )

        if filters.state_code is not None:
            # "Customers in Maharashtra" is a question about their addresses.
            # EXISTS rather than a join: a customer with three addresses in the
            # state must still appear once.
            stmt = stmt.where(
                exists().where(
                    CustomerAddress.company_id == self.tenant.company_id,
                    CustomerAddress.customer_id == Customer.id,
                    CustomerAddress.state_code == filters.state_code,
                    CustomerAddress.deleted_at.is_(None),
                )
            )

        stmt = stmt.options(load_only(*CUSTOMER_SUMMARY_COLUMNS, raiseload=True))

        # Code is the stable, unique, indexed ordering key; id breaks ties so a
        # row cannot appear on two pages or on none.
        default_order = sort.resolve(self.sortable_fields, Customer.code.asc())
        return stmt.order_by(default_order, Customer.id.asc())

    async def get_by_code(self, code: str, *, include_deleted: bool = False) -> Customer | None:
        stmt = self._scoped(include_deleted=include_deleted).where(Customer.code == code)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def code_taken(self, code: str) -> bool:
        """Is this code in use, including by a retired customer?

        Retired rows count: the code is quoted on their historical documents, so
        handing it to a new account would make that history ambiguous.
        """
        stmt = (
            select(Customer.id)
            .where(Customer.company_id == self.tenant.company_id, Customer.code == code)
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def gstin_taken(self, gstin: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        """Is another live customer already registered under this GSTIN?

        Mirrors the partial unique index, so the service can return a clean
        409 instead of letting a constraint violation surface as a 500.
        """
        stmt = select(Customer.id).where(
            Customer.company_id == self.tenant.company_id,
            Customer.gstin == gstin,
            Customer.deleted_at.is_(None),
        )
        if exclude_id is not None:
            stmt = stmt.where(Customer.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def lock_for_update(self, customer_id: uuid.UUID) -> Customer | None:
        """Read a customer under a row lock.

        Used by the outstanding-balance adjustment: two invoices posting for the
        same customer at the same instant must serialise on this row, or one
        increment is lost and the credit limit stops meaning anything.
        """
        stmt = self._scoped().where(Customer.id == customer_id).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def sales_user_exists(self, user_id: uuid.UUID) -> bool:
        """Is this an active user of the same tenant?

        Checked in the service rather than trusted from the payload: the foreign
        key alone would allow assigning an account to a deactivated user.
        """
        stmt = select(
            exists().where(
                User.id == user_id,
                User.company_id == self.tenant.company_id,
                User.deleted_at.is_(None),
                User.is_active.is_(True),
            )
        )
        return bool((await self.session.execute(stmt)).scalar())

    async def transporter_exists(self, transporter_id: uuid.UUID) -> bool | None:
        """Does this transporter belong to the tenant?

        Returns ``None`` when the ``transporters`` table is not present in this
        database - the vendors module may not have been migrated yet, and a
        customer edit must not fail because of that. The service treats ``None``
        as "unverified" and lets the write proceed.

        The ``to_regclass`` probe has to be its own round trip: a statement that
        *references* a missing relation fails at parse time and aborts the
        surrounding transaction, so the check cannot be folded into one SELECT.
        """
        found = await self.session.execute(text("SELECT to_regclass('public.transporters')"))
        if found.scalar() is None:
            return None

        row = await self.session.execute(
            text(
                "SELECT 1 FROM transporters "
                "WHERE id = :id AND company_id = :company_id AND deleted_at IS NULL LIMIT 1"
            ),
            {"id": transporter_id, "company_id": self.tenant.company_id},
        )
        return row.first() is not None


class CustomerAddressRepository(BaseRepository[CustomerAddress]):
    """Persistence for customer billing and shipping addresses."""

    model = CustomerAddress
    searchable_fields = ("label", "address_line1", "city", "postal_code")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "address_type": CustomerAddress.address_type,
        "city": CustomerAddress.city,
        "created_at": CustomerAddress.created_at,
    }

    def build_list_stmt(
        self,
        customer_id: uuid.UUID,
        *,
        address_type: AddressType | None = None,
        is_active: bool | None = None,
        include_deleted: bool = False,
    ) -> Select[Any]:
        stmt = self._scoped(include_deleted=include_deleted).where(
            CustomerAddress.customer_id == customer_id
        )
        if address_type is not None:
            stmt = stmt.where(CustomerAddress.address_type == address_type)
        if is_active is not None:
            stmt = stmt.where(CustomerAddress.is_active == is_active)
        # Defaults first: the ship-to picker opens on the address that will be
        # used unless someone changes it.
        return stmt.order_by(
            CustomerAddress.address_type.asc(),
            CustomerAddress.is_default.desc(),
            CustomerAddress.created_at.asc(),
        )

    async def list_for_customer(
        self, customer_id: uuid.UUID, *, limit: int | None = None
    ) -> list[CustomerAddress]:
        """Live addresses of one customer, in picker order.

        ``limit`` is applied in SQL rather than by slicing the result: the
        embedded list on a customer detail response is capped, and a cap that
        only trims after the rows have been fetched is not a cap at all. The
        full list is available on the paginated sub-resource.
        """
        stmt = self.build_list_stmt(customer_id)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_default(
        self, customer_id: uuid.UUID, address_type: AddressType
    ) -> CustomerAddress | None:
        stmt = self._scoped().where(
            CustomerAddress.customer_id == customer_id,
            CustomerAddress.address_type == address_type,
            CustomerAddress.is_default.is_(True),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def clear_default(
        self,
        customer_id: uuid.UUID,
        address_type: AddressType,
        *,
        keep_id: uuid.UUID | None = None,
    ) -> int:
        """Demote the current default of this (customer, address_type).

        One UPDATE, run *before* the new default is set, so the partial unique
        index never sees two flagged rows even momentarily.
        """
        stmt = (
            update(CustomerAddress)
            .where(
                CustomerAddress.company_id == self.tenant.company_id,
                CustomerAddress.customer_id == customer_id,
                CustomerAddress.address_type == address_type,
                CustomerAddress.is_default.is_(True),
                CustomerAddress.deleted_at.is_(None),
            )
            .values(is_default=False, updated_by_id=self.tenant.user_id)
            .execution_options(synchronize_session=False)
        )
        if keep_id is not None:
            stmt = stmt.where(CustomerAddress.id != keep_id)
        return affected_rows(await self.session.execute(stmt))

    async def next_default_candidate(
        self,
        customer_id: uuid.UUID,
        address_type: AddressType,
        *,
        exclude_id: uuid.UUID,
    ) -> CustomerAddress | None:
        """The address that should inherit the default flag.

        Oldest active address of the same type, so promotion is deterministic
        rather than dependent on physical row order.
        """
        stmt = (
            self._scoped()
            .where(
                CustomerAddress.customer_id == customer_id,
                CustomerAddress.address_type == address_type,
                CustomerAddress.is_active.is_(True),
                CustomerAddress.id != exclude_id,
            )
            .order_by(CustomerAddress.created_at.asc(), CustomerAddress.id.asc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def soft_delete_for_customer(self, customer_id: uuid.UUID) -> int:
        """Retire every address of a customer being retired.

        An address cannot outlive the account it belongs to; doing it in one
        UPDATE keeps the retirement a single statement rather than a loop.
        """
        stmt = (
            update(CustomerAddress)
            .where(
                CustomerAddress.company_id == self.tenant.company_id,
                CustomerAddress.customer_id == customer_id,
                CustomerAddress.deleted_at.is_(None),
            )
            .values(deleted_at=utcnow(), is_default=False, updated_by_id=self.tenant.user_id)
            .execution_options(synchronize_session=False)
        )
        return affected_rows(await self.session.execute(stmt))


class CustomerContactRepository(BaseRepository[CustomerContact]):
    """Persistence for the people at a customer."""

    model = CustomerContact
    searchable_fields = ("name", "designation", "email", "phone")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "name": CustomerContact.name,
        "created_at": CustomerContact.created_at,
    }

    def build_list_stmt(
        self,
        customer_id: uuid.UUID,
        *,
        is_active: bool | None = None,
        include_deleted: bool = False,
    ) -> Select[Any]:
        stmt = self._scoped(include_deleted=include_deleted).where(
            CustomerContact.customer_id == customer_id
        )
        if is_active is not None:
            stmt = stmt.where(CustomerContact.is_active == is_active)
        return stmt.order_by(
            CustomerContact.is_primary.desc(),
            CustomerContact.name.asc(),
            CustomerContact.id.asc(),
        )

    async def list_for_customer(
        self, customer_id: uuid.UUID, *, limit: int | None = None
    ) -> list[CustomerContact]:
        """Live contacts of one customer, primary first, optionally capped."""
        stmt = self.build_list_stmt(customer_id)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def has_primary(self, customer_id: uuid.UUID) -> bool:
        """EXISTS probe - the first contact of a customer becomes the primary,
        and answering that needs one row, not the whole list."""
        stmt = select(
            exists().where(
                CustomerContact.company_id == self.tenant.company_id,
                CustomerContact.customer_id == customer_id,
                CustomerContact.is_primary.is_(True),
                CustomerContact.deleted_at.is_(None),
            )
        )
        return bool((await self.session.execute(stmt)).scalar())

    async def clear_primary(
        self, customer_id: uuid.UUID, *, keep_id: uuid.UUID | None = None
    ) -> int:
        stmt = (
            update(CustomerContact)
            .where(
                CustomerContact.company_id == self.tenant.company_id,
                CustomerContact.customer_id == customer_id,
                CustomerContact.is_primary.is_(True),
                CustomerContact.deleted_at.is_(None),
            )
            .values(is_primary=False, updated_by_id=self.tenant.user_id)
            .execution_options(synchronize_session=False)
        )
        if keep_id is not None:
            stmt = stmt.where(CustomerContact.id != keep_id)
        return affected_rows(await self.session.execute(stmt))

    async def next_primary_candidate(
        self, customer_id: uuid.UUID, *, exclude_id: uuid.UUID
    ) -> CustomerContact | None:
        stmt = (
            self._scoped()
            .where(
                CustomerContact.customer_id == customer_id,
                CustomerContact.is_active.is_(True),
                CustomerContact.id != exclude_id,
            )
            .order_by(CustomerContact.created_at.asc(), CustomerContact.id.asc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def soft_delete_for_customer(self, customer_id: uuid.UUID) -> int:
        stmt = (
            update(CustomerContact)
            .where(
                CustomerContact.company_id == self.tenant.company_id,
                CustomerContact.customer_id == customer_id,
                CustomerContact.deleted_at.is_(None),
            )
            .values(deleted_at=utcnow(), is_primary=False, updated_by_id=self.tenant.user_id)
            .execution_options(synchronize_session=False)
        )
        return affected_rows(await self.session.execute(stmt))

    async def count_for_customers(self, customer_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        """Contact counts for a whole page of customers in one query."""
        if not customer_ids:
            return {}
        from sqlalchemy import func

        stmt = (
            select(CustomerContact.customer_id, func.count())
            .where(
                CustomerContact.company_id == self.tenant.company_id,
                CustomerContact.customer_id.in_(list(customer_ids)),
                CustomerContact.deleted_at.is_(None),
            )
            .group_by(CustomerContact.customer_id)
        )
        return {row[0]: int(row[1]) for row in (await self.session.execute(stmt)).all()}


__all__ = [
    "CUSTOMER_SUMMARY_COLUMNS",
    "CustomerAddressRepository",
    "CustomerContactRepository",
    "CustomerRepository",
]
