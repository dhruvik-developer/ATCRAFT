"""Queries for job work orders, challans and receipts.

Four shapes here exist for performance or correctness reasons rather than
convenience:

* **Every master-data lookup is batched.** ``ItemLookupRepository`` and the
  warehouse, vendor and transporter lookups all take a collection of ids and
  answer in one statement. A twenty-line consignment validated with a lookup per
  line would be eighty round trips on the write path - the N+1 section 32
  forbids.
* **The pending-at-vendor feed is one aggregate, not a list plus a read per
  row.** Grouping in the database is the only way to answer "what is out, at
  whom, for how long" without walking every open order in Python.
* **The vendor charge aggregate never touches the orders table.** Charges accrue
  on ``job_work_receipts``, which carries its own ``vendor_id``, so finance's
  period query is a single grouped scan of one index.
* **Narrow projections.** ``items`` is a wide table and order entry reads it for
  every line; pulling whole rows to use seven columns is the ``SELECT *`` the
  rules forbid.

Reads that cross into ``items``, ``warehouses``, ``vendors`` and
``inventory_balances`` go through ``BaseRepository`` so the tenant filter - and
the soft-delete filter where the table has one - stays automatic rather than
something each statement has to remember.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, case, delete, func, select
from sqlalchemy.orm import InstrumentedAttribute

from src.common.repository import BaseRepository
from src.core.clock import today, utcnow
from src.core.database import affected_rows
from src.core.pagination import COUNT_CEILING, PageParams, SortParams
from src.core.responses import OffsetPagination
from src.modules.inventory.models import InventoryBalance
from src.modules.job_work.models import (
    MATERIAL_AT_VENDOR_STATUSES,
    OPEN_SUPPLY_STATUSES,
    JobWorkChallan,
    JobWorkChallanLine,
    JobWorkOrder,
    JobWorkOrderLine,
    JobWorkReceipt,
    JobWorkReceiptLine,
    JobWorkSourceType,
)
from src.modules.job_work.schemas import (
    ChallanFilters,
    JobWorkOrderFilters,
    PendingAtVendorRow,
    VendorChargeRow,
)
from src.modules.products.models import Item
from src.modules.vendors.models import Transporter, Vendor
from src.modules.warehouses.models import Warehouse


@dataclass(frozen=True, slots=True)
class JobWorkItem:
    """The item-master facts a job work order validates against.

    A projection, not the ``Item`` entity - see the module docstring. Every
    column is read by a rule: ``code`` names the item in an error, ``base_uom_id``
    is what the line is measured in, the two tracking flags decide whether this
    module can post movements for it at all, ``can_job_work`` is the capability
    check database.md section 3 requires instead of reading ``item_group``, and
    ``is_active`` stops a retired part being sent out.
    """

    id: uuid.UUID
    code: str
    base_uom_id: uuid.UUID
    is_batch_tracked: bool
    is_serial_tracked: bool
    can_job_work: bool
    is_active: bool


@dataclass(frozen=True, slots=True)
class VendorFacts:
    """The vendor facts an order validates against."""

    id: uuid.UUID
    code: str
    name: str
    is_active: bool
    is_approved: bool


class ItemLookupRepository(BaseRepository[Item]):
    """Read-only access to the item master for order entry and receiving."""

    model = Item

    async def load(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, JobWorkItem]:
        """Batched id -> item facts. One statement for a whole consignment."""
        if not item_ids:
            return {}
        stmt = select(
            Item.id,
            Item.code,
            Item.base_uom_id,
            Item.is_batch_tracked,
            Item.is_serial_tracked,
            Item.can_job_work,
            Item.is_active,
        ).where(
            Item.company_id == self.tenant.company_id,
            Item.deleted_at.is_(None),
            Item.id.in_(list(dict.fromkeys(item_ids))),
        )
        return {
            row.id: JobWorkItem(
                id=row.id,
                code=row.code,
                base_uom_id=row.base_uom_id,
                is_batch_tracked=row.is_batch_tracked,
                is_serial_tracked=row.is_serial_tracked,
                can_job_work=row.can_job_work,
                is_active=row.is_active,
            )
            for row in (await self.session.execute(stmt)).all()
        }


class WarehouseLookupRepository(BaseRepository[Warehouse]):
    """Existence and activity checks for the source and return warehouses."""

    model = Warehouse

    async def load(self, warehouse_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, tuple[str, bool]]:
        """Batched id -> (code, is_active). Both warehouses in one statement."""
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


class VendorLookupRepository(BaseRepository[Vendor]):
    """The processor's master record, for the checks made when an order is raised."""

    model = Vendor

    async def load(self, vendor_id: uuid.UUID) -> VendorFacts | None:
        stmt = select(
            Vendor.id, Vendor.code, Vendor.name, Vendor.is_active, Vendor.is_approved
        ).where(
            Vendor.company_id == self.tenant.company_id,
            Vendor.deleted_at.is_(None),
            Vendor.id == vendor_id,
        )
        row = (await self.session.execute(stmt)).first()
        if row is None:
            return None
        return VendorFacts(
            id=row.id,
            code=row.code,
            name=row.name,
            is_active=row.is_active,
            is_approved=row.is_approved,
        )


