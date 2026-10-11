"""Queries for the organisation masters.

Everything that touches the database lives here; the service decides *whether*
an operation is allowed, this file decides *how* it is read or written.

Three points are worth reading twice:

* :class:`CompanyRepository` scopes on ``companies.id``, not on a ``company_id``
  column - ``companies`` is the tenant root and is the one table that does not
  carry the tenant column. Expressing the tenant boundary as an identity filter
  means the "read my own company" query and the "read someone else's" query are
  the same statement, and the second one simply returns nothing.
* The delete guards reach into ``users`` through the ORM but into ``warehouses``
  with raw SQL. ``users`` and ``departments`` already reference each other by
  foreign key, so importing that model changes nothing about the dependency
  order. ``warehouses`` sits *above* organisation - it imports these models -
  and importing it back would make the two modules impossible to build or test
  independently. The ``to_regclass`` probe also covers a partially migrated
  database where the table does not exist yet.
* :meth:`FinancialYearRepository.overlapping_active` is a half-open-free,
  inclusive range overlap: two closed intervals overlap when each starts on or
  before the other ends. Written as two predicates rather than an OR chain so
  the composite ``(company_id, start_date, end_date)`` index can be used.
* The retire guards for tables owned by *higher* modules - work centres,
  production orders, requisitions, documents carrying a cost centre - are
  :class:`ReferenceProbe` rows answered by :meth:`_OrgRepositoryMixin.
  _carrier_references` in two round trips however many tables are probed: one
  ``to_regclass`` sweep, one ``UNION ALL`` of ``EXISTS`` probes. A loop of one
  query per table would be the N+1 the codebase treats as a defect.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, literal, select, text
from sqlalchemy.orm import InstrumentedAttribute

from src.common.codes import DocumentSequence
from src.common.repository import BaseRepository
from src.core.pagination import SortParams
from src.modules.organization.models import (
    Branch,
    Company,
    CostCentre,
    Department,
    FinancialYear,
    Plant,
)
from src.modules.organization.schemas import (
    BranchFilters,
    CostCentreFilters,
    DepartmentFilters,
    FinancialYearFilters,
    PlantFilters,
)

# Read-only reference to the identity module. ``departments.head_user_id`` and
# ``users.department_id`` are already a mutual foreign key pair, so this import
# introduces no dependency that the schema did not already have.
from src.modules.users.models import User

#: Guard for the recursive ancestor walk. Bounds the query on a hierarchy that
#: has somehow become cyclic in the database (a direct SQL edit, a restore from
#: an older dump) instead of letting the CTE run forever.
MAX_HIERARCHY_WALK = 64

_WAREHOUSE_BRANCH_SQL = (
    "SELECT 1 FROM warehouses "
    "WHERE company_id = :company_id AND branch_id = :entity_id "
    "AND deleted_at IS NULL LIMIT 1"
)
_WAREHOUSE_PLANT_SQL = (
    "SELECT 1 FROM warehouses "
    "WHERE company_id = :company_id AND plant_id = :entity_id "
    "AND deleted_at IS NULL LIMIT 1"
)


async def _warehouses_table_exists(session: Any) -> bool:
    """Is the ``warehouses`` table present in this database?

    ``to_regclass`` returns NULL instead of raising for an unknown relation,
    which is the only way to ask without aborting the surrounding transaction.
    It must be a separate round trip: a statement that *references* a missing
    table fails at parse time, so the probe cannot be folded into the same
    SELECT.
    """
    found = await session.execute(text("SELECT to_regclass('public.warehouses')"))
    return found.scalar() is not None


@dataclass(frozen=True, slots=True)
class ReferenceProbe:
    """A column in a table owned by a module above organisation.

    Probed with raw SQL so organisation never imports the module that owns the
    table - see the module docstring. ``soft_deleted`` decides what "still
    referenced" means: a master table with ``deleted_at`` is referenced by its
    live rows only, while a document table (no soft delete) is referenced by
    every row it holds, whatever its status - a closed order is still history
    that names the record. ``label`` is how the refusal names the table to a
    person.
    """

    table: str
    column: str
    label: str
    soft_deleted: bool = False

    def exists_sql(self) -> str:
        """``SELECT '<table>' WHERE EXISTS (...)`` for this one probe."""
        live = " AND deleted_at IS NULL" if self.soft_deleted else ""
        return (
            f"SELECT '{self.table}' AS carrier WHERE EXISTS ("  # noqa: S608 - module constants only
            f"SELECT 1 FROM {self.table} WHERE company_id = :company_id "
            f"AND {self.column} = :entity_id{live})"
        )


#: Tables above organisation that may name a plant.
PLANT_REFERENCE_PROBES: tuple[ReferenceProbe, ...] = (
    ReferenceProbe("work_centers", "plant_id", "work centres", soft_deleted=True),
    ReferenceProbe("production_orders", "plant_id", "production orders"),
)

#: Tables above organisation that may name a department.
DEPARTMENT_REFERENCE_PROBES: tuple[ReferenceProbe, ...] = (
    ReferenceProbe("work_centers", "department_id", "work centres", soft_deleted=True),
    ReferenceProbe("purchase_requisitions", "department_id", "purchase requisitions"),
)

#: Tables above organisation where a cost centre is a *default* rather than a
#: posted fact. Deactivation is refused while one of these still names it: a
#: default pointing at a centre that may no longer be assigned would hand every
#: new document a reference its own validation refuses.
COST_CENTRE_DEFAULT_PROBES: tuple[ReferenceProbe, ...] = (
    ReferenceProbe("work_centers", "cost_centre_id", "work centres", soft_deleted=True),
)

#: Every carrier above organisation. Retirement is refused while any of these
#: still names the centre. ``ledger_entries`` is deliberately absent: posted
#: history keeps its cost centre after retirement, and a reversal copies it
#: (docs/assumptions.md §43). A module that adds a carrier adds it here.
COST_CENTRE_REFERENCE_PROBES: tuple[ReferenceProbe, ...] = (
    *COST_CENTRE_DEFAULT_PROBES,
    ReferenceProbe("production_orders", "cost_centre_id", "production orders"),
    ReferenceProbe("job_work_orders", "cost_centre_id", "job-work orders"),
    ReferenceProbe("stock_adjustments", "cost_centre_id", "stock adjustments"),
    ReferenceProbe("vendor_invoices", "cost_centre_id", "vendor invoices"),
    ReferenceProbe("vendor_invoice_lines", "cost_centre_id", "vendor invoice lines"),
)


async def _existing_tables(session: Any, tables: Iterable[str]) -> set[str]:
    """Which of these tables exist, in one round trip.

    The ``to_regclass`` sweep for every probe at once - see
    :func:`_warehouses_table_exists` for why it cannot share a statement with
    the probes it guards.
    """
    wanted = sorted(set(tables))
    if not wanted:
        return set()
    found = await session.execute(
        text(
            "SELECT name FROM unnest(CAST(:names AS text[])) AS name "
            "WHERE to_regclass('public.' || name) IS NOT NULL"
        ),
        {"names": wanted},
    )
    return {row[0] for row in found.all()}


def reference_labels(probes: Sequence[ReferenceProbe], tables: Iterable[str]) -> list[str]:
    """The human labels of the probes whose table was found, in probe order.

    Deduplicated: two probes over the same table (a plant named on a work
    centre twice over) read as one reason.
    """
    hit = set(tables)
    labels: list[str] = []
    for probe in probes:
        if probe.table in hit and probe.label not in labels:
            labels.append(probe.label)
    return labels


class _OrgRepositoryMixin:
    """Shared probes for the tenant-scoped organisation masters."""

    session: Any
    tenant: Any

    async def _carrier_references(
        self, probes: Sequence[ReferenceProbe], entity_id: uuid.UUID
    ) -> list[str]:
        """Labels of the probed tables that still name this record.

        Two round trips whatever the number of probes: which tables exist, then
        one ``UNION ALL`` of tenant-scoped ``EXISTS`` checks over those that do.
        A table that has never been created holds no reference, so a partially
        migrated database answers "none" rather than raising.
        """
        present = await _existing_tables(self.session, (probe.table for probe in probes))
        live = [probe for probe in probes if probe.table in present]
        if not live:
            return []
        statement = " UNION ALL ".join(probe.exists_sql() for probe in live)
        found = await self.session.execute(
            text(statement),
            {"company_id": self.tenant.company_id, "entity_id": entity_id},
        )
        return reference_labels(live, (row[0] for row in found.all()))

    async def _user_references(
        self, column: InstrumentedAttribute[Any], entity_id: uuid.UUID
    ) -> bool:
        """Does a live user still point at this record?

        Scoped by ``company_id`` as well as by the column: the foreign key can
        only prove a row exists *somewhere*, and a cross-tenant match would be a
        leak the database would happily accept.
        """
        stmt = (
            select(literal(1))
            .select_from(User)
            .where(
                User.company_id == self.tenant.company_id,
                column == entity_id,
                User.deleted_at.is_(None),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def _warehouse_references(self, sql: str, entity_id: uuid.UUID) -> bool:
        """Does a live warehouse still point at this record?

        Returns False when ``warehouses`` does not exist yet, which is correct:
        a table that has never been created cannot reference anything.
        """
        if not await _warehouses_table_exists(self.session):
            return False
        found = await self.session.execute(
            text(sql),
            {"company_id": self.tenant.company_id, "entity_id": entity_id},
        )
        return found.first() is not None


# -----------------------------------------------------------------------------
# Company
# -----------------------------------------------------------------------------


class CompanyRepository(BaseRepository[Company]):
    """Persistence for the tenant root.

    ``companies`` has no ``company_id``; the tenant filter is the primary key
    itself. Inherited helpers that build a ``company_id`` predicate
    (:meth:`exists`) are overridden accordingly, and :meth:`add` is refused
    outright - provisioning a tenant is a platform operation, not something a
    request inside a tenant may do.
    """

    model = Company
    searchable_fields = ("code", "name", "legal_name")

    def _scoped(self, *, include_deleted: bool = False) -> Select[tuple[Company]]:
        stmt = select(Company).where(Company.id == self.tenant.company_id)
        if not include_deleted:
            stmt = stmt.where(Company.deleted_at.is_(None))
        return stmt

    async def exists(self, **filters: Any) -> bool:
        stmt = self._scoped()
        for key, value in filters.items():
            stmt = stmt.where(getattr(Company, key) == value)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    def add(self, instance: Company) -> Company:
        raise TypeError(
            "Companies are provisioned by platform onboarding, not through the tenant API."
        )

    async def get_current(self) -> Company | None:
        """The caller's own company. The only way this module reads a company."""
        return (await self.session.execute(self._scoped())).scalar_one_or_none()


