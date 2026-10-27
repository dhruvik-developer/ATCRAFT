"""Additive D1 examples: pooled demand, an issued revision and duplicate approval.

The four new documents are created in one transaction. Existing markers are
registered without advancing or rewriting them, preserving operator changes.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from src.core.clock import today
from src.core.database import transaction
from src.core.demo_data import DemoContext
from src.core.demo_data.procurement import _existing, _seen, _tagged
from src.modules.procurement import purchase_order_changes as changes
from src.modules.procurement import service
from src.modules.procurement.models import (
    PurchaseOrder,
    PurchaseOrderStatus,
    PurchaseRequisition,
    PurchaseRequisitionLine,
    RequisitionStatus,
)
from src.modules.procurement.schemas import (
    PurchaseOrderAmendRequest,
    PurchaseOrderCreate,
    PurchaseOrderDuplicateResolutionRequest,
    PurchaseOrderLineIn,
    RequisitionCreate,
    RequisitionLineIn,
)
from src.modules.products.models import Item
from src.modules.users.models import User


async def seed(ctx: DemoContext) -> None:
    markers = [ctx.code("D1", name) for name in ("PR-A", "PR-B", "PO-POOL", "PO-DUPLICATE")]
    existing_requisitions = await _existing(ctx, PurchaseRequisition)
    existing_orders = await _existing(ctx, PurchaseOrder)
    if any(marker in {**existing_requisitions, **existing_orders} for marker in markers):
        for kind, marker in zip(
            ("requisition", "requisition", "purchase_order", "purchase_order"), markers, strict=True
        ):
            row = (existing_requisitions if kind == "requisition" else existing_orders).get(marker)
            if row is not None:
                _seen(ctx, kind, marker, row.id, created=False)
        return
    manager_id = ctx.maybe_id("user", ctx.code("USER", "VEN-PACKING"))
    item_id = ctx.maybe_id("item", ctx.code("PK-BOX"))
    warehouse_id = ctx.maybe_id("warehouse", "WH-PKG")
    if manager_id is None or item_id is None or warehouse_id is None:
        ctx.skipped.append(
            (
                "procurement",
                "D1 examples need the packing manager, carton item and packaging warehouse",
            )
        )
        return
    manager = await ctx.session.scalar(
        select(User).where(User.id == manager_id, User.company_id == ctx.company_id)
    )
    item = await ctx.session.scalar(
        select(Item).where(Item.id == item_id, Item.company_id == ctx.company_id)
    )
    if manager is None or manager.vendor_id is None or item is None:
        ctx.skipped.append(("procurement", "D1 example master records are unavailable"))
        return
    required = today() + timedelta(days=365)
    async with transaction(ctx.session):
        source_lines = []
        requisitions = []
        for marker, quantity in zip(markers[:2], (Decimal("5"), Decimal("7")), strict=True):
            requisition, lines = await service.create_requisition(
                ctx.session,
                ctx.tenant,
                ctx.principal,
                RequisitionCreate(
                    warehouse_id=warehouse_id,
                    required_date=required,
                    notes=_tagged(marker, "D1 pooled purchasing demand"),
                    lines=[
                        RequisitionLineIn(
                            line_number=1,
                            item_id=item_id,
                            quantity=quantity,
                            uom_id=item.purchase_uom_id or item.base_uom_id,
                            estimated_unit_price=item.standard_cost,
                            suggested_vendor_id=manager.vendor_id,
                        )
                    ],
                ),
            )
            for status in (RequisitionStatus.PENDING_APPROVAL, RequisitionStatus.APPROVED):
                await service.transition_requisition(
                    ctx.session,
                    ctx.tenant,
                    ctx.principal,
                    requisition.id,
                    status,
                    reason="Demonstration demand reviewed",
                )
            source_lines.append(lines[0])
            requisitions.append(requisition)

        def proposal(
            quantity: Decimal,
            source_line: PurchaseRequisitionLine | None = None,
            notes: str | None = None,
        ) -> PurchaseOrderCreate:
            return PurchaseOrderCreate(
                vendor_id=manager.vendor_id,
                warehouse_id=warehouse_id,
                assigned_vendor_user_id=manager_id,
                expected_delivery_date=required,
                notes=notes,
                lines=[
                    PurchaseOrderLineIn(
                        line_number=1,
                        item_id=item_id,
                        quantity=quantity,
                        uom_id=item.purchase_uom_id or item.base_uom_id,
                        unit_price=item.standard_cost,
                        required_date=required,
                        requisition_line_id=source_line.id if source_line else None,
                    )
                ],
            )

        order, _, _ = await service.create_purchase_order(
            ctx.session,
            ctx.tenant,
            ctx.principal,
            proposal(
                Decimal("5"),
                source_lines[0],
                _tagged(markers[2], "Pooled demand; revision awaiting approval"),
            ),
        )
        result = await changes.resolve_duplicate(
            ctx.session,
            ctx.tenant,
            ctx.principal,
            PurchaseOrderDuplicateResolutionRequest(
                action="MERGE",
                candidate_purchase_order_id=order.id,
                expected_candidate_version=order.version,
                reason="Combine compatible carton requirements",
                draft={"kind": "MANUAL", "purchase_order": proposal(Decimal("7"), source_lines[1])},
            ),
        )
        assert result.purchase_orders[0].id == order.id
        for order_status in (
            PurchaseOrderStatus.PENDING_APPROVAL,
            PurchaseOrderStatus.APPROVED,
            PurchaseOrderStatus.SENT,
        ):
            await service.transition_purchase_order(
                ctx.session,
                ctx.tenant,
                ctx.principal,
                order.id,
                order_status,
                reason="Demonstration purchase terms approved",
                confirm_duplicate=bool(order.duplicate_acknowledged),
            )
        detail = await service.get_purchase_order_detail(ctx.session, ctx.tenant, order.id)
        await changes.amend_purchase_order(
            ctx.session,
            ctx.tenant,
            order.id,
            PurchaseOrderAmendRequest(
                expected_version=detail.version,
                reason="Reduce the second request while retaining explicit allocations",
                lines=[
                    {
                        "line_id": detail.lines[0].id,
                        "quantity": "10",
                        "source_allocations": [
                            {"requisition_line_id": line.id, "quantity": "5"}
                            for line in source_lines
                        ],
                    }
                ],
            ),
        )
        duplicate = await changes.resolve_duplicate(
            ctx.session,
            ctx.tenant,
            ctx.principal,
            PurchaseOrderDuplicateResolutionRequest(
                action="CREATE_WITH_APPROVAL",
                reason="Separate urgent call-off needs a second approval",
                draft={
                    "kind": "MANUAL",
                    "purchase_order": proposal(
                        Decimal("2"),
                        notes=_tagged(
                            markers[3], "Duplicate demand awaiting explicit confirmation"
                        ),
                    ),
                },
            ),
        )
        second_id = duplicate.purchase_orders[0].id
        await service.transition_purchase_order(
            ctx.session,
            ctx.tenant,
            ctx.principal,
            second_id,
            PurchaseOrderStatus.PENDING_APPROVAL,
            reason="Review the separate duplicate requirement",
        )
        for marker, requisition in zip(markers[:2], requisitions, strict=True):
            _seen(ctx, "requisition", marker, requisition.id, created=True)
        _seen(ctx, "purchase_order", markers[2], order.id, created=True)
        _seen(ctx, "purchase_order", markers[3], second_id, created=True)
