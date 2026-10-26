"""Queries for quality control.

Four shapes here exist for performance or correctness reasons rather than
convenience:

* **Template resolution is one statement with a deterministic winner.** The
  three scopes an inspection could match - this item, this item's category, the
  stage default - are fetched together and ranked in SQL, so picking a template
  costs one round trip and the answer does not depend on which index the planner
  reached for. The partial unique indexes on ``qc_templates`` guarantee at most
  one active candidate per rank, so the ordering never has to break a tie.

* **Check sheets and measurements load batched, never per row.** A detail page
  reads the parameters and the recorded results in two statements whatever the
  size of the sheet, and ``list_for_templates`` answers a whole page of templates
  at once. A lookup per parameter would be the N+1 section 32 forbids, on the
  write path where it hurts most.

* **The reports aggregate in the database.** ``item_history_rows`` and
  ``vendor_item_rows`` return already-summed periods and per-item totals. Pulling
  raw inspections into Python to add them up would move a whole window of rows
  over the wire to produce a dozen numbers. Both are bounded - by the window on
  one side and an explicit ``LIMIT`` on the other - so no result set is unbounded
  however it is reached (section 31).

* **Narrow projections.** ``items`` is a very wide table and raising an
  inspection reads it for the batch-tracking flag and the category; pulling whole
  rows to use four columns is the ``SELECT *`` the rules forbid.

Every statement is built from ``BaseRepository._scoped``, so the tenant filter -
and the soft-delete filter where the table has one - stays automatic rather than
something each query has to remember.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, case, delete, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from src.common.repository import BaseRepository
from src.core.database import affected_rows
from src.core.pagination import PageParams, SortParams, paginate
from src.core.responses import OffsetPagination
from src.modules.inventory.models import InventoryTransaction, StockSerial
from src.modules.products.models import Item
from src.modules.quality.models import (
    OPEN_STATUSES,
    InspectionResult,
    InspectionStatus,
    QcInspection,
    QcInspectionDecision,
    QcInspectionResult,
    QcReworkRecord,
    QcStage,
    QcTemplate,
    QcTemplateParameter,
    ReworkStatus,
)
from src.modules.quality.schemas import InspectionFilters, ReworkFilters, TemplateFilters
from src.modules.warehouses.models import Warehouse


@dataclass(frozen=True, slots=True)
class InspectableItem:
    """The item-master facts an inspection validates against.

    A projection, not the ``Item`` entity - see the module docstring. Four
    columns of a very wide table, and every one is read by a rule: ``code`` names
    the item in an error, ``category_id`` is what a category-scoped template
    resolves on, ``is_batch_tracked`` decides whether a batch is required before
    any movement can be posted, and ``is_active`` stops a retired part being
    inspected back into stock.
    """

    id: uuid.UUID
    code: str
    category_id: uuid.UUID | None
    is_batch_tracked: bool
    is_active: bool
    is_serial_tracked: bool = False


@dataclass(frozen=True, slots=True)
class QualityTotals:
    """Summed dispositions over one slice of the inspection history."""

    inspection_count: int
    inspected_quantity: Decimal
    accepted_quantity: Decimal
    rejected_quantity: Decimal
    rework_quantity: Decimal
    scrap_quantity: Decimal


@dataclass(frozen=True, slots=True)
class QualityPeriodTotals:
    """One calendar month of :class:`QualityTotals`."""

    period_start: datetime
    totals: QualityTotals


@dataclass(frozen=True, slots=True)
class VendorItemTotals:
    """One item's contribution to a vendor's quality record."""

    item_id: uuid.UUID
    totals: QualityTotals


class ItemLookupRepository(BaseRepository[Item]):
    """Read-only access to the item master for raising inspections."""

    model = Item

    async def load(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, InspectableItem]:
        """Batched id -> item facts. One statement for a whole set of items."""
        if not item_ids:
            return {}
        stmt = select(
            Item.id,
            Item.code,
            Item.category_id,
            Item.is_batch_tracked,
            Item.is_active,
            Item.is_serial_tracked,
        ).where(
            Item.company_id == self.tenant.company_id,
            Item.deleted_at.is_(None),
            Item.id.in_(list(dict.fromkeys(item_ids))),
        )
        return {
            row.id: InspectableItem(
                id=row.id,
                code=row.code,
                category_id=row.category_id,
                is_batch_tracked=row.is_batch_tracked,
                is_active=row.is_active,
                is_serial_tracked=row.is_serial_tracked,
            )
            for row in (await self.session.execute(stmt)).all()
        }


class WarehouseLookupRepository(BaseRepository[Warehouse]):
    """Existence and activity checks for the position being inspected."""

    model = Warehouse

    async def load(self, warehouse_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, tuple[str, bool]]:
        """Batched id -> (code, is_active)."""
        if not warehouse_ids:
            return {}
        stmt = select(Warehouse.id, Warehouse.code, Warehouse.is_active).where(
            Warehouse.company_id == self.tenant.company_id,
            Warehouse.deleted_at.is_(None),
            Warehouse.id.in_(list(dict.fromkeys(warehouse_ids))),
        )
        return {
            row.id: (row.code, row.is_active) for row in (await self.session.execute(stmt)).all()
        }


class SerialLabelRepository(BaseRepository[StockSerial]):
    """QC-owned label projection; inspecting units does not require inventory access."""

    model = StockSerial

    async def for_inspection(self, inspection: QcInspection) -> dict[uuid.UUID, str]:
        if not inspection.serial_ids:
            return {}
        stmt = select(StockSerial.id, StockSerial.serial_number).where(
            StockSerial.company_id == self.tenant.company_id,
            StockSerial.item_id == inspection.item_id,
            StockSerial.id.in_(inspection.serial_ids),
        )
        return dict((await self.session.execute(stmt)).tuples().all())


class QcTemplateRepository(BaseRepository[QcTemplate]):
    model = QcTemplate
    entity_label = "QC template"
    searchable_fields = ("code", "name")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": QcTemplate.code,
        "name": QcTemplate.name,
        "qc_stage": QcTemplate.qc_stage,
        "version": QcTemplate.version,
        "created_at": QcTemplate.created_at,
        "updated_at": QcTemplate.updated_at,
    }

    async def get_by_code_version(self, code: str, version: int) -> QcTemplate | None:
        stmt = self._scoped().where(QcTemplate.code == code, QcTemplate.version == version)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def latest_version(self, code: str) -> int:
        """Highest revision this code has reached, including retired ones.

        Read inside the transaction that holds the row lock on the template being
        forked, so two concurrent forks cannot both claim the same number - and
        the unique constraint on (company, code, version) is the backstop if they
        somehow try.
        """
        stmt = select(func.coalesce(func.max(QcTemplate.version), 0)).where(
            QcTemplate.company_id == self.tenant.company_id, QcTemplate.code == code
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def lock_for_update(self, template_id: uuid.UUID) -> QcTemplate | None:
        """Row-lock a template before editing or forking it.

        Editing checks whether the template has been used and then writes. Two
        editors doing that at once would each decide against the state they read
        first, and one edit would silently mutate a specification that had just
        been applied to an inspection.
        """
        stmt = self._scoped().where(QcTemplate.id == template_id).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def resolve(
        self, *, item_id: uuid.UUID, category_id: uuid.UUID | None, stage: QcStage
    ) -> QcTemplate | None:
        """The active template governing this item at this stage, narrowest first.

        One statement, ranked in SQL: an item-specific template beats a
        category-wide one, which beats the stage default. The partial unique
        indexes mean at most one active row can occupy each rank, so the ordering
        never has to break a tie - if it ever did, the answer would depend on
        physical row order, and two identical receipts could end up inspected
        against different specifications.
        """
        scopes: list[ColumnElement[bool]] = [QcTemplate.item_id == item_id]
        ranks: list[tuple[ColumnElement[bool], int]] = [(QcTemplate.item_id == item_id, 1)]
        if category_id is not None:
            scopes.append(QcTemplate.item_category_id == category_id)
            ranks.append((QcTemplate.item_category_id == category_id, 2))
        scopes.append(and_(QcTemplate.item_id.is_(None), QcTemplate.item_category_id.is_(None)))

        stmt = (
            self._scoped()
            .where(
                QcTemplate.qc_stage == stage,
                QcTemplate.is_active.is_(True),
                or_(*scopes),
            )
            # The version tiebreak is belt and braces: the partial unique indexes
            # make two active rows at the same rank impossible, but if one were
            # ever created by a direct write, the newest revision is the right
            # answer rather than whichever row the scan reached first.
            .order_by(case(*ranks, else_=3), QcTemplate.version.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def active_for_scope(
        self,
        *,
        item_id: uuid.UUID | None,
        category_id: uuid.UUID | None,
        stage: QcStage,
        exclude_id: uuid.UUID | None = None,
    ) -> QcTemplate | None:
        """The active template already claiming exactly this scope, if any.

        Used before activating a template so the caller can be told *which*
        template owns the scope, rather than receiving an integrity error naming
        a partial unique index. The index remains the actual guarantee - this
        read is advisory and the write still has to survive it.
        """
        stmt = self._scoped().where(QcTemplate.qc_stage == stage, QcTemplate.is_active.is_(True))
        if item_id is not None:
            stmt = stmt.where(QcTemplate.item_id == item_id)
        elif category_id is not None:
            stmt = stmt.where(QcTemplate.item_category_id == category_id)
        else:
            stmt = stmt.where(QcTemplate.item_id.is_(None), QcTemplate.item_category_id.is_(None))
        if exclude_id is not None:
            stmt = stmt.where(QcTemplate.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).scalar_one_or_none()

    def _filtered(self, filters: TemplateFilters) -> Select[Any]:
        stmt = self._scoped()
        if filters.item_id is not None:
            stmt = stmt.where(QcTemplate.item_id == filters.item_id)
        if filters.item_category_id is not None:
            stmt = stmt.where(QcTemplate.item_category_id == filters.item_category_id)
        if filters.qc_stage is not None:
            stmt = stmt.where(QcTemplate.qc_stage == filters.qc_stage)
        if filters.sampling_plan is not None:
            stmt = stmt.where(QcTemplate.sampling_plan == filters.sampling_plan)
        if filters.is_active is not None:
            stmt = stmt.where(QcTemplate.is_active.is_(filters.is_active))
        return self._apply_search(stmt, filters.search)

    async def list_templates(
        self, filters: TemplateFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[QcTemplate], OffsetPagination]:
        order_by = sort.resolve(self.sortable_fields, QcTemplate.created_at.desc())
        return await self.list_page(params, stmt=self._filtered(filters), order_by=order_by)


class QcTemplateParameterRepository(BaseRepository[QcTemplateParameter]):
    model = QcTemplateParameter
    entity_label = "QC template parameter"
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "sequence": QcTemplateParameter.sequence,
    }

    async def list_for_template(self, template_id: uuid.UUID) -> list[QcTemplateParameter]:
        """The whole check sheet, in the order it is worked.

        Not paginated, by deliberate exception to section 31: the set is bounded
        by ``MAX_PARAMETERS_PER_TEMPLATE`` at write time, and a verdict is only
        correct if the evaluator sees every mandatory and every critical
        parameter - a page of them would silently drop the one that rejects.
        """
        stmt = (
            self._scoped()
            .where(QcTemplateParameter.template_id == template_id)
            .order_by(QcTemplateParameter.sequence)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_templates(
        self, template_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[QcTemplateParameter]]:
        """Check sheets for several templates in one statement."""
        if not template_ids:
            return {}
        stmt = (
            self._scoped()
            .where(QcTemplateParameter.template_id.in_(list(dict.fromkeys(template_ids))))
            .order_by(QcTemplateParameter.template_id, QcTemplateParameter.sequence)
        )
        grouped: dict[uuid.UUID, list[QcTemplateParameter]] = {}
        for row in (await self.session.execute(stmt)).scalars().all():
            grouped.setdefault(row.template_id, []).append(row)
        return grouped

    async def delete_for_template(self, template_id: uuid.UUID) -> int:
        """Drop an unused template's parameter set before rewriting it.

        A Core DELETE rather than a loop: replacing a forty-line check sheet must
        be one statement. Safe only while the template has never been applied -
        the RESTRICT on ``qc_inspection_results.parameter_id`` is the backstop
        that turns a mistake here into an integrity error rather than a silently
        rewritten history.
        """
        stmt = (
            delete(QcTemplateParameter)
            .where(
                QcTemplateParameter.company_id == self.tenant.company_id,
                QcTemplateParameter.template_id == template_id,
            )
            .execution_options(synchronize_session=False)
        )
        return affected_rows(await self.session.execute(stmt))


#: The five sums and the count every quality aggregate reports. Declared once so
#: the two reports cannot drift apart, and evaluated in a single pass -
#: PostgreSQL computes all six from the same scan, where six separate statements
#: would read the same index six times for no benefit.
def _totals_columns() -> tuple[Any, ...]:
    return (
        func.count().label("inspection_count"),
        func.coalesce(func.sum(QcInspection.inspected_quantity), 0).label("inspected"),
        func.coalesce(func.sum(QcInspection.accepted_quantity), 0).label("accepted"),
        func.coalesce(func.sum(QcInspection.rejected_quantity), 0).label("rejected"),
        func.coalesce(func.sum(QcInspection.rework_quantity), 0).label("rework"),
        func.coalesce(func.sum(QcInspection.scrap_quantity), 0).label("scrap"),
    )


def _totals_from(row: Any) -> QualityTotals:
    # str() before Decimal() because asyncpg hands NUMERIC back as Decimal but
    # the COALESCE literal can arrive as an int; going through the string keeps
    # both exact and never introduces a float (section 84).
    return QualityTotals(
        inspection_count=int(row.inspection_count),
        inspected_quantity=Decimal(str(row.inspected)),
        accepted_quantity=Decimal(str(row.accepted)),
        rejected_quantity=Decimal(str(row.rejected)),
        rework_quantity=Decimal(str(row.rework)),
        scrap_quantity=Decimal(str(row.scrap)),
    )


class QcInspectionRepository(BaseRepository[QcInspection]):
    model = QcInspection
    entity_label = "QC inspection"
    searchable_fields = ("code", "reference_code")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": QcInspection.code,
        "status": QcInspection.status,
        "result": QcInspection.result,
        "qc_stage": QcInspection.qc_stage,
        "inspected_at": QcInspection.inspected_at,
        "inspected_quantity": QcInspection.inspected_quantity,
        "created_at": QcInspection.created_at,
    }

    async def lock_for_update(self, inspection_id: uuid.UUID) -> QcInspection | None:
        """Row-lock the inspection before deciding it.

        A decision reads the dispositions already applied, works out the delta
        still to post, moves stock and writes the new totals back. Two decisions
        running at once would each compute their delta from the same stale
        quantities and both post it, releasing more than was ever held. The header
        is the serialisation point for the whole document (database.md section
        4.4).
        """
        stmt = (
            self._scoped()
            .where(QcInspection.id == inspection_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock_pending_packing(self, line_ids: Sequence[uuid.UUID]) -> list[QcInspection]:
        """Stable document locks serialize cancellation with a competing verdict."""
        if not line_ids:
            return []
        stmt = (
            self._scoped()
            .where(
                QcInspection.reference_type == "packing_order",
                QcInspection.reference_id.in_(line_ids),
                QcInspection.qc_stage == QcStage.FINAL,
                QcInspection.status.in_(OPEN_STATUSES),
            )
            .order_by(QcInspection.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.scalars(stmt)).all())

    async def inspection_ids_with_evidence(self, ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        """One batched read excludes measurements, decisions and explicit stock holds."""
        if not ids:
            return set()
        stmt = (
            select(QcInspectionResult.inspection_id)
            .where(
                QcInspectionResult.company_id == self.tenant.company_id,
                QcInspectionResult.inspection_id.in_(ids),
            )
            .union(
                select(QcInspectionDecision.inspection_id).where(
                    QcInspectionDecision.company_id == self.tenant.company_id,
                    QcInspectionDecision.inspection_id.in_(ids),
                ),
                select(InventoryTransaction.reference_id).where(
                    InventoryTransaction.company_id == self.tenant.company_id,
                    InventoryTransaction.reference_type == "qc_inspection",
                    InventoryTransaction.reference_id.in_(ids),
                ),
            )
        )
        return {
            inspection_id
            for inspection_id in await self.session.scalars(stmt)
            if inspection_id is not None
        }

    async def count_for_template(self, template_id: uuid.UUID) -> int:
        """How many inspections have applied this specification.

        Drives the freeze: a template with inspections behind it is forked into a
        new version rather than edited, so a past pass or fail stays reproducible
        against the specification it was judged by.
        """
        stmt = (
            select(func.count())
            .select_from(QcInspection)
            .where(
                QcInspection.company_id == self.tenant.company_id,
                QcInspection.template_id == template_id,
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def list_for_reference(
        self, reference_type: str, reference_id: uuid.UUID | Sequence[uuid.UUID]
    ) -> list[QcInspection]:
        """Every inspection one document raised - the receipt's own QC panel.

        Bounded by the referring document: a goods receipt raises one inspection
        per held line, and its line count is capped by procurement.
        """
        stmt = (
            self._scoped()
            .where(
                QcInspection.reference_type == reference_type,
                QcInspection.reference_id.in_(
                    [reference_id] if isinstance(reference_id, uuid.UUID) else reference_id
                ),
            )
            # created_at is PostgreSQL now() - transaction START time - so every row
            # written in one transaction shares it. The UUIDv7 id is the
            # tiebreaker that recovers true insertion order.
            .order_by(QcInspection.created_at, QcInspection.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_references(
        self, references: Sequence[tuple[str, uuid.UUID]]
    ) -> list[QcInspection]:
        """Every inspection a set of documents raised, in one statement.

        The batched form of :meth:`list_for_reference`, for an owner reading the
        inspections over several of its documents at once. An empty set is an
        empty answer, never "every inspection".
        """
        if not references:
            return []
        stmt = (
            self._scoped()
            .where(
                tuple_(QcInspection.reference_type, QcInspection.reference_id).in_(
                    list(dict.fromkeys(references))
                )
            )
            .order_by(QcInspection.created_at, QcInspection.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def settled_ids_for_references(
        self, references: Sequence[tuple[str, uuid.UUID]]
    ) -> list[uuid.UUID]:
        """Ids of the decided inspections, with nothing left on hold, that the
        given documents raised - the owner reconciliation sweep's work list.

        One statement for a whole batch of documents, through the
        ``(company_id, reference_type, reference_id)`` index. "Settled" is the
        same test the owners apply: a verdict exists and no quantity is still in
        rework or hold. Ids only, because the sweep repairs each in its own
        transaction and must not hold ORM rows across a rolled-back one.
        An empty batch is an empty answer, never "every inspection".
        """
        if not references:
            return []
        stmt = (
            select(QcInspection.id)
            .where(
                QcInspection.company_id == self.tenant.company_id,
                tuple_(QcInspection.reference_type, QcInspection.reference_id).in_(
                    list(dict.fromkeys(references))
                ),
                QcInspection.result != InspectionResult.PENDING,
                QcInspection.rework_quantity + QcInspection.hold_quantity == 0,
            )
            .order_by(QcInspection.created_at, QcInspection.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    def _filtered(self, filters: InspectionFilters) -> Select[Any]:
        stmt = self._scoped()
        if filters.qc_stage is not None:
            stmt = stmt.where(QcInspection.qc_stage == filters.qc_stage)
        if filters.status is not None:
            stmt = stmt.where(QcInspection.status == filters.status)
        if filters.result is not None:
            stmt = stmt.where(QcInspection.result == filters.result)
        if filters.item_id is not None:
            stmt = stmt.where(QcInspection.item_id == filters.item_id)
        if filters.warehouse_id is not None:
            stmt = stmt.where(QcInspection.warehouse_id == filters.warehouse_id)
        if filters.vendor_id is not None:
            stmt = stmt.where(QcInspection.vendor_id == filters.vendor_id)
        if filters.reference_type is not None:
            stmt = stmt.where(QcInspection.reference_type == filters.reference_type)
        if filters.reference_id is not None:
            stmt = stmt.where(QcInspection.reference_id == filters.reference_id)
        if filters.pending_only:
            # Matches the partial index on the open statuses exactly, so the
            # queue stays a small index scan rather than a filter over history.
            stmt = stmt.where(QcInspection.status.in_(OPEN_STATUSES))
        if filters.rework_only:
            stmt = stmt.where(
                QcInspection.rework_quantity > 0,
                QcInspection.status != InspectionStatus.CLOSED,
            )
        if filters.inspected_from is not None:
            stmt = stmt.where(QcInspection.inspected_at >= filters.inspected_from)
        if filters.inspected_to is not None:
            stmt = stmt.where(QcInspection.inspected_at <= filters.inspected_to)
        return self._apply_search(stmt, filters.search)

    async def list_inspections(
        self, filters: InspectionFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[QcInspection], OffsetPagination]:
        order_by = sort.resolve(self.sortable_fields, QcInspection.created_at.desc())
        return await self.list_page(params, stmt=self._filtered(filters), order_by=order_by)

    async def list_pending(
        self, params: PageParams, *, stage: QcStage | None, warehouse_id: uuid.UUID | None
    ) -> tuple[list[QcInspection], OffsetPagination]:
        """The inspection queue, oldest first - the oldest hold is the one
        blocking a delivery.
        """
        stmt = self._scoped().where(QcInspection.status.in_(OPEN_STATUSES))
        if stage is not None:
            stmt = stmt.where(QcInspection.qc_stage == stage)
        if warehouse_id is not None:
            stmt = stmt.where(QcInspection.warehouse_id == warehouse_id)
        # Paginated directly rather than through ``list_page``, which takes a
        # single ORDER BY expression. The second key is not decoration: without a
        # deterministic tiebreaker on a timestamp that repeats - and a batch of
        # inspections raised by one goods receipt all share it - rows can repeat
        # or vanish between pages.
        return await paginate(
            self.session,
            stmt.order_by(QcInspection.created_at.asc(), QcInspection.id.asc()),
            params,
        )

    # --- reports ------------------------------------------------------------

    def _window(self, *, since: datetime, until: datetime) -> list[ColumnElement[bool]]:
        """Filter shared by both reports.

        Only *decided* inspections count: ``inspected_at`` is written by the
        decision and by nothing else. A pending inspection has no verdict, and
        counting it would drag the rejection rate towards zero purely because
        nobody has looked at the goods yet.
        """
        return [
            QcInspection.company_id == self.tenant.company_id,
            QcInspection.inspected_at.is_not(None),
            QcInspection.inspected_at >= since,
            QcInspection.inspected_at <= until,
        ]

    async def item_totals(
        self, item_id: uuid.UUID, *, since: datetime, until: datetime
    ) -> QualityTotals:
        stmt = select(*_totals_columns()).where(
            *self._window(since=since, until=until), QcInspection.item_id == item_id
        )
        return _totals_from((await self.session.execute(stmt)).one())

    async def item_history_rows(
        self, item_id: uuid.UUID, *, since: datetime, until: datetime
    ) -> list[QualityPeriodTotals]:
        """Monthly rejection history for one item.

        Bucketed with ``date_trunc`` in the database. The row count is the number
        of months in the window, which the service caps - so this is bounded
        without a LIMIT that could silently drop the newest period.
        """
        period = func.date_trunc("month", QcInspection.inspected_at).label("period_start")
        stmt = (
            select(period, *_totals_columns())
            .where(*self._window(since=since, until=until), QcInspection.item_id == item_id)
            .group_by(period)
            .order_by(period)
        )
        return [
            QualityPeriodTotals(period_start=row.period_start, totals=_totals_from(row))
            for row in (await self.session.execute(stmt)).all()
        ]

    async def vendor_totals(
        self, vendor_id: uuid.UUID, *, since: datetime, until: datetime
    ) -> QualityTotals:
        stmt = select(*_totals_columns()).where(
            *self._window(since=since, until=until), QcInspection.vendor_id == vendor_id
        )
        return _totals_from((await self.session.execute(stmt)).one())

    async def vendor_item_rows(
        self, vendor_id: uuid.UUID, *, since: datetime, until: datetime, limit: int
    ) -> list[VendorItemTotals]:
        """A vendor's rejection record broken down by item.

        Ordered by inspected quantity so ``limit`` drops the tail rather than an
        arbitrary slice: the items that dominate the vendor's rate are the ones a
        buyer needs to see. One extra row is fetched so the caller can say whether
        anything was cut.
        """
        stmt = (
            select(QcInspection.item_id, *_totals_columns())
            .where(*self._window(since=since, until=until), QcInspection.vendor_id == vendor_id)
            .group_by(QcInspection.item_id)
            .order_by(func.sum(QcInspection.inspected_quantity).desc(), QcInspection.item_id)
            .limit(limit + 1)
        )
        return [
            VendorItemTotals(item_id=row.item_id, totals=_totals_from(row))
            for row in (await self.session.execute(stmt)).all()
        ]


class QcInspectionResultRepository(BaseRepository[QcInspectionResult]):
    model = QcInspectionResult
    entity_label = "QC inspection result"
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {}  # noqa: RUF012

    async def list_for_inspection(self, inspection_id: uuid.UUID) -> list[QcInspectionResult]:
        """Every measurement recorded against one inspection.

        Not paginated, for the same reason the check sheet is not: a verdict
        computed from a page of the readings would miss the critical parameter
        that rejects the lot. The set is bounded by the template's parameter
        count.
        """
        stmt = self._scoped().where(QcInspectionResult.inspection_id == inspection_id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def map_for_inspection(
        self, inspection_id: uuid.UUID
    ) -> dict[uuid.UUID, QcInspectionResult]:
        """The same set keyed by parameter, for correcting a reading in place."""
        return {row.parameter_id: row for row in await self.list_for_inspection(inspection_id)}


class QcInspectionDecisionRepository(BaseRepository[QcInspectionDecision]):
    model = QcInspectionDecision
    entity_label = "QC inspection decision"
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {}  # noqa: RUF012

    async def next_sequence(self, inspection_id: uuid.UUID) -> int:
        """The sequence number the next verdict on this inspection carries.

        Read under the inspection's row lock, which every decision takes before
        computing anything: two verdicts cannot claim one number. The first
        verdict on an inspection is 1, so a history that starts at 2 is evidence
        of a deleted row rather than of anything this code does.
        """
        stmt = select(func.coalesce(func.max(QcInspectionDecision.sequence), 0)).where(
            QcInspectionDecision.company_id == self.tenant.company_id,
            QcInspectionDecision.inspection_id == inspection_id,
        )
        return int((await self.session.execute(stmt)).scalar_one()) + 1

    async def list_for_inspection(self, inspection_id: uuid.UUID) -> list[QcInspectionDecision]:
        """Every verdict one inspection has carried, oldest first.

        Not paginated: a decision history is short - one row per verdict - and a
        page of it would be a history with the earlier cycles missing, which is
        exactly what this table exists to prevent.
        """
        stmt = (
            self._scoped()
            .where(QcInspectionDecision.inspection_id == inspection_id)
            .order_by(QcInspectionDecision.sequence)
        )
        return list((await self.session.execute(stmt)).scalars().all())


class QcReworkRepository(BaseRepository[QcReworkRecord]):
    model = QcReworkRecord
    entity_label = "QC rework record"
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": QcReworkRecord.code,
        "status": QcReworkRecord.status,
        "created_at": QcReworkRecord.created_at,
    }

    async def lock_for_update(self, record_id: uuid.UUID) -> QcReworkRecord | None:
        """Row-lock the cycle before starting or completing it.

        The header is the serialisation point the way the inspection header is
        for a decision: completing a cycle reads the inspection's rework
        quantity and re-opens it, and two completions running at once would
        each re-open from the same stale quantities.
        """
        stmt = self._scoped().where(QcReworkRecord.id == record_id).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_inspection(self, inspection_id: uuid.UUID) -> list[QcReworkRecord]:
        """Every cycle raised against one inspection, oldest first.

        Bounded by the inspection: a lot is reworked a handful of times, not
        hundreds, and the detail dialog reads the whole history at once.
        """
        stmt = (
            self._scoped()
            .where(QcReworkRecord.inspection_id == inspection_id)
            # created_at is transaction-start time, so rows written in one
            # transaction share it; the UUIDv7 id recovers insertion order.
            .order_by(QcReworkRecord.created_at, QcReworkRecord.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def open_quantity_for_inspection(self, inspection_id: uuid.UUID) -> Decimal:
        """Quantity already covered by unfinished cycles on this inspection.

        A new cycle may only cover what is still uncovered: two open cycles
        each covering the whole rework quantity would promise the bench twice
        the material the inspection holds.
        """
        stmt = select(func.coalesce(func.sum(QcReworkRecord.quantity), 0)).where(
            QcReworkRecord.company_id == self.tenant.company_id,
            QcReworkRecord.inspection_id == inspection_id,
            QcReworkRecord.status != ReworkStatus.DONE,
        )
        return Decimal(str((await self.session.execute(stmt)).scalar_one()))

    async def list_rework(
        self, filters: ReworkFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[QcReworkRecord], OffsetPagination]:
        """The bench's work list, paginated."""
        stmt = self._scoped()
        if filters.inspection_id is not None:
            stmt = stmt.where(QcReworkRecord.inspection_id == filters.inspection_id)
        if filters.status is not None:
            stmt = stmt.where(QcReworkRecord.status == filters.status)
        if filters.assigned_to is not None:
            stmt = stmt.where(QcReworkRecord.assigned_to == filters.assigned_to)
        if filters.work_center_id is not None:
            stmt = stmt.where(QcReworkRecord.work_center_id == filters.work_center_id)
        order_by = sort.resolve(self.sortable_fields, QcReworkRecord.created_at.desc())
        return await self.list_page(params, stmt=stmt, order_by=order_by)


