"""PO revisions and duplicate actions preserve physical lines and source demand."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select

from src.core.clock import today
from src.core.errors import (
    AuthorizationError,
    BusinessRuleError,
    ConflictError,
    InvalidStateTransitionError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from src.core.pagination import CursorParams
from src.modules.audit.models import AuditLog
from src.modules.procurement import purchase_order_changes as changes
from src.modules.procurement import service
from src.modules.procurement.models import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderSourceAllocation,
    PurchaseOrderStatus,
    PurchaseRequisition,
    PurchaseRequisitionLine,
    RequisitionLineStatus,
    RequisitionStatus,
)
from src.modules.procurement.schemas import (
    PurchaseOrderAmendLineIn,
    PurchaseOrderAmendRequest,
    PurchaseOrderCreate,
    PurchaseOrderDuplicateResolutionRequest,
    PurchaseOrderLineIn,
    RequisitionCreate,
    RequisitionLineIn,
)
from tests import seed_support
from tests.integration.test_procurement_service import _schema as _schema
from tests.integration.test_procurement_service import approver as approver
from tests.integration.test_procurement_service import buyer as buyer
from tests.integration.test_procurement_service import (
    buyer_without_approval as buyer_without_approval,
)
from tests.integration.test_procurement_service import db as db
from tests.integration.test_procurement_service import make_order, receive, send_order
from tests.integration.test_procurement_service import masters as masters
from tests.integration.test_procurement_service import other_tenant as other_tenant
from tests.integration.test_procurement_service import tenant as tenant

pytestmark = pytest.mark.integration


async def test_cancel_request_respects_the_requisition_warehouse(
    db, tenant, buyer, approver, masters
):
    header, lines = await approved_request(db, tenant, buyer, approver, masters, "10")
    source_id, header_id = lines[0].id, header.id
    other = await seed_support.seed_warehouse(db, company_id=tenant.company_id, code="OTHER-SCOPE")
    restricted = dataclasses.replace(tenant, allowed_warehouse_ids=(other.id,))
    request = PurchaseOrderDuplicateResolutionRequest(
        action="CANCEL_REQUEST",
        reason="Cancel selected duplicate",
        draft={
            "kind": "REQUISITION",
            "requisition_ids": [header_id],
            "requisition_line_ids": [source_id],
            "conversion": {},
        },
    )
    with pytest.raises(AuthorizationError):
        await changes.resolve_duplicate(db, restricted, buyer, request)
    assert (
        await fresh(db, PurchaseRequisitionLine, source_id)
    ).status != RequisitionLineStatus.CANCELLED


async def test_cancel_request_does_not_ignore_an_unknown_requisition(
    db, tenant, buyer, approver, masters
):
    header, lines = await approved_request(db, tenant, buyer, approver, masters, "10")
    source_id, header_id = lines[0].id, header.id
    request = PurchaseOrderDuplicateResolutionRequest(
        action="CANCEL_REQUEST",
        reason="Cancel selected duplicate",
        draft={
            "kind": "REQUISITION",
            "requisition_ids": [header_id, uuid.uuid4()],
            "requisition_line_ids": [source_id],
            "conversion": {},
        },
    )
    with pytest.raises(NotFoundError, match="Purchase requisition"):
        await changes.resolve_duplicate(db, tenant, buyer, request)
    assert (
        await fresh(db, PurchaseRequisitionLine, source_id)
    ).status != RequisitionLineStatus.CANCELLED


def manual_payload(masters: dict[str, Any], quantity: str = "25", **overrides: Any):
    values = {
        "vendor_id": masters["vendor"].id,
        "warehouse_id": masters["warehouse"].id,
        "expected_delivery_date": today() + timedelta(days=10),
        "lines": [
            PurchaseOrderLineIn(
                line_number=1,
                item_id=masters["item"].id,
                quantity=Decimal(quantity),
                unit_price=Decimal("50"),
            )
        ],
    }
    values.update(overrides)
    return PurchaseOrderCreate.model_validate(values)


def manual_resolution(order, draft, *, action="MERGE", line_id=None, version=None):
    return PurchaseOrderDuplicateResolutionRequest(
        action=action,
        candidate_purchase_order_id=order.id,
        candidate_line_id=line_id,
        expected_candidate_version=order.version if version is None else version,
        reason="Additional approved demand",
        draft={"kind": "MANUAL", "purchase_order": draft},
    )


async def fresh(db, model, identifier):
    return (
        await db.execute(
            select(model).where(model.id == identifier).execution_options(populate_existing=True)
        )
    ).scalar_one()


async def approved_request(db, tenant, buyer, approver, masters, *quantities):
    payload = RequisitionCreate(
        required_date=today() + timedelta(days=10),
        warehouse_id=masters["warehouse"].id,
        lines=[
            RequisitionLineIn(
                line_number=number,
                item_id=masters["item"].id,
                quantity=Decimal(quantity),
                suggested_vendor_id=masters["vendor"].id,
                estimated_unit_price=Decimal("50"),
            )
            for number, quantity in enumerate(quantities or ("20",), 1)
        ],
    )
    header, lines = await service.create_requisition(db, tenant, buyer, payload)
    for status in (RequisitionStatus.PENDING_APPROVAL, RequisitionStatus.APPROVED):
        await service.transition_requisition(
            db, tenant, approver, header.id, status, reason="Demand approved"
        )
    return header, lines


async def linked_order(db, tenant, buyer, masters, allocations, *, quantity=None):
    total = sum((Decimal(value) for _, value in allocations), Decimal("0"))
    payload = manual_payload(
        masters,
        lines=[
            PurchaseOrderLineIn(
                line_number=1,
                item_id=masters["item"].id,
                uom_id=masters["uom"].id,
                quantity=Decimal(quantity) if quantity else total,
                unit_price=Decimal("50"),
                source_allocations=[
                    {"requisition_line_id": identifier, "quantity": value}
                    for identifier, value in allocations
                ],
            )
        ],
    )
    return await service.create_purchase_order(db, tenant, buyer, payload)


@pytest.mark.parametrize("status", ["DRAFT", "PENDING_APPROVAL", "SENT"])
async def test_merge_retains_line_identity_and_requires_reapproval_after_send(
    db, tenant, buyer, masters, status, monkeypatch
):
    order, lines, _ = await make_order(db, tenant, buyer, masters)
    if status == "PENDING_APPROVAL":
        await service.transition_purchase_order(
            db, tenant, buyer, order.id, PurchaseOrderStatus.PENDING_APPROVAL, reason="Submit"
        )
    elif status == "SENT":
        await send_order(db, tenant, buyer, order)
    original_line_id = lines[0].id
    original_sent_at = order.sent_at
    called = []

    async def changed(row):
        called.append(row.id)

    monkeypatch.setattr(changes, "on_purchase_order_content_changed", changed)
    result = await changes.resolve_duplicate(
        db, tenant, buyer, manual_resolution(order, manual_payload(masters))
    )
    assert [row.id for row in result.purchase_orders] == [order.id]
    current = await service.get_purchase_order_detail(db, tenant, order.id)
    assert len(current.lines) == 1
    assert current.lines[0].id == original_line_id
    assert current.lines[0].quantity == Decimal("125")
    assert current.total_amount == Decimal("7375")
    assert called == [order.id]
    if status == "SENT":
        assert current.status == PurchaseOrderStatus.PENDING_APPROVAL
        assert current.amendment_number == 1
        assert current.last_amended_by_id == tenant.user_id
        assert current.sent_at == original_sent_at
        history, pagination = await changes.list_amendments(db, tenant, order.id, CursorParams())
        assert not pagination.has_next
        assert len(history) == 1
        assert history[0].old_values.lines[0].quantity == Decimal("100")
        assert history[0].new_values.lines[0].quantity == Decimal("125")
    else:
        assert current.status == status
        assert current.amendment_number == 0


async def test_increase_only_changes_the_named_matching_line(db, tenant, buyer, masters):
    order, lines, _ = await make_order(db, tenant, buyer, masters)
    result = await changes.resolve_duplicate(
        db,
        tenant,
        buyer,
        manual_resolution(
            order, manual_payload(masters, "5"), action="INCREASE", line_id=lines[0].id
        ),
    )
    assert result.action == "INCREASE"
    assert (await fresh(db, PurchaseOrderLine, lines[0].id)).quantity == Decimal("105")


@pytest.mark.parametrize("discount_form", ["flat", "percentage"])
async def test_merge_keeps_each_incoming_discount_value(db, tenant, buyer, masters, discount_form):
    original = manual_payload(masters, "100")
    incoming = manual_payload(masters, "25")
    if discount_form == "flat":
        original.lines[0].discount = Decimal("10")
        incoming.lines[0].discount = Decimal("10")
    else:
        original.lines[0].discount_percentage = Decimal("10")
        incoming.lines[0].discount_percentage = Decimal("10")
    order, _, _ = await service.create_purchase_order(db, tenant, buyer, original)
    await changes.resolve_duplicate(db, tenant, buyer, manual_resolution(order, incoming))
    detail = await service.get_purchase_order_detail(db, tenant, order.id)
    assert detail.lines[0].quantity == Decimal("125")
    if discount_form == "flat":
        assert detail.lines[0].discount == Decimal("20")
        assert detail.total_amount == Decimal("7351.40")
    else:
        assert detail.lines[0].discount_percentage == Decimal("10")
        assert detail.lines[0].discount == Decimal("625")
        assert detail.total_amount == Decimal("6637.50")


async def test_merge_preserves_additional_incoming_items(db, tenant, buyer, masters):
    order, _, _ = await make_order(db, tenant, buyer, masters)
    draft = manual_payload(masters)
    draft.lines.append(
        PurchaseOrderLineIn(
            line_number=2,
            item_id=masters["batched_item"].id,
            quantity=Decimal("3"),
            unit_price=Decimal("7"),
        )
    )
    await changes.resolve_duplicate(db, tenant, buyer, manual_resolution(order, draft))
    current = await service.get_purchase_order_detail(db, tenant, order.id)
    assert {line.item_id: line.quantity for line in current.lines} == {
        masters["item"].id: Decimal("125"),
        masters["batched_item"].id: Decimal("3"),
    }


async def test_increase_refuses_a_multi_item_request_without_discarding_anything(
    db, tenant, buyer, masters
):
    order, lines, _ = await make_order(db, tenant, buyer, masters)
    order_id, line_id = order.id, lines[0].id
    draft = manual_payload(masters)
    draft.lines.append(
        PurchaseOrderLineIn(
            line_number=2,
            item_id=masters["batched_item"].id,
            quantity=Decimal("3"),
            unit_price=Decimal("7"),
        )
    )
    with pytest.raises(ValidationError, match="Increase"):
        await changes.resolve_duplicate(
            db, tenant, buyer, manual_resolution(order, draft, action="INCREASE", line_id=line_id)
        )
    assert (await fresh(db, PurchaseOrderLine, line_id)).quantity == Decimal("100")
    assert (
        await db.scalar(
            select(func.count()).select_from(PurchaseOrder).where(PurchaseOrder.id == order_id)
        )
        == 1
    )


@pytest.mark.parametrize("difference", ["price", "qc", "discount", "date", "terms", "currency"])
async def test_incompatible_terms_refuse_atomically(db, tenant, buyer, masters, difference):
    order, lines, _ = await make_order(db, tenant, buyer, masters)
    order_id, line_id = order.id, lines[0].id
    draft = manual_payload(masters)
    if difference == "price":
        draft.lines[0].unit_price = Decimal("51")
    elif difference == "qc":
        draft.lines[0].requires_qc = True
    elif difference == "discount":
        draft.lines[0].discount_percentage = Decimal("2")
    elif difference == "date":
        draft.lines[0].required_date = today() + timedelta(days=11)
    elif difference == "terms":
        draft.terms = "New terms requiring another order"
    else:
        draft.currency = "USD"
    with pytest.raises(ValidationError, match="terms"):
        await changes.resolve_duplicate(db, tenant, buyer, manual_resolution(order, draft))
    assert (await fresh(db, PurchaseOrderLine, line_id)).quantity == Decimal("100")
    current = await fresh(db, PurchaseOrder, order_id)
    assert current.total_amount == Decimal("5900")
    assert current.amendment_number == 0


async def test_stale_candidate_version_refuses_before_mutation(db, tenant, buyer, masters):
    order, lines, _ = await make_order(db, tenant, buyer, masters)
    line_id = lines[0].id
    with pytest.raises(ConflictError):
        await changes.resolve_duplicate(
            db,
            tenant,
            buyer,
            manual_resolution(order, manual_payload(masters), version=order.version + 1),
        )
    assert (await fresh(db, PurchaseOrderLine, line_id)).quantity == Decimal("100")


@pytest.mark.parametrize("cancelled", [False, True])
async def test_any_receipt_history_blocks_merge_and_amend_even_with_zero_received(
    db, tenant, buyer, masters, cancelled
):
    order, lines, _ = await make_order(db, tenant, buyer, masters)
    await send_order(db, tenant, buyer, order)
    receipt, _ = await receive(db, tenant, buyer, order, lines[0], received="1", post=False)
    if cancelled:
        await service.cancel_goods_receipt(db, tenant, receipt.id, reason="Delivery withdrawn")
    order_id, line_id = order.id, lines[0].id
    version = order.version
    with pytest.raises(BusinessRuleError, match="receipt history"):
        await changes.amend_purchase_order(
            db,
            tenant,
            order_id,
            PurchaseOrderAmendRequest(
                expected_version=version,
                reason="Need additional quantity",
                lines=[PurchaseOrderAmendLineIn(line_id=line_id, quantity=Decimal("101"))],
            ),
        )
    current = await fresh(db, PurchaseOrder, order_id)
    with pytest.raises(BusinessRuleError, match="receipt history"):
        await changes.resolve_duplicate(
            db, tenant, buyer, manual_resolution(current, manual_payload(masters))
        )
    assert (await fresh(db, PurchaseOrderLine, line_id)).quantity == Decimal("100")


async def test_submit_cannot_use_amendment_edges_to_bypass_revision_flow(
    db, tenant, buyer, masters
):
    order, _, _ = await make_order(db, tenant, buyer, masters)
    await send_order(db, tenant, buyer, order)
    order_id = order.id
    with pytest.raises(BusinessRuleError, match="Use Amend"):
        await service.transition_purchase_order(
            db,
            tenant,
            buyer,
            order_id,
            PurchaseOrderStatus.PENDING_APPROVAL,
            reason="Bypass amendment",
        )
    assert (await fresh(db, PurchaseOrder, order_id)).amendment_number == 0


async def test_pooled_allocations_drive_counters_and_explicit_reductions(
    db, tenant, buyer, approver, masters
):
    _, sources = await approved_request(db, tenant, buyer, approver, masters, "20", "20")
    source_ids = [line.id for line in sources]
    order, lines, _ = await linked_order(
        db, tenant, buyer, masters, [(source_ids[0], "10"), (source_ids[1], "5")]
    )
    assert lines[0].requisition_line_id is None
    assert (await fresh(db, PurchaseRequisitionLine, source_ids[0])).ordered_quantity == Decimal(
        "10"
    )
    assert (await fresh(db, PurchaseRequisitionLine, source_ids[1])).ordered_quantity == Decimal(
        "5"
    )
    await send_order(db, tenant, buyer, order)
    order_id, line_id, version = order.id, lines[0].id, order.version
    with pytest.raises(ValidationError, match="explicit source_allocations"):
        await changes.amend_purchase_order(
            db,
            tenant,
            order_id,
            PurchaseOrderAmendRequest(
                expected_version=version,
                reason="Demand reduced",
                lines=[PurchaseOrderAmendLineIn(line_id=line_id, quantity=Decimal("8"))],
            ),
        )
    order = await fresh(db, PurchaseOrder, order_id)
    await changes.amend_purchase_order(
        db,
        tenant,
        order_id,
        PurchaseOrderAmendRequest(
            expected_version=order.version,
            reason="Demand reduced by agreement",
            lines=[
                PurchaseOrderAmendLineIn(
                    line_id=line_id,
                    quantity=Decimal("8"),
                    source_allocations=[
                        {"requisition_line_id": source_ids[0], "quantity": "5"},
                        {"requisition_line_id": source_ids[1], "quantity": "3"},
                    ],
                )
            ],
        ),
    )
    assert (await fresh(db, PurchaseRequisitionLine, source_ids[0])).ordered_quantity == Decimal(
        "5"
    )
    assert (await fresh(db, PurchaseRequisitionLine, source_ids[1])).ordered_quantity == Decimal(
        "3"
    )
    assert (await fresh(db, PurchaseOrderLine, line_id)).quantity == Decimal("8")


async def test_release_does_not_reopen_cancelled_outstanding_requisition_demand(
    db, tenant, buyer, approver, masters
):
    header, sources = await approved_request(db, tenant, buyer, approver, masters, "20")
    header_id, source_id = header.id, sources[0].id
    order, lines, _ = await linked_order(db, tenant, buyer, masters, [(source_id, "10")])
    order_id, line_id = order.id, lines[0].id
    await changes.resolve_duplicate(
        db,
        tenant,
        buyer,
        PurchaseOrderDuplicateResolutionRequest(
            action="CANCEL_REQUEST",
            reason="Remaining demand is redundant",
            draft={
                "kind": "REQUISITION",
                "requisition_ids": [header_id],
                "requisition_line_ids": [source_id],
                "conversion": {},
            },
        ),
    )
    await service.transition_purchase_order(
        db, tenant, buyer, order_id, PurchaseOrderStatus.CANCELLED, reason="Vendor order withdrawn"
    )
    source = await fresh(db, PurchaseRequisitionLine, source_id)
    assert source.ordered_quantity == Decimal("0")
    assert source.status == RequisitionLineStatus.CANCELLED
    assert (await fresh(db, PurchaseRequisition, header_id)).status == RequisitionStatus.CONVERTED
    mappings = list(
        await db.scalars(
            select(PurchaseOrderSourceAllocation).where(
                PurchaseOrderSourceAllocation.purchase_order_line_id == line_id
            )
        )
    )
    assert len(mappings) == 1
    assert mappings[0].quantity == Decimal("10")
    assert mappings[0].is_active is False


async def test_separate_duplicate_proposal_needs_confirmation_at_approval(
    db, tenant, buyer, buyer_without_approval, approver, masters
):
    await make_order(db, tenant, buyer, masters)
    result = await changes.resolve_duplicate(
        db,
        tenant,
        buyer_without_approval,
        PurchaseOrderDuplicateResolutionRequest(
            action="CREATE_WITH_APPROVAL",
            reason="Distinct rush-job demand accepted for review",
            draft={"kind": "MANUAL", "purchase_order": manual_payload(masters)},
        ),
    )
    order_id = result.purchase_orders[0].id
    current = await fresh(db, PurchaseOrder, order_id)
    assert current.duplicate_acknowledged
    assert current.duplicate_confirmed_at is None
    await service.transition_purchase_order(
        db,
        tenant,
        buyer,
        order_id,
        PurchaseOrderStatus.PENDING_APPROVAL,
        reason="Submit duplicate for approval",
    )
    with pytest.raises(BusinessRuleError, match="Confirm the duplicate"):
        await service.transition_purchase_order(
            db, tenant, approver, order_id, PurchaseOrderStatus.APPROVED, reason="Approved"
        )
    approved = await service.transition_purchase_order(
        db,
        tenant,
        approver,
        order_id,
        PurchaseOrderStatus.APPROVED,
        reason="Separate physical demand confirmed",
        confirm_duplicate=True,
    )
    assert approved.duplicate_confirmed_by_id == approver.user_id
    assert approved.duplicate_confirmation_reason == "Separate physical demand confirmed"


async def test_amendment_author_and_original_creator_cannot_self_approve(
    db, tenant, buyer, approver, masters
):
    order, lines, _ = await make_order(db, tenant, buyer, masters)
    await send_order(db, tenant, buyer, order)
    order_id, line_id = order.id, lines[0].id
    amender = dataclasses.replace(tenant, user_id=approver.user_id)
    revised, _ = await changes.amend_purchase_order(
        db,
        amender,
        order_id,
        PurchaseOrderAmendRequest(
            expected_version=order.version,
            reason="Amended by the second operator",
            lines=[PurchaseOrderAmendLineIn(line_id=line_id, quantity=Decimal("110"))],
        ),
    )
    assert revised.last_amended_by_id == approver.user_id
    for principal in (buyer, approver):
        with pytest.raises(AuthorizationError):
            await service.transition_purchase_order(
                db,
                tenant,
                principal,
                order_id,
                PurchaseOrderStatus.APPROVED,
                reason="Attempted own approval",
            )
    elevated = dataclasses.replace(approver, is_superuser=True)
    approved = await service.transition_purchase_order(
        db,
        tenant,
        elevated,
        order_id,
        PurchaseOrderStatus.APPROVED,
        reason="Explicit superuser exception",
    )
    assert approved.status == PurchaseOrderStatus.APPROVED


async def test_only_selected_conversion_demand_is_merged(db, tenant, buyer, approver, masters):
    header, lines = await approved_request(db, tenant, buyer, approver, masters, "10", "4")
    header_id, selected_id, unselected_id = header.id, lines[0].id, lines[1].id
    order, _, _ = await make_order(db, tenant, buyer, masters, quantity="3")
    await changes.resolve_duplicate(
        db,
        tenant,
        buyer,
        PurchaseOrderDuplicateResolutionRequest(
            action="MERGE",
            candidate_purchase_order_id=order.id,
            expected_candidate_version=order.version,
            reason="Group this selected requirement",
            draft={
                "kind": "REQUISITION",
                "requisition_ids": [header_id],
                "requisition_line_ids": [selected_id],
                "conversion": {},
            },
        ),
    )
    detail = await service.get_purchase_order_detail(db, tenant, order.id)
    assert sum((line.quantity for line in detail.lines), Decimal("0")) == Decimal("13")
    assert (await fresh(db, PurchaseRequisitionLine, selected_id)).ordered_quantity == Decimal("10")
    unselected = await fresh(db, PurchaseRequisitionLine, unselected_id)
    assert unselected.ordered_quantity == Decimal("0")
    assert unselected.status == RequisitionLineStatus.PENDING


async def test_cancelling_all_untouched_demand_cancels_header_with_reason(
    db, tenant, buyer, approver, masters
):
    header, lines = await approved_request(db, tenant, buyer, approver, masters)
    header_id, source_id = header.id, lines[0].id
    result = await changes.resolve_duplicate(
        db,
        tenant,
        buyer,
        PurchaseOrderDuplicateResolutionRequest(
            action="CANCEL_REQUEST",
            reason="Duplicate demand cancelled",
            draft={
                "kind": "REQUISITION",
                "requisition_ids": [header_id],
                "requisition_line_ids": [source_id],
                "conversion": {},
            },
        ),
    )
    assert result.cancelled_requisition_line_ids == [source_id]
    current = await fresh(db, PurchaseRequisition, header_id)
    assert current.status == RequisitionStatus.CANCELLED
    assert current.cancellation_reason == "Duplicate demand cancelled"


async def test_manual_discard_is_side_effect_free(db, tenant, buyer, masters):
    count = await db.scalar(select(func.count()).select_from(PurchaseOrder))
    audits = await db.scalar(select(func.count()).select_from(AuditLog))
    result = await changes.resolve_duplicate(
        db,
        tenant,
        buyer,
        PurchaseOrderDuplicateResolutionRequest(
            action="CANCEL_REQUEST",
            reason="The draft is redundant",
            draft={"kind": "MANUAL", "purchase_order": manual_payload(masters)},
        ),
    )
    assert result.discarded
    assert not result.purchase_orders
    assert await db.scalar(select(func.count()).select_from(PurchaseOrder)) == count
    assert await db.scalar(select(func.count()).select_from(AuditLog)) == audits


async def test_action_permission_is_checked_in_the_service(db, tenant, buyer, masters):
    restricted = dataclasses.replace(buyer, permissions=frozenset())
    with pytest.raises(PermissionDeniedError):
        await changes.resolve_duplicate(
            db,
            tenant,
            restricted,
            PurchaseOrderDuplicateResolutionRequest(
                action="CANCEL_REQUEST",
                reason="Discard duplicate draft",
                draft={"kind": "MANUAL", "purchase_order": manual_payload(masters)},
            ),
        )


async def test_foreign_source_allocations_are_not_found(db, tenant, buyer, masters, other_tenant):
    foreign_uom = await seed_support.seed_uom(db, company_id=other_tenant.company_id)
    foreign_item = await seed_support.seed_item(
        db,
        company_id=other_tenant.company_id,
        code=f"FOREIGN-{uuid.uuid4().hex[:8]}",
        base_uom_id=foreign_uom.id,
        can_purchase=True,
    )
    foreign_warehouse = await seed_support.seed_warehouse(
        db, company_id=other_tenant.company_id, code=f"FOREIGN-{uuid.uuid4().hex[:8]}"
    )
    header = PurchaseRequisition(
        company_id=other_tenant.company_id,
        code=f"FOREIGN-{uuid.uuid4().hex}",
        requisition_date=today(),
        required_date=today(),
        warehouse_id=foreign_warehouse.id,
    )
    db.add(header)
    await db.flush()
    line = PurchaseRequisitionLine(
        company_id=other_tenant.company_id,
        requisition_id=header.id,
        line_number=1,
        item_id=foreign_item.id,
        quantity=Decimal("10"),
        uom_id=foreign_uom.id,
    )
    db.add(line)
    await db.flush()
    identifier = line.id
    with pytest.raises(NotFoundError, match="Purchase requisition line"):
        await linked_order(db, tenant, buyer, masters, [(identifier, "5")])


@pytest.mark.parametrize("status", [PurchaseOrderStatus.CANCELLED, PurchaseOrderStatus.REJECTED])
async def test_releasing_a_pooled_order_releases_each_source_once(
    db, tenant, buyer, approver, masters, status
):
    _, sources = await approved_request(db, tenant, buyer, approver, masters, "20", "20")
    identifiers = [line.id for line in sources]
    order, _, _ = await linked_order(
        db, tenant, buyer, masters, [(identifiers[0], "10"), (identifiers[1], "5")]
    )
    order_id = order.id
    if status == PurchaseOrderStatus.REJECTED:
        await service.transition_purchase_order(
            db, tenant, buyer, order_id, PurchaseOrderStatus.PENDING_APPROVAL, reason="Submit"
        )
    await service.transition_purchase_order(
        db, tenant, approver, order_id, status, reason="Order withdrawn after review"
    )
    for identifier in identifiers:
        assert (await fresh(db, PurchaseRequisitionLine, identifier)).ordered_quantity == Decimal(
            "0"
        )
    with pytest.raises(InvalidStateTransitionError):
        await service.transition_purchase_order(
            db, tenant, approver, order_id, status, reason="Repeated withdrawal refused"
        )
    for identifier in identifiers:
        assert (await fresh(db, PurchaseRequisitionLine, identifier)).ordered_quantity == Decimal(
            "0"
        )
