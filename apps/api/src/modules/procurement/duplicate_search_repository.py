"""Batched, tenant-scoped facts for duplicate purchase-order detection."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Text, and_, bindparam, cast, exists, func, literal, or_, select, union_all
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError
from src.modules.procurement.models import (
    OPEN_PO_LINE_STATUSES,
    OPEN_PO_STATUSES,
    GoodsReceipt,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderSourceAllocation,
    PurchaseOrderStatus,
    PurchaseRequisitionLine,
    PurchaseSourceType,
)
from src.modules.procurement.repository import DUPLICATE_SCAN_LIMIT
from src.modules.products.models import Item, ItemCategory
from src.modules.vendors.models import VendorCategory


@dataclass(frozen=True, slots=True)
class RequisitionDemandFacts:
    item_id: uuid.UUID
    source_demand_refs: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class OpenDuplicateLine:
    purchase_order_id: uuid.UUID
    purchase_order_code: str
    purchase_order_version: int
    purchase_order_line_id: uuid.UUID
    status: PurchaseOrderStatus
    vendor_id: uuid.UUID
    warehouse_id: uuid.UUID
    item_id: uuid.UUID
    category_path: str | None
    quantity: Decimal
    received_quantity: Decimal
    required_date: date | None
    source_type: PurchaseSourceType
    source_id: uuid.UUID | None
    has_receipt_history: bool


class DuplicateSearchRepository:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant

    async def item_lineages(self, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str | None]:
        if not item_ids:
            return {}
        stmt = (
            select(Item.id, ItemCategory.path)
            .outerjoin(
                ItemCategory,
                and_(
                    ItemCategory.id == Item.category_id, ItemCategory.company_id == Item.company_id
                ),
            )
            .where(Item.company_id == self.tenant.company_id, Item.id.in_(set(item_ids)))
        )
        return {row.id: row.path for row in await self.session.execute(stmt)}

    async def vendor_categories(
        self, vendor_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, frozenset[uuid.UUID]]:
        if not vendor_ids:
            return {}
        stmt = select(VendorCategory.vendor_id, VendorCategory.item_category_id).where(
            VendorCategory.company_id == self.tenant.company_id,
            VendorCategory.vendor_id.in_(set(vendor_ids)),
        )
        result: dict[uuid.UUID, set[uuid.UUID]] = {}
        for row in await self.session.execute(stmt):
            result.setdefault(row.vendor_id, set()).add(row.item_category_id)
        return {vendor_id: frozenset(categories) for vendor_id, categories in result.items()}

    async def requisition_demands(
        self, source_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, RequisitionDemandFacts]:
        if not source_ids:
            return {}
        stmt = select(
            PurchaseRequisitionLine.id,
            PurchaseRequisitionLine.item_id,
            PurchaseRequisitionLine.source_demand_refs,
        ).where(
            PurchaseRequisitionLine.company_id == self.tenant.company_id,
            PurchaseRequisitionLine.id.in_(set(source_ids)),
        )
        return {
            row.id: RequisitionDemandFacts(row.item_id, tuple(row.source_demand_refs or ()))
            for row in await self.session.execute(stmt)
        }

    async def open_lines(
        self,
        *,
        vendor_warehouses: Sequence[tuple[uuid.UUID, uuid.UUID]],
        item_ids: Sequence[uuid.UUID],
        category_ids: Sequence[uuid.UUID],
        date_from: date | None,
        date_to: date | None,
        unrestricted_dates: bool,
        header_sources: Sequence[tuple[PurchaseSourceType, uuid.UUID]],
        requisition_line_ids: Sequence[uuid.UUID],
        demand_refs: Sequence[Mapping[str, Any]],
        group_count: int,
        exclude_order_ids: Sequence[uuid.UUID] = (),
    ) -> list[OpenDuplicateLine]:
        if not vendor_warehouses or not item_ids:
            return []
        required = func.coalesce(
            PurchaseOrderLine.required_date, PurchaseOrder.expected_delivery_date
        )
        in_window = (
            literal(True)
            if unrestricted_dates
            else or_(required.is_(None), required.between(date_from, date_to))
        )
        same_header = or_(
            *[
                and_(PurchaseOrder.source_type == source_type, PurchaseOrder.source_id == source_id)
                for source_type, source_id in header_sources
            ],
            literal(False),
        )
        any_mapping = exists(
            select(PurchaseOrderSourceAllocation.id).where(
                PurchaseOrderSourceAllocation.company_id == self.tenant.company_id,
                PurchaseOrderSourceAllocation.purchase_order_line_id == PurchaseOrderLine.id,
            )
        )
        # A durable mapping is authoritative, including when all its rows are
        # inactive. Only pre-migration/imported rows use the scalar fallback.
        active_sources = select(PurchaseOrderSourceAllocation.requisition_line_id).where(
            PurchaseOrderSourceAllocation.company_id == self.tenant.company_id,
            PurchaseOrderSourceAllocation.purchase_order_line_id == PurchaseOrderLine.id,
            PurchaseOrderSourceAllocation.is_active.is_(True),
        )
        same_pr = (
            or_(
                exists(
                    active_sources.where(
                        PurchaseOrderSourceAllocation.requisition_line_id.in_(
                            set(requisition_line_ids)
                        )
                    )
                ),
                and_(
                    ~any_mapping,
                    PurchaseOrderLine.requisition_line_id.in_(set(requisition_line_ids)),
                ),
            )
            if requisition_line_ids
            else literal(False)
        )
        same_root: ColumnElement[bool] = literal(False)
        if demand_refs:
            # One JSONB parameter avoids an OR clause per demand. PostgreSQL
            # can probe the existing GIN index with each canonical containment
            # value; no display-list truncation rules are involved here.
            roots = (
                func.jsonb_array_elements(
                    bindparam("duplicate_root_refs", list(demand_refs), type_=JSONB)
                )
                .table_valued("value")
                .alias("duplicate_roots")
            )
            matching_sources = (
                select(PurchaseRequisitionLine.id)
                .select_from(PurchaseRequisitionLine)
                .join(roots, literal(True))
                .where(
                    PurchaseRequisitionLine.company_id == self.tenant.company_id,
                    PurchaseRequisitionLine.source_demand_refs.contains(
                        func.jsonb_build_array(roots.c.value)
                    ),
                )
                .distinct()
                .cte("duplicate_root_sources")
            )
            same_root = or_(
                exists(
                    active_sources.where(
                        PurchaseOrderSourceAllocation.requisition_line_id.in_(
                            select(matching_sources.c.id)
                        )
                    )
                ),
                and_(
                    ~any_mapping,
                    PurchaseOrderLine.requisition_line_id.in_(select(matching_sources.c.id)),
                ),
            )
        category_match = (
            func.string_to_array(ItemCategory.path, "/").op("&&")(
                cast([str(category_id) for category_id in set(category_ids)], ARRAY(Text))
            )
            if category_ids
            else literal(False)
        )
        has_receipt = exists(
            select(GoodsReceipt.id).where(
                GoodsReceipt.company_id == self.tenant.company_id,
                GoodsReceipt.purchase_order_id == PurchaseOrder.id,
            )
        )
        limit = DUPLICATE_SCAN_LIMIT * max(1, group_count)
        stmt = (
            select(
                PurchaseOrder.id.label("purchase_order_id"),
                PurchaseOrder.code.label("purchase_order_code"),
                PurchaseOrder.version.label("purchase_order_version"),
                PurchaseOrder.status,
                PurchaseOrder.vendor_id,
                PurchaseOrder.warehouse_id,
                PurchaseOrder.source_type,
                PurchaseOrder.source_id,
                PurchaseOrderLine.id.label("purchase_order_line_id"),
                PurchaseOrderLine.item_id,
                PurchaseOrderLine.quantity,
                PurchaseOrderLine.received_quantity,
                required.label("required_date"),
                ItemCategory.path.label("category_path"),
                has_receipt.label("has_receipt_history"),
            )
            .select_from(PurchaseOrderLine)
            .join(
                PurchaseOrder,
                and_(
                    PurchaseOrder.id == PurchaseOrderLine.purchase_order_id,
                    PurchaseOrder.company_id == PurchaseOrderLine.company_id,
                ),
            )
            .join(
                Item,
                and_(
                    Item.id == PurchaseOrderLine.item_id,
                    Item.company_id == PurchaseOrderLine.company_id,
                ),
            )
            .outerjoin(
                ItemCategory,
                and_(
                    ItemCategory.id == Item.category_id, ItemCategory.company_id == Item.company_id
                ),
            )
            .where(
                PurchaseOrderLine.company_id == self.tenant.company_id,
                PurchaseOrder.status.in_(OPEN_PO_STATUSES),
                PurchaseOrderLine.status.in_(OPEN_PO_LINE_STATUSES),
                or_(
                    *[
                        and_(
                            PurchaseOrder.vendor_id == vendor_id,
                            PurchaseOrder.warehouse_id == warehouse_id,
                        )
                        for vendor_id, warehouse_id in set(vendor_warehouses)
                    ]
                ),
                or_(
                    and_(
                        PurchaseOrderLine.item_id.in_(set(item_ids)),
                        or_(in_window, same_header, same_pr, same_root),
                    ),
                    and_(category_match, in_window),
                ),
            )
            .order_by(PurchaseOrder.po_date.desc(), PurchaseOrder.id.desc(), PurchaseOrderLine.id)
            .limit(limit + 1)
        )
        if exclude_order_ids:
            stmt = stmt.where(PurchaseOrder.id.not_in(set(exclude_order_ids)))
        rows = (await self.session.execute(stmt)).all()
        if len(rows) > limit:
            raise BusinessRuleError(
                "Too many open purchase-order lines match this duplicate check. "
                "Narrow the selected demand or delivery dates before continuing.",
                details={"limit": limit},
            )
        return [
            OpenDuplicateLine(
                purchase_order_id=row.purchase_order_id,
                purchase_order_code=row.purchase_order_code,
                purchase_order_version=row.purchase_order_version,
                purchase_order_line_id=row.purchase_order_line_id,
                status=PurchaseOrderStatus(row.status),
                vendor_id=row.vendor_id,
                warehouse_id=row.warehouse_id,
                item_id=row.item_id,
                category_path=row.category_path,
                quantity=Decimal(row.quantity),
                received_quantity=Decimal(row.received_quantity),
                required_date=row.required_date,
                source_type=PurchaseSourceType(row.source_type),
                source_id=row.source_id,
                has_receipt_history=bool(row.has_receipt_history),
            )
            for row in rows
        ]

    async def candidate_sources(
        self, line_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[tuple[uuid.UUID, tuple[Mapping[str, Any], ...]]]]:
        if not line_ids:
            return {}
        mapped = (
            select(
                PurchaseOrderSourceAllocation.purchase_order_line_id.label("line_id"),
                PurchaseOrderSourceAllocation.requisition_line_id.label("source_id"),
                PurchaseRequisitionLine.source_demand_refs,
            )
            .join(
                PurchaseRequisitionLine,
                and_(
                    PurchaseRequisitionLine.id == PurchaseOrderSourceAllocation.requisition_line_id,
                    PurchaseRequisitionLine.company_id == PurchaseOrderSourceAllocation.company_id,
                ),
            )
            .where(
                PurchaseOrderSourceAllocation.company_id == self.tenant.company_id,
                PurchaseOrderSourceAllocation.purchase_order_line_id.in_(set(line_ids)),
                PurchaseOrderSourceAllocation.is_active.is_(True),
            )
        )
        has_mapping = exists(
            select(PurchaseOrderSourceAllocation.id).where(
                PurchaseOrderSourceAllocation.company_id == self.tenant.company_id,
                PurchaseOrderSourceAllocation.purchase_order_line_id == PurchaseOrderLine.id,
            )
        )
        legacy = (
            select(
                PurchaseOrderLine.id.label("line_id"),
                PurchaseOrderLine.requisition_line_id.label("source_id"),
                PurchaseRequisitionLine.source_demand_refs,
            )
            .join(
                PurchaseRequisitionLine,
                and_(
                    PurchaseRequisitionLine.id == PurchaseOrderLine.requisition_line_id,
                    PurchaseRequisitionLine.company_id == PurchaseOrderLine.company_id,
                ),
            )
            .where(
                PurchaseOrderLine.company_id == self.tenant.company_id,
                PurchaseOrderLine.id.in_(set(line_ids)),
                ~has_mapping,
            )
        )
        result: dict[uuid.UUID, list[tuple[uuid.UUID, tuple[Mapping[str, Any], ...]]]] = {}
        for row in await self.session.execute(union_all(mapped, legacy)):
            result.setdefault(row.line_id, []).append(
                (row.source_id, tuple(row.source_demand_refs or ()))
            )
        return result
