"""Atomic duplicate resolutions and revisions of approved purchase orders.

The physical PO line and its requisition allocations are separate facts. An
amendment preserves retained line IDs, adjusts explicit source quantities, and
records the full before/after terms before requiring another approval.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from src.common.warehouse_scope import assert_warehouse_scope
from src.core.clock import utcnow
from src.core.database import advisory_xact_lock, transaction
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    AuthorizationError,
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from src.core.pagination import CursorParams
from src.core.permissions import Permission
from src.core.responses import CursorPagination
from src.core.types import ZERO, to_quantity
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit
from src.modules.notifications.models import NotificationType
from src.modules.notifications.service import notify
from src.modules.procurement import purchase_order_sources as sources
from src.modules.procurement import service
from src.modules.procurement.inbound_service import buyer_recipients
from src.modules.procurement.models import (
    EDITABLE_PO_STATUSES,
    OPEN_REQUISITION_STATUSES,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    RequisitionLineStatus,
    RequisitionStatus,
)
from src.modules.procurement.purchase_order_changes_repository import (
    SourceAllocationRepository,
    amendment_history,
    has_receipt_history,
)
from src.modules.procurement.repository import (
    PurchaseOrderLineRepository,
    PurchaseOrderRepository,
    RequisitionLineRepository,
    RequisitionRepository,
)
from src.modules.procurement.schemas import (
    MAX_LINES_PER_DOCUMENT,
    PurchaseOrderAmendLineIn,
    PurchaseOrderAmendmentRead,
    PurchaseOrderAmendmentValues,
    PurchaseOrderAmendRequest,
    PurchaseOrderCreate,
    PurchaseOrderDuplicateResolutionRequest,
    PurchaseOrderDuplicateResolutionResult,
    PurchaseOrderLineIn,
    PurchaseOrderSummary,
    RequisitionConversionLineOverride,
)

AMENDABLE_STATUSES = frozenset(
    {PurchaseOrderStatus.APPROVED, PurchaseOrderStatus.SENT, PurchaseOrderStatus.ACKNOWLEDGED}
)


def _assert_internal(tenant: TenantContext) -> None:
    if tenant.vendor_id is not None or tenant.customer_id is not None:
        raise AuthorizationError(
            "Purchase-order revisions and duplicate actions are for internal purchasing users."
        )


async def on_purchase_order_content_changed(order: PurchaseOrder) -> None:
    """D4 seam: a future signer must supersede a signature after this event."""


def _line_payload(line: PurchaseOrderLine) -> PurchaseOrderLineIn:
    return PurchaseOrderLineIn(
        line_number=line.line_number,
        item_id=line.item_id,
        description=line.description,
        quantity=line.quantity,
        uom_id=line.uom_id,
        unit_price=line.unit_price,
        discount_percentage=line.discount_percentage if line.discount_percentage else None,
        discount=None if line.discount_percentage else line.discount,
        required_date=line.required_date,
        requires_qc=line.requires_qc,
        quotation_line_id=line.quotation_line_id,
        notes=line.notes,
    )


async def _values(
    session: AsyncSession,
    tenant: TenantContext,
    order: PurchaseOrder,
    lines: Sequence[PurchaseOrderLine],
) -> dict[str, Any]:
    detail = await service.describe_purchase_order(session, tenant, order, lines)
    return PurchaseOrderAmendmentValues(
        status=order.status,
        expected_delivery_date=order.expected_delivery_date,
        freight_amount=order.freight_amount,
        terms=order.terms,
        delivery_address=order.delivery_address,
        amendment_number=order.amendment_number,
        lines=[line.model_dump() for line in detail.lines],
    ).model_dump(mode="json")


async def _apply_lines(
    session: AsyncSession,
    tenant: TenantContext,
    order: PurchaseOrder,
    incoming: Sequence[PurchaseOrderAmendLineIn],
    outgoing: Sequence[PurchaseOrderLine],
) -> list[PurchaseOrderLine]:
    by_id = {line.id: line for line in outgoing}
    old_allocations = await sources.effective_allocations(session, tenant, outgoing)
    payloads: list[PurchaseOrderLineIn] = []
    desired: list[dict[uuid.UUID, Decimal]] = []
    for index, incoming_line in enumerate(incoming, 1):
        old = by_id.get(incoming_line.line_id) if incoming_line.line_id else None
        if incoming_line.line_id is not None and old is None:
            raise NotFoundError("Purchase order line", incoming_line.line_id)
        if old is not None and incoming_line.item_id not in (None, old.item_id):
            raise ValidationError(
                "An existing PO line cannot change its item. Remove it and add a new line."
            )
        values = _line_payload(old).model_dump() if old else {}
        values.update(
            incoming_line.model_dump(exclude_unset=True, exclude={"line_id", "source_allocations"})
        )
        values["line_number"] = incoming_line.line_number or index
        if incoming_line.source_allocations is not None:
            allocations = {
                allocation.requisition_line_id: allocation.quantity
                for allocation in incoming_line.source_allocations
            }
        else:
            allocations = old_allocations.get(old.id, {}) if old else {}
        if sum(allocations.values(), ZERO) > incoming_line.quantity:
            raise ValidationError(
                "This reduction needs explicit source_allocations showing which "
                "requisition quantities to retain."
            )
        # Changing discount form must clear the other form inherited from the
        # old line. Omitted fields otherwise retain the agreed commercial terms.
        if "discount_percentage" in incoming_line.model_fields_set:
            values["discount"] = None
        elif "discount" in incoming_line.model_fields_set:
            values["discount_percentage"] = None
        values["source_allocations"] = None
        values["requisition_line_id"] = None
        payloads.append(PurchaseOrderLineIn.model_validate(values))
        desired.append(allocations)
    numbers = [line.line_number for line in payloads]
    if len(set(numbers)) != len(numbers):
        raise ValidationError("Each purchase order line number must be unique.")
    items = await service._load_items(session, tenant, [line.item_id for line in payloads])
    service._assert_purchasable(items)
    existing_ids = {source_id for links in old_allocations.values() for source_id in links}
    incoming_ids = {source_id for links in desired for source_id in links}
    requisitions, source_lines = await service._lock_requisition_sources(
        session, tenant, sorted(existing_ids | incoming_ids)
    )
    linked_payloads = [
        payload.model_copy(update={"requisition_line_id": source_id, "quantity": quantity})
        for payload, allocations in zip(payloads, desired, strict=True)
        for source_id, quantity in allocations.items()
    ]
    service._assert_requisition_links(
        linked_payloads, requisitions, source_lines, already_linked=frozenset(existing_ids)
    )
    sources.assert_source_units(
        linked_payloads, source_lines, {key: item.order_uom_id for key, item in items.items()}
    )
    await service._assert_quotation_links(
        session, tenant, vendor_id=order.vendor_id, lines=payloads
    )
    category, _ = await service._order_category(
        session,
        tenant,
        vendor_id=order.vendor_id,
        items=items,
        item_ids=[line.item_id for line in payloads],
    )
    if category != order.item_category_id:
        raise BusinessRuleError(
            "An amendment must keep the purchase order's procurement category. "
            "Raise a separate order for another category."
        )
    # Two flushes avoid the immediate line-number uniqueness constraint during
    # reorder/removal. No caller observes these temporary positive numbers.
    for old in outgoing:
        old.line_number += 1_000_000
    await session.flush()
    # Retained lines keep the tax rate the vendor was actually sent. Updating
    # an unrelated item master must not rewrite their frozen commercial terms.
    line_items = [
        replace(items[payload.item_id], gst_rate=by_id[change.line_id].tax_rate)
        if change.line_id
        else items[payload.item_id]
        for change, payload in zip(incoming, payloads, strict=True)
    ]
    amounts = [
        service.compute_line_amounts(
            quantity=payload.quantity,
            unit_price=payload.unit_price,
            tax_rate=item.gst_rate,
            discount_percentage=payload.discount_percentage,
            discount_amount=payload.discount,
        )
        for payload, item in zip(payloads, line_items, strict=True)
    ]
    repo = PurchaseOrderLineRepository(session, tenant)
    lines: list[PurchaseOrderLine] = []
    allocations_by_line: dict[uuid.UUID, dict[uuid.UUID, Decimal]] = {
        line.id: {} for line in outgoing
    }
    for change, payload, amount, item in zip(incoming, payloads, amounts, line_items, strict=True):
        line = (
            by_id[change.line_id]
            if change.line_id
            else repo.add(PurchaseOrderLine(purchase_order_id=order.id))
        )
        service._apply_order_line(line, payload, item, amount)
        repo.touch(line)
        lines.append(line)
    await session.flush()
    allocations_by_line.update(
        {line.id: allocation for line, allocation in zip(lines, desired, strict=True)}
    )
    every = {line.id: line for line in [*outgoing, *lines]}
    await sources.sync_allocations(session, tenant, list(every.values()), allocations_by_line)
    removed_ids = by_id.keys() - {line.id for line in lines}
    if removed_ids:
        await session.execute(
            delete(PurchaseOrderLine).where(
                PurchaseOrderLine.company_id == tenant.company_id,
                PurchaseOrderLine.id.in_(removed_ids),
            )
        )
    service._apply_order_totals(order, amounts, order.freight_amount)
    await service._link_source_documents(session, tenant, order, lines)
    return lines


async def _revise_locked(
    session: AsyncSession,
    tenant: TenantContext,
    order: PurchaseOrder,
    payload: PurchaseOrderAmendRequest,
    *,
    allow_draft: bool = False,
) -> tuple[PurchaseOrder, list[PurchaseOrderLine]]:
    status = PurchaseOrderStatus(order.status)
    if status not in AMENDABLE_STATUSES and not (allow_draft and status in EDITABLE_PO_STATUSES):
        raise BusinessRuleError(f"{order.code} cannot be amended while it is {status}.")
    if await has_receipt_history(session, tenant, order.id):
        raise BusinessRuleError(f"{order.code} has receipt history; raise a new purchase order.")
    outgoing = await PurchaseOrderLineRepository(session, tenant).lock_for_order(order.id)
    if any(line.received_quantity for line in outgoing):
        raise BusinessRuleError(f"{order.code} has receipts; raise a new purchase order.")
    before = await _values(session, tenant, order, outgoing)
    for name in ("expected_delivery_date", "freight_amount", "terms", "delivery_address"):
        if name in payload.model_fields_set:
            setattr(order, name, getattr(payload, name))
    if order.freight_amount is None:
        raise ValidationError("Freight cannot be null.")
    if order.expected_delivery_date is not None and order.expected_delivery_date < order.po_date:
        raise ValidationError("The expected delivery date cannot be before the order date.")
    lines = await _apply_lines(session, tenant, order, payload.lines, outgoing)
    if status in AMENDABLE_STATUSES:
        order.amendment_number += 1
        order.last_amended_at = utcnow()
        order.last_amended_by_id = tenant.user_id
        order.acknowledged_at = None
        order.vendor_committed_date = None
        order.approved_at = None
        order.approved_by_id = None
        order.duplicate_confirmed_at = None
        order.duplicate_confirmed_by_id = None
        order.duplicate_confirmation_reason = None
        await service._transition(
            session,
            tenant,
            machine=service.PURCHASE_ORDER_LIFECYCLE,
            entity=order,
            entity_type=service.AUDIT_PURCHASE_ORDER,
            entity_code=order.code,
            target=PurchaseOrderStatus.PENDING_APPROVAL,
            reason=payload.reason,
            status_enum=PurchaseOrderStatus,
        )
    service._touch_order_content(session, tenant, order)
    await session.flush()
    after = await _values(session, tenant, order, lines)
    await record_audit(
        session,
        company_id=tenant.company_id,
        action=AuditAction.UPDATE,
        entity_type=service.AUDIT_PURCHASE_ORDER,
        entity_id=order.id,
        entity_code=order.code,
        user_id=tenant.user_id,
        summary=f"Revised {order.code}: {payload.reason}",
        old_values=before,
        new_values=after,
        context={
            "event": "purchase_order_amendment"
            if status in AMENDABLE_STATUSES
            else "purchase_order_duplicate_resolution",
            "reason": payload.reason,
            "from_status": status,
            "sent_at": order.sent_at,
            "before": before,
            "after": after,
        },
    )
    await on_purchase_order_content_changed(order)
    if status in AMENDABLE_STATUSES:
        if order.sent_at is not None:
            await service.notify_vendor_about_order(
                session,
                tenant,
                order,
                permission=Permission.VENDOR_PORTAL_ACCESS,
                notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
                title=f"{order.code} amended (rev {order.amendment_number})",
                body=(
                    "The order is being reviewed. Revised terms will be available "
                    "after approval and resend."
                ),
                entity_type=service.AUDIT_PURCHASE_ORDER,
                entity_id=order.id,
                action_url=service.vendor_order_url(order.id),
            )
        await notify(
            session,
            company_id=tenant.company_id,
            user_ids=await buyer_recipients(session, tenant, order),
            notification_type=NotificationType.APPROVAL_PENDING,
            title=f"Review {order.code} revision {order.amendment_number}",
            body=payload.reason,
            entity_type=service.AUDIT_PURCHASE_ORDER,
            entity_id=order.id,
            action_url=f"/procurement/purchase-orders/{order.id}",
            actor_id=tenant.user_id,
        )
    return order, lines


async def amend_purchase_order(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    payload: PurchaseOrderAmendRequest,
) -> tuple[PurchaseOrder, list[PurchaseOrderLine]]:
    _assert_internal(tenant)
    incoming_ids = sorted(
        {
            allocation.requisition_line_id
            for line in payload.lines
            for allocation in line.source_allocations or ()
        }
    )
    async with transaction(session):
        order = await sources.lock_order_sources(
            session,
            tenant,
            order_id,
            incoming_ids=incoming_ids,
            expected_version=payload.expected_version,
        )
        return await _revise_locked(session, tenant, order, payload)


async def list_amendments(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID, params: CursorParams
) -> tuple[list[PurchaseOrderAmendmentRead], CursorPagination]:
    _assert_internal(tenant)
    order = await PurchaseOrderRepository(session, tenant).get_or_404(order_id)
    assert_warehouse_scope(tenant, order.warehouse_id)
    rows, pagination = await amendment_history(session, tenant, order_id, params)
    return [
        PurchaseOrderAmendmentRead(
            id=row.id,
            amendment_number=(row.new_values or {})["amendment_number"],
            amended_at=row.created_at,
            amended_by_id=row.created_by_id,
            amended_by_name=row.actor_label,
            reason=(row.context or {})["reason"],
            from_status=(row.context or {})["from_status"],
            old_values=(row.context or {})["before"],
            new_values=(row.context or {})["after"],
        )
        for row in rows
    ], pagination


def _assert_terms(
    order: PurchaseOrder, incoming: PurchaseOrderCreate, vendor_payment_terms: int
) -> None:
    fields = {
        "vendor": (order.vendor_id, incoming.vendor_id),
        "warehouse": (order.warehouse_id, incoming.warehouse_id),
        "currency": (order.currency, incoming.currency),
        "payment terms": (
            order.payment_terms_days,
            incoming.payment_terms_days
            if incoming.payment_terms_days is not None
            else vendor_payment_terms,
        ),
        "terms": (order.terms, incoming.terms),
        "delivery address": (order.delivery_address, incoming.delivery_address),
    }
    fields["priority"] = (order.priority, incoming.priority)
    different = [name for name, (old, new) in fields.items() if old != new]
    for name in (
        "expected_delivery_date",
        "branch_id",
        "vendor_reference",
        "notes",
        "assigned_vendor_user_id",
    ):
        value = getattr(incoming, name)
        if value is not None and value != getattr(order, name):
            different.append(name.replace("_", " "))
    if incoming.freight_amount:
        different.append("additional freight")
    if different:
        raise ValidationError(
            "These order terms differ: " + ", ".join(different) + ". Use a separate purchase order."
        )


def _same_line_terms(
    existing: PurchaseOrderLine,
    incoming: PurchaseOrderLineIn,
    incoming_tax: Decimal,
    order: PurchaseOrder,
    default_uom_id: uuid.UUID,
) -> bool:
    old = _line_payload(existing)
    return all(
        (
            old.item_id == incoming.item_id,
            old.uom_id == (incoming.uom_id or default_uom_id),
            old.unit_price == incoming.unit_price,
            (old.discount_percentage or ZERO) == (incoming.discount_percentage or ZERO),
            (old.discount or ZERO) == (incoming.discount or ZERO),
            existing.tax_rate == incoming_tax,
            old.requires_qc == incoming.requires_qc,
            old.quotation_line_id == incoming.quotation_line_id,
            (old.required_date or order.expected_delivery_date)
            == (incoming.required_date or order.expected_delivery_date),
            old.notes == incoming.notes,
            incoming.description in (None, old.description),
        )
    )


async def resolve_duplicate(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    payload: PurchaseOrderDuplicateResolutionRequest,
) -> PurchaseOrderDuplicateResolutionResult:
    _assert_internal(tenant)
    permission = {
        "MERGE": Permission.PURCHASE_ORDER_UPDATE,
        "INCREASE": Permission.PURCHASE_ORDER_UPDATE,
        "CREATE_WITH_APPROVAL": Permission.PURCHASE_ORDER_CREATE,
        "CANCEL_REQUEST": Permission.PURCHASE_REQUISITION_CREATE,
    }[payload.action]
    if not principal.has(permission):
        raise PermissionDeniedError(str(permission))
    async with transaction(session):
        candidate = None
        candidate_source_ids: frozenset[uuid.UUID] = frozenset()
        observed_version = None
        if payload.candidate_purchase_order_id is not None:
            candidate = await PurchaseOrderRepository(session, tenant).get_or_404(
                payload.candidate_purchase_order_id
            )
            observed_version = candidate.version
            assert_warehouse_scope(tenant, candidate.warehouse_id)
            candidate_source_ids = await SourceAllocationRepository(
                session, tenant
            ).source_ids_for_order(candidate.id)
        plan = None
        if payload.draft.kind == "REQUISITION":
            draft = payload.draft
            line_repo = RequisitionLineRepository(session, tenant)
            grouped = await line_repo.list_for_requisitions(draft.requisition_ids)
            all_lines = [line for lines in grouped.values() for line in lines]
            selected = set(draft.requisition_line_ids)
            if selected - {line.id for line in all_lines}:
                raise ValidationError(
                    "Selected demand must belong to the requisitions in this request."
                )
            parent_ids = await line_repo.requisition_ids_of(sorted(candidate_source_ids))
            await RequisitionRepository(session, tenant).lock_many(
                sorted(set(draft.requisition_ids) | set(parent_ids))
            )
            await line_repo.lock_many(
                sorted(candidate_source_ids | {line.id for line in all_lines})
            )
            if payload.action == "CANCEL_REQUEST":
                return await _cancel_demand(
                    session, tenant, payload, draft.requisition_ids, selected
                )
            overrides = {line.requisition_line_id: line for line in draft.conversion.lines}
            for demand_line in all_lines:
                if demand_line.id not in selected:
                    overrides[demand_line.id] = RequisitionConversionLineOverride(
                        requisition_line_id=demand_line.id, exclude=True
                    )
            conversion = draft.conversion.model_copy(update={"lines": list(overrides.values())})
            plan = await service._plan_conversion(
                session, tenant, draft.requisition_ids, conversion, lock=True
            )
            planned_ids = {
                line.requisition_line_id for group in plan.groups for line in group.lines
            }
            if plan.unresolved or planned_ids != selected:
                raise BusinessRuleError(
                    "The selected demand has changed or cannot be resolved. "
                    "Refresh the conversion preview."
                )
            drafts = [
                service._conversion_order_payload(
                    group, conversion, po_date=plan.po_date, assignee_id=plan.assignees[index][0]
                )
                for index, group in enumerate(plan.groups)
            ]
        else:
            if payload.action == "CANCEL_REQUEST":
                return PurchaseOrderDuplicateResolutionResult(action=payload.action, discarded=True)
            incoming = payload.draft.purchase_order
            incoming_ids = set(sources.source_ids(incoming.lines))
            await service._lock_requisition_sources(
                session, tenant, sorted(candidate_source_ids | incoming_ids)
            )
            drafts = [incoming]
        keys = {(draft.vendor_id, draft.warehouse_id) for draft in drafts}
        if candidate is not None:
            keys.add((candidate.vendor_id, candidate.warehouse_id))
        for vendor_id, warehouse_id in sorted(keys):
            await advisory_xact_lock(
                session, tenant.company_id, "po-duplicate", vendor_id, warehouse_id
            )
        if candidate is not None:
            candidate = await PurchaseOrderRepository(session, tenant).lock_for_update(candidate.id)
            if (
                candidate is None
                or candidate.version != observed_version
                or (
                    payload.action in {"MERGE", "INCREASE"}
                    and candidate.version != payload.expected_candidate_version
                )
            ):
                raise ConflictError()
        orders: list[PurchaseOrder] = []
        if payload.action == "CREATE_WITH_APPROVAL":
            for incoming in drafts:
                proposed = incoming.model_copy(
                    update={
                        "acknowledge_duplicates": True,
                        "duplicate_acknowledgement_reason": payload.reason,
                    }
                )
                order, _, _ = await service.create_purchase_order(
                    session, tenant, principal, proposed, allow_duplicate_proposal=True
                )
                orders.append(order)
        else:
            if candidate is None:
                raise ValidationError("Select the purchase order to change.")
            if len(drafts) != 1:
                raise ValidationError("Resolve one vendor, category and warehouse group at a time.")
            incoming = drafts[0]
            vendor, items, category = await service._prepare_purchase_order(
                session, tenant, principal, incoming
            )
            if category != candidate.item_category_id:
                raise ValidationError("The candidate belongs to another procurement category.")
            _assert_terms(candidate, incoming, vendor.payment_terms_days)
            matches = await service.find_duplicates_for_lines(
                session,
                tenant,
                vendor_id=incoming.vendor_id,
                warehouse_id=incoming.warehouse_id,
                lines=[
                    service.ProposedLine(
                        item_id=line.item_id,
                        required_date=line.required_date or incoming.expected_delivery_date,
                        requisition_line_id=line.requisition_line_id,
                        requisition_line_ids=frozenset(
                            allocation.requisition_line_id
                            for allocation in sources.allocation_inputs(line)
                        ),
                    )
                    for line in incoming.lines
                ],
                source_type=incoming.source_type,
                source_id=incoming.source_id,
            )
            if not any(match.purchase_order_id == candidate.id for match in matches):
                raise ConflictError(
                    "This order no longer matches the demand. Refresh the duplicate check."
                )
            existing = await PurchaseOrderLineRepository(session, tenant).list_for_order(
                candidate.id
            )
            allocations = await sources.effective_allocations(session, tenant, existing)
            changes = [
                PurchaseOrderAmendLineIn(line_id=line.id, quantity=line.quantity)
                for line in existing
            ]
            by_id = {line.id: index for index, line in enumerate(existing)}
            if payload.action == "INCREASE" and any(
                line.item_id
                != next(
                    (old.item_id for old in existing if old.id == payload.candidate_line_id), None
                )
                for line in incoming.lines
            ):
                raise ValidationError(
                    "Increase can only consume demand for the selected item's line. "
                    "Resolve other items separately."
                )
            for line in incoming.lines:
                same_item = [old for old in existing if old.item_id == line.item_id]
                eligible = [
                    old
                    for old in same_item
                    if _same_line_terms(
                        old,
                        line,
                        items[line.item_id].gst_rate,
                        candidate,
                        items[line.item_id].order_uom_id,
                    )
                    and (payload.action != "INCREASE" or old.id == payload.candidate_line_id)
                ]
                if same_item and not eligible:
                    raise ValidationError(
                        "The matched line has different commercial terms. "
                        "Use a separate purchase order."
                    )
                if eligible:
                    old = eligible[0]
                    index = by_id[old.id]
                    updated_allocations = dict(allocations[old.id])
                    for allocated in sources.allocation_inputs(line):
                        updated_allocations[allocated.requisition_line_id] = (
                            updated_allocations.get(allocated.requisition_line_id, ZERO)
                            + allocated.quantity
                        )
                    allocations[old.id] = updated_allocations
                    flat_discount: dict[str, Any] = {}
                    if not old.discount_percentage and old.discount:
                        previous_discount = changes[index].discount
                        flat_discount["discount"] = (
                            previous_discount if previous_discount is not None else old.discount
                        ) + (line.discount or ZERO)
                    try:
                        changes[index] = PurchaseOrderAmendLineIn(
                            line_id=old.id,
                            quantity=to_quantity(changes[index].quantity + line.quantity),
                            source_allocations=[
                                {"requisition_line_id": key, "quantity": value}
                                for key, value in updated_allocations.items()
                            ],
                            **flat_discount,
                        )
                    except PydanticValidationError as exc:
                        raise ValidationError(
                            "The merged quantity or discount is too large. "
                            "Reduce the incoming demand."
                        ) from exc
                else:
                    changes.append(
                        PurchaseOrderAmendLineIn.model_validate(
                            {
                                **line.model_dump(
                                    exclude_none=True,
                                    exclude={"requisition_line_id", "source_allocations"},
                                ),
                                "line_number": None,
                                "source_allocations": [
                                    allocation.model_dump()
                                    for allocation in sources.allocation_inputs(line)
                                ],
                            }
                        )
                    )
            if len(changes) > MAX_LINES_PER_DOCUMENT:
                raise ValidationError("The merged purchase order has too many lines.")
            revision = PurchaseOrderAmendRequest(
                expected_version=candidate.version, reason=payload.reason, lines=changes
            )
            order, _ = await _revise_locked(session, tenant, candidate, revision, allow_draft=True)
            orders.append(order)
        if plan is not None and len(orders) == len(plan.groups):
            await service._audit_conversion(session, tenant, plan, orders)
        return PurchaseOrderDuplicateResolutionResult(
            action=payload.action,
            purchase_orders=[PurchaseOrderSummary.model_validate(order) for order in orders],
        )


async def _cancel_demand(
    session: AsyncSession,
    tenant: TenantContext,
    payload: PurchaseOrderDuplicateResolutionRequest,
    requisition_ids: Sequence[uuid.UUID],
    selected: set[uuid.UUID],
) -> PurchaseOrderDuplicateResolutionResult:
    headers = await RequisitionRepository(session, tenant).lock_many(requisition_ids)
    missing = set(requisition_ids) - {header.id for header in headers}
    if missing:
        raise NotFoundError("Purchase requisition", ", ".join(map(str, sorted(missing))))
    all_lines = await RequisitionLineRepository(session, tenant).lock_for_requisitions(
        requisition_ids
    )
    touched_header_ids = {line.requisition_id for line in all_lines if line.id in selected}
    for header in headers:
        if header.id in touched_header_ids:
            assert_warehouse_scope(tenant, header.warehouse_id)
    if any(
        header.status not in OPEN_REQUISITION_STATUSES
        for header in headers
        if header.id in touched_header_ids
    ):
        raise BusinessRuleError(
            "Only outstanding demand on an approved or converted requisition can be cancelled."
        )
    for line in all_lines:
        if line.id in selected:
            if (
                line.status == RequisitionLineStatus.CANCELLED
                or line.ordered_quantity >= line.quantity
            ):
                raise BusinessRuleError("Only outstanding requisition demand can be cancelled.")
            line.status = service.requisition_line_status_for(
                quantity=line.quantity, ordered_quantity=line.ordered_quantity, cancelled=True
            )
            RequisitionLineRepository(session, tenant).touch(line)
    for header in headers:
        if header.id not in touched_header_ids:
            continue
        children = [line for line in all_lines if line.requisition_id == header.id]
        if (
            children
            and all(line.status == RequisitionLineStatus.CANCELLED for line in children)
            and not any(line.ordered_quantity for line in children)
        ):
            # ASSUMPTION: exhausted converted requests close through their
            # existing graph; untouched approved requests can be cancelled.
            target = (
                RequisitionStatus.CANCELLED
                if header.status == RequisitionStatus.APPROVED
                else RequisitionStatus.CLOSED
            )
            if target == RequisitionStatus.CANCELLED:
                header.cancellation_reason = payload.reason
            await service._transition(
                session,
                tenant,
                machine=service.REQUISITION_LIFECYCLE,
                entity=header,
                entity_type=service.AUDIT_REQUISITION,
                entity_code=header.code,
                target=target,
                reason=payload.reason,
                status_enum=RequisitionStatus,
            )
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CANCEL,
            entity_type=service.AUDIT_REQUISITION,
            entity_id=header.id,
            entity_code=header.code,
            user_id=tenant.user_id,
            summary=f"Cancelled duplicate demand: {payload.reason}",
            context={
                "requisition_line_ids": [line.id for line in children if line.id in selected],
                "reason": payload.reason,
            },
        )
    await session.flush()
    return PurchaseOrderDuplicateResolutionResult(
        action=payload.action, cancelled_requisition_line_ids=sorted(selected)
    )