class TransporterLookupRepository(BaseRepository[Transporter]):
    """The carrier named on a challan."""

    model = Transporter

    async def load(self, transporter_id: uuid.UUID) -> tuple[str, bool] | None:
        stmt = select(Transporter.code, Transporter.is_active).where(
            Transporter.company_id == self.tenant.company_id,
            Transporter.deleted_at.is_(None),
            Transporter.id == transporter_id,
        )
        row = (await self.session.execute(stmt)).first()
        return (row.code, row.is_active) if row is not None else None


class JobWorkBalanceRepository(BaseRepository[InventoryBalance]):
    """Live reads of the job-work pool.

    This is the other half of the traceability claim: the module's own tables say
    how much material is at a vendor, and this says how much the stock ledger
    believes is in the ``JOB_WORK`` state. The reconciliation report shows both,
    because a difference between them is the failure mode the module exists to
    make visible.
    """

    model = InventoryBalance

    async def job_work_quantity(
        self, item_ids: Sequence[uuid.UUID], warehouse_id: uuid.UUID
    ) -> Decimal:
        """Sum of ``job_work_quantity`` for a set of items at one warehouse.

        Never cached: this is an availability-adjacent figure and section 34 is
        explicit that those are read from the balance table on every request.
        """
        if not item_ids:
            return Decimal("0")
        stmt = select(func.coalesce(func.sum(InventoryBalance.job_work_quantity), 0)).where(
            InventoryBalance.company_id == self.tenant.company_id,
            InventoryBalance.warehouse_id == warehouse_id,
            InventoryBalance.item_id.in_(list(dict.fromkeys(item_ids))),
        )
        return Decimal(str((await self.session.execute(stmt)).scalar_one()))


