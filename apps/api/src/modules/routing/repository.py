"""Queries for routings, work centres and machines.

Two shapes in this file carry the module's performance story.

* **Costing and lead time load a routing in one join.** ``costed_operations``
  selects the operation columns *and* the work-centre rate and shift length in a
  single statement, so the pure calculation functions in ``service`` never have
  to reach back into the database. Relationships are ``lazy="raise"`` by design
  (database.md section 1); a per-operation rate lookup would be an N+1 on the
  hottest read in the module, and this is the query that makes it impossible.
* **Everything the planner asks for is set-at-a-time.**
  ``effective_routings_for_items`` and ``costed_operations_for_routings`` take a
  collection and answer in one statement, because MRP resolves the manufacture
  lead time for a whole shortage list at once, not item by item.

Projections are explicit: no ``SELECT *`` on ``routings`` when three columns are
wanted (master rules section 30).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, delete, exists, func, literal, or_, select
from sqlalchemy.orm import InstrumentedAttribute

from src.common.repository import BaseRepository
from src.core.database import affected_rows
from src.core.pagination import SortParams

# Read-only references to other modules' master tables. Routing validates that a
# plant, department or item belongs to the same tenant before pointing at it; it
# never writes to those tables.
from src.modules.organization.models import Department, Plant
from src.modules.products.models import Item
from src.modules.routing.models import (
    Machine,
    MachineStatus,
    Routing,
    RoutingOperation,
    RoutingStatus,
    WorkCenter,
)
from src.modules.routing.schemas import MachineFilters, RoutingFilters, WorkCenterFilters

#: Statuses that still describe how the plant is meant to work. ``OBSOLETE`` is
#: excluded from "is this work centre still in use?" checks: a retired process
#: must not block a work centre from being retired too.
LIVE_ROUTING_STATUSES: tuple[RoutingStatus, ...] = (RoutingStatus.DRAFT, RoutingStatus.APPROVED)

#: Machine states in which a machine can accept work. Used to warn on approval
#: rather than to block it - a machine down for maintenance today does not make
#: the routing wrong.
WORKABLE_MACHINE_STATUSES: tuple[MachineStatus, ...] = (
    MachineStatus.AVAILABLE,
    MachineStatus.RUNNING,
)


@dataclass(frozen=True, slots=True)
class WorkCenterRates:
    """The work-centre facts costing and scheduling need.

    A projection rather than the entity: the roll-up reads four columns of a row
    that also carries addresses, descriptions and audit stamps, and loading the
    whole thing to use a third of it is the ``SELECT *`` the rules forbid.
    """

    id: uuid.UUID
    code: str
    name: str
    cost_per_hour: Decimal
    working_hours_per_day: Decimal
    is_active: bool


@dataclass(frozen=True, slots=True)
class RoutingItemInfo:
    """The item-master facts routing validation needs.

    Three columns of a very wide table (master rules section 3 lists nine
    capability flags alone), so it is a projection rather than an ``Item``.
    """

    id: uuid.UUID
    code: str
    can_manufacture: bool
    is_active: bool


@dataclass(frozen=True, slots=True)
class MachineInfo:
    """The machine facts routing validation needs."""

    id: uuid.UUID
    code: str
    work_center_id: uuid.UUID
    status: MachineStatus
    is_active: bool


@dataclass(frozen=True, slots=True)
class CostedOperation:
    """One routing step with its work-centre rate already resolved.

    Frozen and free of ORM state on purpose: the time, cost and lead-time
    functions in ``service`` are pure and unit-tested without a database, and
    they can only stay that way if the rate arrives with the step rather than
    being fetched from a relationship mid-calculation.
    """

    sequence: int
    operation_code: str
    operation_name: str
    work_center_id: uuid.UUID
    work_center_code: str
    work_center_cost_per_hour: Decimal
    work_center_working_hours_per_day: Decimal
    setup_time_minutes: Decimal
    run_time_per_unit_minutes: Decimal
    queue_time_minutes: Decimal
    move_time_minutes: Decimal
    process_cost: Decimal
    is_outsourced: bool


@dataclass(frozen=True, slots=True)
class CostedRouting:
    """A routing reduced to what costing and scheduling read.

    ``id`` and ``code`` are carried so a result can name the routing it came
    from without a second query.
    """

    id: uuid.UUID
    code: str
    item_id: uuid.UUID
    operations: tuple[CostedOperation, ...]


# -----------------------------------------------------------------------------
# Cross-module lookups
# -----------------------------------------------------------------------------


class ItemLookupRepository(BaseRepository[Item]):
    """Read-only access to the item master.

    Routing owns no item data; it joins to ``items`` because a process is
    meaningless without the part it makes. Going through ``BaseRepository``
    keeps the tenant and soft-delete filters automatic rather than something
    each query has to remember.
    """

    model = Item

    async def load(self, item_id: uuid.UUID) -> RoutingItemInfo | None:
        stmt = select(Item.id, Item.code, Item.can_manufacture, Item.is_active).where(
            Item.company_id == self.tenant.company_id,
            Item.deleted_at.is_(None),
            Item.id == item_id,
        )
        row = (await self.session.execute(stmt)).first()
        if row is None:
            return None
        return RoutingItemInfo(
            id=row.id,
            code=row.code,
            can_manufacture=row.can_manufacture,
            is_active=row.is_active,
        )


# -----------------------------------------------------------------------------
# Work centres
# -----------------------------------------------------------------------------


#: The record's user-facing name. Shared by the not-found path here, the
#: service's own not-found messages and ``WORK_CENTER_LIFECYCLE``, so one
#: missing work centre is never "WorkCenter" in one message and "Work centre"
#: in the next.
WORK_CENTER_ENTITY = "Work centre"


class WorkCenterRepository(BaseRepository[WorkCenter]):
    model = WorkCenter
    # One user-facing name for the record, shared with the service's own
    # not-found and state-transition messages.
    entity_label = WORK_CENTER_ENTITY
    searchable_fields = ("code", "name")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": WorkCenter.code,
        "name": WorkCenter.name,
        "cost_per_hour": WorkCenter.cost_per_hour,
        "is_active": WorkCenter.is_active,
        "created_at": WorkCenter.created_at,
        "updated_at": WorkCenter.updated_at,
    }

    def build_list_stmt(self, filters: WorkCenterFilters, sort: SortParams) -> Select[Any]:
        stmt = self._scoped(include_deleted=filters.include_deleted)
        stmt = self._apply_search(stmt, filters.search)

        if filters.plant_id is not None:
            stmt = stmt.where(WorkCenter.plant_id == filters.plant_id)
        if filters.department_id is not None:
            stmt = stmt.where(WorkCenter.department_id == filters.department_id)
        if filters.is_active is not None:
            stmt = stmt.where(WorkCenter.is_active.is_(filters.is_active))

        # Code is the stable, unique, indexed ordering key; id breaks ties so a
        # row cannot appear on two pages.
        order = sort.resolve(self.sortable_fields, WorkCenter.code.asc())
        return stmt.order_by(order, WorkCenter.id.asc())

    async def code_taken(self, code: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        """Is this code already used in the tenant, including by a retired row?

        Soft-deleted rows count: their code is quoted on historical travellers,
        so reusing it would make two different centres read as one in the
        archive.
        """
        stmt = select(literal(1)).where(
            WorkCenter.company_id == self.tenant.company_id, WorkCenter.code == code
        )
        if exclude_id is not None:
            stmt = stmt.where(WorkCenter.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def rates(self, work_center_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, WorkCenterRates]:
        """Batched id -> rate facts. One statement for a whole routing."""
        if not work_center_ids:
            return {}
        stmt = select(
            WorkCenter.id,
            WorkCenter.code,
            WorkCenter.name,
            WorkCenter.cost_per_hour,
            WorkCenter.working_hours_per_day,
            WorkCenter.is_active,
        ).where(
            WorkCenter.company_id == self.tenant.company_id,
            WorkCenter.deleted_at.is_(None),
            WorkCenter.id.in_(list(dict.fromkeys(work_center_ids))),
        )
        return {
            row.id: WorkCenterRates(
                id=row.id,
                code=row.code,
                name=row.name,
                cost_per_hour=row.cost_per_hour,
                working_hours_per_day=row.working_hours_per_day,
                is_active=row.is_active,
            )
            for row in (await self.session.execute(stmt))
        }

    async def machine_counts(self, work_center_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        """How many live machines sit in each centre - one grouped query."""
        if not work_center_ids:
            return {}
        stmt = (
            select(Machine.work_center_id, func.count(Machine.id))
            .where(
                Machine.company_id == self.tenant.company_id,
                Machine.deleted_at.is_(None),
                Machine.work_center_id.in_(list(dict.fromkeys(work_center_ids))),
            )
            .group_by(Machine.work_center_id)
        )
        return {row[0]: int(row[1]) for row in (await self.session.execute(stmt))}

    async def organisation_refs_exist(
        self, *, plant_id: uuid.UUID | None, department_id: uuid.UUID | None
    ) -> tuple[bool, bool]:
        """Confirm a plant and a department belong to this tenant.

        Both are probed in one round trip rather than two: they are always
        supplied together, and a foreign key alone would let a work centre point
        at another company's plant - the FK does not know about ``company_id``.
        ``None`` means "not supplied", which is always valid.
        """
        plant_ok = plant_id is None
        department_ok = department_id is None
        if plant_ok and department_ok:
            return True, True

        checks = []
        if not plant_ok:
            checks.append(
                select(literal("plant").label("kind"), Plant.id).where(
                    Plant.company_id == self.tenant.company_id,
                    Plant.deleted_at.is_(None),
                    Plant.id == plant_id,
                )
            )
        if not department_ok:
            checks.append(
                select(literal("department").label("kind"), Department.id).where(
                    Department.company_id == self.tenant.company_id,
                    Department.deleted_at.is_(None),
                    Department.id == department_id,
                )
            )
        stmt = checks[0] if len(checks) == 1 else checks[0].union_all(checks[1])

        for row in await self.session.execute(stmt):
            if row.kind == "plant":
                plant_ok = True
            else:
                department_ok = True
        return plant_ok, department_ok

    async def used_by_live_routing(self, work_center_id: uuid.UUID) -> bool:
        """Is a draft or approved routing still planning work at this centre?"""
        probe = (
            select(literal(1))
            .select_from(RoutingOperation)
            .join(Routing, Routing.id == RoutingOperation.routing_id)
            .where(
                RoutingOperation.company_id == self.tenant.company_id,
                RoutingOperation.work_center_id == work_center_id,
                Routing.status.in_(list(LIVE_ROUTING_STATUSES)),
            )
        )
        return bool((await self.session.execute(select(exists(probe)))).scalar_one())


# -----------------------------------------------------------------------------
# Machines
# -----------------------------------------------------------------------------


class MachineRepository(BaseRepository[Machine]):
    model = Machine
    searchable_fields = ("code", "name", "machine_type")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": Machine.code,
        "name": Machine.name,
        "status": Machine.status,
        "next_maintenance_date": Machine.next_maintenance_date,
        "created_at": Machine.created_at,
        "updated_at": Machine.updated_at,
    }

    def build_list_stmt(self, filters: MachineFilters, sort: SortParams) -> Select[Any]:
        stmt = self._scoped(include_deleted=filters.include_deleted)
        stmt = self._apply_search(stmt, filters.search)

        if filters.work_center_id is not None:
            stmt = stmt.where(Machine.work_center_id == filters.work_center_id)
        if filters.status is not None:
            stmt = stmt.where(Machine.status == filters.status)
        if filters.is_active is not None:
            stmt = stmt.where(Machine.is_active.is_(filters.is_active))
        if filters.maintenance_due_before is not None:
            # NULL is "no service scheduled", which is not the same as "not due"
            # - it is simply unknown, and it must not appear on a due list.
            stmt = stmt.where(
                Machine.next_maintenance_date.is_not(None),
                Machine.next_maintenance_date <= filters.maintenance_due_before,
            )

        order = sort.resolve(self.sortable_fields, Machine.code.asc())
        return stmt.order_by(order, Machine.id.asc())

    async def code_taken(self, code: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        stmt = select(literal(1)).where(
            Machine.company_id == self.tenant.company_id, Machine.code == code
        )
        if exclude_id is not None:
            stmt = stmt.where(Machine.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def load(self, machine_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, MachineInfo]:
        """Batched id -> machine facts, for validating a whole routing at once."""
        if not machine_ids:
            return {}
        stmt = select(
            Machine.id,
            Machine.code,
            Machine.work_center_id,
            Machine.status,
            Machine.is_active,
        ).where(
            Machine.company_id == self.tenant.company_id,
            Machine.deleted_at.is_(None),
            Machine.id.in_(list(dict.fromkeys(machine_ids))),
        )
        return {
            row.id: MachineInfo(
                id=row.id,
                code=row.code,
                work_center_id=row.work_center_id,
                status=MachineStatus(row.status),
                is_active=row.is_active,
            )
            for row in (await self.session.execute(stmt))
        }

    async def used_by_live_routing(self, machine_id: uuid.UUID) -> bool:
        probe = (
            select(literal(1))
            .select_from(RoutingOperation)
            .join(Routing, Routing.id == RoutingOperation.routing_id)
            .where(
                RoutingOperation.company_id == self.tenant.company_id,
                RoutingOperation.machine_id == machine_id,
                Routing.status.in_(list(LIVE_ROUTING_STATUSES)),
            )
        )
        return bool((await self.session.execute(select(exists(probe)))).scalar_one())


# -----------------------------------------------------------------------------
# Routings
# -----------------------------------------------------------------------------


class RoutingRepository(BaseRepository[Routing]):
    model = Routing
    searchable_fields = ("code", "name")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": Routing.code,
        "version": Routing.version,
        "status": Routing.status,
        "effective_from": Routing.effective_from,
        "created_at": Routing.created_at,
        "updated_at": Routing.updated_at,
    }

    def build_list_stmt(self, filters: RoutingFilters, sort: SortParams) -> Select[Any]:
        stmt = self._apply_search(self._scoped(), filters.search)

        if filters.item_id is not None:
            stmt = stmt.where(Routing.item_id == filters.item_id)
        if filters.status is not None:
            stmt = stmt.where(Routing.status == filters.status)
        if filters.is_default is not None:
            stmt = stmt.where(Routing.is_default.is_(filters.is_default))
        if filters.effective_on is not None:
            stmt = stmt.where(
                Routing.effective_from <= filters.effective_on,
                or_(Routing.effective_to.is_(None), Routing.effective_to >= filters.effective_on),
            )
        if filters.work_center_id is not None:
            # EXISTS rather than a join: a routing with three operations at the
            # same centre must appear once, and DISTINCT over the whole row to
            # undo a join's fan-out would force a sort of the entire result.
            stmt = stmt.where(
                exists(
                    select(literal(1)).where(
                        RoutingOperation.routing_id == Routing.id,
                        RoutingOperation.company_id == self.tenant.company_id,
                        RoutingOperation.work_center_id == filters.work_center_id,
                    )
                )
            )

        order = sort.resolve(self.sortable_fields, Routing.created_at.desc())
        return stmt.order_by(order, Routing.id.desc())

    async def code_taken(self, code: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        stmt = select(literal(1)).where(
            Routing.company_id == self.tenant.company_id, Routing.code == code
        )
        if exclude_id is not None:
            stmt = stmt.where(Routing.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def max_version_for_item(self, item_id: uuid.UUID) -> int:
        """Highest version issued for an item, 0 when there is none.

        Only safe under the per-item advisory lock the service takes first: two
        concurrent readers would otherwise both propose the same number and the
        second insert would fail on the unique constraint.
        """
        stmt = select(func.max(Routing.version)).where(
            Routing.company_id == self.tenant.company_id, Routing.item_id == item_id
        )
        return int((await self.session.execute(stmt)).scalar() or 0)

    def versions_stmt(self, item_id: uuid.UUID) -> Select[Any]:
        return self._scoped().where(Routing.item_id == item_id)

    async def effective_routings_for_items(
        self, item_ids: Sequence[uuid.UUID], as_of: date
    ) -> dict[uuid.UUID, Routing]:
        """The routing to use for each item on ``as_of`` - one ``DISTINCT ON``.

        Ranking: the default wins, then the highest version, so an item with a
        default plus two superseded-but-still-effective revisions resolves
        deterministically rather than by whichever row came back first. Only
        ``APPROVED`` rows are considered: planning must never be driven by a
        process an engineer is still editing.
        """
        if not item_ids:
            return {}
        stmt = (
            self._scoped()
            .where(
                Routing.item_id.in_(list(dict.fromkeys(item_ids))),
                Routing.status == RoutingStatus.APPROVED,
                Routing.effective_from <= as_of,
                or_(Routing.effective_to.is_(None), Routing.effective_to >= as_of),
            )
            .distinct(Routing.item_id)
            .order_by(Routing.item_id, Routing.is_default.desc(), Routing.version.desc())
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return {routing.item_id: routing for routing in rows}

    async def list_default_candidates(self, item_id: uuid.UUID) -> list[Routing]:
        """Approved routings currently flagged default for an item.

        Loaded as entities rather than updated in bulk: they carry an
        optimistic-lock column, and a Core ``UPDATE`` would leave the in-session
        copies stale while the row version moved underneath them.
        """
        stmt = self._scoped().where(
            Routing.item_id == item_id,
            Routing.is_default.is_(True),
            Routing.status == RoutingStatus.APPROVED,
        )
        return list((await self.session.execute(stmt)).scalars().all())


class RoutingOperationRepository(BaseRepository[RoutingOperation]):
    model = RoutingOperation
    sortable_fields = {"sequence": RoutingOperation.sequence}  # noqa: RUF012

    async def list_for_routing(self, routing_id: uuid.UUID) -> list[RoutingOperation]:
        """Every step of one routing, in execution order.

        Not paginated, by deliberate exception to section 31: the set is bounded
        by ``MAX_OPERATIONS_PER_ROUTING`` at write time, and a process is only
        meaningful whole.
        """
        stmt = (
            self._scoped()
            .where(RoutingOperation.routing_id == routing_id)
            .order_by(RoutingOperation.sequence)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def operations_for_routings(
        self, routing_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[RoutingOperation]]:
        """Steps for several routings in one statement."""
        if not routing_ids:
            return {}
        stmt = (
            self._scoped()
            .where(RoutingOperation.routing_id.in_(list(dict.fromkeys(routing_ids))))
            .order_by(RoutingOperation.routing_id, RoutingOperation.sequence)
        )
        grouped: dict[uuid.UUID, list[RoutingOperation]] = {}
        for operation in (await self.session.execute(stmt)).scalars().all():
            grouped.setdefault(operation.routing_id, []).append(operation)
        return grouped

    def _costed_stmt(self, routing_ids: Sequence[uuid.UUID]) -> Select[Any]:
        """Operations joined to their work-centre rates, named columns only."""
        return (
            select(
                RoutingOperation.routing_id,
                RoutingOperation.sequence,
                RoutingOperation.operation_code,
                RoutingOperation.operation_name,
                RoutingOperation.work_center_id,
                RoutingOperation.setup_time_minutes,
                RoutingOperation.run_time_per_unit_minutes,
                RoutingOperation.queue_time_minutes,
                RoutingOperation.move_time_minutes,
                RoutingOperation.process_cost,
                RoutingOperation.is_outsourced,
                WorkCenter.code.label("work_center_code"),
                WorkCenter.cost_per_hour.label("work_center_cost_per_hour"),
                WorkCenter.working_hours_per_day.label("work_center_working_hours_per_day"),
            )
            .join(WorkCenter, WorkCenter.id == RoutingOperation.work_center_id)
            .where(
                RoutingOperation.company_id == self.tenant.company_id,
                WorkCenter.company_id == self.tenant.company_id,
                RoutingOperation.routing_id.in_(list(dict.fromkeys(routing_ids))),
            )
            .order_by(RoutingOperation.routing_id, RoutingOperation.sequence)
        )

    async def costed_operations(self, routing_id: uuid.UUID) -> list[CostedOperation]:
        """One routing's steps with their rates resolved, in one query."""
        rows = await self.session.execute(self._costed_stmt([routing_id]))
        return [_costed(row) for row in rows]

    async def costed_operations_for_routings(
        self, routing_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[CostedOperation]]:
        """The batched form, for costing a planner's whole shortage list."""
        if not routing_ids:
            return {}
        grouped: dict[uuid.UUID, list[CostedOperation]] = {}
        for row in await self.session.execute(self._costed_stmt(routing_ids)):
            grouped.setdefault(row.routing_id, []).append(_costed(row))
        return grouped

    async def delete_for_routing(self, routing_id: uuid.UUID) -> int:
        """Drop a draft's steps before rewriting them.

        A Core DELETE rather than a loop: replacing a 40-step routing must be
        one statement. ``synchronize_session=False`` is safe because the service
        re-reads the operations after the rewrite.
        """
        stmt = (
            delete(RoutingOperation)
            .where(
                RoutingOperation.company_id == self.tenant.company_id,
                RoutingOperation.routing_id == routing_id,
            )
            .execution_options(synchronize_session=False)
        )
        return affected_rows(await self.session.execute(stmt))


def _costed(row: Any) -> CostedOperation:
    return CostedOperation(
        sequence=row.sequence,
        operation_code=row.operation_code,
        operation_name=row.operation_name,
        work_center_id=row.work_center_id,
        work_center_code=row.work_center_code,
        work_center_cost_per_hour=row.work_center_cost_per_hour,
        work_center_working_hours_per_day=row.work_center_working_hours_per_day,
        setup_time_minutes=row.setup_time_minutes,
        run_time_per_unit_minutes=row.run_time_per_unit_minutes,
        queue_time_minutes=row.queue_time_minutes,
        move_time_minutes=row.move_time_minutes,
        process_cost=row.process_cost,
        is_outsourced=row.is_outsourced,
    )


__all__ = [
    "LIVE_ROUTING_STATUSES",
    "WORKABLE_MACHINE_STATUSES",
    "CostedOperation",
    "CostedRouting",
    "ItemLookupRepository",
    "MachineInfo",
    "MachineRepository",
    "RoutingItemInfo",
    "RoutingOperationRepository",
    "RoutingRepository",
    "WorkCenterRates",
    "WorkCenterRepository",
]
