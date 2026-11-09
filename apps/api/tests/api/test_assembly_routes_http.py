"""Assembly write routes over HTTP: amend, close, cancel, the packing claim and
the sales order a new order says it covers.

The bench rules - one-level explosion, the kit reservation, completion and the
single QC release path - are pinned in ``tests/integration/test_assembly_service.py``
and ``test_assembly_completion.py``, and ``test_assembly_completion_http.py``
walks one order from draft to packed. These drive the four assembly routes no
backend test had reached through HTTP: the status and envelope each answers
with, the row, reservation and audit entry it leaves behind, the 422 a payload
or an order's state earns, and the 404 another company's order answers with -
never a 403, which would confirm that it exists.

Permission gates on the routes themselves are covered by
``tests/unit/test_route_authorization.py``; the caller is the ``api_client``
principal, holding every permission. The kit is the integration suite's lock
set - a handle, two keys and four screw kits per set, a thousand of each on
the shelf - and orders are walked forward through the assembly service with
that suite's own builders. Ids are held as plain values: an expected failure
rolls its savepoint back and expires what was loaded before it.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.deps import TenantContext
from src.modules.allocation.models import ReservationStatus, StockReservation
from src.modules.assembly import service
from src.modules.assembly.models import AssemblyOrder, AssemblyOrderComponent
from src.modules.assembly.schemas import IssueComponentsRequest
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.inventory.models import InventoryBalance
from src.modules.packing import service as packing_service
from src.modules.packing.schemas import PackingOrderCreate, PackingOrderLineIn
from src.modules.sales.models import SalesOrder
from src.workers import celery_app as celery_module
from tests import seed_support
from tests.integration.test_assembly_completion import (
    Kit,
    completed_order,
    make_kit,
    raise_order,
    to_in_progress,
)

pytestmark = [pytest.mark.api, pytest.mark.integration]

REASON = "Bench supervisor decision"


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


@pytest.fixture
async def kit(session: AsyncSession, tenant: TenantContext) -> Kit:
    return await make_kit(session, tenant, separate_output_warehouse=True)


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


# -----------------------------------------------------------------------------
# Builders and database readers
# -----------------------------------------------------------------------------


async def drafted(session: AsyncSession, tenant: TenantContext, kit: Kit, **fields) -> uuid.UUID:
    order = await raise_order(session, tenant, kit, **fields)
    return order.id


async def released(session: AsyncSession, tenant: TenantContext, kit: Kit) -> uuid.UUID:
    order_id = await drafted(session, tenant, kit)
    await service.approve_order(session, tenant, order_id, reason="Scheduled")
    await service.release_order(session, tenant, order_id, reason="To the bench")
    return order_id


async def packing_order_for(session: AsyncSession, tenant: TenantContext, kit: Kit) -> uuid.UUID:
    """A real draft packing order for the kit's output, raised on its own."""
    customer = await seed_support.seed_customer(session, company_id=tenant.company_id)
    order, _ = await packing_service.create_order(
        session,
        tenant,
        PackingOrderCreate(
            customer_id=customer.id,
            warehouse_id=kit.output_warehouse_id,
            planned_date=today(),
            lines=[
                PackingOrderLineIn(
                    item_id=kit.output_item_id, quantity_to_pack=Decimal("6"), uom_id=kit.uom_id
                )
            ],
        ),
    )
    return order.id


async def column(session: AsyncSession, attribute: Any, row_id: uuid.UUID) -> Any:
    """One stored value, read past the identity map."""
    return await session.scalar(select(attribute).where(attribute.class_.id == row_id))


async def stored_requirements(session: AsyncSession, order_id: uuid.UUID) -> dict[uuid.UUID, Any]:
    stmt = select(AssemblyOrderComponent.item_id, AssemblyOrderComponent.required_quantity).where(
        AssemblyOrderComponent.assembly_order_id == order_id
    )
    return {row.item_id: row.required_quantity for row in (await session.execute(stmt)).all()}


