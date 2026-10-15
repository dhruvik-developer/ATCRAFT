"""Assembly over HTTP: the whole run from a screen's point of view (WP E4).

The rules are covered in ``tests/integration/test_assembly_completion.py``.
These assert the boundary a screen meets: the new reads are gated on
``assembly_order.view`` and the hand-over to packing on ``packing.manage``,
exactly as the route decorators declare; an order taken DRAFT -> QC_PENDING
through the API lists on the board; its own verdict endpoint refuses and names
the inspection; deciding the inspection on /quality completes it; and the
completed order is packed through ``/packing/from-assembly``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.database import transaction
from src.core.deps import Principal, TenantContext
from src.core.permissions import Permission
from src.modules.bom.models import Bom, BomLine, BomStatus, BomType
from src.modules.inventory import service as inv
from src.modules.inventory.models import TransactionType
from src.modules.products.models import ItemGroup
from tests import seed_support

pytestmark = [pytest.mark.api, pytest.mark.integration]


def _holding(base: Principal, *permissions: Permission) -> Principal:
    """The same person, holding exactly these rights."""
    return Principal(
        user_id=base.user_id,
        company_id=base.company_id,
        email=base.email,
        full_name=base.full_name,
        is_superuser=False,
        permissions=frozenset(str(p) for p in permissions),
        role_codes=base.role_codes,
    )


def _as(principal: Principal) -> None:
    from src.core.deps import get_current_principal
    from src.main import app

    app.dependency_overrides[get_current_principal] = lambda: principal


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6].upper()}"


async def _lock_set(session: AsyncSession, tenant: TenantContext) -> dict[str, uuid.UUID]:
    """A lock set (ASSEMBLY group) of two components, stocked, with its BOM."""
    uom = await seed_support.seed_uom(session, company_id=tenant.company_id)
    store = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=_code("WH")
    )
    finished = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=_code("FG")
    )
    output = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=_code("LOCKSET"),
        base_uom_id=uom.id,
        item_group=ItemGroup.ASSEMBLY,
        can_manufacture=True,
    )
    bom = Bom(
        company_id=tenant.company_id,
        code=_code("BOM"),
        item_id=output.id,
        version=1,
        status=BomStatus.APPROVED,
        bom_type=BomType.ASSEMBLY,
        is_default=True,
        output_quantity=Decimal("1"),
        base_uom_id=uom.id,
        effective_from=today(),
        approved_by_id=tenant.user_id,
        approved_at=utcnow(),
    )
    session.add(bom)
    await session.flush()
    for number, quantity_per in enumerate((Decimal("1"), Decimal("2")), start=1):
        part = await seed_support.seed_item(
            session,
            company_id=tenant.company_id,
            code=_code(f"PART{number}"),
            base_uom_id=uom.id,
            item_group=ItemGroup.COMPONENT,
            standard_cost=Decimal("10"),
            can_be_assembly_component=True,
        )
        session.add(
            BomLine(
                company_id=tenant.company_id,
                bom_id=bom.id,
                parent_item_id=output.id,
                line_number=number,
                component_item_id=part.id,
                quantity_per=quantity_per,
                uom_id=uom.id,
            )
        )
        async with transaction(session):
            await inv.post_movement(
                session,
                tenant,
                item_id=part.id,
                warehouse_id=store.id,
                quantity=Decimal("100"),
                transaction_type=TransactionType.OPENING_BALANCE,
                reference_type="opening",
                reference_id=None,
                reference_code="OPENING",
                reason="assembly http fixture opening stock",
                unit_cost=Decimal("10"),
            )
    await session.flush()
    return {"item": output.id, "store": store.id, "finished": finished.id}


class TestPermissions:
    async def test_the_board_and_the_kit_read_need_assembly_view(
        self, api_client, principal: Principal
    ) -> None:
        unknown = uuid.uuid4()
        _as(_holding(principal, Permission.PACKING_VIEW))
        for path in ("/assembly/board/lines", "/assembly/board/orders"):
            refused = await api_client.get(path)
            assert refused.status_code == 403
            assert refused.json()["error"]["details"]["required_permission"] == (
                "assembly_order.view"
            )
        assert (await api_client.get(f"/assembly/{unknown}/availability")).status_code == 403

        _as(_holding(principal, Permission.ASSEMBLY_ORDER_VIEW))
        assert (await api_client.get("/assembly/board/lines")).status_code == 200
        assert (await api_client.get("/assembly/board/orders")).status_code == 200
        assert (await api_client.get(f"/assembly/{unknown}/availability")).status_code == 404

    async def test_packing_from_an_assembly_needs_packing_manage(
        self, api_client, principal: Principal
    ) -> None:
        body = {"assembly_order_id": str(uuid.uuid4())}
        _as(_holding(principal, Permission.ASSEMBLY_ORDER_MANAGE, Permission.PACKING_VIEW))
        refused = await api_client.post("/packing/from-assembly", json=body)
        assert refused.status_code == 403
        assert refused.json()["error"]["details"]["required_permission"] == "packing.manage"

        _as(_holding(principal, Permission.PACKING_MANAGE))
        # Through the gate, and refused by the service for an order that is not there.
        assert (await api_client.post("/packing/from-assembly", json=body)).status_code == 404

    async def test_asking_for_one_line_and_the_unassigned_ones_is_refused(self, api_client) -> None:
        response = await api_client.get(
            "/assembly/board/orders", params={"assembly_line": "Bench 1", "unassigned_line": "true"}
        )
        assert response.status_code == 422


class TestTheRunOverHttp:
    async def test_draft_to_packed_with_the_verdict_recorded_once_on_quality(
        self, api_client, session: AsyncSession, tenant: TenantContext
    ) -> None:
        kit = await _lock_set(session, tenant)
        line = _code("BENCH")
        created = await api_client.post(
            "/assembly",
            json={
                "item_id": str(kit["item"]),
                "planned_quantity": "4",
                "warehouse_id": str(kit["store"]),
                "output_warehouse_id": str(kit["finished"]),
                "assembly_line": line,
                "priority": 5,
                "requires_qc": True,
            },
        )
        assert created.status_code == 201
        order = created.json()["data"]
        order_id = order["id"]
        assert order["priority"] == 5

        kit_read = await api_client.get(f"/assembly/{order_id}/availability")
        assert kit_read.status_code == 200
        assert [row["shortage_quantity"] for row in kit_read.json()["data"]] == ["0.0000", "0.0000"]

        for action in ("approve", "release", "issue-components", "start"):
            moved = await api_client.post(
                f"/assembly/{order_id}/{action}", json={"reason": f"{action} over http"}
            )
            assert moved.status_code == 200, moved.json()
        completed = await api_client.post(
            f"/assembly/{order_id}/complete",
            json={"assembled_quantity": "4", "reason": "Bench finished"},
        )
        assert completed.status_code == 200
        inspection_id = completed.json()["data"]["order"]["qc_inspection_id"]
        assert inspection_id is not None

        board = await api_client.get("/assembly/board/orders", params={"assembly_line": line})
        assert [row["id"] for row in board.json()["data"]] == [order_id]
        assert board.json()["data"][0]["status"] == "QC_PENDING"

        refused = await api_client.post(
            f"/assembly/{order_id}/qc-result",
            json={"accepted_quantity": "4", "reason": "Recorded on the order"},
        )
        assert refused.status_code == 422
        error = refused.json()["error"]
        assert error["code"] == "INVALID_STATE_TRANSITION"
        assert error["details"]["qc_inspection_id"] == inspection_id

        decided = await api_client.post(
            f"/quality/inspections/{inspection_id}/decide",
            json={"accepted_quantity": "4", "reason": "Every set keyed and turned"},
        )
        assert decided.status_code == 200
        reloaded = (await api_client.get(f"/assembly/{order_id}")).json()["data"]
        assert reloaded["status"] == "COMPLETED"
        assert reloaded["assembled_quantity"] == "4.0000"

        ready = await api_client.get(
            "/assembly/ready-for-packing", params={"warehouse_id": kit["finished"]}
        )
        rows = ready.json()["data"]
        assert [row["assembly_order_id"] for row in rows] == [order_id]
        assert rows[0]["source_type"] == "MANUAL"

        customer = await seed_support.seed_customer(session, company_id=tenant.company_id)
        packed = await api_client.post(
            "/packing/from-assembly",
            json={"assembly_order_id": order_id, "customer_id": str(customer.id)},
        )
        assert packed.status_code == 201
        packing_order = packed.json()["data"]
        assert packing_order["warehouse_id"] == str(kit["finished"])
        assert packing_order["lines"][0]["quantity_to_pack"] == "4.0000"

        claimed = (await api_client.get(f"/assembly/{order_id}")).json()["data"]
        assert claimed["packing_order_id"] == packing_order["id"]
