"""Real PostgreSQL containment, allocation and category duplicate queries."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

from src.core.clock import today
from src.core.ids import new_id
from src.modules.procurement.duplicate_search import find_duplicates_for_lines
from src.modules.procurement.models import (
    GoodsReceipt,
    GoodsReceiptStatus,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderSourceAllocation,
    PurchaseOrderStatus,
    PurchaseRequisition,
    PurchaseRequisitionLine,
    PurchaseSourceType,
)
from src.modules.procurement.service_rules import DuplicateAction, DuplicateConfidence, ProposedLine
from src.modules.products.models import ItemCategory
from src.modules.vendors.models import VendorCategory
from tests.integration.test_procurement_service import _schema as _schema
from tests.integration.test_procurement_service import db as db
from tests.integration.test_procurement_service import masters as masters
from tests.integration.test_procurement_service import other_tenant as other_tenant
from tests.integration.test_procurement_service import tenant as tenant
from tests.seed_support import seed_item

pytestmark = pytest.mark.integration


async def _source(db, tenant, masters, refs):
    requisition = PurchaseRequisition(
        company_id=tenant.company_id,
        code=f"SEARCH-PR-{uuid.uuid4().hex[:10]}",
        requisition_date=today(),
        required_date=today(),
        warehouse_id=masters["warehouse"].id,
        requested_by_id=tenant.user_id,
    )
    db.add(requisition)
    await db.flush()
    line = PurchaseRequisitionLine(
        company_id=tenant.company_id,
        requisition_id=requisition.id,
        line_number=1,
        item_id=masters["item"].id,
        uom_id=masters["uom"].id,
        quantity=Decimal("10"),
        source_demand_refs=refs,
    )
    db.add(line)
    await db.flush()
    return line


async def _candidate(db, tenant, masters, *, item_id=None, required_date=None, source_id=None):
    order = PurchaseOrder(
        company_id=tenant.company_id,
        code=f"SEARCH-PO-{uuid.uuid4().hex[:10]}",
        po_date=today(),
        expected_delivery_date=required_date or today(),
        vendor_id=masters["vendor"].id,
        warehouse_id=masters["warehouse"].id,
        status=PurchaseOrderStatus.SENT,
    )
    db.add(order)
    await db.flush()
    line = PurchaseOrderLine(
        company_id=tenant.company_id,
        purchase_order_id=order.id,
        line_number=1,
        item_id=item_id or masters["item"].id,
        uom_id=masters["uom"].id,
        quantity=Decimal("16"),
        unit_price=Decimal("50"),
        requisition_line_id=source_id,
    )
    db.add(line)
    await db.flush()
    return order, line


async def _find(db, tenant, masters, source_id=None, **kwargs):
    return await find_duplicates_for_lines(
        db,
        tenant,
        vendor_id=masters["vendor"].id,
        warehouse_id=masters["warehouse"].id,
        lines=[ProposedLine(masters["item"].id, today(), requisition_line_id=source_id)],
        source_type=PurchaseSourceType.MANUAL,
        source_id=None,
        **kwargs,
    )


async def test_jsonb_root_overlap_finds_out_of_window_pooled_line(db, tenant, masters):
    refs = [
        {"demand_type": "SALES_ORDER", "demand_id": str(new_id()), "demand_line_id": str(new_id())}
    ]
    incoming = await _source(db, tenant, masters, refs)
    first = await _source(db, tenant, masters, [])
    linked = await _source(db, tenant, masters, refs)
    order, line = await _candidate(db, tenant, masters, required_date=today() + timedelta(days=40))
    db.add_all(
        [
            PurchaseOrderSourceAllocation(
                company_id=tenant.company_id,
                purchase_order_line_id=line.id,
                requisition_line_id=first.id,
                quantity=Decimal("10"),
            ),
            PurchaseOrderSourceAllocation(
                company_id=tenant.company_id,
                purchase_order_line_id=line.id,
                requisition_line_id=linked.id,
                quantity=Decimal("6"),
            ),
        ]
    )
    await db.flush()
    matches = await _find(db, tenant, masters, incoming.id)
    assert len(matches) == 1
    assert matches[0].purchase_order_id == order.id
    assert matches[0].confidence is DuplicateConfidence.EXACT
    assert matches[0].ordered_quantity == Decimal("16")
    assert matches[0].purchase_order_version == order.version


async def test_inactive_mapping_overrides_historical_scalar_fallback(db, tenant, masters):
    refs = [{"demand_type": "SALES_ORDER", "demand_id": str(new_id())}]
    incoming = await _source(db, tenant, masters, refs)
    old = await _source(db, tenant, masters, refs)
    _, line = await _candidate(
        db, tenant, masters, required_date=today() + timedelta(days=40), source_id=old.id
    )
    db.add(
        PurchaseOrderSourceAllocation(
            company_id=tenant.company_id,
            purchase_order_line_id=line.id,
            requisition_line_id=old.id,
            quantity=Decimal("16"),
            is_active=False,
        )
    )
    await db.flush()
    assert await _find(db, tenant, masters, incoming.id) == []


async def test_legacy_scalar_root_can_match_missing_optional_line_key(db, tenant, masters):
    root_id = new_id()
    incoming = await _source(
        db,
        tenant,
        masters,
        [{"demand_type": "SALES_ORDER", "demand_id": str(root_id), "demand_line_id": None}],
    )
    old = await _source(
        db, tenant, masters, [{"demand_type": "SALES_ORDER", "demand_id": str(root_id)}]
    )
    _, line = await _candidate(
        db, tenant, masters, required_date=today() + timedelta(days=40), source_id=old.id
    )
    matches = await _find(db, tenant, masters, incoming.id)
    assert matches[0].purchase_order_line_id == line.id
    assert matches[0].confidence is DuplicateConfidence.EXACT


async def test_category_match_uses_qualified_ancestor_across_different_leaf_items(
    db, tenant, masters
):
    root_id, first_id, second_id = new_id(), new_id(), new_id()
    for category_id, code, parent_id, path in (
        (root_id, "ROOT", None, f"/{root_id}/"),
        (first_id, "FIRST", root_id, f"/{root_id}/{first_id}/"),
        (second_id, "SECOND", root_id, f"/{root_id}/{second_id}/"),
    ):
        db.add(
            ItemCategory(
                id=category_id,
                company_id=tenant.company_id,
                code=code,
                name=code,
                parent_id=parent_id,
                path=path,
            )
        )
    await db.flush()
    masters["item"].category_id = first_id
    other = await seed_item(
        db,
        company_id=tenant.company_id,
        code="SEARCH-OTHER",
        base_uom_id=masters["uom"].id,
        category_id=second_id,
    )
    db.add(
        VendorCategory(
            company_id=tenant.company_id, vendor_id=masters["vendor"].id, item_category_id=root_id
        )
    )
    _, line = await _candidate(db, tenant, masters, item_id=other.id)
    await db.flush()
    matches = await _find(db, tenant, masters)
    assert matches[0].purchase_order_line_id == line.id
    assert matches[0].confidence is DuplicateConfidence.POSSIBLE
    assert matches[0].item_category_id == root_id
    assert DuplicateAction.INCREASE_EXISTING_QUANTITY not in matches[0].available_actions


@pytest.mark.parametrize("receipt_status", [GoodsReceiptStatus.DRAFT, GoodsReceiptStatus.CANCELLED])
async def test_any_receipt_header_blocks_mutating_actions(db, tenant, masters, receipt_status):
    order, _ = await _candidate(db, tenant, masters)
    db.add(
        GoodsReceipt(
            company_id=tenant.company_id,
            code="SEARCH-GRN",
            purchase_order_id=order.id,
            vendor_id=order.vendor_id,
            warehouse_id=order.warehouse_id,
            receipt_date=today(),
            status=receipt_status,
            cancellation_reason="Historical cancellation"
            if receipt_status is GoodsReceiptStatus.CANCELLED
            else None,
        )
    )
    await db.flush()
    matches = await _find(db, tenant, masters)
    assert matches[0].has_receipt_history is True
    assert matches[0].received_quantity == 0
    assert DuplicateAction.MERGE_WITH_EXISTING not in matches[0].available_actions
    assert DuplicateAction.INCREASE_EXISTING_QUANTITY not in matches[0].available_actions


async def test_other_tenant_cannot_discover_candidate_order(db, tenant, other_tenant, masters):
    await _candidate(db, tenant, masters)
    # The foreign item itself is not found rather than confirming any order.
    from src.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await _find(db, other_tenant, masters)