async def reservations_for(session: AsyncSession, order_id: uuid.UUID) -> list[Row[Any]]:
    stmt = (
        select(StockReservation.item_id, StockReservation.quantity, StockReservation.status)
        .where(StockReservation.demand_id == order_id)
        .order_by(StockReservation.created_at, StockReservation.id)
    )
    return list((await session.execute(stmt)).all())


async def reserved_at(
    session: AsyncSession, company_id: uuid.UUID, item_id: uuid.UUID, warehouse_id: uuid.UUID
) -> Any:
    return await session.scalar(
        select(InventoryBalance.reserved_quantity).where(
            InventoryBalance.company_id == company_id,
            InventoryBalance.item_id == item_id,
            InventoryBalance.warehouse_id == warehouse_id,
            InventoryBalance.location_id.is_(None),
            InventoryBalance.batch_id.is_(None),
        )
    )


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


def moves(rows: list[Row[Any]]) -> list[tuple[str, str]]:
    return [
        (row.old_values["status"], row.new_values["status"])
        for row in rows
        if (row.old_values or {}).get("status") and len(row.old_values) == 1
    ]


async def ready_for_packing(api_client: AsyncClient, kit: Kit) -> list[str]:
    rows = ok(
        await api_client.get(
            "/assembly/ready-for-packing", params={"warehouse_id": str(kit.output_warehouse_id)}
        )
    )
    return [row["assembly_order_id"] for row in rows]


# -----------------------------------------------------------------------------
# PATCH /assembly/{order_id}
# -----------------------------------------------------------------------------