async def decided_inspection_companies(session: AsyncSession) -> list[uuid.UUID]:
    """Companies holding at least one decided inspection.

    Cross-tenant on purpose and therefore not a ``BaseRepository`` method: it is
    the discovery step of a beat sweep that has no tenant yet (see
    ``service.companies_with_decided_inspections``). It reads one column.
    """
    stmt = (
        select(QcInspection.company_id)
        .where(QcInspection.result != InspectionResult.PENDING)
        .distinct()
    )
    return list((await session.execute(stmt)).scalars().all())


def open_inspection_count(
    company_id: uuid.UUID, reference_type: str, reference_ids: Any
) -> ColumnElement[int]:
    """Undecided inspections raised by the documents ``reference_ids`` selects.

    A correlatable scalar subquery rather than a count fetched per document:
    procurement's inbound board asks it once per purchase order inside one
    statement, passing a correlated select of that order's posted receipts.
    Reached through a guarded import (``inbound_repository``), the same seam
    procurement uses for every other quality call, so the planning layer never
    imports this module's types.
    """
    return (
        select(func.count())
        .select_from(QcInspection)
        .where(
            QcInspection.company_id == company_id,
            QcInspection.reference_type == reference_type,
            QcInspection.reference_id.in_(reference_ids),
            QcInspection.status.in_(OPEN_STATUSES),
        )
        .scalar_subquery()
    )


__all__ = [
    "InspectableItem",
    "ItemLookupRepository",
    "QcInspectionDecisionRepository",
    "QcInspectionRepository",
    "QcInspectionResultRepository",
    "QcReworkRepository",
    "QcTemplateParameterRepository",
    "QcTemplateRepository",
    "QualityPeriodTotals",
    "QualityTotals",
    "VendorItemTotals",
    "WarehouseLookupRepository",
    "decided_inspection_companies",
    "open_inspection_count",
]
