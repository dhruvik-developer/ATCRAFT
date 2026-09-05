"""Queries for bills of materials.

Everything the explosion engine needs is exposed here as a **set-at-a-time**
call: ``effective_boms_for_items``, ``lines_for_boms``, ``operations_for_boms``
and ``ItemLookupRepository.load`` all take a collection of ids and answer in one
statement. That shape is not a convenience, it is the whole performance story of
this module - a traversal that asked per node would issue one query per part on
a structure with hundreds of parts (master rules section 32). There is
deliberately no ``lines_for_bom(bom_id)`` variant used inside the engine, so the
per-node call is not even available to reach for.

Two other choices worth knowing about:

* **Effective-BOM resolution is one ``DISTINCT ON``.** "The structure for this
  item on this date" is a ranking - prefer the default, then the highest version
  - and PostgreSQL can answer it for a hundred items in a single index scan.
  Doing it in Python would mean fetching every approved revision of every item.
* **Projections are explicit.** The engine's BOM query names its columns with
  ``load_only(..., raiseload=True)``: touching a column the traversal did not
  ask for raises instead of silently emitting another query per row.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.orm import InstrumentedAttribute, load_only

from src.common.repository import BaseRepository
from src.core.database import affected_rows
from src.core.pagination import PageParams, SortParams, paginate
from src.core.responses import OffsetPagination
from src.modules.bom.models import Bom, BomLine, BomOperation, BomStatus
from src.modules.bom.schemas import BomFilters
from src.modules.products.models import Item, ItemVendorLink

#: Statuses a structure can be in while it is still part of the live
#: engineering record. OBSOLETE is excluded: a where-used impact analysis is
#: about what a change would break now, not what it would have broken.
LIVE_BOM_STATUSES: tuple[BomStatus, ...] = (
    BomStatus.DRAFT,
    BomStatus.PENDING_APPROVAL,
    BomStatus.APPROVED,
)

#: Columns the traversal reads. ``row_version`` is included even though the
#: engine never reads it: it is the optimistic-lock column, and SQLAlchemy needs
#: its value loaded if the same instance is later updated in this session.
EXPLOSION_BOM_COLUMNS: tuple[InstrumentedAttribute[Any], ...] = (
    Bom.id,
    Bom.code,
    Bom.item_id,
    Bom.version,
    Bom.status,
    Bom.bom_type,
    Bom.is_default,
    Bom.output_quantity,
    Bom.base_uom_id,
    Bom.effective_from,
    Bom.effective_to,
    Bom.row_version,
)


@dataclass(frozen=True, slots=True)
class ItemInfo:
    """The item-master facts the BOM engine needs.

    A plain projection rather than an ``Item`` entity: the engine reads ten
    columns of a very wide table for every part in a structure, and loading the
    whole row (with its nine capability flags, prices and planning parameters)
    to use a third of it is exactly the ``SELECT *`` the rules forbid.
    """

    id: uuid.UUID
    code: str
    name: str
    base_uom_id: uuid.UUID
    standard_cost: Decimal
    production_type: str
    can_purchase: bool
    can_manufacture: bool
    can_be_bom_component: bool
    is_active: bool

    @property
    def is_purchased(self) -> bool:
        """Read from the capability flag, never from ``item_group``.

        Master rules section 1: one item may be bought *and* made, so this is
        not the negation of :attr:`is_manufactured`.
        """
        return self.can_purchase

    @property
    def is_manufactured(self) -> bool:
        return self.can_manufacture


class ItemLookupRepository(BaseRepository[Item]):
    """Read-only access to the item master for BOM validation and costing.

    The BOM module owns no item data; it joins to ``items`` because a structure
    is meaningless without the parts it names. Going through ``BaseRepository``
    keeps the tenant filter and the soft-delete filter automatic rather than
    something each query has to remember.
    """

    model = Item

    async def load(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, ItemInfo]:
        """Batched id -> item facts. One statement for a whole explosion level."""
        if not item_ids:
            return {}
        stmt = select(
            Item.id,
            Item.code,
            Item.name,
            Item.base_uom_id,
            Item.standard_cost,
            Item.production_type,
            Item.can_purchase,
            Item.can_manufacture,
            Item.can_be_bom_component,
            Item.is_active,
        ).where(
            Item.company_id == self.tenant.company_id,
            Item.deleted_at.is_(None),
            Item.id.in_(list(dict.fromkeys(item_ids))),
        )
        rows = (await self.session.execute(stmt)).all()
        return {
            row.id: ItemInfo(
                id=row.id,
                code=row.code,
                name=row.name,
                base_uom_id=row.base_uom_id,
                standard_cost=row.standard_cost,
                production_type=str(row.production_type),
                can_purchase=row.can_purchase,
                can_manufacture=row.can_manufacture,
                can_be_bom_component=row.can_be_bom_component,
                is_active=row.is_active,
            )
            for row in rows
        }

    async def last_purchase_prices(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Decimal]:
        """Fallback cost for items with no standard cost on file.

        ``DISTINCT ON`` picks the preferred vendor first, then the most recent
        purchase - one statement for every leaf of the structure rather than a
        lookup per part.
        """
        if not item_ids:
            return {}
        stmt = (
            select(ItemVendorLink.item_id, ItemVendorLink.last_purchase_price)
            .where(
                ItemVendorLink.company_id == self.tenant.company_id,
                ItemVendorLink.deleted_at.is_(None),
                ItemVendorLink.is_active.is_(True),
                ItemVendorLink.last_purchase_price.is_not(None),
                ItemVendorLink.item_id.in_(list(dict.fromkeys(item_ids))),
            )
            .distinct(ItemVendorLink.item_id)
            .order_by(
                ItemVendorLink.item_id,
                ItemVendorLink.is_preferred.desc(),
                ItemVendorLink.last_purchase_date.desc().nullslast(),
            )
        )
        return {row.item_id: row.last_purchase_price for row in (await self.session.execute(stmt))}

    async def uom_exists(self, uom_id: uuid.UUID) -> bool:
        """Confirm a unit belongs to this tenant before it is written to a line."""
        from src.modules.products.models import UnitOfMeasure

        stmt = select(UnitOfMeasure.id).where(
            UnitOfMeasure.company_id == self.tenant.company_id,
            UnitOfMeasure.deleted_at.is_(None),
            UnitOfMeasure.id == uom_id,
        )
        return (await self.session.execute(stmt.limit(1))).first() is not None


class BomRepository(BaseRepository[Bom]):
    model = Bom
    #: What ``BaseRepository.get_or_404`` calls the record in its not-found
    #: sentence. Set explicitly because the automatic split of the class name
    #: would say "Bom", and every other message in this module says "BOM".
    entity_label = "BOM"
    searchable_fields = ("code", "revision")
    sortable_fields = {  # noqa: RUF012 - class-level configuration, mirrors the base class
        "code": Bom.code,
        "version": Bom.version,
        "status": Bom.status,
        "effective_from": Bom.effective_from,
        "created_at": Bom.created_at,
        "updated_at": Bom.updated_at,
    }

    # --- reads --------------------------------------------------------------

    async def get_by_code(self, code: str) -> Bom | None:
        return await self.get_by_field("code", code)

    async def max_version_for_item(self, item_id: uuid.UUID) -> int:
        """Highest version number issued for an item, 0 when there is none.

        Callers must hold the per-item advisory lock before using this to mint
        the next version - see ``service.next_version``. On its own it is a
        read, and two concurrent readers would both propose the same number.
        """
        stmt = select(func.max(Bom.version)).where(
            Bom.company_id == self.tenant.company_id, Bom.item_id == item_id
        )
        return int((await self.session.execute(stmt)).scalar() or 0)

    def _filtered(self, filters: BomFilters) -> Select[Any]:
        stmt = self._scoped()
        if filters.item_id is not None:
            stmt = stmt.where(Bom.item_id == filters.item_id)
        if filters.status is not None:
            stmt = stmt.where(Bom.status == filters.status)
        if filters.bom_type is not None:
            stmt = stmt.where(Bom.bom_type == filters.bom_type)
        if filters.is_default is not None:
            stmt = stmt.where(Bom.is_default.is_(filters.is_default))
        if filters.effective_on is not None:
            stmt = stmt.where(
                Bom.effective_from <= filters.effective_on,
                or_(Bom.effective_to.is_(None), Bom.effective_to >= filters.effective_on),
            )
        return self._apply_search(stmt, filters.search)

    async def list_page_filtered(
        self, filters: BomFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[Bom], OffsetPagination]:
        """One page of structures, ordered deterministically.

        ``Bom.id`` is appended to whatever the caller sorted by, and the page is
        cut by :func:`paginate` directly rather than through ``list_page`` -
        which applies a single expression and only adds a tiebreaker when no
        sort was given at all. Every sortable column here has duplicates:
        ``status``, ``effective_from`` and ``version`` obviously, and
        ``created_at`` most of all, because ``server_default=func.now()`` is
        transaction start time, so an import that writes sixty structures in one
        transaction gives all sixty the identical timestamp. OFFSET over a tie
        is free to return rows in a different order per statement, so page 2
        repeats rows from page 1 and silently drops others - the structures a
        planner never sees are the ones that were never on any page. The UUIDv7
        primary key is the correct tiebreaker: it is monotonic, so it recovers
        true insertion order (CLAUDE.md, "traps").
        """
        order_by = sort.resolve(self.sortable_fields, Bom.created_at.desc())
        stmt = self._filtered(filters).order_by(order_by, Bom.id.desc())
        return await paginate(self.session, stmt, params)

    async def list_versions(
        self, item_id: uuid.UUID, params: PageParams
    ) -> tuple[list[Bom], OffsetPagination]:
        """Every revision of one item's structure, newest version first."""
        stmt = self._scoped().where(Bom.item_id == item_id)
        return await self.list_page(params, stmt=stmt, order_by=Bom.version.desc())

    async def effective_boms_for_items(
        self, item_ids: Sequence[uuid.UUID], as_of: date
    ) -> dict[uuid.UUID, Bom]:
        """The structure to use for each of ``item_ids`` on ``as_of``.

        One ``DISTINCT ON`` for the whole level. Ranking: the default structure
        wins, then the highest version - so an item with a default and three
        superseded-but-still-effective alternates resolves deterministically
        rather than by whichever row the planner happened to fetch first.

        Only ``APPROVED`` rows are considered (database.md section 5): planning
        must never be driven by a structure an engineer is still editing.
        """
        if not item_ids:
            return {}
        stmt = (
            select(Bom)
            .options(load_only(*EXPLOSION_BOM_COLUMNS, raiseload=True))
            .where(
                Bom.company_id == self.tenant.company_id,
                Bom.item_id.in_(list(dict.fromkeys(item_ids))),
                Bom.status == BomStatus.APPROVED,
                Bom.effective_from <= as_of,
                or_(Bom.effective_to.is_(None), Bom.effective_to >= as_of),
            )
            .distinct(Bom.item_id)
            .order_by(Bom.item_id, Bom.is_default.desc(), Bom.version.desc())
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return {bom.item_id: bom for bom in rows}

    async def default_approved_for_item(self, item_id: uuid.UUID) -> Bom | None:
        """The one structure the partial unique index allows to be default."""
        stmt = self._scoped().where(
            Bom.item_id == item_id,
            Bom.is_default.is_(True),
            Bom.status == BomStatus.APPROVED,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_default_candidates(self, item_id: uuid.UUID) -> list[Bom]:
        """Approved structures currently flagged default for an item.

        Loaded as entities, not updated in bulk: they carry an optimistic-lock
        column, and a Core ``UPDATE`` would leave the in-session copies stale
        while the row version moved underneath them.
        """
        stmt = self._scoped().where(
            Bom.item_id == item_id,
            Bom.is_default.is_(True),
            Bom.status == BomStatus.APPROVED,
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def has_alternates(self, bom_id: uuid.UUID) -> bool:
        stmt = select(Bom.id).where(
            Bom.company_id == self.tenant.company_id, Bom.parent_bom_id == bom_id
        )
        return (await self.session.execute(stmt.limit(1))).first() is not None


class BomLineRepository(BaseRepository[BomLine]):
    model = BomLine
    #: What ``BaseRepository.get_or_404`` calls the record in its not-found
    #: sentence. Set explicitly because the automatic split of the class name
    #: would say "Bom", and every other message in this module says "BOM".
    entity_label = "BOM line"
    sortable_fields = {"line_number": BomLine.line_number}  # noqa: RUF012

    async def list_for_bom(self, bom_id: uuid.UUID) -> list[BomLine]:
        """All lines of one BOM, for the detail view.

        Not paginated by deliberate exception to section 31: the line set is
        bounded by ``MAX_LINES_PER_BOM`` at write time, and a structure is only
        meaningful whole.
        """
        stmt = self._scoped().where(BomLine.bom_id == bom_id).order_by(BomLine.line_number)
        return list((await self.session.execute(stmt)).scalars().all())

    async def lines_for_boms(self, bom_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[BomLine]]:
        """Every line of every BOM at one explosion level, in one statement.

        This is the query that keeps the engine at O(depth) round-trips instead
        of O(nodes).
        """
        if not bom_ids:
            return {}
        stmt = (
            self._scoped()
            .where(BomLine.bom_id.in_(list(dict.fromkeys(bom_ids))))
            .order_by(BomLine.bom_id, BomLine.line_number)
        )
        grouped: dict[uuid.UUID, list[BomLine]] = {}
        for line in (await self.session.execute(stmt)).scalars().all():
            grouped.setdefault(line.bom_id, []).append(line)
        return grouped

    async def delete_for_bom(self, bom_id: uuid.UUID) -> int:
        """Drop a draft's lines before rewriting them.

        A Core DELETE rather than a loop: replacing a 200-line structure must be
        one statement. ``synchronize_session=False`` is safe because the service
        re-reads the lines after the rewrite.
        """
        stmt = (
            delete(BomLine)
            .where(BomLine.company_id == self.tenant.company_id, BomLine.bom_id == bom_id)
            .execution_options(synchronize_session=False)
        )
        return affected_rows(await self.session.execute(stmt))

    async def direct_parents_page(
        self,
        component_item_id: uuid.UUID,
        params: PageParams,
        *,
        statuses: Sequence[BomStatus] = LIVE_BOM_STATUSES,
    ) -> tuple[list[BomLine], OffsetPagination]:
        """Lines that consume this component, paginated in SQL.

        The single-level case is the common one - "what breaks if I change this
        screw?" - and a widely used part can appear in thousands of structures,
        so the page is cut by PostgreSQL rather than by slicing a fully
        materialised list in memory (section 31).
        """
        stmt = (
            self._scoped()
            .join(Bom, Bom.id == BomLine.bom_id)
            .where(
                BomLine.component_item_id == component_item_id,
                Bom.company_id == self.tenant.company_id,
                Bom.status.in_(list(statuses)),
            )
        )
        # Ordered on the statement: ``order_by=`` takes a single expression, and
        # the reverse-explosion page reads best grouped by structure.
        stmt = stmt.order_by(BomLine.bom_id.desc(), BomLine.line_number.asc())
        return await self.list_page(params, stmt=stmt)

    async def parent_lines_for_components(
        self,
        component_item_ids: Sequence[uuid.UUID],
        *,
        statuses: Sequence[BomStatus] = LIVE_BOM_STATUSES,
        limit: int,
    ) -> list[BomLine]:
        """One level of reverse explosion for a set of components, batched.

        ``limit`` is a hard stop rather than pagination: a multi-level
        where-used has no stable sort key across levels, so the traversal is
        bounded instead and the caller is told when it was truncated.
        """
        if not component_item_ids:
            return []
        stmt = (
            self._scoped()
            .join(Bom, Bom.id == BomLine.bom_id)
            .where(
                BomLine.component_item_id.in_(list(dict.fromkeys(component_item_ids))),
                Bom.company_id == self.tenant.company_id,
                Bom.status.in_(list(statuses)),
            )
            .order_by(BomLine.bom_id, BomLine.line_number)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())


class BomOperationRepository(BaseRepository[BomOperation]):
    model = BomOperation
    #: What ``BaseRepository.get_or_404`` calls the record in its not-found
    #: sentence. Set explicitly because the automatic split of the class name
    #: would say "Bom", and every other message in this module says "BOM".
    entity_label = "BOM operation"
    sortable_fields = {"operation_sequence": BomOperation.operation_sequence}  # noqa: RUF012

    async def list_for_bom(self, bom_id: uuid.UUID) -> list[BomOperation]:
        stmt = (
            self._scoped()
            .where(BomOperation.bom_id == bom_id)
            .order_by(BomOperation.operation_sequence)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def operations_for_boms(
        self, bom_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[BomOperation]]:
        """Operations for every BOM in a cost roll-up, in one statement."""
        if not bom_ids:
            return {}
        stmt = (
            self._scoped()
            .where(BomOperation.bom_id.in_(list(dict.fromkeys(bom_ids))))
            .order_by(BomOperation.bom_id, BomOperation.operation_sequence)
        )
        grouped: dict[uuid.UUID, list[BomOperation]] = {}
        for operation in (await self.session.execute(stmt)).scalars().all():
            grouped.setdefault(operation.bom_id, []).append(operation)
        return grouped

    async def delete_for_bom(self, bom_id: uuid.UUID) -> int:
        stmt = (
            delete(BomOperation)
            .where(
                BomOperation.company_id == self.tenant.company_id,
                BomOperation.bom_id == bom_id,
            )
            .execution_options(synchronize_session=False)
        )
        return affected_rows(await self.session.execute(stmt))


__all__ = [
    "EXPLOSION_BOM_COLUMNS",
    "LIVE_BOM_STATUSES",
    "BomLineRepository",
    "BomOperationRepository",
    "BomRepository",
    "ItemInfo",
    "ItemLookupRepository",
]