class TestUpdateOrder:
    async def test_a_new_quantity_re_explodes_the_kit_and_the_edit_is_audited(
        self, api_client: AsyncClient, session, tenant, kit
    ) -> None:
        order_id = await drafted(session, tenant, kit, planned="10")

        data = ok(
            await api_client.patch(
                f"/assembly/{order_id}",
                json={
                    "planned_quantity": "4",
                    "assembly_line": "BENCH-2",
                    "priority": 7,
                    "notes": "Half batch first",
                },
            )
        )

        assert Decimal(data["planned_quantity"]) == Decimal("4")
        assert (data["assembly_line"], data["priority"], data["notes"]) == (
            "BENCH-2",
            7,
            "Half batch first",
        )
        expected = {item_id: 4 * per for item_id, per in kit.components.items()}
        assert {
            uuid.UUID(row["item_id"]): Decimal(row["required_quantity"])
            for row in data["components"]
        } == expected
        assert await stored_requirements(session, order_id) == expected
        (update,) = await audit_rows(session, tenant.company_id, order_id, AuditAction.UPDATE)
        assert update.entity_type == "assembly_order"
        assert update.created_by_id == tenant.user_id
        assert {"planned_quantity", "assembly_line", "priority", "notes"} <= set(
            update.changed_fields
        )
        assert (update.old_values["planned_quantity"], update.new_values["planned_quantity"]) == (
            "10.0000",
            "4.0000",
        )
        assert update.context["component_count"] == 3

    async def test_an_explicit_null_clears_a_nullable_field(
        self, api_client: AsyncClient, session, tenant, kit
    ) -> None:
        order_id = await drafted(session, tenant, kit, assembly_line="BENCH-1")

        data = ok(await api_client.patch(f"/assembly/{order_id}", json={"assembly_line": None}))

        assert data["assembly_line"] is None
        assert await column(session, AssemblyOrder.assembly_line, order_id) is None

    @pytest.mark.parametrize("field", ["warehouse_id", "output_warehouse_id"])
    async def test_an_explicit_null_on_a_required_warehouse_is_ignored(
        self, api_client: AsyncClient, session, tenant, kit, field
    ) -> None:
        order_id = await drafted(session, tenant, kit)
        attribute = getattr(AssemblyOrder, field)
        stored = await column(session, attribute, order_id)

        response = await api_client.patch(
            f"/assembly/{order_id}", json={field: None, "notes": "Re-sequenced"}
        )

        data = ok(response)
        assert (data[field], data["notes"]) == (str(stored), "Re-sequenced")
        assert await column(session, attribute, order_id) == stored

    async def test_an_approved_order_is_no_longer_editable(
        self, api_client: AsyncClient, session, tenant, kit
    ) -> None:
        order_id = await drafted(session, tenant, kit)
        await service.approve_order(session, tenant, order_id, reason="Scheduled")

        response = await api_client.patch(f"/assembly/{order_id}", json={"planned_quantity": "3"})

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await column(session, AssemblyOrder.planned_quantity, order_id) == Decimal("10")

    async def test_an_end_before_the_start_is_refused(
        self, api_client: AsyncClient, session, tenant, kit
    ) -> None:
        order_id = await drafted(session, tenant, kit)
        start = (utcnow() + timedelta(days=2)).replace(microsecond=0)

        response = await api_client.patch(
            f"/assembly/{order_id}",
            json={
                "planned_start": start.isoformat(),
                "planned_end": (start - timedelta(hours=1)).isoformat(),
            },
        )

        refused(response, 422, "VALIDATION_ERROR")
        assert await column(session, AssemblyOrder.planned_start, order_id) is None

    async def test_another_companys_warehouse_is_not_found(
        self, api_client: AsyncClient, session, tenant, kit, foreign
    ) -> None:
        order_id = await drafted(session, tenant, kit)
        theirs = await seed_support.seed_warehouse(
            session, company_id=foreign.company_id, code=f"WH-{uuid.uuid4().hex[:8]}"
        )
        theirs_id = theirs.id

        response = await api_client.patch(
            f"/assembly/{order_id}", json={"output_warehouse_id": str(theirs_id)}
        )

        refused(response, 404, "NOT_FOUND")
        assert await column(session, AssemblyOrder.output_warehouse_id, order_id) == (
            kit.output_warehouse_id
        )

    @pytest.mark.parametrize(
        ("body", "field"),
        [
            ({"planned_quantity": "0"}, "planned_quantity"),
            ({"priority": 10000}, "priority"),
            ({"item_id": str(uuid.uuid4())}, "item_id"),
        ],
        ids=["zero-quantity", "priority-out-of-range", "item-is-not-editable"],
    )
    async def test_an_invalid_patch_names_the_field(
        self, api_client: AsyncClient, session, tenant, kit, body, field
    ) -> None:
        order_id = await drafted(session, tenant, kit)

        response = await api_client.patch(f"/assembly/{order_id}", json=body)

        assert field in field_errors(response)


# -----------------------------------------------------------------------------
# Another company's order, on every route in the batch
# -----------------------------------------------------------------------------


class TestTenantIsolation:
    @pytest.mark.parametrize(
        ("method", "suffix", "body"),
        [
            ("PATCH", "", {"notes": "Taken over"}),
            ("POST", "/close", {"reason": "Taken over"}),
            ("POST", "/cancel", {"reason": "Taken over"}),
            ("POST", "/pack", {"packing_order_id": str(uuid.uuid4()), "reason": "Taken over"}),
        ],
        ids=["update", "close", "cancel", "pack"],
    )
    async def test_another_companys_order_is_not_found(
        self, api_client: AsyncClient, session, foreign, method, suffix, body
    ) -> None:
        theirs = await make_kit(session, foreign)
        their_order = await drafted(session, foreign, theirs)

        response = await api_client.request(method, f"/assembly/{their_order}{suffix}", json=body)

        refused(response, 404, "NOT_FOUND")
        assert await column(session, AssemblyOrder.status, their_order) == "DRAFT"
        assert await column(session, AssemblyOrder.notes, their_order) is None


# -----------------------------------------------------------------------------
# POST /assembly/{order_id}/close
# -----------------------------------------------------------------------------


