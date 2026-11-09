"""Audit module write routes over HTTP: approval settings and the detached decisions.

``PUT /audit/approval-settings`` stores a sparse per-company override of the
approval thresholds: omitted keys stay as they are, an explicit null restores
the code default, and only a real change writes a CONFIG_CHANGE audit row.

``POST /audit/approvals/{id}/approve`` and ``.../reject`` are deprecated. They
check everything a decision needs - the note a rejection must carry, the row's
state, the caller's authority over that kind of request, segregation of duties -
and then always refuse, because a decision taken here would leave the owning
document pending (it must be made through the document's own workflow, so the
document and the queue row change together). Their contract is therefore the
order and shape of those refusals, and that a refused decision changes nothing.

The generic suites already cover the anonymous, unentitled and unknown-id
answers. Approval requests are opened through ``open_approval``, the way every
owning module raises one.
"""

from __future__ import annotations

import dataclasses
import uuid
from decimal import Decimal
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import TenantContext
from src.core.permissions import Permission
from src.modules.audit import service as audit_service
from src.modules.audit.models import ApprovalRequest, AuditAction, AuditLog, CompanyApprovalSettings
from src.modules.audit.query_service import close_approval, open_approval
from src.modules.audit.schemas import MAX_DECISION_NOTE_LENGTH, ApprovalStatus
from tests import seed_support
from tests.api.vendor_portal_support import sign_in

pytestmark = [pytest.mark.api, pytest.mark.integration]

SETTINGS = "/audit/approval-settings"
DEFAULTS = {
    "po_dual_approval_amount": audit_service.DEFAULT_PO_DUAL_APPROVAL_AMOUNT,
    "adjustment_approval_threshold": audit_service.DEFAULT_ADJUSTMENT_APPROVAL_THRESHOLD,
    "internal_issue_approval_threshold": audit_service.DEFAULT_INTERNAL_ISSUE_APPROVAL_THRESHOLD,
    "requisition_auto_approve_limit": audit_service.DEFAULT_REQUISITION_AUTO_APPROVE_LIMIT,
}


# -----------------------------------------------------------------------------
# Envelope and row helpers
# -----------------------------------------------------------------------------


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


def thresholds(data: dict[str, Any]) -> dict[str, Decimal]:
    return {key: Decimal(str(data[key])) for key in DEFAULTS}


async def settings_audit(session: AsyncSession, company_id: uuid.UUID) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(
            AuditLog.company_id == company_id,
            AuditLog.entity_type == "company_approval_settings",
        )
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    return list((await session.scalars(stmt)).all())


async def request_state(session: AsyncSession, approval_id: uuid.UUID) -> tuple[Any, ...]:
    row = (
        await session.execute(
            select(
                ApprovalRequest.status, ApprovalRequest.decided_by_id, ApprovalRequest.decision_note
            ).where(ApprovalRequest.id == approval_id)
        )
    ).one()
    return tuple(row)


async def raise_request(
    session: AsyncSession,
    company_id: uuid.UUID,
    requested_by: uuid.UUID,
    *,
    permission: Permission = Permission.SALES_ORDER_APPROVE,
) -> uuid.UUID:
    """One PENDING request, opened the way an owning module opens it."""
    document_id = uuid.uuid4()
    record = await open_approval(
        session,
        tenant=TenantContext(company_id=company_id, user_id=requested_by),
        entity_type="sales_order",
        entity_id=document_id,
        entity_code=f"SO-{document_id.hex[:6].upper()}",
        approve_permission=permission,
        requested_by=requested_by,
        summary="Below-cost pricing needs a second signature",
        action_url=f"/sales-orders/{document_id}",
        amount=Decimal("1180"),
    )
    return record.id


@pytest.fixture
async def requester(session, tenant) -> uuid.UUID:
    """The colleague who raised the request: the caller is somebody else."""
    return (await seed_support.seed_user(session, company_id=tenant.company_id)).id


@pytest.fixture
async def pending(session, tenant, requester) -> uuid.UUID:
    return await raise_request(session, tenant.company_id, requester)


# -----------------------------------------------------------------------------
# PUT /audit/approval-settings
# -----------------------------------------------------------------------------


