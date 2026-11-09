"""Job work write routes over HTTP.

Service rules - stock conservation across a vendor's premises, the yield ratio,
loss allowances, the challan and the one QC release path - are pinned in
``tests/integration/test_job_work_service.py``. These drive the eight job-work
routes no backend test had reached through HTTP: the status and envelope each
answers with, the row, reservation, ledger entry and audit row it leaves
behind, the 422 a payload or an order's state earns, and the 404 another
company's order or vendor answers with - never a 403, which would confirm that
it exists.

Permission gates on the routes themselves are covered by
``tests/unit/test_route_authorization.py``; the caller is the ``api_client``
principal, holding every permission. The job is the integration suite's own
worked example (``PvdJob``): a hundred raw handles sent to an approved plating
vendor at 12.50 a piece and returned as coated handles, built through its ORM
seeders and walked forward through the job-work service. Ids are held as plain
values: an expected failure rolls its savepoint back and expires what was
loaded before it.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select, update
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today
from src.core.deps import Principal, TenantContext
from src.core.permissions import Permission
from src.modules.allocation.models import ReservationStatus, StockReservation
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.inventory.models import InventoryBalance, InventoryTransaction
from src.modules.job_work import service
from src.modules.job_work.models import (
    JobWorkChallan,
    JobWorkOrder,
    JobWorkOrderLine,
    JobWorkReceipt,
)
from src.modules.job_work.schemas import ReceiptLineIn, ReceiveRequest
from src.workers import celery_app as celery_module
from tests import seed_support
from tests.integration.test_job_work_service import (
    PvdJob,
    issued_order,
    stock_in,
)
from tests.integration.test_job_work_service import seed_item as seed_job_work_item
from tests.integration.test_job_work_service import seed_uom as seed_job_work_uom
from tests.integration.test_job_work_service import seed_vendor as seed_job_work_vendor
from tests.integration.test_job_work_service import (
    seed_warehouse as seed_job_work_warehouse,
)

pytestmark = [pytest.mark.api, pytest.mark.integration]

REASON = "Rate agreed with the vendor"


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def broker(monkeypatch) -> list[str]:
    """Every task handed to Celery - nothing reaches a broker."""
    sent: list[str] = []

    def accepted(name: str, **_: Any) -> Any:
        sent.append(name)
        return SimpleNamespace(id=uuid.uuid4().hex)

    monkeypatch.setattr(celery_module.celery_app, "send_task", accepted)
    return sent


async def build_job(session: AsyncSession, company_id: uuid.UUID, *, approved: bool = True):
    uom_id = await seed_job_work_uom(session, company_id)
    return PvdJob(
        uom_id=uom_id,
        raw_item_id=await seed_job_work_item(
            session, company_id, uom_id, code=f"RAW-{uuid.uuid4().hex[:8].upper()}"
        ),
        coated_item_id=await seed_job_work_item(
            session,
            company_id,
            uom_id,
            code=f"PVD-{uuid.uuid4().hex[:8].upper()}",
            standard_cost="130",
        ),
        warehouse_id=await seed_job_work_warehouse(session, company_id),
        vendor_id=await seed_job_work_vendor(session, company_id, approved=approved),
    )


@pytest.fixture
async def job(session: AsyncSession, tenant: TenantContext) -> PvdJob:
    return await build_job(session, tenant.company_id)


@pytest.fixture
async def foreign(session: AsyncSession, other_company) -> TenantContext:
    owner = await seed_support.seed_user(session, company_id=other_company.id)
    return TenantContext(company_id=other_company.id, user_id=owner.id)


# -----------------------------------------------------------------------------
# Envelope readers
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


def holding(base: Principal, *permissions: Permission) -> Principal:
    return dataclasses.replace(base, permissions=frozenset(str(p) for p in permissions))


def act_as(principal: Principal) -> None:
    from src.core.deps import get_current_principal
    from src.main import app

    app.dependency_overrides[get_current_principal] = lambda: principal


# -----------------------------------------------------------------------------
# Builders and database readers
# -----------------------------------------------------------------------------


def create_body(job: PvdJob, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "vendor_id": str(job.vendor_id),
        "process_type": "PVD",
        "process_description": "PVD, rose gold, 3 micron",
        "expected_return_date": (today() + timedelta(days=14)).isoformat(),
        "warehouse_id": str(job.warehouse_id),
        "rate_per_unit": "12.50",
        "lines": [
            {
                "line_number": 1,
                "item_id": str(job.raw_item_id),
                "quantity_to_issue": "100",
                "expected_output_item_id": str(job.coated_item_id),
            }
        ],
    }
    body.update(overrides)
    return body


def receipt_body(output: str = "100", *, is_final: bool = True, **fields: Any) -> dict:
    return {
        "reason": "Back from plating and counted",
        "lines": [{"line_number": 1, "output_quantity": output, "is_final": is_final}],
        **fields,
    }


async def drafted(
    session: AsyncSession, tenant: TenantContext, job: PvdJob, **payload
) -> uuid.UUID:
    order, _ = await service.create_order(session, tenant, job.payload(**payload))
    return order.id


async def approved(
    session: AsyncSession, tenant: TenantContext, job: PvdJob, **payload
) -> uuid.UUID:
    await stock_in(session, tenant, job.raw_item_id, job.warehouse_id, "100")
    order_id = await drafted(session, tenant, job, **payload)
    await service.approve_order(session, tenant, order_id, reason=REASON)
    return order_id


async def issued(session: AsyncSession, tenant: TenantContext, job: PvdJob, **payload) -> uuid.UUID:
    order, _ = await issued_order((session, tenant), job, **payload)
    return order.id


async def received(
    session: AsyncSession, tenant: TenantContext, principal: Principal, job: PvdJob, **payload
) -> uuid.UUID:
    order_id = await issued(session, tenant, job, **payload)
    await service.receive(
        session,
        tenant,
        principal,
        order_id,
        ReceiveRequest(
            reason="Back from plating",
            lines=[ReceiptLineIn(line_number=1, output_quantity=Decimal("100"), is_final=True)],
        ),
    )
    return order_id


async def column(session: AsyncSession, attribute: Any, row_id: uuid.UUID) -> Any:
    """One stored value, read past the identity map."""
    return await session.scalar(select(attribute).where(attribute.class_.id == row_id))


async def stock_at(
    session: AsyncSession, company_id: uuid.UUID, item_id: uuid.UUID, warehouse_id: uuid.UUID
) -> Row[Any]:
    stmt = select(
        InventoryBalance.on_hand_quantity,
        InventoryBalance.reserved_quantity,
        InventoryBalance.job_work_quantity,
        InventoryBalance.qc_hold_quantity,
        InventoryBalance.damaged_quantity,
        InventoryBalance.available_quantity,
    ).where(
        InventoryBalance.company_id == company_id,
        InventoryBalance.item_id == item_id,
        InventoryBalance.warehouse_id == warehouse_id,
        InventoryBalance.location_id.is_(None),
        InventoryBalance.batch_id.is_(None),
    )
    return (await session.execute(stmt)).one()


async def reservations_for(session: AsyncSession, order_id: uuid.UUID) -> list[Row[Any]]:
    stmt = (
        select(
            StockReservation.id,
            StockReservation.quantity,
            StockReservation.status,
            StockReservation.demand_type,
        )
        .where(StockReservation.demand_id == order_id)
        .order_by(StockReservation.created_at, StockReservation.id)
    )
    return list((await session.execute(stmt)).all())


async def count_orders(session: AsyncSession, company_id: uuid.UUID) -> int:
    stmt = (
        select(func.count()).select_from(JobWorkOrder).where(JobWorkOrder.company_id == company_id)
    )
    return int((await session.execute(stmt)).scalar_one())


async def audit_rows(
    session: AsyncSession,
    company_id: uuid.UUID,
    entity_id: uuid.UUID,
    action: AuditAction | None = None,
) -> list[Row[Any]]:
    stmt = (
        select(
            AuditLog.action,
            AuditLog.entity_type,
            AuditLog.created_by_id,
            AuditLog.old_values,
            AuditLog.new_values,
            AuditLog.changed_fields,
            AuditLog.context,
        )
        .where(AuditLog.company_id == company_id, AuditLog.entity_id == entity_id)
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list((await session.execute(stmt)).all())


def moves(rows: list[Row[Any]]) -> list[tuple[str, str]]:
    return [
        (row.old_values["status"], row.new_values["status"])
        for row in rows
        if row.old_values and "status" in row.old_values and len(row.old_values) == 1
    ]


# -----------------------------------------------------------------------------
# POST /job-work
# -----------------------------------------------------------------------------


class TestCreateOrder:
    async def test_a_draft_is_raised_with_its_charge_frozen_and_nothing_reserved(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        data = ok(await api_client.post("/job-work", json=create_body(job)), 201)

        assert data["status"] == "DRAFT"
        assert data["vendor_id"] == str(job.vendor_id)
        assert data["return_warehouse_id"] == str(job.warehouse_id)
        assert Decimal(data["estimated_charges"]) == Decimal("1250")
        assert (Decimal(data["tax_rate"]), data["sac_code"]) == (Decimal("18"), "9988")
        (line,) = data["lines"]
        assert line["expected_output_item_id"] == str(job.coated_item_id)
        assert Decimal(line["quantity_to_issue"]) == Decimal("100")
        assert Decimal(line["expected_output_quantity"]) == Decimal("100")
        assert line["reservation_id"] is None
        order_id = uuid.UUID(data["id"])
        assert await column(session, JobWorkOrder.company_id, order_id) == tenant.company_id
        assert await reservations_for(session, order_id) == []
        (created,) = await audit_rows(session, tenant.company_id, order_id, AuditAction.CREATE)
        assert created.entity_type == "job_work_order"
        assert created.created_by_id == tenant.user_id
        read = ok(await api_client.get(f"/job-work/{order_id}"))
        assert read["code"] == data["code"]

    async def test_an_expected_return_in_the_past_is_refused(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        yesterday = (today() - timedelta(days=1)).isoformat()

        response = await api_client.post(
            "/job-work", json=create_body(job, expected_return_date=yesterday)
        )

        # Named as a field error, by the same check the draft edit applies.
        assert "expected_return_date" in field_errors(response)
        assert await count_orders(session, tenant.company_id) == 0

    async def test_an_unapproved_vendor_cannot_be_sent_material(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        pending = await seed_job_work_vendor(session, tenant.company_id, approved=False)

        response = await api_client.post("/job-work", json=create_body(job, vendor_id=str(pending)))

        refused(response, 422, "APPROVAL_REQUIRED")
        assert await count_orders(session, tenant.company_id) == 0

    async def test_an_item_not_flagged_for_job_work_is_refused(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        bought = await seed_job_work_item(
            session,
            tenant.company_id,
            job.uom_id,
            code=f"BUY-{uuid.uuid4().hex[:8]}",
            can_job_work=False,
        )
        body = create_body(job)
        body["lines"][0]["item_id"] = str(bought)

        response = await api_client.post("/job-work", json=body)

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await count_orders(session, tenant.company_id) == 0

    @pytest.mark.parametrize("reference", ["vendor_id", "warehouse_id"])
    async def test_another_companys_vendor_or_store_is_not_found(
        self, api_client: AsyncClient, session, tenant, job, foreign, reference
    ) -> None:
        theirs = await build_job(session, foreign.company_id)
        their_id = theirs.vendor_id if reference == "vendor_id" else theirs.warehouse_id

        response = await api_client.post(
            "/job-work", json=create_body(job, **{reference: str(their_id)})
        )

        refused(response, 404, "NOT_FOUND")
        assert await count_orders(session, tenant.company_id) == 0

    @pytest.mark.parametrize("whose", ["another-companys-order", "no-such-order"])
    async def test_a_sales_order_this_company_does_not_hold_is_not_found(
        self, api_client: AsyncClient, session, tenant, job, foreign, whose
    ) -> None:
        source_id = (
            (await seed_support.seed_sales_order(session, company_id=foreign.company_id)).id
            if whose == "another-companys-order"
            else uuid.uuid4()
        )

        response = await api_client.post(
            "/job-work",
            json=create_body(job, source_type="SALES_ORDER", source_id=str(source_id)),
        )

        error = refused(response, 404, "NOT_FOUND")
        assert error["message"].startswith("Sales order not found")
        assert await count_orders(session, tenant.company_id) == 0

    async def test_this_companys_sales_order_is_kept_as_the_source(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        ours = (await seed_support.seed_sales_order(session, company_id=tenant.company_id)).id

        data = ok(
            await api_client.post(
                "/job-work", json=create_body(job, source_type="SALES_ORDER", source_id=str(ours))
            ),
            201,
        )

        assert (data["source_type"], data["source_id"]) == ("SALES_ORDER", str(ours))
        assert await column(session, JobWorkOrder.source_id, uuid.UUID(data["id"])) == ours

    @pytest.mark.parametrize(
        ("shape", "field"),
        [
            ("no-lines", "lines"),
            ("repeated-line-number", "_root"),
            ("source-on-a-manual-order", "_root"),
            ("negative-rate", "rate_per_unit"),
            ("zero-quantity", "lines.0.quantity_to_issue"),
            ("loss-over-100", "lines.0.expected_loss_percentage"),
        ],
    )
    async def test_an_invalid_order_names_the_field(
        self, api_client: AsyncClient, job, shape, field
    ) -> None:
        line = create_body(job)["lines"][0]
        body = {
            "no-lines": create_body(job, lines=[]),
            "repeated-line-number": create_body(job, lines=[line, line]),
            "source-on-a-manual-order": create_body(job, source_id=str(uuid.uuid4())),
            "negative-rate": create_body(job, rate_per_unit="-1"),
            "zero-quantity": create_body(job, lines=[{**line, "quantity_to_issue": "0"}]),
            "loss-over-100": create_body(job, lines=[{**line, "expected_loss_percentage": "101"}]),
        }[shape]

        response = await api_client.post("/job-work", json=body)

        assert field in field_errors(response)


# -----------------------------------------------------------------------------
# PATCH /job-work/{order_id}
# -----------------------------------------------------------------------------


class TestUpdateOrder:
    async def test_a_new_rate_reprices_the_draft_and_the_change_is_audited(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        order_id = await drafted(session, tenant, job)

        data = ok(
            await api_client.patch(
                f"/job-work/{order_id}", json={"rate_per_unit": "15", "notes": "Rate revised"}
            )
        )

        assert Decimal(data["rate_per_unit"]) == Decimal("15")
        assert Decimal(data["estimated_charges"]) == Decimal("1500")
        assert data["notes"] == "Rate revised"
        assert await column(session, JobWorkOrder.rate_per_unit, order_id) == Decimal("15")
        (update,) = await audit_rows(session, tenant.company_id, order_id, AuditAction.UPDATE)
        assert {"rate_per_unit", "estimated_charges", "notes"} <= set(update.changed_fields)
        assert update.context["lines_replaced"] is False

    async def test_supplying_lines_replaces_the_whole_set(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        order_id = await drafted(session, tenant, job)

        data = ok(
            await api_client.patch(
                f"/job-work/{order_id}",
                json={
                    "lines": [
                        {
                            "line_number": 1,
                            "item_id": str(job.raw_item_id),
                            "quantity_to_issue": "40",
                            "expected_output_item_id": str(job.coated_item_id),
                        }
                    ]
                },
            )
        )

        (line,) = data["lines"]
        assert Decimal(line["quantity_to_issue"]) == Decimal("40")
        assert Decimal(data["estimated_charges"]) == Decimal("500")
        stored = await session.scalar(
            select(func.count())
            .select_from(JobWorkOrderLine)
            .where(JobWorkOrderLine.job_work_order_id == order_id)
        )
        assert stored == 1
        (update,) = await audit_rows(session, tenant.company_id, order_id, AuditAction.UPDATE)
        assert update.context["lines_replaced"] is True

    async def test_an_approved_order_is_no_longer_editable(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        order_id = await approved(session, tenant, job)

        response = await api_client.patch(f"/job-work/{order_id}", json={"rate_per_unit": "99"})

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await column(session, JobWorkOrder.rate_per_unit, order_id) == Decimal("12.5")

    async def test_an_expected_return_in_the_past_is_refused_on_edit_too(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        order_id = await drafted(session, tenant, job)
        planned = await column(session, JobWorkOrder.expected_return_date, order_id)

        response = await api_client.patch(
            f"/job-work/{order_id}",
            json={"expected_return_date": (today() - timedelta(days=3)).isoformat()},
        )

        assert "expected_return_date" in field_errors(response)
        assert await column(session, JobWorkOrder.expected_return_date, order_id) == planned

    async def test_a_new_return_date_ahead_is_stored(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        order_id = await drafted(session, tenant, job)
        later = today() + timedelta(days=30)

        data = ok(
            await api_client.patch(
                f"/job-work/{order_id}", json={"expected_return_date": later.isoformat()}
            )
        )

        assert data["expected_return_date"] == later.isoformat()
        assert await column(session, JobWorkOrder.expected_return_date, order_id) == later

    async def test_a_draft_whose_return_date_slipped_can_still_be_amended(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        # The edit form always re-sends the stored date. A promise that has
        # since lapsed is not a new one, so it must not lock the draft.
        order_id = await drafted(session, tenant, job)
        slipped = today() - timedelta(days=2)
        await session.execute(
            update(JobWorkOrder)
            .where(JobWorkOrder.id == order_id)
            .values(expected_return_date=slipped)
        )

        data = ok(
            await api_client.patch(
                f"/job-work/{order_id}",
                json={"expected_return_date": slipped.isoformat(), "notes": "Vendor chased"},
            )
        )

        assert (data["expected_return_date"], data["notes"]) == (
            slipped.isoformat(),
            "Vendor chased",
        )
        assert await column(session, JobWorkOrder.notes, order_id) == "Vendor chased"

    async def test_an_explicit_null_on_a_required_rate_is_ignored(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        order_id = await drafted(session, tenant, job)

        data = ok(
            await api_client.patch(
                f"/job-work/{order_id}", json={"rate_per_unit": None, "notes": "Rate kept"}
            )
        )

        assert Decimal(data["rate_per_unit"]) == Decimal("12.5")
        assert Decimal(data["estimated_charges"]) == Decimal("1250")
        assert data["notes"] == "Rate kept"

    @pytest.mark.parametrize("field", ["warehouse_id", "return_warehouse_id"])
    async def test_an_explicit_null_on_a_required_warehouse_is_ignored(
        self, api_client: AsyncClient, session, tenant, job, field
    ) -> None:
        order_id = await drafted(session, tenant, job)
        attribute = getattr(JobWorkOrder, field)
        stored = await column(session, attribute, order_id)

        response = await api_client.patch(
            f"/job-work/{order_id}", json={field: None, "notes": "Re-sequenced"}
        )

        data = ok(response)
        assert (data[field], data["notes"]) == (str(stored), "Re-sequenced")
        assert await column(session, attribute, order_id) == stored

    @pytest.mark.parametrize(
        ("body", "field"),
        [
            ({"lines": []}, "lines"),
            ({"vendor_id": str(uuid.uuid4())}, "vendor_id"),
            ({"rate_per_unit": "-5"}, "rate_per_unit"),
        ],
        ids=["empty-line-set", "vendor-is-not-editable", "negative-rate"],
    )
    async def test_an_invalid_patch_names_the_field(
        self, api_client: AsyncClient, session, tenant, job, body, field
    ) -> None:
        order_id = await drafted(session, tenant, job)

        response = await api_client.patch(f"/job-work/{order_id}", json=body)

        assert field in field_errors(response)


# -----------------------------------------------------------------------------
# Another company's order, on every route in the batch
# -----------------------------------------------------------------------------


class TestTenantIsolation:
    @pytest.mark.parametrize(
        ("method", "suffix", "body"),
        [
            ("PATCH", "", {"notes": "Taken over"}),
            ("POST", "/approve", {"reason": REASON}),
            ("POST", "/cancel", {"reason": "Taken over"}),
            ("POST", "/issue", {"reason": "Taken over"}),
            ("POST", "/receive", receipt_body()),
            (
                "POST",
                "/qc-result",
                {
                    "reason": "Taken over",
                    "items": [{"output_item_id": str(uuid.uuid4()), "accepted_quantity": "1"}],
                },
            ),
            ("POST", "/close", {"reason": "Taken over"}),
        ],
        ids=["update", "approve", "cancel", "issue", "receive", "qc-result", "close"],
    )
    async def test_another_companys_order_is_not_found(
        self, api_client: AsyncClient, session, foreign, method, suffix, body
    ) -> None:
        theirs = await build_job(session, foreign.company_id)
        their_order = await drafted(session, foreign, theirs)

        response = await api_client.request(method, f"/job-work/{their_order}{suffix}", json=body)

        refused(response, 404, "NOT_FOUND")
        assert await column(session, JobWorkOrder.status, their_order) == "DRAFT"
        assert await column(session, JobWorkOrder.notes, their_order) is None


# -----------------------------------------------------------------------------
# POST /job-work/{order_id}/approve
# -----------------------------------------------------------------------------


class TestApproveOrder:
    async def test_approval_reserves_everything_the_order_will_send(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        await stock_in(session, tenant, job.raw_item_id, job.warehouse_id, "100")
        order_id = await drafted(session, tenant, job)

        data = ok(await api_client.post(f"/job-work/{order_id}/approve", json={"reason": REASON}))

        assert data["status"] == "APPROVED"
        assert data["approved_by_id"] == str(tenant.user_id)
        (line,) = data["lines"]
        (held,) = await reservations_for(session, order_id)
        assert line["reservation_id"] == str(held.id)
        assert (held.quantity, held.status, held.demand_type) == (
            Decimal("100"),
            ReservationStatus.ACTIVE,
            "JOB_WORK_ORDER",
        )
        raw = await stock_at(session, tenant.company_id, job.raw_item_id, job.warehouse_id)
        assert (raw.reserved_quantity, raw.available_quantity) == (Decimal("100"), Decimal("0"))
        approval = await audit_rows(session, tenant.company_id, order_id, AuditAction.APPROVE)
        assert moves(approval) == [("DRAFT", "APPROVED")]
        assert approval[0].context["reason"] == REASON

    async def test_a_short_line_refuses_the_whole_approval(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        await stock_in(session, tenant, job.raw_item_id, job.warehouse_id, "40")
        order_id = await drafted(session, tenant, job)

        response = await api_client.post(f"/job-work/{order_id}/approve", json={"reason": REASON})

        refused(response, 422, "INSUFFICIENT_AVAILABLE_STOCK")
        assert await column(session, JobWorkOrder.status, order_id) == "DRAFT"
        assert await reservations_for(session, order_id) == []

    async def test_an_approved_order_cannot_be_approved_again(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        order_id = await approved(session, tenant, job)

        response = await api_client.post(f"/job-work/{order_id}/approve", json={"reason": REASON})

        refused(response, 422, "INVALID_STATE_TRANSITION")
        assert len(await reservations_for(session, order_id)) == 1

    async def test_an_approval_needs_a_reason(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        order_id = await drafted(session, tenant, job)

        response = await api_client.post(f"/job-work/{order_id}/approve", json={"reason": "ok"})

        assert "reason" in field_errors(response)


# -----------------------------------------------------------------------------
# POST /job-work/{order_id}/cancel
# -----------------------------------------------------------------------------


class TestCancelOrder:
    async def test_cancelling_an_approved_order_gives_the_material_back(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        order_id = await approved(session, tenant, job)

        data = ok(
            await api_client.post(
                f"/job-work/{order_id}/cancel", json={"reason": "Vendor shut for Diwali"}
            )
        )

        assert data["status"] == "CANCELLED"
        assert data["cancellation_reason"] == "Vendor shut for Diwali"
        assert {row.status for row in await reservations_for(session, order_id)} == {
            ReservationStatus.RELEASED
        }
        raw = await stock_at(session, tenant.company_id, job.raw_item_id, job.warehouse_id)
        assert (raw.reserved_quantity, raw.available_quantity) == (Decimal("0"), Decimal("100"))
        rows = await audit_rows(session, tenant.company_id, order_id)
        assert ("APPROVED", "CANCELLED") in moves(
            [row for row in rows if row.action == AuditAction.CANCEL]
        )
        (released,) = [row for row in rows if row.action == AuditAction.RESERVATION_CHANGE]
        assert released.new_values["released_reservations"] == 1

    async def test_cancelling_a_draft_releases_nothing(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        order_id = await drafted(session, tenant, job)

        data = ok(
            await api_client.post(f"/job-work/{order_id}/cancel", json={"reason": "Not needed"})
        )

        assert data["status"] == "CANCELLED"
        assert (
            await audit_rows(session, tenant.company_id, order_id, AuditAction.RESERVATION_CHANGE)
            == []
        )

    async def test_an_order_whose_material_has_left_cannot_be_cancelled(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        order_id = await issued(session, tenant, job)

        response = await api_client.post(
            f"/job-work/{order_id}/cancel", json={"reason": "Vendor shut for Diwali"}
        )

        refused(response, 422, "INVALID_STATE_TRANSITION")
        assert await column(session, JobWorkOrder.status, order_id) == "ISSUED"


# -----------------------------------------------------------------------------
# POST /job-work/{order_id}/issue
# -----------------------------------------------------------------------------


class TestIssueOrder:
    async def test_issuing_raises_the_challan_and_moves_the_stock_to_the_vendor(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        order_id = await approved(session, tenant, job)

        data = ok(
            await api_client.post(
                f"/job-work/{order_id}/issue",
                json={"reason": "Handed to the vendor's vehicle", "vehicle_number": "GJ01AB1234"},
            )
        )

        assert data["order"]["status"] == "ISSUED"
        assert data["order"]["issued_at"] is not None
        assert data["movement_count"] == 1
        assert Decimal(data["quantity_at_vendor"]) == Decimal("100")
        challan = data["challan"]
        assert challan["vehicle_number"] == "GJ01AB1234"
        assert [Decimal(line["quantity"]) for line in challan["lines"]] == [Decimal("100")]
        challan_id = uuid.UUID(challan["id"])
        assert await column(session, JobWorkChallan.job_work_order_id, challan_id) == order_id
        raw = await stock_at(session, tenant.company_id, job.raw_item_id, job.warehouse_id)
        assert (raw.on_hand_quantity, raw.reserved_quantity, raw.job_work_quantity) == (
            Decimal("0"),
            Decimal("0"),
            Decimal("100"),
        )
        ledger = await session.scalars(
            select(InventoryTransaction.transaction_type).where(
                InventoryTransaction.reference_id == order_id
            )
        )
        assert list(ledger) == ["JOB_WORK_ISSUE"]
        changes = await audit_rows(session, tenant.company_id, order_id)
        assert ("APPROVED", "ISSUED") in moves(changes)

    async def test_part_of_a_line_can_be_sent_this_week(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        order_id = await approved(session, tenant, job)

        data = ok(
            await api_client.post(
                f"/job-work/{order_id}/issue",
                json={
                    "reason": "Vendor can take forty this week",
                    "lines": [{"line_number": 1, "issued_quantity": "40"}],
                },
            )
        )

        assert Decimal(data["quantity_at_vendor"]) == Decimal("40")
        assert Decimal(data["order"]["lines"][0]["issued_quantity"]) == Decimal("40")
        raw = await stock_at(session, tenant.company_id, job.raw_item_id, job.warehouse_id)
        assert (raw.on_hand_quantity, raw.job_work_quantity) == (Decimal("60"), Decimal("40"))

    async def test_more_than_the_line_ordered_is_refused_and_nothing_moves(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        order_id = await approved(session, tenant, job)

        response = await api_client.post(
            f"/job-work/{order_id}/issue",
            json={"reason": "Overfilled", "lines": [{"line_number": 1, "issued_quantity": "120"}]},
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        raw = await stock_at(session, tenant.company_id, job.raw_item_id, job.warehouse_id)
        assert (raw.on_hand_quantity, raw.reserved_quantity, raw.job_work_quantity) == (
            Decimal("100"),
            Decimal("100"),
            Decimal("0"),
        )
        assert await column(session, JobWorkOrder.status, order_id) == "APPROVED"

    async def test_a_draft_cannot_be_issued(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        order_id = await drafted(session, tenant, job)

        response = await api_client.post(
            f"/job-work/{order_id}/issue", json={"reason": "Handed over"}
        )

        refused(response, 422, "INVALID_STATE_TRANSITION")
        assert (
            await session.scalar(
                select(JobWorkChallan.id).where(JobWorkChallan.job_work_order_id == order_id)
            )
            is None
        )

    async def test_a_return_date_before_the_challan_is_refused(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        order_id = await approved(session, tenant, job)

        response = await api_client.post(
            f"/job-work/{order_id}/issue",
            json={
                "reason": "Handed over",
                "challan_date": today().isoformat(),
                "expected_return_date": (today() - timedelta(days=1)).isoformat(),
            },
        )

        refused(response, 422, "VALIDATION_ERROR")
        assert await column(session, JobWorkOrder.status, order_id) == "APPROVED"

    @pytest.mark.parametrize(
        ("body", "field"),
        [
            ({}, "reason"),
            (
                {
                    "reason": "Handed over",
                    "lines": [
                        {"line_number": 1, "issued_quantity": "10"},
                        {"line_number": 1, "issued_quantity": "20"},
                    ],
                },
                "_root",
            ),
            (
                {"reason": "Handed over", "lines": [{"line_number": 1, "issued_quantity": "0"}]},
                "lines.0.issued_quantity",
            ),
        ],
        ids=["no-reason", "one-line-twice", "zero-quantity"],
    )
    async def test_an_invalid_issue_names_the_field(
        self, api_client: AsyncClient, session, tenant, job, body, field
    ) -> None:
        order_id = await approved(session, tenant, job)

        response = await api_client.post(f"/job-work/{order_id}/issue", json=body)

        assert field in field_errors(response)


# -----------------------------------------------------------------------------
# POST /job-work/{order_id}/receive
# -----------------------------------------------------------------------------


class TestReceiveLot:
    async def test_a_full_return_posts_the_output_and_draws_the_pool_down(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        order_id = await issued(session, tenant, job)

        data = ok(
            await api_client.post(
                f"/job-work/{order_id}/receive",
                json=receipt_body(vendor_challan_number="VC-0077"),
            )
        )

        assert data["order"]["status"] == "RECEIVED"
        assert data["output_held_for_qc"] is False
        assert data["movement_count"] >= 2
        receipt = data["receipt"]
        assert receipt["vendor_challan_number"] == "VC-0077"
        assert Decimal(receipt["total_output_quantity"]) == Decimal("100")
        assert Decimal(receipt["total_input_consumed"]) == Decimal("100")
        assert Decimal(receipt["total_charges"]) == Decimal("1250")
        receipt_id = uuid.UUID(receipt["id"])
        assert await column(session, JobWorkReceipt.job_work_order_id, receipt_id) == order_id
        raw = await stock_at(session, tenant.company_id, job.raw_item_id, job.warehouse_id)
        assert raw.job_work_quantity == Decimal("0")
        coated = await stock_at(session, tenant.company_id, job.coated_item_id, job.warehouse_id)
        assert (coated.on_hand_quantity, coated.available_quantity) == (
            Decimal("100"),
            Decimal("100"),
        )
        posted = await audit_rows(
            session, tenant.company_id, receipt_id, AuditAction.INVENTORY_MOVEMENT
        )
        assert [row.entity_type for row in posted] == ["job_work_receipt"]

    async def test_a_part_return_leaves_the_rest_at_the_vendor(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        order_id = await issued(session, tenant, job)

        data = ok(
            await api_client.post(
                f"/job-work/{order_id}/receive", json=receipt_body("40", is_final=False)
            )
        )

        assert data["order"]["status"] == "PARTIALLY_RECEIVED"
        raw = await stock_at(session, tenant.company_id, job.raw_item_id, job.warehouse_id)
        assert raw.job_work_quantity == Decimal("60")

    @pytest.mark.parametrize("approve_excess", [False, True], ids=["refused", "approved"])
    async def test_loss_past_the_allowance_needs_approval_and_is_debited(
        self, api_client: AsyncClient, session, tenant, job, approve_excess
    ) -> None:
        order_id = await issued(session, tenant, job)

        response = await api_client.post(
            f"/job-work/{order_id}/receive",
            json=receipt_body("90", approve_excess_loss=approve_excess),
        )

        if not approve_excess:
            error = refused(response, 422, "APPROVAL_REQUIRED")
            assert Decimal(error["details"]["excess_loss_quantity"]) == Decimal("10")
            assert await column(session, JobWorkOrder.status, order_id) == "ISSUED"
            assert (
                await session.scalar(
                    select(JobWorkReceipt.id).where(JobWorkReceipt.job_work_order_id == order_id)
                )
                is None
            )
            return
        data = ok(response)
        assert data["receipt"]["has_excess_loss"] is True
        assert Decimal(data["receipt"]["total_scrap_quantity"]) == Decimal("10")
        assert Decimal(data["order"]["vendor_debit_amount"]) > 0

    async def test_accepting_excess_loss_needs_the_close_right(
        self, api_client: AsyncClient, session, tenant, principal, job
    ) -> None:
        order_id = await issued(session, tenant, job)
        act_as(holding(principal, Permission.JOB_WORK_RECEIVE))

        response = await api_client.post(
            f"/job-work/{order_id}/receive", json=receipt_body("90", approve_excess_loss=True)
        )

        error = refused(response, 403, "PERMISSION_DENIED")
        assert error["details"]["required_permission"] == "job_work.close"
        assert await column(session, JobWorkOrder.status, order_id) == "ISSUED"
        raw = await stock_at(session, tenant.company_id, job.raw_item_id, job.warehouse_id)
        assert raw.job_work_quantity == Decimal("100")

    async def test_an_order_whose_material_has_not_left_cannot_be_received(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        order_id = await approved(session, tenant, job)

        response = await api_client.post(f"/job-work/{order_id}/receive", json=receipt_body())

        refused(response, 422, "INVALID_STATE_TRANSITION")

    async def test_a_line_the_order_does_not_have_is_not_found(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        order_id = await issued(session, tenant, job)
        body = receipt_body()
        body["lines"][0]["line_number"] = 9

        response = await api_client.post(f"/job-work/{order_id}/receive", json=body)

        refused(response, 404, "NOT_FOUND")
        raw = await stock_at(session, tenant.company_id, job.raw_item_id, job.warehouse_id)
        assert raw.job_work_quantity == Decimal("100")

    @pytest.mark.parametrize(
        ("body", "field"),
        [
            (receipt_body("0", is_final=False), "lines.0"),
            ({"reason": "Nothing listed", "lines": []}, "lines"),
            (
                {
                    "reason": "Listed twice",
                    "lines": [
                        {"line_number": 1, "output_quantity": "10"},
                        {"line_number": 1, "output_quantity": "20"},
                    ],
                },
                "_root",
            ),
        ],
        ids=["reports-nothing", "no-lines", "one-line-twice"],
    )
    async def test_an_invalid_receipt_names_the_field(
        self, api_client: AsyncClient, session, tenant, job, body, field
    ) -> None:
        order_id = await issued(session, tenant, job)

        response = await api_client.post(f"/job-work/{order_id}/receive", json=body)

        assert field in field_errors(response)


# -----------------------------------------------------------------------------
# POST /job-work/{order_id}/qc-result
# -----------------------------------------------------------------------------


def verdict(item_id: uuid.UUID, accepted: str, rejected: str = "0") -> dict[str, Any]:
    return {
        "reason": "Coating thickness checked",
        "items": [
            {
                "output_item_id": str(item_id),
                "accepted_quantity": accepted,
                "rejected_quantity": rejected,
            }
        ],
    }


class TestRecordQcResult:
    async def test_a_verdict_on_an_uninspected_hold_releases_and_damages_the_lot(
        self, api_client: AsyncClient, session, tenant, principal, job, monkeypatch
    ) -> None:
        # Quality's absence is simulated by forcing its seam to resolve to nothing.
        monkeypatch.setattr(service, "resolve_quality_port", lambda: None)
        order_id = await received(session, tenant, principal, job, requires_qc=True)
        assert await column(session, JobWorkOrder.status, order_id) == "QC_PENDING"

        data = ok(
            await api_client.post(
                f"/job-work/{order_id}/qc-result", json=verdict(job.coated_item_id, "95", "5")
            )
        )

        assert data["status"] == "COMPLETED"
        coated = await stock_at(session, tenant.company_id, job.coated_item_id, job.warehouse_id)
        assert (
            coated.qc_hold_quantity,
            coated.damaged_quantity,
            coated.available_quantity,
        ) == (Decimal("0"), Decimal("5"), Decimal("95"))
        released_at = await session.scalar(
            select(JobWorkReceipt.qc_released_at).where(
                JobWorkReceipt.job_work_order_id == order_id
            )
        )
        assert released_at is not None
        decisions = await audit_rows(session, tenant.company_id, order_id, AuditAction.QC_DECISION)
        assert ("QC_PENDING", "COMPLETED") in moves(decisions)

    async def test_a_verdict_that_leaves_part_of_the_hold_undecided_is_refused(
        self, api_client: AsyncClient, session, tenant, principal, job, monkeypatch
    ) -> None:
        monkeypatch.setattr(service, "resolve_quality_port", lambda: None)
        order_id = await received(session, tenant, principal, job, requires_qc=True)

        response = await api_client.post(
            f"/job-work/{order_id}/qc-result", json=verdict(job.coated_item_id, "60")
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await column(session, JobWorkOrder.status, order_id) == "QC_PENDING"
        coated = await stock_at(session, tenant.company_id, job.coated_item_id, job.warehouse_id)
        assert coated.qc_hold_quantity == Decimal("100")

    async def test_a_hold_an_open_inspection_covers_is_pointed_at_the_inspection(
        self, api_client: AsyncClient, session, tenant, principal, job
    ) -> None:
        order_id = await received(session, tenant, principal, job, requires_qc=True)

        response = await api_client.post(
            f"/job-work/{order_id}/qc-result", json=verdict(job.coated_item_id, "100")
        )

        error = refused(response, 422, "INVALID_STATE_TRANSITION")
        assert error["details"]["qc_inspection_codes"]
        coated = await stock_at(session, tenant.company_id, job.coated_item_id, job.warehouse_id)
        assert coated.qc_hold_quantity == Decimal("100")

    async def test_an_order_with_nothing_on_hold_is_refused(
        self, api_client: AsyncClient, session, tenant, principal, job
    ) -> None:
        order_id = await received(session, tenant, principal, job)

        response = await api_client.post(
            f"/job-work/{order_id}/qc-result", json=verdict(job.coated_item_id, "100")
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await column(session, JobWorkOrder.status, order_id) == "RECEIVED"

    @pytest.mark.parametrize(
        ("shape", "field"),
        [("no-items", "items"), ("one-item-twice", "_root"), ("accounts-for-nothing", "items.0")],
    )
    async def test_an_invalid_verdict_names_the_field(
        self, api_client: AsyncClient, session, tenant, job, shape, field
    ) -> None:
        order_id = await drafted(session, tenant, job)
        item = {"output_item_id": str(job.coated_item_id), "accepted_quantity": "5"}
        items = {
            "no-items": [],
            "one-item-twice": [item, item],
            "accounts-for-nothing": [{**item, "accepted_quantity": "0"}],
        }[shape]

        response = await api_client.post(
            f"/job-work/{order_id}/qc-result", json={"reason": "Checked", "items": items}
        )

        assert field in field_errors(response)


# -----------------------------------------------------------------------------
# POST /job-work/{order_id}/close
# -----------------------------------------------------------------------------


class TestCloseOrder:
    async def test_a_received_order_is_completed_and_closed_in_one_call(
        self, api_client: AsyncClient, session, tenant, principal, job
    ) -> None:
        order_id = await received(session, tenant, principal, job)

        data = ok(await api_client.post(f"/job-work/{order_id}/close", json={"reason": "All back"}))

        assert data["status"] == "CLOSED"
        assert data["closed_at"] is not None
        assert await column(session, JobWorkOrder.status, order_id) == "CLOSED"
        changes = moves(await audit_rows(session, tenant.company_id, order_id))
        assert changes[-2:] == [("RECEIVED", "COMPLETED"), ("COMPLETED", "CLOSED")]

    async def test_an_order_with_material_still_out_cannot_be_closed(
        self, api_client: AsyncClient, session, tenant, job
    ) -> None:
        order_id = await issued(session, tenant, job)

        response = await api_client.post(f"/job-work/{order_id}/close", json={"reason": "Done"})

        refused(response, 422, "INVALID_STATE_TRANSITION")
        assert await column(session, JobWorkOrder.closed_at, order_id) is None

    async def test_a_closure_needs_a_reason(
        self, api_client: AsyncClient, session, tenant, principal, job
    ) -> None:
        order_id = await received(session, tenant, principal, job)

        response = await api_client.post(f"/job-work/{order_id}/close", json={})

        assert "reason" in field_errors(response)