class TestCloseOrder:
    async def test_a_completed_order_is_closed_with_its_reason(
        self, api_client: AsyncClient, session, tenant, kit
    ) -> None:
        order_id = await completed_order(session, tenant, kit)

        data = ok(
            await api_client.post(f"/assembly/{order_id}/close", json={"reason": "Run reconciled"})
        )

        assert data["status"] == "CLOSED"
        assert await column(session, AssemblyOrder.status, order_id) == "CLOSED"
        changes = await audit_rows(session, tenant.company_id, order_id, AuditAction.STATUS_CHANGE)
        assert moves(changes)[-1] == ("COMPLETED", "CLOSED")
        assert changes[-1].context["reason"] == "Run reconciled"

    async def test_an_order_still_on_the_bench_cannot_close(
        self, api_client: AsyncClient, session, tenant, kit
    ) -> None:
        order_id = await drafted(session, tenant, kit)
        await to_in_progress(session, tenant, order_id)

        response = await api_client.post(f"/assembly/{order_id}/close", json={"reason": REASON})

        refused(response, 422, "INVALID_STATE_TRANSITION")
        assert await column(session, AssemblyOrder.status, order_id) == "IN_PROGRESS"

    async def test_a_closure_needs_a_reason(
        self, api_client: AsyncClient, session, tenant, kit
    ) -> None:
        order_id = await completed_order(session, tenant, kit)

        response = await api_client.post(f"/assembly/{order_id}/close", json={})

        assert "reason" in field_errors(response)


# -----------------------------------------------------------------------------
# POST /assembly/{order_id}/cancel
# -----------------------------------------------------------------------------


class TestCancelOrder:
    async def test_a_released_order_gives_every_component_back(
        self, api_client: AsyncClient, session, tenant, kit
    ) -> None:
        order_id = await released(session, tenant, kit)
        assert len(await reservations_for(session, order_id)) == 3

        data = ok(
            await api_client.post(
                f"/assembly/{order_id}/cancel", json={"reason": "Customer changed the finish"}
            )
        )

        assert data["status"] == "CANCELLED"
        assert data["cancellation_reason"] == "Customer changed the finish"
        assert {row.status for row in await reservations_for(session, order_id)} == {
            ReservationStatus.RELEASED
        }
        for item_id in kit.components:
            assert await reserved_at(
                session, tenant.company_id, item_id, kit.warehouse_id
            ) == Decimal("0")
        cancels = await audit_rows(session, tenant.company_id, order_id, AuditAction.CANCEL)
        assert moves(cancels) == [("RELEASED", "CANCELLED")]
        (released_row,) = await audit_rows(
            session, tenant.company_id, order_id, AuditAction.RESERVATION_CHANGE
        )
        assert released_row.new_values["released_reservations"] == 3

    async def test_a_draft_is_cancelled_with_nothing_to_release(
        self, api_client: AsyncClient, session, tenant, kit
    ) -> None:
        order_id = await drafted(session, tenant, kit)

        data = ok(await api_client.post(f"/assembly/{order_id}/cancel", json={"reason": REASON}))

        assert data["status"] == "CANCELLED"
        assert (
            await audit_rows(session, tenant.company_id, order_id, AuditAction.RESERVATION_CHANGE)
            == []
        )

    async def test_an_order_whose_components_are_on_the_bench_cannot_cancel(
        self, api_client: AsyncClient, session, tenant, kit
    ) -> None:
        order_id = await released(session, tenant, kit)
        await service.issue_components(
            session, tenant, order_id, IssueComponentsRequest(reason="Kitted")
        )

        response = await api_client.post(f"/assembly/{order_id}/cancel", json={"reason": REASON})

        refused(response, 422, "INVALID_STATE_TRANSITION")
        assert await column(session, AssemblyOrder.status, order_id) == "COMPONENTS_ISSUED"
        assert {row.status for row in await reservations_for(session, order_id)} == {
            ReservationStatus.ACTIVE
        }