async def test_a_threshold_override_is_stored_audited_and_read_back(api_client, session, tenant):
    data = ok(await api_client.put(SETTINGS, json={"po_dual_approval_amount": "250000"}))

    assert thresholds(data) == {**DEFAULTS, "po_dual_approval_amount": Decimal("250000")}
    assert data["is_default"] is False
    read_back = ok(await api_client.get(SETTINGS))
    assert (thresholds(read_back), read_back["is_default"]) == (thresholds(data), False)
    [row] = (
        await session.scalars(
            select(CompanyApprovalSettings).where(
                CompanyApprovalSettings.company_id == tenant.company_id
            )
        )
    ).all()
    assert (row.po_dual_approval_amount, row.adjustment_approval_threshold) == (
        Decimal("250000"),
        None,
    )
    [entry] = await settings_audit(session, tenant.company_id)
    assert (entry.action, entry.created_by_id, entry.entity_id) == (
        AuditAction.CONFIG_CHANGE,
        tenant.user_id,
        row.id,
    )
    assert entry.old_values == {"po_dual_approval_amount": None}
    assert Decimal(entry.new_values["po_dual_approval_amount"]) == Decimal("250000")


async def test_an_explicit_null_restores_the_default_and_a_repeat_writes_nothing(
    api_client, session, tenant
):
    ok(await api_client.put(SETTINGS, json={"adjustment_approval_threshold": "5000"}))
    ok(await api_client.put(SETTINGS, json={"adjustment_approval_threshold": "5000.0000"}))
    ok(await api_client.put(SETTINGS, json={}))
    assert len(await settings_audit(session, tenant.company_id)) == 1

    restored = ok(await api_client.put(SETTINGS, json={"adjustment_approval_threshold": None}))

    assert thresholds(restored) == DEFAULTS
    assert restored["is_default"] is True
    trail = await settings_audit(session, tenant.company_id)
    assert len(trail) == 2
    assert trail[1].new_values == {"adjustment_approval_threshold": None}


async def test_restoring_defaults_on_a_company_that_never_overrode_them_stores_nothing(
    api_client, session, tenant
):
    data = ok(await api_client.put(SETTINGS, json={"requisition_auto_approve_limit": None}))

    assert data["is_default"] is True
    count = await session.scalar(
        select(func.count())
        .select_from(CompanyApprovalSettings)
        .where(CompanyApprovalSettings.company_id == tenant.company_id)
    )
    assert count == 0
    assert await settings_audit(session, tenant.company_id) == []


async def test_saving_this_companys_thresholds_leaves_another_companys_alone(
    api_client, session, tenant, other_company
):
    theirs = CompanyApprovalSettings(
        company_id=other_company.id, po_dual_approval_amount=Decimal("90000")
    )
    session.add(theirs)
    await session.flush()
    theirs_id = theirs.id

    data = ok(await api_client.put(SETTINGS, json={"po_dual_approval_amount": "100"}))

    assert Decimal(data["po_dual_approval_amount"]) == Decimal("100")
    untouched = await session.scalar(
        select(CompanyApprovalSettings.po_dual_approval_amount).where(
            CompanyApprovalSettings.id == theirs_id
        )
    )
    assert untouched == Decimal("90000")
    assert await settings_audit(session, other_company.id) == []


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"po_dual_approval_amount": "-1"}, "po_dual_approval_amount"),
        ({"internal_issue_approval_threshold": "1.00001"}, "internal_issue_approval_threshold"),
        ({"requisition_auto_approve_limit": "1e20"}, "requisition_auto_approve_limit"),
        ({"approval_chain": "CFO"}, "approval_chain"),
    ],
    ids=["negative", "beyond-scale", "beyond-precision", "unknown-setting"],
)
async def test_malformed_thresholds_are_field_errors_and_store_nothing(
    api_client, session, tenant, body, field
):
    assert field in field_errors(await api_client.put(SETTINGS, json=body))
    count = await session.scalar(
        select(func.count())
        .select_from(CompanyApprovalSettings)
        .where(CompanyApprovalSettings.company_id == tenant.company_id)
    )
    assert count == 0


