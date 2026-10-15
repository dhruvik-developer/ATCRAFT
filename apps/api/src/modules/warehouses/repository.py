"""Queries for warehouses and storage locations.

Everything that touches the database lives here; the service decides *whether*
an operation is allowed, this file decides *how* it is read or written.

Two things are worth reading twice:

* :meth:`WarehouseRepository._scoped` narrows to the principal's permitted
  warehouses on top of the tenant filter, so a storekeeper limited to two
  warehouses gets a 404 for the third rather than a 200 with data they may not
  see (section 39).
* :meth:`WarehouseRepository.has_stock_balance` reaches into ``inventory_balances``
  with raw SQL rather than importing the inventory module. Master data must not
  depend on the inventory engine - the dependency runs the other way - and the
  table may not exist yet in a partially migrated database, which is what the
  ``to_regclass`` probe handles.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, func, literal, select, text, update
from sqlalchemy.orm import InstrumentedAttribute

from src.common.repository import BaseRepository
from src.common.warehouse_scope import warehouse_scope
from src.core.clock import utcnow
from src.core.database import affected_rows
from src.core.pagination import SortParams

# Read-only references to other modules' master tables. Warehouses validate that
# a branch, plant or manager belongs to the same tenant before pointing at it;
# they never write to those tables.
from src.modules.organization.models import Branch, Plant
from src.modules.users.models import User
from src.modules.warehouses.models import StorageLocation, Warehouse
from src.modules.warehouses.schemas import StorageLocationFilters, WarehouseFilters

#: Guard for the recursive ancestor walk. Bounds the query on a hierarchy that
#: has somehow become cyclic in the database (a direct SQL edit, a restore from
#: an older dump) instead of letting the CTE run forever.
MAX_HIERARCHY_WALK = 64

#: Balance columns that mean "this warehouse or location is still holding
#: something". ``available_quantity`` is deliberately absent: it is a generated
#: column derived from these, so including it would double-count.
_BALANCE_COLUMNS = (
    "on_hand_quantity",
    "reserved_quantity",
    "allocated_quantity",
    "in_transit_quantity",
    "qc_hold_quantity",
    "damaged_quantity",
    "job_work_quantity",
    "scrap_quantity",
)

_NON_ZERO_BALANCE = " OR ".join(f"COALESCE({c}, 0) <> 0" for c in _BALANCE_COLUMNS)

# Built once from the constant column list above - no request data is ever
# interpolated into either statement; the parameters are bound.
_WAREHOUSE_BALANCE_SQL = (
    "SELECT 1 FROM inventory_balances "  # noqa: S608 - only the constant above is interpolated
    "WHERE company_id = :company_id AND warehouse_id = :warehouse_id "
    f"AND ({_NON_ZERO_BALANCE}) LIMIT 1"
)
_LOCATION_BALANCE_SQL = (
    "SELECT 1 FROM inventory_balances "  # noqa: S608 - only the constant above is interpolated
    "WHERE company_id = :company_id AND location_id = :location_id "
    f"AND ({_NON_ZERO_BALANCE}) LIMIT 1"
)


async def _inventory_balances_exists(session: Any) -> bool:
    """Is the ``inventory_balances`` table present in this database?

    ``to_regclass`` returns NULL instead of raising for an unknown relation,
    which is the only way to ask the question without aborting the surrounding
    transaction. It must be a separate round trip: a statement that *references*
    a missing table fails at parse time, so the probe cannot be folded into the
    same SELECT.
    """
    found = await session.execute(text("SELECT to_regclass('public.inventory_balances')"))
    return found.scalar() is not None


class WarehouseRepository(BaseRepository[Warehouse]):
    """Persistence for :class:`~src.modules.warehouses.models.Warehouse`."""

    model = Warehouse
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": Warehouse.code,
        "name": Warehouse.name,
        "warehouse_type": Warehouse.warehouse_type,
        "is_active": Warehouse.is_active,
        "created_at": Warehouse.created_at,
        "updated_at": Warehouse.updated_at,
    }
    searchable_fields = ("code", "name", "city")

    def _scoped(self, *, include_deleted: bool = False) -> Select[tuple[Warehouse]]:
        """Tenant filter plus the principal's warehouse restriction.

        An empty ``allowed_warehouse_ids`` means unrestricted - the same
        convention :meth:`src.core.deps.Principal.can_access_warehouse` uses -
        so the common case adds no predicate.
        """
        stmt = super()._scoped(include_deleted=include_deleted)
        return stmt.where(*warehouse_scope(self.tenant, Warehouse.id))

    # --- reads --------------------------------------------------------------

    def build_list_stmt(self, filters: WarehouseFilters, sort: SortParams) -> Select[Any]:
        stmt = self._scoped(include_deleted=filters.include_deleted)
        stmt = self._apply_search(stmt, filters.search)

        if filters.warehouse_type is not None:
            stmt = stmt.where(Warehouse.warehouse_type == filters.warehouse_type)
        if filters.branch_id is not None:
            stmt = stmt.where(Warehouse.branch_id == filters.branch_id)
        if filters.plant_id is not None:
            stmt = stmt.where(Warehouse.plant_id == filters.plant_id)
        if filters.parent_warehouse_id is not None:
            stmt = stmt.where(Warehouse.parent_warehouse_id == filters.parent_warehouse_id)
        if filters.is_active is not None:
            stmt = stmt.where(Warehouse.is_active == filters.is_active)
        if filters.allow_negative_stock is not None:
            stmt = stmt.where(Warehouse.allow_negative_stock == filters.allow_negative_stock)

        # Code is the stable, unique, indexed ordering key; id breaks ties so a
        # row cannot appear on two pages.
        default_order = sort.resolve(self.sortable_fields, Warehouse.code.asc())
        return stmt.order_by(default_order, Warehouse.id.asc())

    async def get_by_code(self, code: str, *, include_deleted: bool = False) -> Warehouse | None:
        stmt = self._scoped(include_deleted=include_deleted).where(Warehouse.code == code)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def default_warehouse(self) -> Warehouse | None:
        """The tenant's default active warehouse, if one is flagged.

        The dealer-portal checkout fulfils from here: a dealer names no
        warehouse, and the order still has to reserve against one
        (docs/assumptions.md §C3-5). Unscoped by the principal's warehouse
        restriction would be wrong - but the dealer tenant carries no such
        restriction, so ``_scoped`` is the right base.
        """
        stmt = (
            self._scoped()
            .where(Warehouse.is_active.is_(True), Warehouse.is_default.is_(True))
            .order_by(Warehouse.code.asc(), Warehouse.id.asc())
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def code_taken(self, code: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        """Is this code already used in the tenant, including by a retired row?

        Soft-deleted rows are counted: their code is still quoted on historical
        documents, so reusing it would make two different warehouses read as one
        in the archive.
        """
        stmt = (
            select(literal(1))
            .select_from(Warehouse)
            .where(Warehouse.company_id == self.tenant.company_id, Warehouse.code == code)
        )
        if exclude_id is not None:
            stmt = stmt.where(Warehouse.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def list_options(self, *, warehouse_type: str | None = None) -> list[Warehouse]:
        """Active warehouses for a picker.

        Bounded by the number of warehouses a manufacturer operates (tens, not
        thousands) and driven by an index on (company_id, is_active), so it is
        returned whole rather than paginated - a paginated dropdown is worse for
        the user and no cheaper for the database.
        """
        stmt = self._scoped().where(Warehouse.is_active.is_(True))
        if warehouse_type is not None:
            stmt = stmt.where(Warehouse.warehouse_type == warehouse_type)
        stmt = stmt.order_by(Warehouse.is_default.desc(), Warehouse.code.asc()).limit(500)
        return list((await self.session.execute(stmt)).scalars().all())

    async def location_counts(self, warehouse_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        """Live location count per warehouse, for a whole page in one query.

        The alternative - counting inside a loop over the page - is the textbook
        N+1 that section 32 forbids.
        """
        if not warehouse_ids:
            return {}
        stmt = (
            select(StorageLocation.warehouse_id, func.count())
            .where(
                StorageLocation.company_id == self.tenant.company_id,
                StorageLocation.warehouse_id.in_(list(warehouse_ids)),
                StorageLocation.deleted_at.is_(None),
            )
            .group_by(StorageLocation.warehouse_id)
        )
        rows = (await self.session.execute(stmt)).all()
        return {row[0]: int(row[1]) for row in rows}

    async def ancestor_ids(self, warehouse_id: uuid.UUID) -> list[uuid.UUID]:
        """Ids from ``warehouse_id`` up to the root, nearest first.

        One recursive CTE rather than a loop of single-row loads: the depth is
        unknown in advance, so a loop would be an unbounded query count.
        """
        base = (
            select(
                Warehouse.id.label("id"),
                Warehouse.parent_warehouse_id.label("parent_warehouse_id"),
                literal(1).label("depth"),
            )
            .where(
                Warehouse.company_id == self.tenant.company_id,
                Warehouse.id == warehouse_id,
            )
            .cte("warehouse_chain", recursive=True)
        )
        parent = base.alias("chain")
        chain = base.union_all(
            select(
                Warehouse.id,
                Warehouse.parent_warehouse_id,
                (parent.c.depth + 1).label("depth"),
            ).where(
                Warehouse.id == parent.c.parent_warehouse_id,
                Warehouse.company_id == self.tenant.company_id,
                parent.c.depth < MAX_HIERARCHY_WALK,
            )
        )
        rows = await self.session.execute(select(chain.c.id).order_by(chain.c.depth.asc()))
        return [row[0] for row in rows.all()]

    async def has_live_children(self, warehouse_id: uuid.UUID) -> bool:
        stmt = (
            select(literal(1))
            .select_from(Warehouse)
            .where(
                Warehouse.company_id == self.tenant.company_id,
                Warehouse.parent_warehouse_id == warehouse_id,
                Warehouse.deleted_at.is_(None),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def has_stock_balance(self, warehouse_id: uuid.UUID) -> bool:
        """Does this warehouse still hold anything?

        Deliberately *not* an import of the inventory module: master data sits
        below the inventory engine in the dependency order, and reversing that
        would make the two modules impossible to build or test independently.
        Reading one table with raw SQL is the smaller price.

        Returns False when ``inventory_balances`` does not exist yet, which is
        correct: a table that has never been created cannot hold stock.
        """
        if not await _inventory_balances_exists(self.session):
            return False

        found = await self.session.execute(
            text(_WAREHOUSE_BALANCE_SQL),
            {"company_id": self.tenant.company_id, "warehouse_id": warehouse_id},
        )
        return found.first() is not None

    async def branch_exists(self, branch_id: uuid.UUID) -> bool:
        return await self._tenant_row_exists(Branch, branch_id)

    async def plant_exists(self, plant_id: uuid.UUID) -> bool:
        return await self._tenant_row_exists(Plant, plant_id)

    async def user_exists(self, user_id: uuid.UUID) -> bool:
        return await self._tenant_row_exists(User, user_id)

    async def _tenant_row_exists(self, model: Any, entity_id: uuid.UUID) -> bool:
        """EXISTS probe against another module's master table, tenant-scoped.

        Scoped on purpose: a foreign key can only prove the row exists *somewhere*,
        and pointing a warehouse at another company's branch would be a tenant
        leak the database would happily accept.
        """
        stmt = (
            select(literal(1))
            .select_from(model)
            .where(
                model.company_id == self.tenant.company_id,
                model.id == entity_id,
                model.deleted_at.is_(None),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None

    # --- writes -------------------------------------------------------------

    async def clear_default(self, *, keep_id: uuid.UUID | None = None) -> int:
        """Demote every other default in the tenant.

        One UPDATE, not a read-then-write loop: the partial unique index would
        reject the second default anyway, and doing it in a single statement
        keeps the window where two rows are flagged from existing at all.
        """
        stmt = (
            update(Warehouse)
            .where(
                Warehouse.company_id == self.tenant.company_id,
                Warehouse.is_default.is_(True),
                Warehouse.deleted_at.is_(None),
            )
            .values(is_default=False, updated_by_id=self.tenant.user_id)
            .execution_options(synchronize_session=False)
        )
        if keep_id is not None:
            stmt = stmt.where(Warehouse.id != keep_id)
        return affected_rows(await self.session.execute(stmt))


class StorageLocationRepository(BaseRepository[StorageLocation]):
    """Persistence for :class:`~src.modules.warehouses.models.StorageLocation`."""

    model = StorageLocation
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": StorageLocation.code,
        "name": StorageLocation.name,
        "location_type": StorageLocation.location_type,
        "is_active": StorageLocation.is_active,
        "created_at": StorageLocation.created_at,
        "updated_at": StorageLocation.updated_at,
    }
    searchable_fields = ("code", "name", "barcode")

    async def lock_for_retirement(self, location_id: uuid.UUID) -> StorageLocation | None:
        """Take the bin's row lock before deciding whether it may be switched off.

        ``FOR NO KEY UPDATE`` conflicts with the ``FOR SHARE`` the item service
        takes on a bin it is about to make an item's default bin, so a retirement
        and an assignment cannot both pass their checks against a state the other
        is about to change - one of them waits and then sees the other's write.
        ``populate_existing`` so the flags are re-read after the lock.
        """
        stmt = (
            self._scoped()
            .where(StorageLocation.id == location_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock_for_warehouse_retirement(self, warehouse_id: uuid.UUID) -> None:
        """The same lock over every live bin of a warehouse, in primary-key order."""
        await self.session.execute(
            self._scoped()
            .where(StorageLocation.warehouse_id == warehouse_id)
            .order_by(StorageLocation.id)
            .with_for_update(key_share=True)
        )

    async def has_item_defaults(
        self, *, location_id: uuid.UUID | None = None, warehouse_id: uuid.UUID | None = None
    ) -> bool:
        """Is this bin - or any bin of this warehouse - some live item's default bin?

        Probed with raw SQL so this module never imports the item master that
        sits above it (docs/architecture.md, dependency direction). Tenant
        scoped on both tables. Whichever of the two arguments is ``None`` does
        not narrow the probe.
        """
        statement = text(
            "SELECT 1 FROM items i JOIN storage_locations l "
            "ON l.id = i.default_location_id AND l.company_id = i.company_id "
            "WHERE i.company_id = :company_id AND i.deleted_at IS NULL "
            "AND l.id = coalesce(CAST(:location_id AS UUID), l.id) "
            "AND l.warehouse_id = coalesce(CAST(:warehouse_id AS UUID), l.warehouse_id) LIMIT 1"
        )
        return (
            await self.session.execute(
                statement,
                {
                    "company_id": self.tenant.company_id,
                    "location_id": location_id,
                    "warehouse_id": warehouse_id,
                },
            )
        ).first() is not None

    def _scoped(self, *, include_deleted: bool = False) -> Select[tuple[StorageLocation]]:
        """Tenant filter plus the principal's warehouse restriction."""
        stmt = super()._scoped(include_deleted=include_deleted)
        return stmt.where(*warehouse_scope(self.tenant, StorageLocation.warehouse_id))

    def build_list_stmt(self, filters: StorageLocationFilters, sort: SortParams) -> Select[Any]:
        stmt = self._scoped(include_deleted=filters.include_deleted)
        stmt = self._apply_search(stmt, filters.search)

        if filters.warehouse_id is not None:
            stmt = stmt.where(StorageLocation.warehouse_id == filters.warehouse_id)
        if filters.location_type is not None:
            stmt = stmt.where(StorageLocation.location_type == filters.location_type)
        if filters.parent_location_id is not None:
            stmt = stmt.where(StorageLocation.parent_location_id == filters.parent_location_id)
        if filters.is_active is not None:
            stmt = stmt.where(StorageLocation.is_active == filters.is_active)

        default_order = sort.resolve(self.sortable_fields, StorageLocation.code.asc())
        return stmt.order_by(default_order, StorageLocation.id.asc())

    async def code_taken(
        self, warehouse_id: uuid.UUID, code: str, *, exclude_id: uuid.UUID | None = None
    ) -> bool:
        stmt = (
            select(literal(1))
            .select_from(StorageLocation)
            .where(
                StorageLocation.company_id == self.tenant.company_id,
                StorageLocation.warehouse_id == warehouse_id,
                StorageLocation.code == code,
            )
        )
        if exclude_id is not None:
            stmt = stmt.where(StorageLocation.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def ancestor_ids(self, location_id: uuid.UUID) -> list[uuid.UUID]:
        """Ids from ``location_id`` up to the top-level location, nearest first."""
        base = (
            select(
                StorageLocation.id.label("id"),
                StorageLocation.parent_location_id.label("parent_location_id"),
                literal(1).label("depth"),
            )
            .where(
                StorageLocation.company_id == self.tenant.company_id,
                StorageLocation.id == location_id,
            )
            .cte("location_chain", recursive=True)
        )
        parent = base.alias("chain")
        chain = base.union_all(
            select(
                StorageLocation.id,
                StorageLocation.parent_location_id,
                (parent.c.depth + 1).label("depth"),
            ).where(
                StorageLocation.id == parent.c.parent_location_id,
                StorageLocation.company_id == self.tenant.company_id,
                parent.c.depth < MAX_HIERARCHY_WALK,
            )
        )
        rows = await self.session.execute(select(chain.c.id).order_by(chain.c.depth.asc()))
        return [row[0] for row in rows.all()]

    async def has_live_children(self, location_id: uuid.UUID) -> bool:
        stmt = (
            select(literal(1))
            .select_from(StorageLocation)
            .where(
                StorageLocation.company_id == self.tenant.company_id,
                StorageLocation.parent_location_id == location_id,
                StorageLocation.deleted_at.is_(None),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def has_stock_balance(self, location_id: uuid.UUID) -> bool:
        """Does this bin still hold anything? See :meth:`WarehouseRepository.has_stock_balance`."""
        if not await _inventory_balances_exists(self.session):
            return False

        found = await self.session.execute(
            text(_LOCATION_BALANCE_SQL),
            {"company_id": self.tenant.company_id, "location_id": location_id},
        )
        return found.first() is not None

    async def soft_delete_for_warehouse(self, warehouse_id: uuid.UUID) -> int:
        """Retire every live location of a warehouse in one statement.

        Called when the warehouse itself is retired: a bin cannot outlive the
        building it is in, and leaving them live would let a movement address a
        location whose warehouse no longer accepts stock.
        """
        stmt = (
            update(StorageLocation)
            .where(
                StorageLocation.company_id == self.tenant.company_id,
                StorageLocation.warehouse_id == warehouse_id,
                StorageLocation.deleted_at.is_(None),
            )
            .values(deleted_at=utcnow(), is_active=False, updated_by_id=self.tenant.user_id)
            .execution_options(synchronize_session=False)
        )
        return affected_rows(await self.session.execute(stmt))


__all__ = [
    "MAX_HIERARCHY_WALK",
    "StorageLocationRepository",
    "WarehouseRepository",
]