# -----------------------------------------------------------------------------
# POST /assembly/{order_id}/pack
# -----------------------------------------------------------------------------


class TestMarkPacked:
    async def test_a_packing_order_claims_the_output_and_it_leaves_the_feed(
        self, api_client: AsyncClient, session, tenant, kit
    ) -> None:
        order_id = await completed_order(session, tenant, kit)
        packing_order_id = await packing_order_for(session, tenant, kit)
        assert str(order_id) in await ready_for_packing(api_client, kit)

        data = ok(
            await api_client.post(
                f"/assembly/{order_id}/pack",
                json={"packing_order_id": str(packing_order_id), "reason": "Packed for dispatch"},
            )
        )

        assert data["packing_order_id"] == str(packing_order_id)
        assert data["packed_at"] is not None
        assert await column(session, AssemblyOrder.packing_order_id, order_id) == packing_order_id
        assert str(order_id) not in await ready_for_packing(api_client, kit)
        updates = await audit_rows(session, tenant.company_id, order_id, AuditAction.UPDATE)
        (claim,) = [row for row in updates if (row.new_values or {}).get("packing_order_id")]
        assert claim.new_values["packing_order_id"] == str(packing_order_id)
        assert claim.context["reason"] == "Packed for dispatch"

    async def test_the_same_claim_again_is_idempotent(
        self, api_client: AsyncClient, session, tenant, kit
    ) -> None:
        order_id = await completed_order(session, tenant, kit)
        packing_order_id = await packing_order_for(session, tenant, kit)
        body = {"packing_order_id": str(packing_order_id), "reason": "Packed for dispatch"}
        first = ok(await api_client.post(f"/assembly/{order_id}/pack", json=body))

        again = ok(await api_client.post(f"/assembly/{order_id}/pack", json=body))

        assert again["packing_order_id"] == first["packing_order_id"]
        assert again["packed_at"] == first["packed_at"]

    async def test_a_second_packing_order_cannot_take_a_claimed_assembly(
        self, api_client: AsyncClient, session, tenant, kit
    ) -> None:
        order_id = await completed_order(session, tenant, kit)
        first = await packing_order_for(session, tenant, kit)
        second = await packing_order_for(session, tenant, kit)
        await service.mark_packed(session, tenant, order_id, packing_order_id=first, reason=REASON)

        response = await api_client.post(
            f"/assembly/{order_id}/pack",
            json={"packing_order_id": str(second), "reason": "Packed again"},
        )

        error = refused(response, 422, "CONFLICT")
        assert error["details"]["packing_order_id"] == str(first)
        assert await column(session, AssemblyOrder.packing_order_id, order_id) == first

    async def test_only_a_completed_assembly_can_be_packed(
        self, api_client: AsyncClient, session, tenant, kit
    ) -> None:
        order_id = await drafted(session, tenant, kit)
        await to_in_progress(session, tenant, order_id)
        packing_order_id = await packing_order_for(session, tenant, kit)

        response = await api_client.post(
            f"/assembly/{order_id}/pack",
            json={"packing_order_id": str(packing_order_id), "reason": "Packed early"},
        )

        refused(response, 422, "BUSINESS_RULE_VIOLATION")
        assert await column(session, AssemblyOrder.packing_order_id, order_id) is None

    @pytest.mark.parametrize("named", ["no-such-order", "another-companys-order"])
    async def test_a_packing_order_that_does_not_exist_is_not_found(
        self, api_client: AsyncClient, session, tenant, foreign, kit, named
    ) -> None:
        order_id = await completed_order(session, tenant, kit)
        packing_order_id = (
            uuid.uuid4()
            if named == "no-such-order"
            else await packing_order_for(session, foreign, await make_kit(session, foreign))
        )

        response = await api_client.post(
            f"/assembly/{order_id}/pack",
            json={"packing_order_id": str(packing_order_id), "reason": "Typed from a label"},
        )

        refused(response, 404, "NOT_FOUND")
        assert await column(session, AssemblyOrder.packing_order_id, order_id) is None
        assert str(order_id) in await ready_for_packing(api_client, kit)

    async def test_without_a_packing_module_no_claim_can_name_an_order(
        self, api_client: AsyncClient, session, tenant, kit, monkeypatch
    ) -> None:
        order_id = await completed_order(session, tenant, kit)
        packing_order_id = await packing_order_for(session, tenant, kit)
        monkeypatch.setattr(service, "resolve_packing_order_reader_port", lambda: None)

        response = await api_client.post(
            f"/assembly/{order_id}/pack",
            json={"packing_order_id": str(packing_order_id), "reason": "Packed for dispatch"},
        )

        refused(response, 404, "NOT_FOUND")
        assert await column(session, AssemblyOrder.packing_order_id, order_id) is None

    @pytest.mark.parametrize(
        ("body", "field"),
        [
            ({"packing_order_id": "PO-17", "reason": REASON}, "packing_order_id"),
            ({"packing_order_id": str(uuid.uuid4())}, "reason"),
        ],
        ids=["malformed-id", "no-reason"],
    )
    async def test_an_invalid_claim_names_the_field(
        self, api_client: AsyncClient, session, tenant, kit, body, field
    ) -> None:
        order_id = await completed_order(session, tenant, kit)

        response = await api_client.post(f"/assembly/{order_id}/pack", json=body)

        assert field in field_errors(response)


