"""Payment follow-up write routes over HTTP, end to end on real rows.

``tests/api/test_collections_http.py`` pins the split between the view and
manage rights, the portal refusal and the ``customer.update`` gate on escalation;
the generic suites pin 401, 403 and unknown ids. None of them ran the five
follow-up write handlers to completion. This file opens, reassigns, promises,
resolves and cancels follow-ups and checks each answer, the row and the system
note left behind, the audit entry written beside them, the 422 a payload or a
closed follow-up earns, and the 404 another company's follow-up answers with.

Opening a follow-up needs money that really is overdue, so those tests use the
invoicing bench of ``test_sales_invoice_service`` and backdate the invoice it
issues, as that suite does. Every other route acts on the follow-up row alone,
so it is built through the ORM by ``test_collections_http``'s own builder: 250
INR overdue, oldest due ten days ago, OPEN.

Ids are held as plain values: an expected failure rolls its savepoint back and
expires whatever was loaded before it (CLAUDE.md).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today
from src.core.deps import Principal, TenantContext
from src.core.permissions import Permission
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.collections import service as collections_service
from src.modules.collections.models import (
    FollowUpChannel,
    FollowUpOutcome,
    FollowUpStatus,
    PaymentFollowUp,
    PaymentFollowUpNote,
)
from src.modules.collections.schemas import (
    FollowUpCancel,
    FollowUpCreate,
    FollowUpEscalate,
    FollowUpPromise,
    FollowUpResolve,
)
from src.modules.finance.models import SalesInvoice
from src.workers import celery_app as celery_module
from tests import seed_support
from tests.api.dealer_portal_support import sign_in
from tests.collections_support import _follow_up
from tests.sales_invoice_support import bench as bench
from tests.sales_invoice_support import issue, make_dispatch

pytestmark = [pytest.mark.api, pytest.mark.integration]

#: What ``_follow_up`` records as overdue.
OVERDUE = Decimal("250")
SOON = timedelta(days=5)


# -----------------------------------------------------------------------------
# Envelope and row helpers
# -----------------------------------------------------------------------------


def ok(response: Response, status: int = 200) -> Any:
    """The success envelope's ``data``, after checking the status and the flag."""
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is True, body
    assert body["message"], body
    return body["data"]


def refused(response: Response, status: int, code: str) -> dict[str, Any]:
    """The error envelope's ``error``, after checking the status and the code."""
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is False, body
    assert body["error"]["code"] == code, body["error"]
    return body["error"]


def field_errors(response: Response) -> dict[str, list[str]]:
    """The per-field messages of a 422 request-validation refusal."""
    return refused(response, 422, "VALIDATION_ERROR")["details"]["fields"]


def money(value: object) -> Decimal:
    return Decimal(str(value))


async def column(session: AsyncSession, attribute: Any, follow_up_id: uuid.UUID) -> Any:
    """One column of the follow-up, read from the database, not the identity map."""
    return await session.scalar(select(attribute).where(PaymentFollowUp.id == follow_up_id))


