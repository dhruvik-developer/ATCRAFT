"""Procurement write routes over HTTP.

Service rules - segregation of duties, FEFO, duplicate detection, over-receipt
tolerance, quotation comparison - are pinned at service level in the
integration suites listed in the procurement campaign brief. These drive the
twenty-four write routes no backend test had reached through HTTP: the status
code and envelope each answers with, the row it leaves behind and the audit
entry written with it, the 422 a payload or a record's state can earn, and the
404 another company's record answers with - never a 403, which would confirm
that it exists.

The caller is the ``api_client`` principal - every permission, not a superuser
- except where maker-checker needs a second actor. Prerequisites are built
through the procurement services and ``seed_support``, so every fixture row has
the shape production gives it. Ids are held as plain values: an expected
failure rolls its savepoint back and expires what was loaded before it.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.deps import Principal, TenantContext
from src.core.permissions import Permission
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.procurement import service as procurement_service
from src.modules.procurement.models import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    PurchaseRequisition,
    QuotationStatus,
    RequisitionStatus,
    Rfq,
    RfqStatus,
    VendorQuotation,
)
from src.modules.procurement.schemas import (
    PurchaseReturnCreate,
    PurchaseReturnLineIn,
    QuotationCreate,
    QuotationLineIn,
    RfqUpdate,
)
from src.modules.products.models import ItemGroup, ItemVendorLink
from src.modules.users.models import User
from src.modules.vendors.models import VendorType
from tests import seed_support
from tests.integration.test_procurement_service import (
    make_order,
    make_requisition,
    make_rfq,
    receive,
    send_order,
)

pytestmark = [pytest.mark.api, pytest.mark.integration]

REASON = "Buyer decision recorded"
NOTES = "Procurement HTTP coverage"


# -----------------------------------------------------------------------------
# Callers, builders and readers
# -----------------------------------------------------------------------------


def as_super(principal: Principal) -> Principal:
    return dataclasses.replace(principal, is_superuser=True, role_codes=("SUPER_ADMIN",))


@pytest.fixture
async def foreign(session: AsyncSession, other_company) -> TenantContext:
    owner = await seed_support.seed_user(session, company_id=other_company.id)
    return TenantContext(company_id=other_company.id, user_id=owner.id)


@pytest.fixture
async def foreign_principal(
    session: AsyncSession, other_company, foreign: TenantContext
) -> Principal:
    user = await session.get(User, foreign.user_id)
    assert user is not None
    return Principal(
        user_id=user.id,
        company_id=other_company.id,
        email=user.email,
        full_name=user.full_name,
        is_superuser=False,
        permissions=frozenset(str(p) for p in Permission),
        role_codes=("FOREIGN_ADMIN",),
    )


@pytest.fixture
async def masters(session: AsyncSession, tenant: TenantContext, unique_code) -> dict[str, Any]:
    """Approved vendor, purchasable item, warehouse - the procurement bench."""
    uom = await seed_support.seed_uom(session, company_id=tenant.company_id)
    warehouse = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=unique_code("WH")
    )
    vendor = await seed_support.seed_vendor(
        session,
        company_id=tenant.company_id,
        code=unique_code("VEN"),
        vendor_types=(VendorType.RAW_MATERIAL_SUPPLIER,),
        is_approved=True,
        approved_by_id=tenant.user_id,
        approved_at=utcnow(),
        is_active=True,
        payment_terms_days=30,
        lead_time_days=7,
        currency="INR",
        state_code="27",
    )
    item = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=unique_code("RM"),
        base_uom_id=uom.id,
        item_group=ItemGroup.RAW_MATERIAL,
        gst_rate=Decimal("18"),
        can_purchase=True,
        standard_cost=Decimal("40"),
    )
    session.add(
        ItemVendorLink(
            company_id=tenant.company_id,
            item_id=item.id,
            vendor_id=vendor.id,
            is_preferred=True,
            last_purchase_price=Decimal("50"),
        )
    )
    await session.flush()
    return {
        "warehouse": warehouse,
        "uom": uom,
        "vendor": vendor,
        "item": item,
        "warehouse_id": warehouse.id,
        "vendor_id": vendor.id,
        "item_id": item.id,
    }


@pytest.fixture
async def maker(session: AsyncSession, tenant: TenantContext, principal: Principal) -> Principal:
    """A second staff user for maker-checker on requisitions and orders."""
    user = await seed_support.seed_user(session, company_id=tenant.company_id)
    return Principal(
        user_id=user.id,
        company_id=tenant.company_id,
        email=user.email,
        full_name=user.full_name,
        is_superuser=False,
        permissions=principal.permissions,
        role_codes=("BUYER",),
    )


def ok(response: Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is True, body
    assert body["message"], body
    return body["data"]


def refused(response: Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is False, body
    assert body["error"]["code"] == code, body["error"]
    return body["error"]


def field_errors(response: Response) -> dict[str, list[str]]:
    return refused(response, 422, "VALIDATION_ERROR")["details"]["fields"]


async def audit_rows(
    session: AsyncSession,
    company_id: uuid.UUID,
    entity_id: uuid.UUID,
    action: AuditAction,
) -> list[Row[Any]]:
    stmt = (
        select(
            AuditLog.entity_type,
            AuditLog.created_by_id,
            AuditLog.old_values,
            AuditLog.new_values,
            AuditLog.changed_fields,
            AuditLog.context,
        )
        .where(
            AuditLog.company_id == company_id,
            AuditLog.entity_id == entity_id,
            AuditLog.action == action,
        )
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    return list((await session.execute(stmt)).all())


async def column(session: AsyncSession, attribute: Any, row_id: uuid.UUID) -> Any:
    model = attribute.class_
    return await session.scalar(select(attribute).where(model.id == row_id))


async def draft_requisition(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    masters: dict[str, Any],
    *,
    quantity: str = "10",
    unit_price: str | None = None,
) -> PurchaseRequisition:
    requisition, _ = await make_requisition(
        session, tenant, principal, masters, quantity=quantity, unit_price=unit_price
    )
    return requisition


def actor_tenant(tenant: TenantContext, principal: Principal) -> TenantContext:
    """Tenant scope whose ``user_id`` matches the principal (created_by evidence)."""
    return TenantContext(company_id=tenant.company_id, user_id=principal.user_id)


async def pending_requisition(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    masters: dict[str, Any],
) -> uuid.UUID:
    """Unpriced draft submitted into the manual queue. Returns the id only."""
    scope = actor_tenant(tenant, principal)
    requisition = await draft_requisition(session, scope, principal, masters)
    requisition_id = requisition.id
    await procurement_service.transition_requisition(
        session,
        scope,
        principal,
        requisition_id,
        RequisitionStatus.PENDING_APPROVAL,
        reason="Submitted for approval",
    )
    return requisition_id


async def approved_requisition(
    session: AsyncSession,
    tenant: TenantContext,
    maker: Principal,
    approver: Principal,
    masters: dict[str, Any],
) -> uuid.UUID:
    requisition_id = await pending_requisition(session, tenant, maker, masters)
    await procurement_service.transition_requisition(
        session,
        actor_tenant(tenant, approver),
        approver,
        requisition_id,
        RequisitionStatus.APPROVED,
        reason="Approved",
    )
    return requisition_id


async def draft_rfq(session: AsyncSession, tenant: TenantContext, masters: dict[str, Any]) -> Rfq:
    return await make_rfq(session, tenant, masters)


async def sent_rfq(
    session: AsyncSession, tenant: TenantContext, masters: dict[str, Any]
) -> uuid.UUID:
    rfq = await draft_rfq(session, tenant, masters)
    rfq_id = rfq.id
    await procurement_service.update_rfq(
        session, tenant, rfq_id, RfqUpdate(vendor_ids=[masters["vendor_id"]])
    )
    await procurement_service.transition_rfq(
        session, tenant, rfq_id, RfqStatus.SENT, reason="Sent to vendors"
    )
    return rfq_id


async def received_quotation(
    session: AsyncSession, tenant: TenantContext, masters: dict[str, Any]
) -> uuid.UUID:
    rfq_id = await sent_rfq(session, tenant, masters)
    quotation, _ = await procurement_service.create_quotation(
        session,
        tenant,
        QuotationCreate(
            rfq_id=rfq_id,
            vendor_id=masters["vendor_id"],
            vendor_reference=f"Q-{uuid.uuid4().hex[:6].upper()}",
            lines=[
                QuotationLineIn(
                    line_number=1,
                    item_id=masters["item_id"],
                    quantity=Decimal("100"),
                    unit_price=Decimal("50"),
                )
            ],
        ),
    )
    return quotation.id


async def draft_order(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    masters: dict[str, Any],
    *,
    quantity: str = "100",
) -> tuple[uuid.UUID, uuid.UUID]:
    order, lines, _ = await make_order(session, tenant, principal, masters, quantity=quantity)
    return order.id, lines[0].id


async def sent_order(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    masters: dict[str, Any],
    *,
    quantity: str = "100",
) -> tuple[uuid.UUID, uuid.UUID]:
    order, lines, _ = await make_order(session, tenant, principal, masters, quantity=quantity)
    order_id, line_id = order.id, lines[0].id
    await send_order(session, tenant, principal, order)
    return order_id, line_id


async def draft_receipt(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    masters: dict[str, Any],
    *,
    received: str = "40",
) -> tuple[uuid.UUID, uuid.UUID]:
    order_id, line_id = await sent_order(session, tenant, principal, masters)
    order = await session.get(PurchaseOrder, order_id)
    line = await session.get(PurchaseOrderLine, line_id)
    assert order is not None and line is not None
    receipt, _ = await receive(
        session, tenant, principal, order, line, received=received, post=False
    )
    return receipt.id, line_id


async def posted_receipt(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    masters: dict[str, Any],
    *,
    received: str = "40",
) -> tuple[uuid.UUID, uuid.UUID]:
    """Posted GRN + its receipt line id (for returns)."""
    order_id, line_id = await sent_order(session, tenant, principal, masters)
    order = await session.get(PurchaseOrder, order_id)
    line = await session.get(PurchaseOrderLine, line_id)
    assert order is not None and line is not None
    receipt, result = await receive(
        session, tenant, principal, order, line, received=received, post=True
    )
    assert result is not None
    return receipt.id, result.receipt.lines[0].id


# -----------------------------------------------------------------------------
# POST /procurement/purchase-requisitions/conversion-preview
# -----------------------------------------------------------------------------


class TestBatchConversionPreview:
    async def test_preview_is_read_only_and_names_groups(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        maker: Principal,
        masters: dict[str, Any],
    ) -> None:
        requisition_id = await approved_requisition(session, tenant, maker, principal, masters)

        response = await api_client.post(
            "/procurement/purchase-requisitions/conversion-preview",
            json={"requisition_ids": [str(requisition_id)]},
        )

        data = ok(response)
        assert data["converted"] is False
        assert data["groups"]
        assert await column(session, PurchaseRequisition.status, requisition_id) == (
            RequisitionStatus.APPROVED
        )

    async def test_empty_batch_is_a_field_error(self, api_client: AsyncClient) -> None:
        response = await api_client.post(
            "/procurement/purchase-requisitions/conversion-preview",
            json={"requisition_ids": []},
        )
        assert "requisition_ids" in field_errors(response)

    async def test_another_companys_requisition_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        foreign_principal: Principal,
        unique_code,
    ) -> None:
        their_masters = {
            "warehouse": await seed_support.seed_warehouse(
                session, company_id=foreign.company_id, code=unique_code("FWH")
            ),
            "item": await seed_support.seed_item(
                session, company_id=foreign.company_id, code=unique_code("FIT"), can_purchase=True
            ),
            "vendor": await seed_support.seed_vendor(
                session,
                company_id=foreign.company_id,
                code=unique_code("FV"),
                is_approved=True,
                approved_by_id=foreign.user_id,
                approved_at=utcnow(),
            ),
        }
        theirs, _ = await make_requisition(session, foreign, foreign_principal, their_masters)
        theirs_id = theirs.id

        refused(
            await api_client.post(
                "/procurement/purchase-requisitions/conversion-preview",
                json={"requisition_ids": [str(theirs_id)]},
            ),
            404,
            "NOT_FOUND",
        )


# -----------------------------------------------------------------------------
# PATCH /procurement/purchase-requisitions/{requisition_id}
# -----------------------------------------------------------------------------


class TestUpdateRequisition:
    async def test_a_draft_is_updated_and_audited(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        requisition = await draft_requisition(session, tenant, principal, masters)
        requisition_id = requisition.id
        required = (today() + timedelta(days=21)).isoformat()

        data = ok(
            await api_client.patch(
                f"/procurement/purchase-requisitions/{requisition_id}",
                json={"purpose": NOTES, "required_date": required},
            )
        )

        assert data["purpose"] == NOTES
        assert data["required_date"] == required
        assert await column(session, PurchaseRequisition.purpose, requisition_id) == NOTES
        (update,) = await audit_rows(session, tenant.company_id, requisition_id, AuditAction.UPDATE)
        assert update.entity_type == "purchase_requisition"
        assert "purpose" in update.changed_fields

    async def test_empty_lines_are_a_field_error(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        requisition = await draft_requisition(session, tenant, principal, masters)
        requisition_id = requisition.id

        response = await api_client.patch(
            f"/procurement/purchase-requisitions/{requisition_id}",
            json={"lines": []},
        )
        assert field_errors(response)

    async def test_a_submitted_requisition_cannot_be_edited(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        requisition_id = await pending_requisition(session, tenant, principal, masters)

        refused(
            await api_client.patch(
                f"/procurement/purchase-requisitions/{requisition_id}",
                json={"purpose": "Too late"},
            ),
            422,
            "BUSINESS_RULE_VIOLATION",
        )
        assert await column(session, PurchaseRequisition.purpose, requisition_id) is None

    async def test_another_companys_requisition_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        foreign_principal: Principal,
        unique_code,
    ) -> None:
        their_masters = {
            "warehouse": await seed_support.seed_warehouse(
                session, company_id=foreign.company_id, code=unique_code("FWH")
            ),
            "item": await seed_support.seed_item(
                session, company_id=foreign.company_id, code=unique_code("FIT"), can_purchase=True
            ),
        }
        theirs, _ = await make_requisition(session, foreign, foreign_principal, their_masters)
        theirs_id = theirs.id

        refused(
            await api_client.patch(
                f"/procurement/purchase-requisitions/{theirs_id}",
                json={"purpose": "Taken over"},
            ),
            404,
            "NOT_FOUND",
        )


# -----------------------------------------------------------------------------
# POST /procurement/purchase-requisitions/{id}/submit|approve|reject|cancel
# -----------------------------------------------------------------------------


class TestSubmitRequisition:
    async def test_submit_puts_an_unpriced_requisition_in_the_queue(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        requisition = await draft_requisition(session, tenant, principal, masters)
        requisition_id = requisition.id

        data = ok(
            await api_client.post(
                f"/procurement/purchase-requisitions/{requisition_id}/submit",
                json={},
            )
        )

        assert data["status"] == "PENDING_APPROVAL"
        assert await column(session, PurchaseRequisition.status, requisition_id) == (
            RequisitionStatus.PENDING_APPROVAL
        )
        (change,) = await audit_rows(
            session, tenant.company_id, requisition_id, AuditAction.STATUS_CHANGE
        )
        assert change.new_values["status"] == "PENDING_APPROVAL"

    async def test_a_draft_cannot_be_submitted_twice(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        requisition_id = await pending_requisition(session, tenant, principal, masters)

        refused(
            await api_client.post(
                f"/procurement/purchase-requisitions/{requisition_id}/submit",
                json={},
            ),
            422,
            "INVALID_STATE_TRANSITION",
        )


class TestApproveRequisition:
    async def test_a_second_person_approves_and_is_audited(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        maker: Principal,
        masters: dict[str, Any],
    ) -> None:
        requisition_id = await pending_requisition(session, tenant, maker, masters)

        data = ok(
            await api_client.post(
                f"/procurement/purchase-requisitions/{requisition_id}/approve",
                json={"reason": REASON},
            )
        )

        assert data["status"] == "APPROVED"
        assert data["approved_by_id"] == str(principal.user_id)
        (approve,) = await audit_rows(
            session, tenant.company_id, requisition_id, AuditAction.APPROVE
        )
        assert approve.created_by_id == principal.user_id
        assert (approve.context or {}).get("reason") == REASON

    async def test_the_requester_cannot_approve_their_own(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        requisition_id = await pending_requisition(session, tenant, principal, masters)

        refused(
            await api_client.post(
                f"/procurement/purchase-requisitions/{requisition_id}/approve",
                json={"reason": REASON},
            ),
            403,
            "APPROVAL_REQUIRED",
        )


class TestRejectRequisition:
    async def test_reject_records_the_reason(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        maker: Principal,
        masters: dict[str, Any],
    ) -> None:
        requisition_id = await pending_requisition(session, tenant, maker, masters)

        data = ok(
            await api_client.post(
                f"/procurement/purchase-requisitions/{requisition_id}/reject",
                json={"reason": "Not needed this month"},
            )
        )

        assert data["status"] == "REJECTED"
        assert (
            await column(session, PurchaseRequisition.rejection_reason, requisition_id)
            == "Not needed this month"
        )
        (reject,) = await audit_rows(session, tenant.company_id, requisition_id, AuditAction.REJECT)
        assert (reject.context or {}).get("reason") == "Not needed this month"

    async def test_reject_needs_a_reason(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        maker: Principal,
        masters: dict[str, Any],
    ) -> None:
        requisition_id = await pending_requisition(session, tenant, maker, masters)

        assert "reason" in field_errors(
            await api_client.post(
                f"/procurement/purchase-requisitions/{requisition_id}/reject",
                json={},
            )
        )


class TestCancelRequisition:
    async def test_a_draft_is_cancelled_with_a_reason(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        requisition = await draft_requisition(session, tenant, principal, masters)
        requisition_id = requisition.id

        data = ok(
            await api_client.post(
                f"/procurement/purchase-requisitions/{requisition_id}/cancel",
                json={"reason": "Raised in error"},
            )
        )

        assert data["status"] == "CANCELLED"
        (cancel,) = await audit_rows(session, tenant.company_id, requisition_id, AuditAction.CANCEL)
        assert cancel.entity_type == "purchase_requisition"

    async def test_cancel_needs_a_reason(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        requisition = await draft_requisition(session, tenant, principal, masters)

        assert "reason" in field_errors(
            await api_client.post(
                f"/procurement/purchase-requisitions/{requisition.id}/cancel",
                json={},
            )
        )


# -----------------------------------------------------------------------------
# RFQs
# -----------------------------------------------------------------------------


class TestCreateRfq:
    async def test_an_rfq_is_raised_normalised_and_audited(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        masters: dict[str, Any],
    ) -> None:
        response = await api_client.post(
            "/procurement/rfqs",
            json={
                "warehouse_id": str(masters["warehouse_id"]),
                "vendor_ids": [str(masters["vendor_id"])],
                "notes": NOTES,
                "lines": [
                    {
                        "line_number": 1,
                        "item_id": str(masters["item_id"]),
                        "quantity": "100",
                    }
                ],
            },
        )

        data = ok(response, 201)
        assert data["status"] == "DRAFT"
        assert data["notes"] == NOTES
        assert data["vendor_ids"] == [str(masters["vendor_id"])]
        rfq_id = uuid.UUID(data["id"])
        (created,) = await audit_rows(session, tenant.company_id, rfq_id, AuditAction.CREATE)
        assert created.entity_type == "rfq"

    async def test_lines_or_a_requisition_are_required(
        self, api_client: AsyncClient, masters
    ) -> None:
        response = await api_client.post(
            "/procurement/rfqs",
            json={"warehouse_id": str(masters["warehouse_id"])},
        )
        assert field_errors(response) or refused(response, 422, "VALIDATION_ERROR")


class TestUpdateRfq:
    async def test_a_draft_rfq_accepts_vendor_ids_and_audits(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        masters: dict[str, Any],
    ) -> None:
        rfq = await draft_rfq(session, tenant, masters)
        rfq_id = rfq.id

        data = ok(
            await api_client.patch(
                f"/procurement/rfqs/{rfq_id}",
                json={"vendor_ids": [str(masters["vendor_id"])], "notes": NOTES},
            )
        )

        assert data["vendor_ids"] == [str(masters["vendor_id"])]
        assert data["notes"] == NOTES
        (update,) = await audit_rows(session, tenant.company_id, rfq_id, AuditAction.UPDATE)
        assert update.entity_type == "rfq"

    async def test_a_sent_rfq_cannot_be_edited(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        masters: dict[str, Any],
    ) -> None:
        rfq_id = await sent_rfq(session, tenant, masters)

        refused(
            await api_client.patch(
                f"/procurement/rfqs/{rfq_id}",
                json={"notes": "Too late"},
            ),
            422,
            "BUSINESS_RULE_VIOLATION",
        )


class TestSendRfq:
    async def test_send_requires_vendors_and_records_sent_at(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        masters: dict[str, Any],
    ) -> None:
        rfq = await draft_rfq(session, tenant, masters)
        rfq_id = rfq.id
        await procurement_service.update_rfq(
            session, tenant, rfq_id, RfqUpdate(vendor_ids=[masters["vendor_id"]])
        )

        data = ok(await api_client.post(f"/procurement/rfqs/{rfq_id}/send", json={}))

        assert data["status"] == "SENT"
        assert data["sent_at"] is not None
        assert await column(session, Rfq.status, rfq_id) == RfqStatus.SENT

    async def test_send_without_vendors_is_refused(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        masters: dict[str, Any],
    ) -> None:
        rfq = await draft_rfq(session, tenant, masters)
        rfq_id = rfq.id

        refused(
            await api_client.post(f"/procurement/rfqs/{rfq_id}/send", json={}),
            422,
            "BUSINESS_RULE_VIOLATION",
        )


class TestCloseRfq:
    async def test_a_sent_rfq_closes(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        masters: dict[str, Any],
    ) -> None:
        rfq_id = await sent_rfq(session, tenant, masters)

        data = ok(await api_client.post(f"/procurement/rfqs/{rfq_id}/close", json={}))

        assert data["status"] == "CLOSED"
        assert await column(session, Rfq.status, rfq_id) == RfqStatus.CLOSED

    async def test_a_draft_cannot_close(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        masters: dict[str, Any],
    ) -> None:
        rfq = await draft_rfq(session, tenant, masters)

        refused(
            await api_client.post(f"/procurement/rfqs/{rfq.id}/close", json={}),
            422,
            "INVALID_STATE_TRANSITION",
        )


class TestCancelRfq:
    async def test_a_draft_rfq_cancels_with_a_reason(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        masters: dict[str, Any],
    ) -> None:
        rfq = await draft_rfq(session, tenant, masters)
        rfq_id = rfq.id

        data = ok(
            await api_client.post(
                f"/procurement/rfqs/{rfq_id}/cancel",
                json={"reason": "Vendor shortlist changed"},
            )
        )

        assert data["status"] == "CANCELLED"
        (cancel,) = await audit_rows(session, tenant.company_id, rfq_id, AuditAction.CANCEL)
        assert cancel.entity_type == "rfq"

    async def test_cancel_needs_a_reason(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        masters: dict[str, Any],
    ) -> None:
        rfq = await draft_rfq(session, tenant, masters)

        assert "reason" in field_errors(
            await api_client.post(f"/procurement/rfqs/{rfq.id}/cancel", json={})
        )


# -----------------------------------------------------------------------------
# Quotations
# -----------------------------------------------------------------------------


class TestCreateQuotation:
    async def test_a_quotation_is_recorded_against_a_live_rfq(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        masters: dict[str, Any],
    ) -> None:
        rfq_id = await sent_rfq(session, tenant, masters)
        reference = f"Q-{uuid.uuid4().hex[:6].upper()}"

        data = ok(
            await api_client.post(
                "/procurement/quotations",
                json={
                    "rfq_id": str(rfq_id),
                    "vendor_id": str(masters["vendor_id"]),
                    "vendor_reference": reference,
                    "lines": [
                        {
                            "line_number": 1,
                            "item_id": str(masters["item_id"]),
                            "quantity": "100",
                            "unit_price": "50",
                        }
                    ],
                },
            ),
            201,
        )

        assert data["status"] == "RECEIVED"
        assert data["vendor_reference"] == reference
        quotation_id = uuid.UUID(data["id"])
        (created,) = await audit_rows(session, tenant.company_id, quotation_id, AuditAction.CREATE)
        assert created.entity_type == "vendor_quotation"

    async def test_a_second_quotation_from_the_same_vendor_is_a_conflict(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        masters: dict[str, Any],
    ) -> None:
        quotation_id = await received_quotation(session, tenant, masters)
        quotation = await session.get(VendorQuotation, quotation_id)
        assert quotation is not None
        rfq_id = quotation.rfq_id

        refused(
            await api_client.post(
                "/procurement/quotations",
                json={
                    "rfq_id": str(rfq_id),
                    "vendor_id": str(masters["vendor_id"]),
                    "vendor_reference": "Q-SECOND",
                    "lines": [
                        {
                            "line_number": 1,
                            "item_id": str(masters["item_id"]),
                            "quantity": "100",
                            "unit_price": "45",
                        }
                    ],
                },
            ),
            409,
            "DUPLICATE_DOCUMENT",
        )


class TestSelectQuotation:
    async def test_select_records_the_buyer_and_reason(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        quotation_id = await received_quotation(session, tenant, masters)

        data = ok(
            await api_client.post(
                f"/procurement/quotations/{quotation_id}/select",
                json={"reason": "Best price and lead time"},
            )
        )

        assert data["status"] == "SELECTED"
        assert await column(session, VendorQuotation.selected_by_id, quotation_id) == (
            principal.user_id
        )
        (approve,) = await audit_rows(session, tenant.company_id, quotation_id, AuditAction.APPROVE)
        assert (approve.context or {}).get("reason") == "Best price and lead time"

    async def test_select_needs_a_reason(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        masters: dict[str, Any],
    ) -> None:
        quotation_id = await received_quotation(session, tenant, masters)

        assert "reason" in field_errors(
            await api_client.post(
                f"/procurement/quotations/{quotation_id}/select",
                json={},
            )
        )


class TestRejectQuotation:
    async def test_reject_records_the_reason(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        masters: dict[str, Any],
    ) -> None:
        quotation_id = await received_quotation(session, tenant, masters)

        data = ok(
            await api_client.post(
                f"/procurement/quotations/{quotation_id}/reject",
                json={"reason": "Lead time too long"},
            )
        )

        assert data["status"] == "REJECTED"
        assert await column(session, VendorQuotation.status, quotation_id) == (
            QuotationStatus.REJECTED
        )
        (reject,) = await audit_rows(session, tenant.company_id, quotation_id, AuditAction.REJECT)
        assert (reject.context or {}).get("reason") == "Lead time too long"

    async def test_another_companys_quotation_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        foreign_principal: Principal,
        unique_code,
    ) -> None:
        uom = await seed_support.seed_uom(session, company_id=foreign.company_id)
        their_masters = {
            "warehouse": await seed_support.seed_warehouse(
                session, company_id=foreign.company_id, code=unique_code("FWH")
            ),
            "item": await seed_support.seed_item(
                session,
                company_id=foreign.company_id,
                code=unique_code("FIT"),
                base_uom_id=uom.id,
                can_purchase=True,
            ),
            "vendor": await seed_support.seed_vendor(
                session,
                company_id=foreign.company_id,
                code=unique_code("FV"),
                is_approved=True,
                approved_by_id=foreign.user_id,
                approved_at=utcnow(),
                payment_terms_days=30,
                lead_time_days=7,
            ),
        }
        their_masters["warehouse_id"] = their_masters["warehouse"].id
        their_masters["item_id"] = their_masters["item"].id
        their_masters["vendor_id"] = their_masters["vendor"].id
        theirs_id = await received_quotation(session, foreign, their_masters)

        refused(
            await api_client.post(
                f"/procurement/quotations/{theirs_id}/reject",
                json={"reason": "Taken over"},
            ),
            404,
            "NOT_FOUND",
        )


# -----------------------------------------------------------------------------
# Purchase orders
# -----------------------------------------------------------------------------


class TestCheckDuplicates:
    async def test_check_reports_candidates_without_writing(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        order_id, _ = await draft_order(session, tenant, principal, masters)
        before = await column(session, PurchaseOrder.code, order_id)

        data = ok(
            await api_client.post(
                "/procurement/purchase-orders/check-duplicates",
                json={
                    "vendor_id": str(masters["vendor_id"]),
                    "warehouse_id": str(masters["warehouse_id"]),
                    "expected_delivery_date": (today() + timedelta(days=10)).isoformat(),
                    "lines": [{"item_id": str(masters["item_id"])}],
                },
            )
        )

        assert data["candidates"]
        assert await column(session, PurchaseOrder.code, order_id) == before

    async def test_lines_are_required(self, api_client: AsyncClient, masters) -> None:
        response = await api_client.post(
            "/procurement/purchase-orders/check-duplicates",
            json={
                "vendor_id": str(masters["vendor_id"]),
                "warehouse_id": str(masters["warehouse_id"]),
                "lines": [],
            },
        )
        assert "lines" in field_errors(response)


class TestUpdatePurchaseOrder:
    async def test_a_draft_order_is_updated_and_audited(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        order_id, _ = await draft_order(session, tenant, principal, masters)

        data = ok(
            await api_client.patch(
                f"/procurement/purchase-orders/{order_id}",
                json={"notes": NOTES, "vendor_reference": "VR-1"},
            )
        )

        assert data["notes"] == NOTES
        assert data["vendor_reference"] == "VR-1"
        (update,) = await audit_rows(session, tenant.company_id, order_id, AuditAction.UPDATE)
        assert update.entity_type == "purchase_order"

    async def test_an_approved_order_cannot_be_edited(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        order, _, _ = await make_order(session, tenant, principal, masters)
        order_id = order.id
        await procurement_service.transition_purchase_order(
            session,
            tenant,
            principal,
            order_id,
            PurchaseOrderStatus.PENDING_APPROVAL,
            reason="submit",
        )
        await procurement_service.transition_purchase_order(
            session,
            tenant,
            as_super(principal),
            order_id,
            PurchaseOrderStatus.APPROVED,
            reason="approve",
        )

        refused(
            await api_client.patch(
                f"/procurement/purchase-orders/{order_id}",
                json={"notes": "Too late"},
            ),
            422,
            "BUSINESS_RULE_VIOLATION",
        )


class TestAcknowledgePurchaseOrder:
    async def test_acknowledge_moves_a_sent_order(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        order_id, _ = await sent_order(session, tenant, principal, masters)
        delivery = (today() + timedelta(days=14)).isoformat()

        data = ok(
            await api_client.post(
                f"/procurement/purchase-orders/{order_id}/acknowledge",
                json={
                    "expected_delivery_date": delivery,
                    "vendor_reference": "ACK-1",
                },
            )
        )

        assert data["status"] == "ACKNOWLEDGED"
        assert await column(session, PurchaseOrder.status, order_id) == (
            PurchaseOrderStatus.ACKNOWLEDGED
        )
        assert await column(session, PurchaseOrder.vendor_reference, order_id) == "ACK-1"

    async def test_a_draft_cannot_be_acknowledged(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        order_id, _ = await draft_order(session, tenant, principal, masters)

        refused(
            await api_client.post(
                f"/procurement/purchase-orders/{order_id}/acknowledge",
                json={},
            ),
            422,
            "INVALID_STATE_TRANSITION",
        )


class TestRecordShipment:
    async def test_shipment_details_are_recorded_on_a_sent_order(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        order_id, _ = await sent_order(session, tenant, principal, masters)

        data = ok(
            await api_client.post(
                f"/procurement/purchase-orders/{order_id}/shipment",
                json={"courier_name": "BlueDart", "tracking_reference": "BD123"},
            )
        )

        assert data["status"] == "SENT"
        assert await column(session, PurchaseOrder.courier_name, order_id) == "BlueDart"
        assert await column(session, PurchaseOrder.tracking_reference, order_id) == "BD123"
        (update,) = await audit_rows(session, tenant.company_id, order_id, AuditAction.UPDATE)
        assert "courier_name" in (update.changed_fields or [])

    async def test_empty_shipment_payload_is_a_field_error(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        order_id, _ = await sent_order(session, tenant, principal, masters)

        assert field_errors(
            await api_client.post(
                f"/procurement/purchase-orders/{order_id}/shipment",
                json={},
            )
        )


class TestCancelPurchaseOrder:
    async def test_a_draft_order_cancels_with_a_reason(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        order_id, _ = await draft_order(session, tenant, principal, masters)

        data = ok(
            await api_client.post(
                f"/procurement/purchase-orders/{order_id}/cancel",
                json={"reason": "Demand cancelled"},
            )
        )

        assert data["status"] == "CANCELLED"
        (cancel,) = await audit_rows(session, tenant.company_id, order_id, AuditAction.CANCEL)
        assert cancel.entity_type == "purchase_order"

    async def test_a_partially_received_order_cannot_cancel(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        order_id, line_id = await sent_order(session, tenant, principal, masters)
        order = await session.get(PurchaseOrder, order_id)
        line = await session.get(PurchaseOrderLine, line_id)
        assert order is not None and line is not None
        await receive(session, tenant, principal, order, line, received="40")

        refused(
            await api_client.post(
                f"/procurement/purchase-orders/{order_id}/cancel",
                json={"reason": "Too late"},
            ),
            422,
            "INVALID_STATE_TRANSITION",
        )


class TestClosePurchaseOrder:
    async def test_short_close_stops_the_undelivered_balance(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        order_id, line_id = await sent_order(session, tenant, principal, masters)
        order = await session.get(PurchaseOrder, order_id)
        line = await session.get(PurchaseOrderLine, line_id)
        assert order is not None and line is not None
        await receive(session, tenant, principal, order, line, received="40")

        data = ok(
            await api_client.post(
                f"/procurement/purchase-orders/{order_id}/close",
                json={"reason": "Vendor cannot supply the rest"},
            )
        )

        assert data["status"] == "CLOSED"
        assert await column(session, PurchaseOrder.status, order_id) == (PurchaseOrderStatus.CLOSED)

    async def test_a_draft_cannot_close(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        order_id, _ = await draft_order(session, tenant, principal, masters)

        refused(
            await api_client.post(
                f"/procurement/purchase-orders/{order_id}/close",
                json={"reason": "Wrong status"},
            ),
            422,
            "INVALID_STATE_TRANSITION",
        )


# -----------------------------------------------------------------------------
# Goods receipts
# -----------------------------------------------------------------------------


class TestCreateGoodsReceipt:
    async def test_a_draft_receipt_is_recorded_against_a_sent_order(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        order_id, line_id = await sent_order(session, tenant, principal, masters)

        data = ok(
            await api_client.post(
                "/procurement/goods-receipts",
                json={
                    "purchase_order_id": str(order_id),
                    "vendor_delivery_note": "DN-1",
                    "lines": [
                        {
                            "line_number": 1,
                            "purchase_order_line_id": str(line_id),
                            "received_quantity": "40",
                            "accepted_quantity": "40",
                            "rejected_quantity": "0",
                        }
                    ],
                },
            ),
            201,
        )

        assert data["status"] == "DRAFT"
        assert data["vendor_delivery_note"] == "DN-1"
        receipt_id = uuid.UUID(data["id"])
        (created,) = await audit_rows(session, tenant.company_id, receipt_id, AuditAction.CREATE)
        assert created.entity_type == "goods_receipt"

    async def test_quantities_must_add_up(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        order_id, line_id = await sent_order(session, tenant, principal, masters)

        response = await api_client.post(
            "/procurement/goods-receipts",
            json={
                "purchase_order_id": str(order_id),
                "lines": [
                    {
                        "line_number": 1,
                        "purchase_order_line_id": str(line_id),
                        "received_quantity": "40",
                        "accepted_quantity": "30",
                        "rejected_quantity": "5",
                    }
                ],
            },
        )
        assert field_errors(response)

    async def test_goods_cannot_be_received_against_a_draft_order(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        order_id, line_id = await draft_order(session, tenant, principal, masters)

        refused(
            await api_client.post(
                "/procurement/goods-receipts",
                json={
                    "purchase_order_id": str(order_id),
                    "lines": [
                        {
                            "line_number": 1,
                            "purchase_order_line_id": str(line_id),
                            "received_quantity": "10",
                            "accepted_quantity": "10",
                            "rejected_quantity": "0",
                        }
                    ],
                },
            ),
            422,
            "BUSINESS_RULE_VIOLATION",
        )


class TestCancelGoodsReceipt:
    async def test_a_draft_receipt_cancels(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        receipt_id, _ = await draft_receipt(session, tenant, principal, masters)

        data = ok(
            await api_client.post(
                f"/procurement/goods-receipts/{receipt_id}/cancel",
                json={"reason": "Wrong delivery note"},
            )
        )

        assert data["status"] == "CANCELLED"
        (cancel,) = await audit_rows(session, tenant.company_id, receipt_id, AuditAction.CANCEL)
        assert cancel.entity_type == "goods_receipt"

    async def test_a_posted_receipt_cannot_cancel(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        receipt_id, _ = await posted_receipt(session, tenant, principal, masters)

        refused(
            await api_client.post(
                f"/procurement/goods-receipts/{receipt_id}/cancel",
                json={"reason": "Too late"},
            ),
            422,
            "BUSINESS_RULE_VIOLATION",
        )


# -----------------------------------------------------------------------------
# Purchase returns
# -----------------------------------------------------------------------------


class TestCreatePurchaseReturn:
    async def test_a_draft_return_is_raised_against_a_posted_receipt(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        receipt_id, receipt_line_id = await posted_receipt(
            session, tenant, principal, masters, received="40"
        )

        data = ok(
            await api_client.post(
                "/procurement/purchase-returns",
                json={
                    "goods_receipt_id": str(receipt_id),
                    "reason": "Damaged in transit",
                    "lines": [
                        {
                            "line_number": 1,
                            "goods_receipt_line_id": str(receipt_line_id),
                            "quantity": "10",
                        }
                    ],
                },
            ),
            201,
        )

        assert data["status"] == "DRAFT"
        return_id = uuid.UUID(data["id"])
        (created,) = await audit_rows(session, tenant.company_id, return_id, AuditAction.CREATE)
        assert created.entity_type == "purchase_return"

    async def test_return_needs_a_reason(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        receipt_id, receipt_line_id = await posted_receipt(session, tenant, principal, masters)

        assert "reason" in field_errors(
            await api_client.post(
                "/procurement/purchase-returns",
                json={
                    "goods_receipt_id": str(receipt_id),
                    "lines": [
                        {
                            "line_number": 1,
                            "goods_receipt_line_id": str(receipt_line_id),
                            "quantity": "1",
                        }
                    ],
                },
            )
        )


class TestCancelPurchaseReturn:
    async def test_a_draft_return_cancels(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        receipt_id, receipt_line_id = await posted_receipt(session, tenant, principal, masters)
        purchase_return, _ = await procurement_service.create_purchase_return(
            session,
            tenant,
            PurchaseReturnCreate(
                goods_receipt_id=receipt_id,
                reason="Damaged in transit",
                lines=[
                    PurchaseReturnLineIn(
                        line_number=1,
                        goods_receipt_line_id=receipt_line_id,
                        quantity=Decimal("5"),
                    )
                ],
            ),
        )
        return_id = purchase_return.id

        data = ok(
            await api_client.post(
                f"/procurement/purchase-returns/{return_id}/cancel",
                json={"reason": "Not returning after all"},
            )
        )

        assert data["status"] == "CANCELLED"
        (cancel,) = await audit_rows(session, tenant.company_id, return_id, AuditAction.CANCEL)
        assert cancel.entity_type == "purchase_return"

    async def test_cancel_needs_a_reason(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        masters: dict[str, Any],
    ) -> None:
        receipt_id, receipt_line_id = await posted_receipt(session, tenant, principal, masters)
        purchase_return, _ = await procurement_service.create_purchase_return(
            session,
            tenant,
            PurchaseReturnCreate(
                goods_receipt_id=receipt_id,
                reason="Damaged in transit",
                lines=[
                    PurchaseReturnLineIn(
                        line_number=1,
                        goods_receipt_line_id=receipt_line_id,
                        quantity=Decimal("5"),
                    )
                ],
            ),
        )

        assert "reason" in field_errors(
            await api_client.post(
                f"/procurement/purchase-returns/{purchase_return.id}/cancel",
                json={},
            )
        )