class JobWorkOrderLineRepository(BaseRepository[JobWorkOrderLine]):
    model = JobWorkOrderLine
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "line_number": JobWorkOrderLine.line_number,
    }

    async def list_for_order(self, order_id: uuid.UUID) -> list[JobWorkOrderLine]:
        """Every line of one order, for the challan and for receiving.

        Not paginated, by deliberate exception to section 31: the set is bounded
        by ``MAX_LINES_PER_ORDER`` at write time, and issuing or receiving is
        only correct if it sees all of it.
        """
        stmt = (
            self._scoped()
            .where(JobWorkOrderLine.job_work_order_id == order_id)
            .order_by(JobWorkOrderLine.line_number)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def delete_for_order(self, order_id: uuid.UUID) -> int:
        """Drop a draft's line set before replacing it.

        A Core DELETE rather than a loop: replacing a 200-line consignment must
        be one statement. Safe only for a DRAFT, where nothing has been issued
        and no challan line points at any of these rows.
        """
        stmt = (
            delete(JobWorkOrderLine)
            .where(
                JobWorkOrderLine.company_id == self.tenant.company_id,
                JobWorkOrderLine.job_work_order_id == order_id,
            )
            .execution_options(synchronize_session=False)
        )
        return affected_rows(await self.session.execute(stmt))


class JobWorkChallanRepository(BaseRepository[JobWorkChallan]):
    model = JobWorkChallan
    searchable_fields = ("code", "vehicle_number")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": JobWorkChallan.code,
        "challan_date": JobWorkChallan.challan_date,
        "expected_return_date": JobWorkChallan.expected_return_date,
        "created_at": JobWorkChallan.created_at,
    }

    async def list_for_order(self, order_id: uuid.UUID) -> list[JobWorkChallan]:
        stmt = (
            self._scoped()
            .where(JobWorkChallan.job_work_order_id == order_id)
            .order_by(JobWorkChallan.challan_date, JobWorkChallan.code)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_order_lines(
        self, order_line_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[JobWorkChallanLine]]:
        """Challan lines grouped by the order line they declared, in one statement.

        The receipt-costing read: an order line's frozen issue cost is the
        quantity-weighted average of what its challans declared.
        """
        if not order_line_ids:
            return {}
        stmt = (
            select(JobWorkChallanLine)
            .where(
                JobWorkChallanLine.company_id == self.tenant.company_id,
                JobWorkChallanLine.job_work_order_line_id.in_(list(dict.fromkeys(order_line_ids))),
            )
            .order_by(JobWorkChallanLine.job_work_order_line_id, JobWorkChallanLine.id)
        )
        grouped: dict[uuid.UUID, list[JobWorkChallanLine]] = {}
        for row in (await self.session.execute(stmt)).scalars().all():
            grouped.setdefault(row.job_work_order_line_id, []).append(row)
        return grouped

    def _filtered(self, filters: ChallanFilters) -> Select[Any]:
        stmt = self._scoped()
        if filters.vendor_id is not None:
            stmt = stmt.where(JobWorkChallan.vendor_id == filters.vendor_id)
        if filters.job_work_order_id is not None:
            stmt = stmt.where(JobWorkChallan.job_work_order_id == filters.job_work_order_id)
        if filters.date_from is not None:
            stmt = stmt.where(JobWorkChallan.challan_date >= filters.date_from)
        if filters.date_to is not None:
            stmt = stmt.where(JobWorkChallan.challan_date <= filters.date_to)
        return self._apply_search(stmt, filters.search)

    async def list_page_filtered(
        self, filters: ChallanFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[JobWorkChallan], OffsetPagination]:
        """Company-wide challan register, newest despatch first (E7b/P10.M3)."""
        order_by = sort.resolve(self.sortable_fields, JobWorkChallan.challan_date.desc())
        return await self.list_page(params, stmt=self._filtered(filters), order_by=order_by)

    async def issued_by_item(
        self, vendor_id: uuid.UUID, date_from: date, date_to: date
    ) -> dict[uuid.UUID, tuple[Decimal, Decimal]]:
        """Challaned quantities per input item: (before the period, in the period).

        One grouped scan of the vendor's challan lines. The challan carries the
        vendor and the date; the line carries the input item and quantity.
        """
        before = func.sum(
            case(
                (JobWorkChallan.challan_date < date_from, JobWorkChallanLine.quantity),
                else_=Decimal("0"),
            )
        )
        period = func.sum(
            case(
                (
                    and_(
                        JobWorkChallan.challan_date >= date_from,
                        JobWorkChallan.challan_date <= date_to,
                    ),
                    JobWorkChallanLine.quantity,
                ),
                else_=Decimal("0"),
            )
        )
        stmt = (
            select(
                JobWorkChallanLine.item_id.label("item_id"),
                before.label("issued_before"),
                period.label("issued_period"),
            )
            .join(JobWorkChallan, JobWorkChallan.id == JobWorkChallanLine.challan_id)
            .where(
                JobWorkChallanLine.company_id == self.tenant.company_id,
                JobWorkChallan.vendor_id == vendor_id,
                JobWorkChallan.challan_date <= date_to,
            )
            .group_by(JobWorkChallanLine.item_id)
        )
        return {
            row.item_id: (Decimal(str(row.issued_before)), Decimal(str(row.issued_period)))
            for row in (await self.session.execute(stmt)).all()
        }


class JobWorkChallanLineRepository(BaseRepository[JobWorkChallanLine]):
    model = JobWorkChallanLine

    async def list_for_challans(
        self, challan_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[JobWorkChallanLine]]:
        """Lines for several challans in one statement.

        The register view renders every challan of an order at once; a query per
        challan is the N+1 section 32 forbids.
        """
        if not challan_ids:
            return {}
        stmt = (
            self._scoped()
            .where(JobWorkChallanLine.challan_id.in_(list(dict.fromkeys(challan_ids))))
            .order_by(JobWorkChallanLine.challan_id, JobWorkChallanLine.line_number)
        )
        grouped: dict[uuid.UUID, list[JobWorkChallanLine]] = {}
        for row in (await self.session.execute(stmt)).scalars().all():
            grouped.setdefault(row.challan_id, []).append(row)
        return grouped


class JobWorkReceiptLineRepository(BaseRepository[JobWorkReceiptLine]):
    model = JobWorkReceiptLine

    async def list_for_receipts(
        self, receipt_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[JobWorkReceiptLine]]:
        """Lines for several receipts in one statement - see the module docstring."""
        if not receipt_ids:
            return {}
        stmt = (
            self._scoped()
            .where(JobWorkReceiptLine.receipt_id.in_(list(dict.fromkeys(receipt_ids))))
            .order_by(JobWorkReceiptLine.receipt_id, JobWorkReceiptLine.line_number)
        )
        grouped: dict[uuid.UUID, list[JobWorkReceiptLine]] = {}
        for row in (await self.session.execute(stmt)).scalars().all():
            grouped.setdefault(row.receipt_id, []).append(row)
        return grouped

    async def held_output_for_order(
        self, receipt_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, Decimal]:
        """Good output per item across a set of receipts, for the QC verdict.

        Aggregated in the database rather than by walking the lines in Python:
        the verdict has to account for every held unit, and summing in SQL keeps
        that total right however many lots the vendor delivered in.
        """
        if not receipt_ids:
            return {}
        stmt = (
            select(
                JobWorkReceiptLine.output_item_id,
                func.coalesce(func.sum(JobWorkReceiptLine.output_quantity), 0).label("held"),
            )
            .where(
                JobWorkReceiptLine.company_id == self.tenant.company_id,
                JobWorkReceiptLine.receipt_id.in_(list(dict.fromkeys(receipt_ids))),
                JobWorkReceiptLine.output_quantity > 0,
            )
            .group_by(JobWorkReceiptLine.output_item_id)
        )
        return {
            row.output_item_id: Decimal(str(row.held))
            for row in (await self.session.execute(stmt)).all()
        }


class JobWorkReceiptRepository(BaseRepository[JobWorkReceipt]):
    model = JobWorkReceipt
    searchable_fields = ("code", "vendor_challan_number")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": JobWorkReceipt.code,
        "receipt_date": JobWorkReceipt.receipt_date,
        "created_at": JobWorkReceipt.created_at,
    }

    async def list_for_order(self, order_id: uuid.UUID) -> list[JobWorkReceipt]:
        stmt = (
            self._scoped()
            .where(JobWorkReceipt.job_work_order_id == order_id)
            .order_by(JobWorkReceipt.receipt_number)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_unreleased_holds(self, order_id: uuid.UUID) -> int:
        """Lots of this order whose output is still on QC hold.

        Zero is what lets the order complete: an order whose every held lot has
        been released has nothing left for an inspection to decide.
        """
        stmt = (
            select(func.count())
            .select_from(JobWorkReceipt)
            .where(
                JobWorkReceipt.company_id == self.tenant.company_id,
                JobWorkReceipt.job_work_order_id == order_id,
                JobWorkReceipt.qc_held_at.is_not(None),
                JobWorkReceipt.qc_released_at.is_(None),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def awaiting_quality_verdict(
        self, *, after: uuid.UUID | None, limit: int
    ) -> list[uuid.UUID]:
        """Held, unreleased lots an inspection was raised for - one keyset page in
        id order for the quality owner sweep, through the partial index on
        exactly the held-and-unreleased lots. A lot held with no inspection is
        not listed: no decision can release it."""
        stmt = (
            select(JobWorkReceipt.id)
            .where(
                JobWorkReceipt.company_id == self.tenant.company_id,
                JobWorkReceipt.qc_held_at.is_not(None),
                JobWorkReceipt.qc_released_at.is_(None),
                JobWorkReceipt.qc_inspection_id.is_not(None),
            )
            .order_by(JobWorkReceipt.id)
            .limit(limit)
        )
        if after is not None:
            stmt = stmt.where(JobWorkReceipt.id > after)
        return list((await self.session.execute(stmt)).scalars().all())

    def charges_stmt(
        self, *, date_from: date, date_to: date, vendor_ids: Sequence[uuid.UUID] | None
    ) -> Select[Any]:
        """Per-vendor charge totals for a period.

        One grouped scan of ``ix_job_work_receipts_vendor_date``: no join to the
        orders table, because the receipt carries the vendor itself. The period
        is applied to ``receipt_date`` - a charge is earned when the goods come
        back, not when they went out.
        """
        stmt = (
            select(
                JobWorkReceipt.vendor_id.label("vendor_id"),
                func.coalesce(func.sum(JobWorkReceipt.total_charges), 0).label("gross_charges"),
                func.coalesce(func.sum(JobWorkReceipt.vendor_debit_amount), 0).label(
                    "vendor_debits"
                ),
                func.count(JobWorkReceipt.id).label("receipt_count"),
                func.count(func.distinct(JobWorkReceipt.job_work_order_id)).label("order_count"),
                func.coalesce(func.sum(JobWorkReceipt.total_output_quantity), 0).label(
                    "output_quantity"
                ),
            )
            .where(
                JobWorkReceipt.company_id == self.tenant.company_id,
                JobWorkReceipt.receipt_date >= date_from,
                JobWorkReceipt.receipt_date <= date_to,
            )
            .group_by(JobWorkReceipt.vendor_id)
        )
        # ``None`` means every vendor; an empty restriction must match nobody
        # rather than silently widen to everybody.
        if vendor_ids is not None:
            stmt = stmt.where(JobWorkReceipt.vendor_id.in_(list(dict.fromkeys(vendor_ids))))
        # Largest spend first: the number a finance team acts on is at the top.
        return stmt.order_by(
            func.coalesce(func.sum(JobWorkReceipt.total_charges), 0).desc(),
            JobWorkReceipt.vendor_id.asc(),
        )

    async def charges_for_period(
        self,
        *,
        date_from: date,
        date_to: date,
        vendor_ids: Sequence[uuid.UUID] | None = None,
        limit: int,
    ) -> list[VendorChargeRow]:
        """Bounded feed for the service-level contract other modules call."""
        stmt = self.charges_stmt(date_from=date_from, date_to=date_to, vendor_ids=vendor_ids).limit(
            limit
        )
        return [_charge_row(row) for row in (await self.session.execute(stmt)).all()]

    async def charges_page(
        self,
        *,
        date_from: date,
        date_to: date,
        vendor_ids: Sequence[uuid.UUID] | None,
        params: PageParams,
    ) -> tuple[list[VendorChargeRow], OffsetPagination]:
        """Paginate the aggregate, as ``pending_at_vendor_page`` does.

        ``core.pagination.paginate`` reads ``.scalars()`` and would keep only the
        first column of an aggregate row, so the semantics are reproduced here:
        one extra row decides ``has_next``, and a bounded count of vendors is
        run only when the caller asked for a total.
        """
        stmt = self.charges_stmt(date_from=date_from, date_to=date_to, vendor_ids=vendor_ids)
        rows = (
            await self.session.execute(stmt.limit(params.limit + 1).offset(params.offset))
        ).all()

        has_next = len(rows) > params.limit
        rows = rows[: params.limit]

        total: int | None = None
        total_pages: int | None = None
        if params.with_total:
            count_stmt = select(func.count()).select_from(
                stmt.order_by(None).limit(COUNT_CEILING).subquery()
            )
            total = int((await self.session.execute(count_stmt)).scalar_one())
            total_pages = max(1, -(-total // params.limit))

        return [_charge_row(row) for row in rows], OffsetPagination(
            page=params.page,
            limit=params.limit,
            total=total,
            total_pages=total_pages,
            has_next=has_next,
            has_previous=params.page > 1,
        )

    async def consumed_by_item(
        self, vendor_id: uuid.UUID, date_from: date, date_to: date
    ) -> dict[uuid.UUID, dict[str, Decimal]]:
        """Receipt-line totals per input item for one vendor.

        The line is read through its order line for the input item: the receipt
        line itself names only the output item, which is a different code
        whenever the process changes what the thing is. Money follows the same
        grain - charges are apportioned per receipt line, so they attribute to
        the input item that earned them without a second query.
        """
        before = JobWorkReceipt.receipt_date < date_from
        in_period = and_(
            JobWorkReceipt.receipt_date >= date_from,
            JobWorkReceipt.receipt_date <= date_to,
        )

        def _sum(column: Any, condition: Any = None) -> Any:
            if condition is None:
                return func.coalesce(func.sum(column), 0)
            return func.coalesce(func.sum(case((condition, column), else_=0)), 0)

        stmt = (
            select(
                JobWorkOrderLine.item_id.label("item_id"),
                _sum(JobWorkReceiptLine.input_consumed_quantity, before).label("consumed_before"),
                _sum(JobWorkReceiptLine.input_consumed_quantity, in_period).label(
                    "consumed_period"
                ),
                _sum(JobWorkReceiptLine.output_quantity, in_period).label("output_period"),
                _sum(JobWorkReceiptLine.returned_unprocessed_quantity, in_period).label(
                    "returned_period"
                ),
                _sum(JobWorkReceiptLine.scrap_quantity, in_period).label("scrap_period"),
                _sum(JobWorkReceiptLine.charges, in_period).label("charges_period"),
                _sum(JobWorkReceiptLine.invoiced_amount, in_period).label("billed_period"),
                _sum(JobWorkReceiptLine.vendor_debit_amount, in_period).label("debits_period"),
            )
            .join(JobWorkReceipt, JobWorkReceipt.id == JobWorkReceiptLine.receipt_id)
            .join(
                JobWorkOrderLine,
                JobWorkOrderLine.id == JobWorkReceiptLine.job_work_order_line_id,
            )
            .where(
                JobWorkReceiptLine.company_id == self.tenant.company_id,
                JobWorkReceipt.vendor_id == vendor_id,
                JobWorkReceipt.receipt_date <= date_to,
            )
            .group_by(JobWorkOrderLine.item_id)
        )
        out: dict[uuid.UUID, dict[str, Decimal]] = {}
        for row in (await self.session.execute(stmt)).all():
            mapping = row._mapping
            out[row.item_id] = {
                name: Decimal(str(mapping[name]))
                for name in (
                    "consumed_before",
                    "consumed_period",
                    "output_period",
                    "returned_period",
                    "scrap_period",
                    "charges_period",
                    "billed_period",
                    "debits_period",
                )
            }
        return out


class JobWorkOrderRepository(BaseRepository[JobWorkOrder]):
    model = JobWorkOrder
    searchable_fields = ("code", "process_description")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": JobWorkOrder.code,
        "status": JobWorkOrder.status,
        "expected_return_date": JobWorkOrder.expected_return_date,
        "issued_at": JobWorkOrder.issued_at,
        "total_charges": JobWorkOrder.total_charges,
        "created_at": JobWorkOrder.created_at,
    }

    # --- reads --------------------------------------------------------------

    async def get_by_code(self, code: str) -> JobWorkOrder | None:
        return await self.get_by_field("code", code)

    async def lock_for_update(self, order_id: uuid.UUID) -> JobWorkOrder | None:
        """Take a row lock on the header before changing its state or its stock.

        Issue, receive, close and cancel all read the lines, decide what to move
        and write back, and the header also carries the receipt counter. Two of
        them running at once would each act on the quantities they read first,
        so the header is the serialisation point for the whole document.
        """
        stmt = self._scoped().where(JobWorkOrder.id == order_id).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def charges_for_production_order(self, production_order_id: uuid.UUID) -> Decimal:
        """Accrued receipt charges for job-work orders sourced by one production order."""
        stmt = select(func.coalesce(func.sum(JobWorkOrder.total_charges), 0)).where(
            JobWorkOrder.company_id == self.tenant.company_id,
            JobWorkOrder.source_type == JobWorkSourceType.PRODUCTION_ORDER,
            JobWorkOrder.source_id == production_order_id,
        )
        return Decimal((await self.session.execute(stmt)).scalar_one())

    def _filtered(self, filters: JobWorkOrderFilters) -> Select[Any]:
        stmt = self._scoped()
        if filters.vendor_id is not None:
            stmt = stmt.where(JobWorkOrder.vendor_id == filters.vendor_id)
        if filters.warehouse_id is not None:
            stmt = stmt.where(JobWorkOrder.warehouse_id == filters.warehouse_id)
        if filters.status is not None:
            stmt = stmt.where(JobWorkOrder.status == filters.status)
        if filters.process_type is not None:
            stmt = stmt.where(JobWorkOrder.process_type == filters.process_type)
        if filters.at_vendor_only:
            stmt = stmt.where(JobWorkOrder.status.in_(MATERIAL_AT_VENDOR_STATUSES))
        if filters.open_only:
            stmt = stmt.where(JobWorkOrder.status.in_(OPEN_SUPPLY_STATUSES))
        if filters.due_before is not None:
            stmt = stmt.where(JobWorkOrder.expected_return_date <= filters.due_before)
        if filters.created_from is not None:
            stmt = stmt.where(JobWorkOrder.created_at >= filters.created_from)
        if filters.created_to is not None:
            stmt = stmt.where(JobWorkOrder.created_at <= filters.created_to)
        if filters.item_id is not None:
            # EXISTS rather than a join: an order with several lines for the same
            # item would otherwise be returned once per line, and DISTINCT on a
            # wide header row is a sort nobody needs to pay for.
            stmt = stmt.where(
                select(JobWorkOrderLine.id)
                .where(
                    JobWorkOrderLine.company_id == self.tenant.company_id,
                    JobWorkOrderLine.job_work_order_id == JobWorkOrder.id,
                    JobWorkOrderLine.item_id == filters.item_id,
                )
                .exists()
            )
        return self._apply_search(stmt, filters.search)

    async def list_page_filtered(
        self, filters: JobWorkOrderFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[JobWorkOrder], OffsetPagination]:
        order_by = sort.resolve(self.sortable_fields, JobWorkOrder.created_at.desc())
        return await self.list_page(params, stmt=self._filtered(filters), order_by=order_by)

    # --- the pending-at-vendor feed ----------------------------------------

    def pending_at_vendor_stmt(
        self,
        *,
        vendor_id: uuid.UUID | None,
        item_id: uuid.UUID | None,
        warehouse_id: uuid.UUID | None,
        overdue_only: bool,
    ) -> Select[Any]:
        """Company material currently at a vendor, by vendor and item, with ageing.

        Aggregated in the database, grouped by ``(vendor, item, warehouse)``. The
        outstanding quantity is ``issued - received`` per line: what left and has
        not been accounted back, which is precisely what the ``JOB_WORK`` pool is
        holding for this order.

        The status predicate matches the partial index
        ``ix_job_work_orders_material_at_vendor``, so a mature company's closed
        orders are never touched. ``HAVING`` drops lines that are fully
        reconciled but sit on an order that still has other lines open.
        """
        outstanding = func.sum(
            JobWorkOrderLine.issued_quantity - JobWorkOrderLine.received_quantity
        )

        stmt = (
            select(
                JobWorkOrder.vendor_id.label("vendor_id"),
                JobWorkOrderLine.item_id.label("item_id"),
                JobWorkOrder.warehouse_id.label("warehouse_id"),
                outstanding.label("outstanding_quantity"),
                func.count(func.distinct(JobWorkOrder.id)).label("order_count"),
                func.min(JobWorkOrder.issued_at).label("oldest_issued_at"),
                func.min(JobWorkOrder.expected_return_date).label("earliest_expected_return"),
            )
            .select_from(JobWorkOrderLine)
            .join(
                JobWorkOrder,
                and_(
                    JobWorkOrder.id == JobWorkOrderLine.job_work_order_id,
                    JobWorkOrder.company_id == JobWorkOrderLine.company_id,
                ),
            )
            .where(
                JobWorkOrderLine.company_id == self.tenant.company_id,
                JobWorkOrder.status.in_(MATERIAL_AT_VENDOR_STATUSES),
                JobWorkOrderLine.issued_quantity > JobWorkOrderLine.received_quantity,
            )
            .group_by(JobWorkOrder.vendor_id, JobWorkOrderLine.item_id, JobWorkOrder.warehouse_id)
            .having(outstanding > 0)
        )

        if vendor_id is not None:
            stmt = stmt.where(JobWorkOrder.vendor_id == vendor_id)
        if item_id is not None:
            stmt = stmt.where(JobWorkOrderLine.item_id == item_id)
        if warehouse_id is not None:
            stmt = stmt.where(JobWorkOrder.warehouse_id == warehouse_id)
        if overdue_only:
            stmt = stmt.where(JobWorkOrder.expected_return_date < today())

        # Oldest despatch first: the material that has been out longest is the
        # material somebody needs to chase. ``vendor_id`` breaks ties so a page
        # boundary cannot repeat or drop a group.
        return stmt.order_by(func.min(JobWorkOrder.issued_at).asc(), JobWorkOrder.vendor_id.asc())

    async def pending_at_vendor(
        self,
        *,
        vendor_id: uuid.UUID | None = None,
        item_id: uuid.UUID | None = None,
        warehouse_id: uuid.UUID | None = None,
        overdue_only: bool = False,
        limit: int,
    ) -> list[PendingAtVendorRow]:
        """Bounded feed for the service-level contract other modules call."""
        stmt = self.pending_at_vendor_stmt(
            vendor_id=vendor_id,
            item_id=item_id,
            warehouse_id=warehouse_id,
            overdue_only=overdue_only,
        ).limit(limit)
        now = utcnow()
        return [_pending_row(row, now) for row in (await self.session.execute(stmt)).all()]

    async def pending_at_vendor_page(
        self,
        *,
        vendor_id: uuid.UUID | None,
        item_id: uuid.UUID | None,
        warehouse_id: uuid.UUID | None,
        overdue_only: bool,
        params: PageParams,
    ) -> tuple[list[PendingAtVendorRow], OffsetPagination]:
        """Paginate the aggregate.

        ``core.pagination.paginate`` cannot be used: it reads ``.scalars()``,
        which would keep only the first column of an aggregate row. The semantics
        are reproduced here - one extra row to decide ``has_next``, and a bounded
        count only when the caller asked for a total.
        """
        stmt = self.pending_at_vendor_stmt(
            vendor_id=vendor_id,
            item_id=item_id,
            warehouse_id=warehouse_id,
            overdue_only=overdue_only,
        )
        rows = (
            await self.session.execute(stmt.limit(params.limit + 1).offset(params.offset))
        ).all()

        has_next = len(rows) > params.limit
        rows = rows[: params.limit]

        total: int | None = None
        total_pages: int | None = None
        if params.with_total:
            count_stmt = select(func.count()).select_from(
                stmt.order_by(None).limit(COUNT_CEILING).subquery()
            )
            total = int((await self.session.execute(count_stmt)).scalar_one())
            total_pages = max(1, -(-total // params.limit))

        now = utcnow()
        return [_pending_row(row, now) for row in rows], OffsetPagination(
            page=params.page,
            limit=params.limit,
            total=total,
            total_pages=total_pages,
            has_next=has_next,
            has_previous=params.page > 1,
        )


def _pending_row(row: Any, now: datetime) -> PendingAtVendorRow:
    """Map one aggregate row, keeping the numerics as ``Decimal`` (section 84).

    Ageing is computed here rather than in SQL so a frozen clock in a test moves
    it, and so the figure is the same whichever database the query ran on.
    """
    issued_at: datetime | None = row.oldest_issued_at
    expected: date | None = row.earliest_expected_return
    return PendingAtVendorRow(
        vendor_id=row.vendor_id,
        item_id=row.item_id,
        warehouse_id=row.warehouse_id,
        outstanding_quantity=Decimal(str(row.outstanding_quantity)),
        order_count=int(row.order_count),
        oldest_issued_at=issued_at,
        ageing_days=max(0, (now - issued_at).days) if issued_at is not None else 0,
        earliest_expected_return=expected,
        is_overdue=expected is not None and expected < now.date(),
    )


def _charge_row(row: Any) -> VendorChargeRow:
    gross = Decimal(str(row.gross_charges))
    debits = Decimal(str(row.vendor_debits))
    return VendorChargeRow(
        vendor_id=row.vendor_id,
        gross_charges=gross,
        vendor_debits=debits,
        net_charges=gross - debits,
        receipt_count=int(row.receipt_count),
        order_count=int(row.order_count),
        output_quantity=Decimal(str(row.output_quantity)),
    )


__all__ = [
    "ItemLookupRepository",
    "JobWorkBalanceRepository",
    "JobWorkChallanLineRepository",
    "JobWorkChallanRepository",
    "JobWorkItem",
    "JobWorkOrderLineRepository",
    "JobWorkOrderRepository",
    "JobWorkReceiptLineRepository",
    "JobWorkReceiptRepository",
    "TransporterLookupRepository",
    "VendorFacts",
    "VendorLookupRepository",
    "WarehouseLookupRepository",
]