# -----------------------------------------------------------------------------
# Branch
# -----------------------------------------------------------------------------


class BranchRepository(_OrgRepositoryMixin, BaseRepository[Branch]):
    model = Branch
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": Branch.code,
        "name": Branch.name,
        "city": Branch.city,
        "is_active": Branch.is_active,
        "created_at": Branch.created_at,
        "updated_at": Branch.updated_at,
    }
    searchable_fields = ("code", "name", "city")

    def build_list_stmt(self, filters: BranchFilters, sort: SortParams) -> Select[Any]:
        stmt = self._scoped(include_deleted=filters.include_deleted)
        stmt = self._apply_search(stmt, filters.search)

        if filters.is_active is not None:
            stmt = stmt.where(Branch.is_active == filters.is_active)
        if filters.is_head_office is not None:
            stmt = stmt.where(Branch.is_head_office == filters.is_head_office)

        # Code is the stable, unique, indexed ordering key; id breaks ties so a
        # row cannot appear on two pages.
        order = sort.resolve(self.sortable_fields, Branch.code.asc())
        return stmt.order_by(order, Branch.id.asc())

    async def code_taken(self, code: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        """Is this code already used in the tenant, including by a retired row?

        Soft-deleted rows count: their code is still quoted on historical
        documents, so reusing it would make two different branches read as one
        in the archive - and the unique constraint covers them anyway.
        """
        stmt = (
            select(literal(1))
            .select_from(Branch)
            .where(Branch.company_id == self.tenant.company_id, Branch.code == code)
        )
        if exclude_id is not None:
            stmt = stmt.where(Branch.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def head_office_id(self, *, exclude_id: uuid.UUID | None = None) -> uuid.UUID | None:
        """The live branch currently flagged as head office, if any."""
        stmt = (
            select(Branch.id)
            .where(
                Branch.company_id == self.tenant.company_id,
                Branch.deleted_at.is_(None),
                Branch.is_head_office.is_(True),
            )
            .limit(1)
        )
        if exclude_id is not None:
            stmt = stmt.where(Branch.id != exclude_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def referenced_by_users(self, branch_id: uuid.UUID) -> bool:
        return await self._user_references(User.branch_id, branch_id)

    async def referenced_by_warehouses(self, branch_id: uuid.UUID) -> bool:
        return await self._warehouse_references(_WAREHOUSE_BRANCH_SQL, branch_id)

    async def referenced_by_plants(self, branch_id: uuid.UUID) -> bool:
        stmt = (
            select(literal(1))
            .select_from(Plant)
            .where(
                Plant.company_id == self.tenant.company_id,
                Plant.branch_id == branch_id,
                Plant.deleted_at.is_(None),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None


# -----------------------------------------------------------------------------
# Plant
# -----------------------------------------------------------------------------


class PlantRepository(_OrgRepositoryMixin, BaseRepository[Plant]):
    model = Plant
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": Plant.code,
        "name": Plant.name,
        "city": Plant.city,
        "is_active": Plant.is_active,
        "created_at": Plant.created_at,
        "updated_at": Plant.updated_at,
    }
    searchable_fields = ("code", "name", "city")

    def build_list_stmt(self, filters: PlantFilters, sort: SortParams) -> Select[Any]:
        stmt = self._scoped(include_deleted=filters.include_deleted)
        stmt = self._apply_search(stmt, filters.search)

        if filters.branch_id is not None:
            stmt = stmt.where(Plant.branch_id == filters.branch_id)
        if filters.is_active is not None:
            stmt = stmt.where(Plant.is_active == filters.is_active)

        order = sort.resolve(self.sortable_fields, Plant.code.asc())
        return stmt.order_by(order, Plant.id.asc())

    async def code_taken(self, code: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        stmt = (
            select(literal(1))
            .select_from(Plant)
            .where(Plant.company_id == self.tenant.company_id, Plant.code == code)
        )
        if exclude_id is not None:
            stmt = stmt.where(Plant.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def branch_exists(self, branch_id: uuid.UUID) -> bool:
        stmt = (
            select(literal(1))
            .select_from(Branch)
            .where(
                Branch.company_id == self.tenant.company_id,
                Branch.id == branch_id,
                Branch.deleted_at.is_(None),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def referenced_by_users(self, plant_id: uuid.UUID) -> bool:
        return await self._user_references(User.plant_id, plant_id)

    async def referenced_by_warehouses(self, plant_id: uuid.UUID) -> bool:
        return await self._warehouse_references(_WAREHOUSE_PLANT_SQL, plant_id)

    async def referenced_by_departments(self, plant_id: uuid.UUID) -> bool:
        stmt = (
            select(literal(1))
            .select_from(Department)
            .where(
                Department.company_id == self.tenant.company_id,
                Department.plant_id == plant_id,
                Department.deleted_at.is_(None),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def referenced_by_cost_centres(self, plant_id: uuid.UUID) -> bool:
        stmt = (
            select(literal(1))
            .select_from(CostCentre)
            .where(
                CostCentre.company_id == self.tenant.company_id,
                CostCentre.plant_id == plant_id,
                CostCentre.deleted_at.is_(None),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def referenced_by_carriers(self, plant_id: uuid.UUID) -> list[str]:
        """Work centres and production orders still naming this plant."""
        return await self._carrier_references(PLANT_REFERENCE_PROBES, plant_id)


# -----------------------------------------------------------------------------
# Department
# -----------------------------------------------------------------------------


class DepartmentRepository(_OrgRepositoryMixin, BaseRepository[Department]):
    model = Department
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": Department.code,
        "name": Department.name,
        "is_active": Department.is_active,
        "created_at": Department.created_at,
        "updated_at": Department.updated_at,
    }
    searchable_fields = ("code", "name")

    def build_list_stmt(self, filters: DepartmentFilters, sort: SortParams) -> Select[Any]:
        stmt = self._scoped(include_deleted=filters.include_deleted)
        stmt = self._apply_search(stmt, filters.search)

        if filters.plant_id is not None:
            stmt = stmt.where(Department.plant_id == filters.plant_id)
        if filters.parent_id is not None:
            stmt = stmt.where(Department.parent_id == filters.parent_id)
        if filters.head_user_id is not None:
            stmt = stmt.where(Department.head_user_id == filters.head_user_id)
        if filters.is_active is not None:
            stmt = stmt.where(Department.is_active == filters.is_active)

        order = sort.resolve(self.sortable_fields, Department.code.asc())
        return stmt.order_by(order, Department.id.asc())

    async def code_taken(self, code: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        stmt = (
            select(literal(1))
            .select_from(Department)
            .where(Department.company_id == self.tenant.company_id, Department.code == code)
        )
        if exclude_id is not None:
            stmt = stmt.where(Department.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def ancestor_ids(self, department_id: uuid.UUID) -> list[uuid.UUID]:
        """Ids from ``department_id`` up to the root, nearest first.

        One recursive CTE rather than a loop of single-row loads: the depth is
        not known in advance, so a loop would be an unbounded query count.
        Soft-deleted rows are included deliberately - a retired department still
        occupies a position in the tree, and skipping it would report a shorter
        chain than the one the ``ondelete="RESTRICT"`` foreign key enforces.
        """
        base = (
            select(
                Department.id.label("id"),
                Department.parent_id.label("parent_id"),
                literal(1).label("depth"),
            )
            .where(
                Department.company_id == self.tenant.company_id,
                Department.id == department_id,
            )
            .cte("department_chain", recursive=True)
        )
        parent = base.alias("chain")
        chain = base.union_all(
            select(
                Department.id,
                Department.parent_id,
                (parent.c.depth + 1).label("depth"),
            ).where(
                Department.id == parent.c.parent_id,
                Department.company_id == self.tenant.company_id,
                parent.c.depth < MAX_HIERARCHY_WALK,
            )
        )
        rows = await self.session.execute(select(chain.c.id).order_by(chain.c.depth.asc()))
        return [row[0] for row in rows.all()]

    async def has_live_children(self, department_id: uuid.UUID) -> bool:
        stmt = (
            select(literal(1))
            .select_from(Department)
            .where(
                Department.company_id == self.tenant.company_id,
                Department.parent_id == department_id,
                Department.deleted_at.is_(None),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def plant_exists(self, plant_id: uuid.UUID) -> bool:
        stmt = (
            select(literal(1))
            .select_from(Plant)
            .where(
                Plant.company_id == self.tenant.company_id,
                Plant.id == plant_id,
                Plant.deleted_at.is_(None),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def user_exists(self, user_id: uuid.UUID) -> bool:
        stmt = (
            select(literal(1))
            .select_from(User)
            .where(
                User.company_id == self.tenant.company_id,
                User.id == user_id,
                User.deleted_at.is_(None),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def referenced_by_users(self, department_id: uuid.UUID) -> bool:
        return await self._user_references(User.department_id, department_id)

    async def referenced_by_carriers(self, department_id: uuid.UUID) -> list[str]:
        """Work centres and purchase requisitions still naming this department.

        Raw SQL: both tables belong to modules above organisation, and
        procurement in particular must not be imported from here.
        """
        return await self._carrier_references(DEPARTMENT_REFERENCE_PROBES, department_id)


# -----------------------------------------------------------------------------
# Cost centre
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CostCentreFacts:
    """What another module needs to know about a cost centre it was handed.

    A projection rather than the ORM row: the batch lookups behind a posting or
    a multi-line document read four columns of many rows, and ``is_archived``
    is the soft-delete flag already interpreted.
    """

    id: uuid.UUID
    code: str
    name: str
    is_active: bool
    is_archived: bool

    @property
    def is_assignable(self) -> bool:
        """May a new document or a new posting name this centre?"""
        return self.is_active and not self.is_archived


class CostCentreRepository(_OrgRepositoryMixin, BaseRepository[CostCentre]):
    model = CostCentre
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": CostCentre.code,
        "name": CostCentre.name,
        "cost_centre_type": CostCentre.cost_centre_type,
        "is_active": CostCentre.is_active,
        "created_at": CostCentre.created_at,
        "updated_at": CostCentre.updated_at,
    }
    searchable_fields = ("code", "name")

    def build_list_stmt(self, filters: CostCentreFilters, sort: SortParams) -> Select[Any]:
        stmt = self._scoped(include_deleted=filters.include_deleted)
        stmt = self._apply_search(stmt, filters.search)

        if filters.parent_id is not None:
            stmt = stmt.where(CostCentre.parent_id == filters.parent_id)
        if filters.plant_id is not None:
            stmt = stmt.where(CostCentre.plant_id == filters.plant_id)
        if filters.cost_centre_type is not None:
            stmt = stmt.where(CostCentre.cost_centre_type == filters.cost_centre_type)
        if filters.is_active is not None:
            stmt = stmt.where(CostCentre.is_active == filters.is_active)

        # Code is the stable, unique, indexed ordering key; id breaks ties so a
        # row cannot appear on two pages.
        order = sort.resolve(self.sortable_fields, CostCentre.code.asc())
        return stmt.order_by(order, CostCentre.id.asc())

    async def code_taken(self, code: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        """Is this code used in the tenant, including by a retired centre?

        Retired codes count: they are printed on posted ledger history, and a
        second centre answering to the same code would merge two lines of a
        cost-centre statement into one.
        """
        stmt = (
            select(literal(1))
            .select_from(CostCentre)
            .where(CostCentre.company_id == self.tenant.company_id, CostCentre.code == code)
        )
        if exclude_id is not None:
            stmt = stmt.where(CostCentre.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def ancestor_ids(self, cost_centre_id: uuid.UUID) -> list[uuid.UUID]:
        """Ids from ``cost_centre_id`` up to the root, nearest first.

        The department walk, over this table: one bounded recursive CTE, retired
        rows included because they still occupy their position in the tree.
        """
        base = (
            select(
                CostCentre.id.label("id"),
                CostCentre.parent_id.label("parent_id"),
                literal(1).label("depth"),
            )
            .where(
                CostCentre.company_id == self.tenant.company_id,
                CostCentre.id == cost_centre_id,
            )
            .cte("cost_centre_chain", recursive=True)
        )
        parent = base.alias("chain")
        chain = base.union_all(
            select(
                CostCentre.id,
                CostCentre.parent_id,
                (parent.c.depth + 1).label("depth"),
            ).where(
                CostCentre.id == parent.c.parent_id,
                CostCentre.company_id == self.tenant.company_id,
                parent.c.depth < MAX_HIERARCHY_WALK,
            )
        )
        rows = await self.session.execute(select(chain.c.id).order_by(chain.c.depth.asc()))
        return [row[0] for row in rows.all()]

    async def subtree_height(self, cost_centre_id: uuid.UUID) -> int:
        """Levels *below* this centre: 0 for a leaf, 1 if it has children only.

        Reparenting moves the whole subtree, so the depth rule has to count the
        deepest descendant, not just the node being moved - otherwise a centre
        with grandchildren could be dropped onto the bottom of the tree and
        leave them below the limit nobody may create a row at. Live rows only:
        a retired descendant can never gain a child.
        """
        base = (
            select(CostCentre.id.label("id"), literal(0).label("depth"))
            .where(
                CostCentre.company_id == self.tenant.company_id,
                CostCentre.id == cost_centre_id,
            )
            .cte("cost_centre_subtree", recursive=True)
        )
        child = base.alias("subtree")
        tree = base.union_all(
            select(CostCentre.id, (child.c.depth + 1).label("depth")).where(
                CostCentre.parent_id == child.c.id,
                CostCentre.company_id == self.tenant.company_id,
                CostCentre.deleted_at.is_(None),
                child.c.depth < MAX_HIERARCHY_WALK,
            )
        )
        found = await self.session.execute(select(func.max(tree.c.depth)))
        return int(found.scalar() or 0)

    async def has_live_children(self, cost_centre_id: uuid.UUID) -> bool:
        """Any non-retired child, active or not - the archive guard."""
        return await self._child_exists(cost_centre_id, active_only=False)

    async def has_active_children(self, cost_centre_id: uuid.UUID) -> bool:
        """Any active child - the deactivation guard."""
        return await self._child_exists(cost_centre_id, active_only=True)

    async def _child_exists(self, cost_centre_id: uuid.UUID, *, active_only: bool) -> bool:
        stmt = (
            select(literal(1))
            .select_from(CostCentre)
            .where(
                CostCentre.company_id == self.tenant.company_id,
                CostCentre.parent_id == cost_centre_id,
                CostCentre.deleted_at.is_(None),
            )
            .limit(1)
        )
        if active_only:
            stmt = stmt.where(CostCentre.is_active.is_(True))
        return (await self.session.execute(stmt)).first() is not None

    async def plant_exists(self, plant_id: uuid.UUID) -> bool:
        stmt = (
            select(literal(1))
            .select_from(Plant)
            .where(
                Plant.company_id == self.tenant.company_id,
                Plant.id == plant_id,
                Plant.deleted_at.is_(None),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def user_exists(self, user_id: uuid.UUID) -> bool:
        stmt = (
            select(literal(1))
            .select_from(User)
            .where(
                User.company_id == self.tenant.company_id,
                User.id == user_id,
                User.deleted_at.is_(None),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def referenced_by_departments(self, cost_centre_id: uuid.UUID) -> bool:
        """A live department names this centre as its default."""
        stmt = (
            select(literal(1))
            .select_from(Department)
            .where(
                Department.company_id == self.tenant.company_id,
                Department.cost_centre_id == cost_centre_id,
                Department.deleted_at.is_(None),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def referenced_as_default(self, cost_centre_id: uuid.UUID) -> list[str]:
        """Carriers above organisation where this centre is a default."""
        return await self._carrier_references(COST_CENTRE_DEFAULT_PROBES, cost_centre_id)

    async def referenced_by_carriers(self, cost_centre_id: uuid.UUID) -> list[str]:
        """Every carrier above organisation that still names this centre."""
        return await self._carrier_references(COST_CENTRE_REFERENCE_PROBES, cost_centre_id)

    async def facts(
        self, cost_centre_ids: Iterable[uuid.UUID], *, include_archived: bool
    ) -> dict[uuid.UUID, CostCentreFacts]:
        """Tenant-scoped facts for a set of ids, in one statement.

        An id from another company is simply absent from the answer, exactly as
        an id that never existed is - the caller reports both as "not found",
        because confirming that a row exists elsewhere is itself a disclosure.
        """
        wanted = {cost_centre_id for cost_centre_id in cost_centre_ids if cost_centre_id}
        if not wanted:
            return {}
        stmt = select(
            CostCentre.id,
            CostCentre.code,
            CostCentre.name,
            CostCentre.is_active,
            CostCentre.deleted_at,
        ).where(
            CostCentre.company_id == self.tenant.company_id,
            CostCentre.id.in_(wanted),
        )
        if not include_archived:
            stmt = stmt.where(CostCentre.deleted_at.is_(None))
        return {
            row.id: CostCentreFacts(
                id=row.id,
                code=row.code,
                name=row.name,
                is_active=bool(row.is_active),
                is_archived=row.deleted_at is not None,
            )
            for row in (await self.session.execute(stmt)).all()
        }


# -----------------------------------------------------------------------------
# Financial year
# -----------------------------------------------------------------------------


class FinancialYearRepository(BaseRepository[FinancialYear]):
    model = FinancialYear
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": FinancialYear.code,
        "start_date": FinancialYear.start_date,
        "end_date": FinancialYear.end_date,
        "is_active": FinancialYear.is_active,
        "is_closed": FinancialYear.is_closed,
        "created_at": FinancialYear.created_at,
    }
    searchable_fields = ("code",)

    def build_list_stmt(self, filters: FinancialYearFilters, sort: SortParams) -> Select[Any]:
        stmt = self._scoped(include_deleted=filters.include_deleted)
        stmt = self._apply_search(stmt, filters.search)

        if filters.is_active is not None:
            stmt = stmt.where(FinancialYear.is_active == filters.is_active)
        if filters.is_closed is not None:
            stmt = stmt.where(FinancialYear.is_closed == filters.is_closed)
        if filters.contains_date is not None:
            stmt = stmt.where(
                FinancialYear.start_date <= filters.contains_date,
                FinancialYear.end_date >= filters.contains_date,
            )

        # Newest period first: that is the one a user is almost always looking
        # for, and it matches the (company_id, start_date, end_date) index.
        order = sort.resolve(self.sortable_fields, FinancialYear.start_date.desc())
        return stmt.order_by(order, FinancialYear.id.asc())

    async def code_taken(self, code: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        stmt = (
            select(literal(1))
            .select_from(FinancialYear)
            .where(
                FinancialYear.company_id == self.tenant.company_id,
                FinancialYear.code == code,
            )
        )
        if exclude_id is not None:
            stmt = stmt.where(FinancialYear.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def overlapping_active(
        self,
        start_date: Any,
        end_date: Any,
        *,
        exclude_id: uuid.UUID | None = None,
    ) -> FinancialYear | None:
        """The first live, active year whose range intersects ``[start, end]``.

        Both endpoints are inclusive, so the predicate is the standard closed
        interval overlap: ``existing.start <= new.end AND existing.end >=
        new.start``. Only ``is_active`` years take part - a closed year is
        history and may legitimately be superseded by a re-cut period.

        This is a check-then-write, so it is not a substitute for a database
        constraint: two concurrent creates can both pass it. The stronger form
        is a ``btree_gist`` EXCLUDE constraint on
        ``(company_id WITH =, daterange(start_date, end_date, '[]') WITH &&)``
        filtered on ``is_active AND deleted_at IS NULL``, which PostgreSQL can
        enforce atomically. Adding it needs an Alembic revision and the
        ``btree_gist`` extension, which this module does not own; the service
        check is the interim guard and it runs inside the write transaction.
        """
        stmt = self._scoped().where(
            FinancialYear.is_active.is_(True),
            FinancialYear.start_date <= end_date,
            FinancialYear.end_date >= start_date,
        )
        # Editing a year must not find itself: without this an ordinary date
        # correction would be rejected as overlapping the row being corrected.
        if exclude_id is not None:
            stmt = stmt.where(FinancialYear.id != exclude_id)
        stmt = stmt.order_by(FinancialYear.start_date.asc()).limit(1)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def document_numbers_issued(self, period_key: str) -> bool:
        """Has any document already been numbered inside this period?

        ASSUMPTION: ``document_sequences.period_key`` holds the financial year
        label produced by :func:`src.common.codes.financial_year_key`
        (``2026-27``), and a financial year is created with that same label as
        its code. Matching on the string is therefore the only link available -
        ``document_sequences`` carries no ``financial_year_id``. The check is
        conservative: a code that does not follow the convention simply finds
        nothing and the year stays deletable.
        """
        stmt = (
            select(literal(1))
            .select_from(DocumentSequence)
            .where(
                DocumentSequence.company_id == self.tenant.company_id,
                DocumentSequence.period_key == period_key,
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).first() is not None


__all__ = [
    "COST_CENTRE_DEFAULT_PROBES",
    "COST_CENTRE_REFERENCE_PROBES",
    "DEPARTMENT_REFERENCE_PROBES",
    "MAX_HIERARCHY_WALK",
    "PLANT_REFERENCE_PROBES",
    "BranchRepository",
    "CompanyRepository",
    "CostCentreFacts",
    "CostCentreRepository",
    "DepartmentRepository",
    "FinancialYearRepository",
    "PlantRepository",
    "ReferenceProbe",
    "reference_labels",
]