# -----------------------------------------------------------------------------
# POST /assembly - the sales order a new order says it covers
# -----------------------------------------------------------------------------


async def sales_order(session: AsyncSession, owner: TenantContext) -> uuid.UUID:
    """A draft sales order of ``owner``'s company, built through the ORM."""
    customer = await seed_support.seed_customer(session, company_id=owner.company_id)
    store = await seed_support.seed_warehouse(
        session, company_id=owner.company_id, code=f"SO-{uuid.uuid4().hex[:6].upper()}"
    )
    order = SalesOrder(
        company_id=owner.company_id,
        code=f"SO-{uuid.uuid4().hex[:8].upper()}",
        customer_id=customer.id,
        order_date=today(),
        required_date=today() + timedelta(days=7),
        warehouse_id=store.id,
    )
    session.add(order)
    await session.flush()
    return order.id


def raised_for(kit: Kit, source_id: uuid.UUID) -> dict[str, Any]:
    return {
        "item_id": str(kit.output_item_id),
        "planned_quantity": "4",
        "warehouse_id": str(kit.warehouse_id),
        "output_warehouse_id": str(kit.output_warehouse_id),
        "source_type": "SALES_ORDER",
        "source_id": str(source_id),
    }


class TestOrderSource:
    @pytest.mark.parametrize("whose", ["another-companys-order", "no-such-order"])
    async def test_a_sales_order_this_company_does_not_hold_is_not_found(
        self, api_client: AsyncClient, session, kit, foreign, whose
    ) -> None:
        source_id = (
            await sales_order(session, foreign)
            if whose == "another-companys-order"
            else uuid.uuid4()
        )

        response = await api_client.post("/assembly", json=raised_for(kit, source_id))

        refused(response, 404, "NOT_FOUND")
        raised = select(func.count()).where(AssemblyOrder.item_id == kit.output_item_id)
        assert await session.scalar(raised) == 0

    async def test_this_companys_sales_order_is_kept_as_the_source(
        self, api_client: AsyncClient, session, tenant, kit
    ) -> None:
        ours = await sales_order(session, tenant)

        data = ok(await api_client.post("/assembly", json=raised_for(kit, ours)), 201)

        assert (data["source_type"], data["source_id"]) == ("SALES_ORDER", str(ours))
        assert await column(session, AssemblyOrder.source_id, uuid.UUID(data["id"])) == ours