async def audit_trail(
    session: AsyncSession, follow_up_id: uuid.UUID, action: AuditAction
) -> list[AuditLog]:
    # (created_at, id): rows written in one transaction share now().
    stmt = (
        select(AuditLog)
        .where(
            AuditLog.entity_id == follow_up_id,
            AuditLog.entity_type == "payment_follow_up",
            AuditLog.action == action,
        )
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    return list((await session.scalars(stmt)).all())


async def system_notes(session: AsyncSession, follow_up_id: uuid.UUID) -> list[PaymentFollowUpNote]:
    stmt = (
        select(PaymentFollowUpNote)
        .where(
            PaymentFollowUpNote.follow_up_id == follow_up_id,
            PaymentFollowUpNote.channel == FollowUpChannel.SYSTEM,
        )
        .order_by(PaymentFollowUpNote.created_at, PaymentFollowUpNote.id)
    )
    return list((await session.scalars(stmt)).all())


async def follow_ups_of(session: AsyncSession, customer_id: uuid.UUID) -> int:
    return await session.scalar(
        select(func.count()).where(PaymentFollowUp.customer_id == customer_id)
    )


def moved(entry: AuditLog) -> tuple[Any, Any]:
    return (entry.old_values or {}).get("status"), (entry.new_values or {}).get("status")


# -----------------------------------------------------------------------------
# Fixtures and builders
# -----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Notification fan-out reaches Celery through ``send_task``; keep it in-process."""
    monkeypatch.setattr(
        celery_module.celery_app,
        "send_task",
        lambda *_, **__: SimpleNamespace(id=uuid.uuid4().hex),
    )


@pytest.fixture
async def customer_id(session: AsyncSession, tenant: TenantContext, unique_code) -> uuid.UUID:
    """A customer that owes nothing."""
    customer = await seed_support.seed_customer(
        session, company_id=tenant.company_id, code=unique_code("CU")
    )
    return customer.id


@pytest.fixture
async def follow_up(session: AsyncSession, tenant: TenantContext, customer_id) -> uuid.UUID:
    """An OPEN follow-up with 250 INR overdue, as the row alone."""
    return (await _follow_up(session, company_id=tenant.company_id, customer_id=customer_id)).id


@pytest.fixture
async def outsider(session: AsyncSession, other_company) -> Principal:
    """A fully entitled staff user of another company."""
    user = await seed_support.seed_user(session, company_id=other_company.id)
    return Principal(
        user_id=user.id,
        company_id=other_company.id,
        email=user.email,
        full_name=user.full_name,
        is_superuser=False,
        permissions=frozenset(str(p) for p in Permission),
        role_codes=("OUTSIDER",),
    )


async def overdue_invoice(b: SimpleNamespace) -> SimpleNamespace:
    """Invoice the bench's 200 units and backdate it ten days past due."""
    invoice = await issue(b, await make_dispatch(b))
    row = await b.session.get(SalesInvoice, invoice.id)
    row.invoice_date = today() - timedelta(days=40)
    row.due_date = today() - timedelta(days=10)
    await b.session.flush()
    return SimpleNamespace(
        id=invoice.id,
        total=invoice.total_amount,
        currency=invoice.currency,
        due_date=row.due_date,
        customer_id=b.customer.id,
    )


async def promised(session: AsyncSession, tenant: TenantContext, follow_up_id: uuid.UUID) -> None:
    await collections_service.record_promise(
        session,
        tenant,
        follow_up_id,
        FollowUpPromise(promised_amount=Decimal("100"), promised_date=today() + SOON),
    )


def path(follow_up_id: uuid.UUID, action: str = "") -> str:
    return f"/collections/follow-ups/{follow_up_id}" + (f"/{action}" if action else "")


# -----------------------------------------------------------------------------
# POST /collections/follow-ups
# -----------------------------------------------------------------------------


class TestOpeningAFollowUp:
    async def test_an_overdue_customer_is_chased_with_its_invoices_listed(
        self, api_client, session, tenant, bench
    ):
        invoice = await overdue_invoice(bench)
        next_action = today() + timedelta(days=2)

        response = await api_client.post(
            "/collections/follow-ups",
            json={
                "customer_id": str(invoice.customer_id),
                "currency": invoice.currency.lower(),
                "assigned_to_id": str(tenant.user_id),
                "next_action_date": next_action.isoformat(),
            },
        )

        data = ok(response, 201)
        assert data["status"] == "OPEN"
        assert data["code"]
        assert data["currency"] == invoice.currency
        assert data["customer_id"] == str(invoice.customer_id)
        assert data["assigned_to_id"] == str(tenant.user_id)
        assert data["next_action_date"] == next_action.isoformat()
        assert money(data["opened_overdue_amount"]) == invoice.total
        assert money(data["current_overdue_amount"]) == invoice.total
        assert data["oldest_due_date"] == invoice.due_date.isoformat()
        [row] = data["invoices"]
        assert row["id"] == str(invoice.id)
        assert row["overdue_days"] == 10
        follow_up_id = uuid.UUID(data["id"])
        assert await column(session, PaymentFollowUp.status, follow_up_id) == FollowUpStatus.OPEN
        assert len(await system_notes(session, follow_up_id)) == 1
        [entry] = await audit_trail(session, follow_up_id, AuditAction.CREATE)
        assert entry.created_by_id == tenant.user_id
        assert entry.new_values["currency"] == invoice.currency
        assert money(entry.new_values["overdue_amount"]) == invoice.total

    async def test_a_second_live_follow_up_for_the_same_money_is_a_conflict(
        self, api_client, session, tenant, bench
    ):
        invoice = await overdue_invoice(bench)
        existing = await collections_service.create_follow_up(
            session,
            tenant,
            FollowUpCreate(customer_id=invoice.customer_id, currency=invoice.currency),
        )
        existing_id = existing.id

        response = await api_client.post(
            "/collections/follow-ups",
            json={"customer_id": str(invoice.customer_id), "currency": invoice.currency},
        )

        error = refused(response, 409, "DUPLICATE_DOCUMENT")
        assert error["details"]["follow_up_id"] == str(existing_id)
        assert await follow_ups_of(session, invoice.customer_id) == 1

    async def test_a_customer_with_nothing_overdue_cannot_be_chased(
        self, api_client, session, customer_id
    ):
        response = await api_client.post(
            "/collections/follow-ups", json={"customer_id": str(customer_id), "currency": "INR"}
        )

        error = refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert error["details"] == {"customer_id": str(customer_id), "currency": "INR"}
        assert await follow_ups_of(session, customer_id) == 0

    async def test_a_currency_that_is_not_an_iso_code_is_refused(
        self, api_client, session, customer_id
    ):
        response = await api_client.post(
            "/collections/follow-ups", json={"customer_id": str(customer_id), "currency": "1NR"}
        )

        assert refused(response, 422, "BUSINESS_RULE_VIOLATION")["details"] == {"currency": "1NR"}
        assert await follow_ups_of(session, customer_id) == 0

    async def test_an_unknown_assignee_is_not_found(self, api_client, session, customer_id):
        response = await api_client.post(
            "/collections/follow-ups",
            json={
                "customer_id": str(customer_id),
                "currency": "INR",
                "assigned_to_id": str(uuid.uuid4()),
            },
        )

        refused(response, 404, "NOT_FOUND")
        assert await follow_ups_of(session, customer_id) == 0

    async def test_a_customer_of_another_company_is_not_found(
        self, api_client, session, other_company, unique_code
    ):
        theirs = await seed_support.seed_customer(
            session, company_id=other_company.id, code=unique_code("CU")
        )
        theirs_id = theirs.id

        response = await api_client.post(
            "/collections/follow-ups", json={"customer_id": str(theirs_id), "currency": "INR"}
        )

        refused(response, 404, "NOT_FOUND")
        assert await follow_ups_of(session, theirs_id) == 0

    @pytest.mark.parametrize(
        ("change", "field"),
        [
            ({"currency": "RUPEE"}, "currency"),
            ({"customer_id": "CU-1"}, "customer_id"),
            ({"next_action_date": "soon"}, "next_action_date"),
            ({"status": "PROMISED"}, "status"),
        ],
        ids=["long-currency", "not-a-uuid", "not-a-date", "status-is-not-a-field"],
    )
    async def test_a_malformed_follow_up_is_a_field_error(
        self, api_client, session, customer_id, change, field
    ):
        body = {"customer_id": str(customer_id), "currency": "INR", **change}

        assert field in field_errors(await api_client.post("/collections/follow-ups", json=body))
        assert await follow_ups_of(session, customer_id) == 0


# -----------------------------------------------------------------------------
# PATCH /collections/follow-ups/{follow_up_id}
# -----------------------------------------------------------------------------


class TestUpdatingAFollowUp:
    async def test_a_follow_up_is_reassigned_and_rescheduled_with_a_diff(
        self, api_client, session, tenant, follow_up
    ):
        colleague = await seed_support.seed_user(
            session, company_id=tenant.company_id, full_name="Credit Controller"
        )
        colleague_id = colleague.id
        next_action = today() + timedelta(days=3)

        data = ok(
            await api_client.patch(
                path(follow_up),
                json={
                    "assigned_to_id": str(colleague_id),
                    "next_action_date": next_action.isoformat(),
                },
            )
        )

        assert data["assigned_to_id"] == str(colleague_id)
        assert data["assigned_to_name"] == "Credit Controller"
        assert data["next_action_date"] == next_action.isoformat()
        assert data["status"] == "OPEN"
        assert await column(session, PaymentFollowUp.assigned_to_id, follow_up) == colleague_id
        [entry] = await audit_trail(session, follow_up, AuditAction.UPDATE)
        assert entry.changed_fields == ["assigned_to_id", "next_action_date"]
        assert entry.new_values == {
            "assigned_to_id": str(colleague_id),
            "next_action_date": next_action.isoformat(),
        }

    async def test_only_the_fields_sent_change_and_a_no_op_is_not_audited(
        self, api_client, session, tenant, follow_up
    ):
        next_action = today() + timedelta(days=3)
        ok(
            await api_client.patch(
                path(follow_up),
                json={
                    "assigned_to_id": str(tenant.user_id),
                    "next_action_date": next_action.isoformat(),
                },
            )
        )

        cleared = ok(await api_client.patch(path(follow_up), json={"assigned_to_id": None}))
        repeated = ok(await api_client.patch(path(follow_up), json={"assigned_to_id": None}))

        assert cleared["assigned_to_id"] is None
        assert cleared["next_action_date"] == next_action.isoformat()
        assert repeated["assigned_to_id"] is None
        assert await column(session, PaymentFollowUp.next_action_date, follow_up) == next_action
        updates = await audit_trail(session, follow_up, AuditAction.UPDATE)
        assert [entry.changed_fields for entry in updates] == [
            ["assigned_to_id", "next_action_date"],
            ["assigned_to_id"],
        ]

    @pytest.mark.parametrize("kind", ["unknown", "inactive", "another-company"])
    async def test_an_assignee_who_cannot_take_the_work_is_not_found(
        self, api_client, session, tenant, other_company, follow_up, kind
    ):
        if kind == "unknown":
            assignee_id = uuid.uuid4()
        else:
            company_id = other_company.id if kind == "another-company" else tenant.company_id
            assignee = await seed_support.seed_user(
                session, company_id=company_id, is_active=kind != "inactive"
            )
            assignee_id = assignee.id

        response = await api_client.patch(
            path(follow_up), json={"assigned_to_id": str(assignee_id)}
        )

        refused(response, 404, "NOT_FOUND")
        assert await column(session, PaymentFollowUp.assigned_to_id, follow_up) is None
        assert await audit_trail(session, follow_up, AuditAction.UPDATE) == []

    async def test_a_closed_follow_up_cannot_be_changed(
        self, api_client, session, tenant, follow_up
    ):
        await collections_service.resolve(
            session, tenant, follow_up, FollowUpResolve(note="Settled by NEFT")
        )

        response = await api_client.patch(
            path(follow_up), json={"next_action_date": today().isoformat()}
        )

        assert refused(response, 422, "BUSINESS_RULE_VIOLATION")["details"] == {
            "status": "RESOLVED"
        }
        assert await column(session, PaymentFollowUp.next_action_date, follow_up) is None

    async def test_another_companys_follow_up_is_not_found(
        self, api_client, session, follow_up, outsider
    ):
        sign_in(outsider)

        response = await api_client.patch(
            path(follow_up), json={"next_action_date": today().isoformat()}
        )

        refused(response, 404, "NOT_FOUND")
        assert await column(session, PaymentFollowUp.next_action_date, follow_up) is None

    @pytest.mark.parametrize(
        ("body", "field"),
        [
            ({"assigned_to_id": "nobody"}, "assigned_to_id"),
            ({"next_action_date": "2026-13-01"}, "next_action_date"),
            ({"status": "RESOLVED"}, "status"),
        ],
        ids=["not-a-uuid", "not-a-date", "status-is-not-a-field"],
    )
    async def test_a_malformed_update_is_a_field_error(
        self, api_client, session, follow_up, body, field
    ):
        assert field in field_errors(await api_client.patch(path(follow_up), json=body))
        assert await column(session, PaymentFollowUp.status, follow_up) == FollowUpStatus.OPEN


# -----------------------------------------------------------------------------
# POST /collections/follow-ups/{follow_up_id}/promise
# -----------------------------------------------------------------------------


class TestPromises:
    async def test_a_dated_promise_moves_the_follow_up_to_promised(
        self, api_client, session, tenant, follow_up
    ):
        promise_date = today() + SOON

        data = ok(
            await api_client.post(
                path(follow_up, "promise"),
                json={"promised_amount": "100", "promised_date": promise_date.isoformat()},
            )
        )

        assert data["status"] == "PROMISED"
        assert money(data["promised_amount"]) == Decimal("100")
        assert data["promised_date"] == promise_date.isoformat()
        assert data["next_action_date"] == promise_date.isoformat()
        assert await column(session, PaymentFollowUp.promised_date, follow_up) == promise_date
        [note] = await system_notes(session, follow_up)
        assert note.outcome == FollowUpOutcome.PROMISED
        assert note.next_action_date == promise_date
        [entry] = await audit_trail(session, follow_up, AuditAction.STATUS_CHANGE)
        assert moved(entry) == ("OPEN", "PROMISED")
        assert entry.created_by_id == tenant.user_id
        assert entry.context["reason"]

    async def test_a_repeated_promise_extends_it_and_is_audited_as_an_update(
        self, api_client, session, tenant, follow_up
    ):
        await promised(session, tenant, follow_up)
        later = today() + SOON + timedelta(days=3)

        data = ok(
            await api_client.post(
                path(follow_up, "promise"),
                json={"promised_amount": "80", "promised_date": later.isoformat()},
            )
        )

        assert data["status"] == "PROMISED"
        assert money(data["promised_amount"]) == Decimal("80")
        assert data["promised_date"] == later.isoformat()
        assert len(await audit_trail(session, follow_up, AuditAction.STATUS_CHANGE)) == 1
        [entry] = await audit_trail(session, follow_up, AuditAction.UPDATE)
        assert money(entry.new_values["promised_amount"]) == Decimal("80")
        assert entry.new_values["promised_date"] == later.isoformat()

    @pytest.mark.parametrize(
        ("amount", "days_ahead", "detail"),
        [("100", -1, "promised_date"), (str(OVERDUE + 1), 5, "overdue_amount")],
        ids=["dated-in-the-past", "more-than-is-overdue"],
    )
    async def test_a_promise_that_cannot_be_kept_is_refused(
        self, api_client, session, follow_up, amount, days_ahead, detail
    ):
        response = await api_client.post(
            path(follow_up, "promise"),
            json={
                "promised_amount": amount,
                "promised_date": (today() + timedelta(days=days_ahead)).isoformat(),
            },
        )

        assert detail in refused(response, 422, "BUSINESS_RULE_VIOLATION")["details"]
        assert await column(session, PaymentFollowUp.status, follow_up) == FollowUpStatus.OPEN
        assert await column(session, PaymentFollowUp.promised_amount, follow_up) is None

    async def test_an_escalated_follow_up_cannot_take_a_promise(
        self, api_client, session, tenant, principal, follow_up
    ):
        await collections_service.escalate(
            session,
            tenant,
            principal,
            follow_up,
            FollowUpEscalate(reason="Three promises broken"),
        )

        response = await api_client.post(
            path(follow_up, "promise"),
            json={"promised_amount": "100", "promised_date": (today() + SOON).isoformat()},
        )

        error = refused(response, 422, "INVALID_STATE_TRANSITION")
        assert error["details"]["current_status"] == "ESCALATED"
        assert await column(session, PaymentFollowUp.promised_amount, follow_up) is None

    async def test_a_cancelled_follow_up_cannot_take_a_promise(
        self, api_client, session, tenant, follow_up
    ):
        await collections_service.cancel(
            session, tenant, follow_up, FollowUpCancel(reason="Opened in error")
        )

        response = await api_client.post(
            path(follow_up, "promise"),
            json={"promised_amount": "100", "promised_date": (today() + SOON).isoformat()},
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await column(session, PaymentFollowUp.promised_amount, follow_up) is None

    @pytest.mark.parametrize(
        ("body", "field"),
        [
            ({"promised_amount": "0", "promised_date": "2026-12-01"}, "promised_amount"),
            ({"promised_amount": "100"}, "promised_date"),
            ({"promised_amount": "1.23456", "promised_date": "2026-12-01"}, "promised_amount"),
        ],
        ids=["zero-amount", "no-date", "five-decimal-places"],
    )
    async def test_a_malformed_promise_is_a_field_error(
        self, api_client, session, follow_up, body, field
    ):
        assert field in field_errors(await api_client.post(path(follow_up, "promise"), json=body))
        assert await column(session, PaymentFollowUp.status, follow_up) == FollowUpStatus.OPEN


# -----------------------------------------------------------------------------
# POST /collections/follow-ups/{follow_up_id}/resolve
# -----------------------------------------------------------------------------


class TestResolving:
    async def test_resolving_closes_the_follow_up_and_retires_its_promise(
        self, api_client, session, tenant, follow_up
    ):
        await promised(session, tenant, follow_up)
        note = "Paid in full by NEFT"

        data = ok(await api_client.post(path(follow_up, "resolve"), json={"note": note}))

        assert data["status"] == "RESOLVED"
        assert data["resolution_note"] == note
        assert data["resolved_by_id"] == str(tenant.user_id)
        assert data["resolved_at"] is not None
        assert data["promised_amount"] is None
        assert data["promised_date"] is None
        assert await column(session, PaymentFollowUp.status, follow_up) == FollowUpStatus.RESOLVED
        assert (await system_notes(session, follow_up))[-1].note.endswith(note)
        moves = await audit_trail(session, follow_up, AuditAction.STATUS_CHANGE)
        assert [moved(entry) for entry in moves] == [("OPEN", "PROMISED"), ("PROMISED", "RESOLVED")]
        assert moves[-1].context["reason"] == note

    async def test_a_resolved_follow_up_cannot_be_resolved_again(
        self, api_client, session, tenant, follow_up
    ):
        await collections_service.resolve(
            session, tenant, follow_up, FollowUpResolve(note="Paid in full")
        )

        response = await api_client.post(
            path(follow_up, "resolve"), json={"note": "Paid twice over"}
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await column(session, PaymentFollowUp.resolution_note, follow_up) == "Paid in full"
        assert len(await audit_trail(session, follow_up, AuditAction.STATUS_CHANGE)) == 1

    async def test_a_resolution_needs_a_note(self, api_client, session, follow_up):
        response = await api_client.post(path(follow_up, "resolve"), json={"note": "ok"})

        assert "note" in field_errors(response)
        assert await column(session, PaymentFollowUp.status, follow_up) == FollowUpStatus.OPEN

    async def test_another_companys_follow_up_is_not_found(
        self, api_client, session, follow_up, outsider
    ):
        sign_in(outsider)

        response = await api_client.post(path(follow_up, "resolve"), json={"note": "Settled"})

        refused(response, 404, "NOT_FOUND")
        assert await column(session, PaymentFollowUp.status, follow_up) == FollowUpStatus.OPEN


# -----------------------------------------------------------------------------
# POST /collections/follow-ups/{follow_up_id}/cancel
# -----------------------------------------------------------------------------


class TestCancelling:
    async def test_cancelling_calls_off_an_open_follow_up(
        self, api_client, session, tenant, follow_up
    ):
        reason = "Opened against the wrong customer"

        data = ok(await api_client.post(path(follow_up, "cancel"), json={"reason": reason}))

        assert data["status"] == "CANCELLED"
        assert data["cancellation_reason"] == reason
        assert await column(session, PaymentFollowUp.status, follow_up) == (
            FollowUpStatus.CANCELLED
        )
        [note] = await system_notes(session, follow_up)
        assert note.note.endswith(reason)
        [entry] = await audit_trail(session, follow_up, AuditAction.CANCEL)
        assert moved(entry) == ("OPEN", "CANCELLED")
        assert entry.context["reason"] == reason
        assert entry.created_by_id == tenant.user_id

    async def test_a_follow_up_holding_a_promise_cannot_be_cancelled(
        self, api_client, session, tenant, follow_up
    ):
        await promised(session, tenant, follow_up)

        response = await api_client.post(
            path(follow_up, "cancel"), json={"reason": "Opened in error"}
        )

        error = refused(response, 422, "INVALID_STATE_TRANSITION")
        assert error["details"]["current_status"] == "PROMISED"
        assert await column(session, PaymentFollowUp.status, follow_up) == FollowUpStatus.PROMISED
        assert await column(session, PaymentFollowUp.promised_amount, follow_up) == 100

    async def test_a_cancelled_follow_up_cannot_be_cancelled_again(
        self, api_client, session, tenant, follow_up
    ):
        await collections_service.cancel(
            session, tenant, follow_up, FollowUpCancel(reason="Opened in error")
        )

        response = await api_client.post(
            path(follow_up, "cancel"), json={"reason": "A second story"}
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await column(session, PaymentFollowUp.cancellation_reason, follow_up) == (
            "Opened in error"
        )

    async def test_a_cancellation_needs_a_reason(self, api_client, session, follow_up):
        response = await api_client.post(path(follow_up, "cancel"), json={"reason": "no"})

        assert "reason" in field_errors(response)
        assert await column(session, PaymentFollowUp.status, follow_up) == FollowUpStatus.OPEN

    async def test_another_companys_follow_up_is_not_found(
        self, api_client, session, follow_up, outsider
    ):
        sign_in(outsider)

        response = await api_client.post(path(follow_up, "cancel"), json={"reason": "From outside"})

        refused(response, 404, "NOT_FOUND")
        assert await column(session, PaymentFollowUp.status, follow_up) == FollowUpStatus.OPEN
