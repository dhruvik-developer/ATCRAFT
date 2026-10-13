"""Queries for sales orders.

Three shapes in this file exist for performance reasons, not convenience:

* **Everything the pricing engine needs is batched.** ``ItemLookupRepository``,
  ``price_candidates`` and ``variants`` all take a collection of ids and answer
  in one statement. A twenty-line order priced with a lookup per line would be
  sixty round trips on the write path (master rules section 32), which is
  exactly the N+1 the rules forbid.
* **Availability is one grouped read, not one read per line.** The
  ``inventory`` module's ``get_available`` is the documented per-item contract;
  asking it in a loop is the same N+1. ``available_by_item`` answers for the
  whole order in a single aggregate over ``inventory_balances``, which is a read
  of a derived table and never cached (database.md section 4.5, section 34).
* **The demand summary is a single ``GROUP BY``.** MRP and the dashboards read
  it; walking orders in Python and summing would be an unbounded scan dressed up
  as a loop.

Reads that cross into ``items``/``item_prices``/``inventory_balances`` go
through ``BaseRepository`` so the tenant filter and soft-delete filter stay
automatic rather than something each statement has to remember.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, delete, func, or_, select
from sqlalchemy.orm import InstrumentedAttribute

from src.common.repository import BaseRepository
from src.core.database import affected_rows
from src.core.pagination import COUNT_CEILING, PageParams, SortParams
from src.core.responses import OffsetPagination
from src.modules.customers.models import Customer, CustomerAddress
from src.modules.inventory.models import InventoryBalance
from src.modules.organization.models import Company
from src.modules.products.models import Item, ItemPrice, ItemVariant, PriceType
from src.modules.sales.models import (
    OPEN_DEMAND_STATUSES,
    SalesOrder,
    SalesOrderLine,
    SalesOrderLineStatus,
)
from src.modules.sales.schemas import DemandSummaryFilters, DemandSummaryRow, SalesOrderFilters


@dataclass(frozen=True, slots=True)
class SellableItem:
    """The item-master facts order entry needs.

    A projection rather than the ``Item`` entity: order entry reads ten columns
    of a very wide table for every line, and loading the whole row - nine
    capability flags, planning parameters, physical attributes - to use a third
    of it is the ``SELECT *`` the rules forbid.
    """

    id: uuid.UUID
    code: str
    name: str
    base_uom_id: uuid.UUID
    sale_uom_id: uuid.UUID | None
    gst_rate: Decimal
    sale_price: Decimal
    dealer_price: Decimal
    #: The floor a price override is measured against. Commercially sensitive -
    #: it is used for a comparison here and never serialised into a response.
    standard_cost: Decimal
    can_sell: bool
    is_active: bool

    @property
    def order_uom_id(self) -> uuid.UUID:
        """The unit a line defaults to: the sale unit, else the stock unit."""
        return self.sale_uom_id or self.base_uom_id


@dataclass(frozen=True, slots=True)
class VariantInfo:
    id: uuid.UUID
    item_id: uuid.UUID
    code: str
    price_delta: Decimal
    is_active: bool


class ItemLookupRepository(BaseRepository[Item]):
    """Read-only access to the item master for order entry and pricing."""

    model = Item

    async def load(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, SellableItem]:
        """Batched id -> item facts. One statement for a whole order."""
        if not item_ids:
            return {}
        stmt = select(
            Item.id,
            Item.code,
            Item.name,
            Item.base_uom_id,
            Item.sale_uom_id,
            Item.gst_rate,
            Item.sale_price,
            Item.dealer_price,
            Item.standard_cost,
            Item.can_sell,
            Item.is_active,
        ).where(
            Item.company_id == self.tenant.company_id,
            Item.deleted_at.is_(None),
            Item.id.in_(list(dict.fromkeys(item_ids))),
        )
        rows = (await self.session.execute(stmt)).all()
        return {
            row.id: SellableItem(
                id=row.id,
                code=row.code,
                name=row.name,
                base_uom_id=row.base_uom_id,
                sale_uom_id=row.sale_uom_id,
                gst_rate=Decimal(row.gst_rate),
                sale_price=Decimal(row.sale_price),
                dealer_price=Decimal(row.dealer_price),
                standard_cost=Decimal(row.standard_cost),
                can_sell=row.can_sell,
                is_active=row.is_active,
            )
            for row in rows
        }

    async def variants(self, variant_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, VariantInfo]:
        if not variant_ids:
            return {}
        stmt = select(
            ItemVariant.id,
            ItemVariant.item_id,
            ItemVariant.code,
            ItemVariant.price_delta,
            ItemVariant.is_active,
        ).where(
            ItemVariant.company_id == self.tenant.company_id,
            ItemVariant.deleted_at.is_(None),
            ItemVariant.id.in_(list(dict.fromkeys(variant_ids))),
        )
        return {
            row.id: VariantInfo(
                id=row.id,
                item_id=row.item_id,
                code=row.code,
                price_delta=Decimal(row.price_delta),
                is_active=row.is_active,
            )
            for row in (await self.session.execute(stmt)).all()
        }

    async def uom_ids_in_tenant(self, uom_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        """Which of these units actually belong to this tenant. One statement."""
        if not uom_ids:
            return set()
        from src.modules.products.models import UnitOfMeasure

        stmt = select(UnitOfMeasure.id).where(
            UnitOfMeasure.company_id == self.tenant.company_id,
            UnitOfMeasure.deleted_at.is_(None),
            UnitOfMeasure.id.in_(list(dict.fromkeys(uom_ids))),
        )
        return {row.id for row in (await self.session.execute(stmt)).all()}


class PriceListRepository(BaseRepository[ItemPrice]):
    """Price-list rows for a whole order in one read.

    Slab ranking itself stays in ``products.service.select_price_row`` - it is a
    business rule worth unit-testing without a database, and duplicating it here
    would let sales and the price-list screen disagree about what an item costs.
    """

    model = ItemPrice

    async def candidates_for_items(
        self,
        item_ids: Sequence[uuid.UUID],
        *,
        price_type: PriceType,
        variant_ids: Sequence[uuid.UUID],
        on_date: date,
    ) -> dict[uuid.UUID, list[ItemPrice]]:
        """Every live price row that could apply to any line, grouped by item.

        Variant-specific rows are fetched alongside the item-wide ones because
        the ranking compares them: a row for the exact variant beats a row for
        the item, and both have to be in hand for that to be decidable.
        """
        if not item_ids:
            return {}
        variant_clause: ColumnElement[bool] = ItemPrice.variant_id.is_(None)
        if variant_ids:
            variant_clause = or_(
                ItemPrice.variant_id.is_(None),
                ItemPrice.variant_id.in_(list(dict.fromkeys(variant_ids))),
            )

        stmt = self._scoped().where(
            ItemPrice.item_id.in_(list(dict.fromkeys(item_ids))),
            ItemPrice.price_type == price_type,
            ItemPrice.is_active.is_(True),
            ItemPrice.valid_from <= on_date,
            or_(ItemPrice.valid_to.is_(None), ItemPrice.valid_to >= on_date),
            variant_clause,
        )
        grouped: dict[uuid.UUID, list[ItemPrice]] = {}
        for row in (await self.session.execute(stmt)).scalars().all():
            grouped.setdefault(row.item_id, []).append(row)
        return grouped


class AvailabilityRepository(BaseRepository[InventoryBalance]):
    """Promisable stock for a whole order in one aggregate."""

    model = InventoryBalance

    async def available_by_item(
        self, item_ids: Sequence[uuid.UUID], warehouse_id: uuid.UUID | None = None
    ) -> dict[uuid.UUID, Decimal]:
        """Sum of ``available_quantity`` per item. Never cached (section 34).

        ``available_quantity`` is a generated column that already nets
        reservations, QC holds and damage out of on-hand, so it is exactly the
        quantity a new order may be promised.
        """
        if not item_ids:
            return {}
        stmt = (
            select(
                InventoryBalance.item_id,
                func.coalesce(func.sum(InventoryBalance.available_quantity), 0).label("available"),
            )
            .where(
                InventoryBalance.company_id == self.tenant.company_id,
                InventoryBalance.item_id.in_(list(dict.fromkeys(item_ids))),
            )
            .group_by(InventoryBalance.item_id)
        )
        if warehouse_id is not None:
            stmt = stmt.where(InventoryBalance.warehouse_id == warehouse_id)

        rows = (await self.session.execute(stmt)).all()
        return {row.item_id: Decimal(str(row.available)) for row in rows}


class OrderPartyRepository(BaseRepository[CustomerAddress]):
    """The customer-side facts an order needs: addresses and the seller's state."""

    model = CustomerAddress

    async def addresses_of(
        self, customer_id: uuid.UUID, address_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, CustomerAddress]:
        """Load the named addresses, scoped to one customer.

        Scoping to the customer here is the check: an address id belonging to
        another account must not be attachable to this order, and filtering in
        the query means no caller can forget to compare afterwards.
        """
        ids = [i for i in dict.fromkeys(address_ids) if i is not None]
        if not ids:
            return {}
        stmt = self._scoped().where(
            CustomerAddress.customer_id == customer_id,
            CustomerAddress.id.in_(ids),
        )
        return {row.id: row for row in (await self.session.execute(stmt)).scalars().all()}

    async def default_shipping_address(self, customer_id: uuid.UUID) -> CustomerAddress | None:
        """The ship-to used when the request does not name one."""
        from src.modules.customers.models import AddressType

        stmt = self._scoped().where(
            CustomerAddress.customer_id == customer_id,
            CustomerAddress.address_type == AddressType.SHIPPING,
            CustomerAddress.is_default.is_(True),
            CustomerAddress.is_active.is_(True),
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def seller_state_code(self, branch_id: uuid.UUID | None = None) -> str | None:
        """The state GST is charged from.

        A branch registered separately under GST supplies from its own state, so
        the branch wins when the order names one; otherwise the company's
        registered state applies.
        """
        if branch_id is not None:
            from src.modules.organization.models import Branch

            branch_stmt = select(Branch.state_code).where(
                Branch.company_id == self.tenant.company_id,
                Branch.id == branch_id,
                Branch.deleted_at.is_(None),
            )
            branch_state = (await self.session.execute(branch_stmt)).scalar_one_or_none()
            if branch_state:
                return branch_state

        stmt = select(Company.state_code).where(Company.id == self.tenant.company_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def warehouse_in_tenant(self, warehouse_id: uuid.UUID) -> bool:
        """Confirm the ship-from warehouse belongs to this tenant.

        The foreign key only proves the row exists somewhere; it says nothing
        about which company owns it, and a client-supplied id must never reach
        another tenant's warehouse (section 40).
        """
        from src.modules.warehouses.models import Warehouse

        stmt = select(Warehouse.id).where(
            Warehouse.company_id == self.tenant.company_id,
            Warehouse.id == warehouse_id,
            Warehouse.deleted_at.is_(None),
            Warehouse.is_active.is_(True),
        )
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def branch_in_tenant(self, branch_id: uuid.UUID) -> bool:
        from src.modules.organization.models import Branch

        stmt = select(Branch.id).where(
            Branch.company_id == self.tenant.company_id,
            Branch.id == branch_id,
            Branch.deleted_at.is_(None),
        )
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def customer_price_type(self, customer_id: uuid.UUID) -> PriceType | None:
        """Which price list this account is charged on."""
        stmt = select(Customer.price_list_type).where(
            Customer.company_id == self.tenant.company_id,
            Customer.id == customer_id,
            Customer.deleted_at.is_(None),
        )
        value = (await self.session.execute(stmt)).scalar_one_or_none()
        return PriceType(value) if value is not None else None


class SalesOrderLineRepository(BaseRepository[SalesOrderLine]):
    model = SalesOrderLine

    async def list_for_order(self, order_id: uuid.UUID) -> list[SalesOrderLine]:
        stmt = (
            self._scoped()
            .where(SalesOrderLine.sales_order_id == order_id)
            .order_by(SalesOrderLine.line_number)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def lock_for_order(self, order_id: uuid.UUID) -> list[SalesOrderLine]:
        """Every line of one order, row-locked in primary-key order and re-read.

        Primary-key order is the order a consignment locks an order's lines in,
        so a top-up and a dispatch touching the same order queue rather than
        deadlock; ``populate_existing`` makes the quantities the ones under the
        lock, not whatever an earlier read left in the identity map.
        """
        stmt = (
            self._scoped()
            .where(SalesOrderLine.sales_order_id == order_id)
            .order_by(SalesOrderLine.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def live_reservation_quantities(
        self, reservation_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, Decimal]:
        """What each named reservation still holds - for the open ones only.

        A RELEASED, EXPIRED or CONSUMED reservation is absent from the answer,
        which is how every caller here reads it: as no live reservation at all.
        One statement for any number of ids.

        The allocation model is imported here rather than at module import, for
        the reason ``sales.service.AllocationService`` gives: the sales module
        stays importable where allocation is not deployed.
        """
        ids = list(dict.fromkeys(reservation_ids))
        if not ids:
            return {}
        from src.modules.allocation.models import OPEN_RESERVATION_STATUSES, StockReservation

        stmt = select(StockReservation.id, StockReservation.remaining_quantity).where(
            StockReservation.company_id == self.tenant.company_id,
            StockReservation.id.in_(ids),
            StockReservation.status.in_(sorted(OPEN_RESERVATION_STATUSES)),
        )
        return {
            row.id: Decimal(row.remaining_quantity)
            for row in (await self.session.execute(stmt)).all()
        }

    async def list_for_orders(
        self, order_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[SalesOrderLine]]:
        """Lines for several orders in one statement - used by batch readers."""
        if not order_ids:
            return {}
        stmt = (
            self._scoped()
            .where(SalesOrderLine.sales_order_id.in_(list(dict.fromkeys(order_ids))))
            .order_by(SalesOrderLine.sales_order_id, SalesOrderLine.line_number)
        )
        grouped: dict[uuid.UUID, list[SalesOrderLine]] = {}
        for row in (await self.session.execute(stmt)).scalars().all():
            grouped.setdefault(row.sales_order_id, []).append(row)
        return grouped

    async def delete_for_order(self, order_id: uuid.UUID) -> int:
        """Drop a draft's line set before rewriting it.

        A hard delete, not a soft one: a draft line has no historical obligation,
        and section 28 reserves soft deletion for records history points at.
        """
        stmt = delete(SalesOrderLine).where(
            SalesOrderLine.company_id == self.tenant.company_id,
            SalesOrderLine.sales_order_id == order_id,
        )
        return affected_rows(await self.session.execute(stmt))


#: The record's user-facing name, shared by the not-found path here and the
#: service's own messages so the same missing order never answers "SalesOrder
#: not found" from one route and "Sales order not found" from another.
SALES_ORDER_ENTITY = "Sales order"


class SalesOrderRepository(BaseRepository[SalesOrder]):
    model = SalesOrder
    entity_label = SALES_ORDER_ENTITY
    searchable_fields = ("code", "customer_po_number")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": SalesOrder.code,
        "order_date": SalesOrder.order_date,
        "required_date": SalesOrder.required_date,
        "promised_date": SalesOrder.promised_date,
        "status": SalesOrder.status,
        "priority": SalesOrder.priority,
        "total_amount": SalesOrder.total_amount,
        "created_at": SalesOrder.created_at,
    }

    # --- reads --------------------------------------------------------------

    async def get_by_code(self, code: str) -> SalesOrder | None:
        return await self.get_by_field("code", code)

    async def lock_for_update(self, order_id: uuid.UUID) -> SalesOrder | None:
        """Take a row lock on the header before changing its allocation.

        Confirmation and cancellation both read the lines, decide what to
        reserve or release, and write back. Two of them running at once on the
        same order would each reserve against the quantities they read first,
        so the header is the serialisation point for the whole document.
        """
        stmt = self._scoped().where(SalesOrder.id == order_id).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def _filtered(self, filters: SalesOrderFilters) -> Select[Any]:
        stmt = self._scoped()
        if filters.customer_id is not None:
            stmt = stmt.where(SalesOrder.customer_id == filters.customer_id)
        if filters.warehouse_id is not None:
            stmt = stmt.where(SalesOrder.warehouse_id == filters.warehouse_id)
        if filters.branch_id is not None:
            stmt = stmt.where(SalesOrder.branch_id == filters.branch_id)
        if filters.status is not None:
            stmt = stmt.where(SalesOrder.status == filters.status)
        if filters.open_only:
            stmt = stmt.where(SalesOrder.status.in_(OPEN_DEMAND_STATUSES))
        if filters.is_urgent is not None:
            stmt = stmt.where(SalesOrder.is_urgent.is_(filters.is_urgent))
        if filters.required_from is not None:
            stmt = stmt.where(SalesOrder.required_date >= filters.required_from)
        if filters.required_to is not None:
            stmt = stmt.where(SalesOrder.required_date <= filters.required_to)
        return self._apply_search(stmt, filters.search)

    async def list_page_filtered(
        self, filters: SalesOrderFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[SalesOrder], OffsetPagination]:
        order_by = sort.resolve(self.sortable_fields, SalesOrder.created_at.desc())
        return await self.list_page(params, stmt=self._filtered(filters), order_by=order_by)

    # --- the MRP / dashboard feed ------------------------------------------

    def demand_summary_stmt(self, filters: DemandSummaryFilters) -> Select[Any]:
        """Open demand per item, as one aggregate.

        The join is on ``(id, company_id)`` rather than id alone: the tenant
        predicate then applies to both sides of the join and PostgreSQL can use
        the tenant-leading indexes on each table instead of joining across
        companies and filtering afterwards.

        ``open`` is what has not shipped; ``shortfall`` is what has neither
        shipped nor been reserved, floored at zero - that is the quantity MRP
        has to plan for (master rules section 72).
        """
        open_quantity = SalesOrderLine.quantity - SalesOrderLine.dispatched_quantity
        shortfall = func.greatest(open_quantity - SalesOrderLine.allocated_quantity, 0)

        stmt = (
            select(
                SalesOrderLine.item_id.label("item_id"),
                func.sum(open_quantity).label("open_quantity"),
                func.sum(SalesOrderLine.allocated_quantity).label("allocated_quantity"),
                func.sum(shortfall).label("shortfall_quantity"),
                func.count(func.distinct(SalesOrderLine.sales_order_id)).label("order_count"),
                func.count(SalesOrderLine.id).label("line_count"),
                func.min(
                    func.coalesce(SalesOrderLine.required_date, SalesOrder.required_date)
                ).label("earliest_required_date"),
                func.min(SalesOrder.priority).label("highest_priority"),
                func.bool_or(SalesOrder.is_urgent).label("has_urgent_order"),
            )
            .select_from(SalesOrderLine)
            .join(
                SalesOrder,
                and_(
                    SalesOrder.id == SalesOrderLine.sales_order_id,
                    SalesOrder.company_id == SalesOrderLine.company_id,
                ),
            )
            .where(
                SalesOrderLine.company_id == self.tenant.company_id,
                SalesOrder.status.in_(OPEN_DEMAND_STATUSES),
                SalesOrderLine.status != SalesOrderLineStatus.CANCELLED,
                SalesOrderLine.dispatched_quantity < SalesOrderLine.quantity,
            )
            .group_by(SalesOrderLine.item_id)
        )

        if filters.warehouse_id is not None:
            stmt = stmt.where(SalesOrder.warehouse_id == filters.warehouse_id)
        if filters.item_id is not None:
            stmt = stmt.where(SalesOrderLine.item_id == filters.item_id)
        if filters.required_before is not None:
            stmt = stmt.where(
                func.coalesce(SalesOrderLine.required_date, SalesOrder.required_date)
                <= filters.required_before
            )
        if filters.shortfall_only:
            stmt = stmt.having(func.sum(shortfall) > 0)

        # Deterministic order: soonest need first, then item id so a page
        # boundary cannot repeat or drop a row when two items share a date.
        return stmt.order_by(
            func.min(func.coalesce(SalesOrderLine.required_date, SalesOrder.required_date)).asc(),
            SalesOrderLine.item_id.asc(),
        )

    async def demand_summary(
        self, filters: DemandSummaryFilters, params: PageParams
    ) -> tuple[list[DemandSummaryRow], OffsetPagination]:
        """Paginate the aggregate.

        ``core.pagination.paginate`` cannot be used: it reads ``.scalars()``,
        which would keep only the first column of an aggregate row. The
        semantics are reproduced here - one extra row to decide ``has_next``,
        and a bounded count only when the caller asked for a total.
        """
        stmt = self.demand_summary_stmt(filters)
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

        summaries = [
            DemandSummaryRow(
                item_id=row.item_id,
                open_quantity=Decimal(str(row.open_quantity)),
                allocated_quantity=Decimal(str(row.allocated_quantity)),
                shortfall_quantity=Decimal(str(row.shortfall_quantity)),
                order_count=int(row.order_count),
                line_count=int(row.line_count),
                earliest_required_date=row.earliest_required_date,
                highest_priority=int(row.highest_priority),
                has_urgent_order=bool(row.has_urgent_order),
            )
            for row in rows
        ]

        return summaries, OffsetPagination(
            page=params.page,
            limit=params.limit,
            total=total,
            total_pages=total_pages,
            has_next=has_next,
            has_previous=params.page > 1,
        )


__all__ = [
    "AvailabilityRepository",
    "ItemLookupRepository",
    "OrderPartyRepository",
    "PriceListRepository",
    "SalesOrderLineRepository",
    "SalesOrderRepository",
    "SellableItem",
    "VariantInfo",
]