# -----------------------------------------------------------------------------
# POST /audit/approvals/{approval_id}/approve | /reject
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(("action", "decision"), [("approve", "APPROVED"), ("reject", "REJECTED")])
async def test_a_detached_decision_is_refused_and_leaves_the_request_pending(
    api_client, session, tenant, pending, action, decision
):
    error = refused(
        await api_client.post(
            f"/audit/approvals/{pending}/{action}", json={"note": "Margin checked"}
        ),
        422,
        "BUSINESS_RULE_VIOLATION",
    )

    assert error["details"] == {
        "approval_request_id": str(pending),
        "requested_action": str(Permission.SALES_ORDER_APPROVE),
        "required_permission": str(Permission.SALES_ORDER_APPROVE),
        "decision": decision,
    }
    assert await request_state(session, pending) == (ApprovalStatus.PENDING.value, None, None)
    audited = await session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.entity_id == pending)
    )
    assert audited == 0


@pytest.mark.parametrize("body", [{}, {"note": "   "}, {"note": "no"}])
async def test_a_rejection_must_state_its_reason_before_anything_else_is_checked(
    api_client, session, pending, body
):
    error = refused(
        await api_client.post(f"/audit/approvals/{pending}/reject", json=body),
        422,
        "VALIDATION_ERROR",
    )

    assert error["details"]["field"] == "note"
    assert (await request_state(session, pending))[0] == ApprovalStatus.PENDING.value


async def test_a_decision_note_longer_than_the_column_is_a_field_error(api_client, pending):
    long_note = "x" * (MAX_DECISION_NOTE_LENGTH + 1)

    assert "note" in field_errors(
        await api_client.post(f"/audit/approvals/{pending}/approve", json={"note": long_note})
    )


async def test_the_person_who_raised_a_request_cannot_decide_it(api_client, session, tenant, user):
    own = await raise_request(session, tenant.company_id, user.id)

    for action in ("approve", "reject"):
        error = refused(
            await api_client.post(f"/audit/approvals/{own}/{action}", json={"note": "Self check"}),
            403,
            "APPROVAL_REQUIRED",
        )
        assert error["message"] == (
            "This request must be decided by someone other than the person who raised it."
        )
    assert (await request_state(session, own))[0] == ApprovalStatus.PENDING.value


async def test_an_approver_of_another_kind_is_told_which_right_the_request_needs(
    api_client, principal, pending
):
    sign_in(
        dataclasses.replace(
            principal,
            permissions=frozenset({str(Permission.PAYMENT_APPROVE)}),
            role_codes=("ACCOUNTANT",),
        )
    )

    error = refused(
        await api_client.post(f"/audit/approvals/{pending}/approve", json={}),
        403,
        "PERMISSION_DENIED",
    )

    assert error["details"]["required_permission"] == str(Permission.SALES_ORDER_APPROVE)


async def test_a_request_already_decided_by_its_workflow_cannot_be_decided_again(
    api_client, session, tenant, requester, pending
):
    await close_approval(
        session,
        tenant=tenant,
        entity_type="sales_order",
        entity_id=await session.scalar(
            select(ApprovalRequest.entity_id).where(ApprovalRequest.id == pending)
        ),
        requested_action=Permission.SALES_ORDER_APPROVE,
        outcome=ApprovalStatus.APPROVED,
        decided_by=tenant.user_id,
        decision_note="Approved on the order",
        action_url="/sales-orders/decided",
    )

    for action in ("approve", "reject"):
        refused(
            await api_client.post(f"/audit/approvals/{pending}/{action}", json={"note": "Again"}),
            422,
            "INVALID_STATE_TRANSITION",
        )
    assert await request_state(session, pending) == (
        ApprovalStatus.APPROVED.value,
        tenant.user_id,
        "Approved on the order",
    )


async def test_another_companys_request_is_not_found(api_client, session, other_company):
    their_requester = await seed_support.seed_user(session, company_id=other_company.id)
    theirs = await raise_request(session, other_company.id, their_requester.id)

    for action in ("approve", "reject"):
        refused(
            await api_client.post(f"/audit/approvals/{theirs}/{action}", json={"note": "Not ours"}),
            404,
            "NOT_FOUND",
        )
    assert (await request_state(session, theirs))[0] == ApprovalStatus.PENDING.value
