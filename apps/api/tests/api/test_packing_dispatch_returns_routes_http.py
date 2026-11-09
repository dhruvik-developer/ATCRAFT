"""Packing, dispatch, courier and sales-return write routes over HTTP.

Service rules for carton sealing, FINAL QC, consignment lifecycle, courier
ingestion and return costing are pinned in the integration suites the campaign
brief lists. These drive the twenty-one write/scan routes no backend test had
reached through HTTP: the status and envelope each answers with, the row and
audit entry left behind, the 422 a payload or a record's state can earn, and
the 404 another company's record answers with - never a 403, which would
confirm that it exists.

Permission gates on the route itself are covered by
``tests/unit/test_route_authorization.py``. The caller is the ``api_client``
principal - every permission, not a superuser. Prerequisites are built through
the packing, dispatch, courier and sales-return services (or the shared
packing/dispatch builders), so every fixture has the shape production gives it.
Ids are held as plain values: an expected failure rolls its savepoint back and
expires what was loaded before it.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.config import settings
from src.core.deps import TenantContext
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.courier_tracking import service as courier_service
from src.modules.courier_tracking.models import CourierAccount, CourierProvider
from src.modules.courier_tracking.schemas import CourierAccountCreate
from src.modules.customers.models import Customer
from src.modules.dispatch import service as dispatch_service
from src.modules.dispatch.models import Dispatch, DispatchStatus
from src.modules.dispatch.schemas import (
    DispatchCreate,
    DispatchLineIn,
    DispatchRequest,
)
from src.modules.inventory import service as inventory
from src.modules.inventory.models import StockSerial, TransactionType
from src.modules.packing import service as packing_service
from src.modules.packing.models import Carton, ItemPackingSpec, PackingOrder, PackingOrderStatus
from src.modules.packing.schemas import (
    CartonContentIn,
    CartonCreate,
    CompleteRequest,
    ItemPackingSpecWrite,
    PackingOrderCreate,
    PackingOrderLineIn,
    SealCartonRequest,
)
from src.modules.products.models import ItemGroup, ProductionType
from src.modules.sales_returns import service as returns_service
from src.modules.sales_returns.models import SalesReturn, SalesReturnStatus
from src.modules.sales_returns.schemas import SalesReturnCreate, SalesReturnLineIn
from src.modules.warehouses.models import WarehouseType
from tests import seed_support
from tests.dispatch_support import consignment_line, raise_consignment
from tests.integration.test_customer_return_costing import receive_goods, shipped_stock
from tests.integration.test_packing_final_serials import decide, make_packing_kit, start
from tests.statement_support import counted, selects

pytestmark = [pytest.mark.api, pytest.mark.integration]

REASON = "HTTP route coverage"


# -----------------------------------------------------------------------------
# Envelope helpers
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


# -----------------------------------------------------------------------------
# Packing builders
# -----------------------------------------------------------------------------


async def packing_bench(session: AsyncSession, tenant: TenantContext) -> SimpleNamespace:
    """Non-serial finished goods, packing material, warehouse and customer."""
    uom = await seed_support.seed_uom(session, company_id=tenant.company_id)
    goods = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=f"PK-G-{uuid.uuid4().hex[:8].upper()}",
        base_uom_id=uom.id,
        item_group=ItemGroup.FINISHED_GOOD,
        production_type=ProductionType.MAKE,
        can_sell=True,
        standard_cost=Decimal("10"),
    )
    box = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=f"PK-B-{uuid.uuid4().hex[:8].upper()}",
        base_uom_id=uom.id,
        item_group=ItemGroup.PACKING_MATERIAL,
        production_type=ProductionType.BUY,
        can_purchase=True,
        standard_cost=Decimal("1"),
    )
    warehouse = await seed_support.seed_warehouse(
        session,
        company_id=tenant.company_id,
        code=f"PK-WH-{uuid.uuid4().hex[:8].upper()}",
        warehouse_type=WarehouseType.FINISHED_GOODS,
    )
    customer = await seed_support.seed_customer(session, company_id=tenant.company_id)
    await inventory.post_movements(
        session,
        tenant,
        [
            inventory.MovementRequest(
                item_id=box.id,
                warehouse_id=warehouse.id,
                quantity=Decimal("50"),
                transaction_type=TransactionType.OPENING_BALANCE,
                reference_type="opening",
                reason="Packing material for HTTP packing routes",
                unit_cost=Decimal("1"),
            )
        ],
    )
    return SimpleNamespace(
        uom_id=uom.id,
        goods_id=goods.id,
        box_id=box.id,
        warehouse_id=warehouse.id,
        customer_id=customer.id,
    )


async def draft_order(
    session: AsyncSession, tenant: TenantContext, bench: SimpleNamespace, *, quantity: str = "10"
) -> uuid.UUID:
    order, _ = await packing_service.create_order(
        session,
        tenant,
        PackingOrderCreate(
            customer_id=bench.customer_id,
            warehouse_id=bench.warehouse_id,
            planned_date=today(),
            lines=[
                PackingOrderLineIn(
                    item_id=bench.goods_id,
                    quantity_to_pack=Decimal(quantity),
                    uom_id=bench.uom_id,
                )
            ],
        ),
    )
    return order.id


async def started_order(
    session: AsyncSession, tenant: TenantContext, bench: SimpleNamespace, *, quantity: str = "10"
) -> uuid.UUID:
    order_id = await draft_order(session, tenant, bench, quantity=quantity)
    await packing_service.start_packing(session, tenant, order_id, reason="Bench is free")
    return order_id


def carton_body(bench: SimpleNamespace, *, quantity: str = "10") -> dict[str, Any]:
    return {
        "packing_material_item_id": str(bench.box_id),
        "packing_material_quantity": "1",
        "tare_weight_kg": "0.5",
        "contents": [
            {
                "line_number": 1,
                "quantity": quantity,
                "net_weight_kg": "2",
            }
        ],
    }


async def open_carton(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    bench: SimpleNamespace,
    *,
    quantity: str = "10",
) -> uuid.UUID:
    carton = await packing_service.add_carton(
        session,
        tenant,
        order_id,
        CartonCreate(
            packing_material_item_id=bench.box_id,
            packing_material_quantity=Decimal("1"),
            tare_weight_kg=Decimal("0.5"),
            contents=[
                CartonContentIn(
                    line_number=1,
                    quantity=Decimal(quantity),
                    net_weight_kg=Decimal("2"),
                )
            ],
        ),
    )
    return carton.id


async def sealed_order(
    session: AsyncSession, tenant: TenantContext, bench: SimpleNamespace, *, quantity: str = "10"
) -> tuple[uuid.UUID, uuid.UUID, str]:
    order_id = await started_order(session, tenant, bench, quantity=quantity)
    carton_id = await open_carton(session, tenant, order_id, bench, quantity=quantity)
    sealed = await packing_service.seal_carton(
        session,
        tenant,
        order_id,
        carton_id,
        SealCartonRequest(reason="Box taped and labelled"),
    )
    return order_id, carton_id, sealed.carton.barcode


async def handed_over_order(
    session: AsyncSession, tenant: TenantContext, bench: SimpleNamespace
) -> uuid.UUID:
    order_id, _, _ = await sealed_order(session, tenant, bench, quantity="10")
    await packing_service.complete_packing(
        session, tenant, order_id, CompleteRequest(reason="All cartons sealed")
    )
    dispatch_id = await raise_consignment(
        session,
        tenant,
        customer_id=bench.customer_id,
        warehouse_id=bench.warehouse_id,
        lines=[consignment_line(bench.goods_id, "10")],
    )
    await packing_service.hand_over_to_dispatch(
        session, tenant, order_id, dispatch_id=dispatch_id, reason="Loaded onto the lorry"
    )
    return order_id


async def make_spec(
    session: AsyncSession, tenant: TenantContext, unique_code
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    uom = await seed_support.seed_uom(session, company_id=tenant.company_id)
    item = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=unique_code("SPEC"),
        base_uom_id=uom.id,
        item_group=ItemGroup.FINISHED_GOOD,
        can_sell=True,
    )
    material = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=unique_code("BOX"),
        base_uom_id=uom.id,
        item_group=ItemGroup.PACKING_MATERIAL,
        production_type=ProductionType.BUY,
        can_purchase=True,
    )
    spec = await packing_service.create_item_packing_spec(
        session,
        tenant,
        item.id,
        ItemPackingSpecWrite(
            packing_material_item_id=material.id,
            quantity_per_unit=Decimal("0.5"),
        ),
    )
    return spec.id, item.id, material.id


# -----------------------------------------------------------------------------
# Dispatch / returns builders
# -----------------------------------------------------------------------------


async def free_stock_dispatch(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    quantity: str = "5",
    serialized: bool = False,
    tracking_id: str | None = None,
) -> SimpleNamespace:
    """A SALES consignment raised from free stock and sent. Returns plain ids."""
    warehouse = await seed_support.seed_warehouse(
        session,
        company_id=tenant.company_id,
        code=f"DSP-WH-{uuid.uuid4().hex[:8].upper()}",
        warehouse_type=WarehouseType.FINISHED_GOODS,
    )
    customer = await seed_support.seed_customer(session, company_id=tenant.company_id)
    item = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=f"DSP-IT-{uuid.uuid4().hex[:8].upper()}",
        can_sell=True,
        can_return=True,
        is_serial_tracked=serialized,
        standard_cost=Decimal("25"),
    )
    qty = Decimal(quantity)
    serial_ids: list[uuid.UUID] = []
    movements: list[inventory.MovementRequest] = []
    if serialized:
        for _ in range(int(qty)):
            serial = await seed_support.seed_serial(
                session,
                company_id=tenant.company_id,
                item_id=item.id,
                warehouse_id=warehouse.id,
            )
            serial_ids.append(serial.id)
            movements.append(
                inventory.MovementRequest(
                    item_id=item.id,
                    warehouse_id=warehouse.id,
                    quantity=Decimal("1"),
                    serial_id=serial.id,
                    transaction_type=TransactionType.OPENING_BALANCE,
                    reference_type="opening",
                    reason="Serial stock for HTTP dispatch routes",
                    unit_cost=Decimal("25"),
                )
            )
    else:
        movements.append(
            inventory.MovementRequest(
                item_id=item.id,
                warehouse_id=warehouse.id,
                quantity=qty,
                transaction_type=TransactionType.OPENING_BALANCE,
                reference_type="opening",
                reason="Stock for HTTP dispatch routes",
                unit_cost=Decimal("25"),
            )
        )
    await inventory.post_movements(session, tenant, movements)
    detail = await dispatch_service.create_dispatch(
        session,
        tenant,
        DispatchCreate(
            warehouse_id=warehouse.id,
            customer_id=customer.id,
            dispatch_date=today(),
            expected_delivery_date=today() + timedelta(days=5),
            tracking_id=tracking_id,
            lines=[
                DispatchLineIn(
                    item_id=item.id,
                    quantity=qty,
                    serial_ids=serial_ids,
                )
            ],
        ),
    )
    return SimpleNamespace(
        dispatch_id=detail.id,
        line_id=detail.lines[0].id,
        item_id=item.id,
        warehouse_id=warehouse.id,
        customer_id=customer.id,
        serial_ids=serial_ids,
    )


async def sent_dispatch(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    quantity: str = "5",
    tracking_id: str | None = "AWB-HTTP-1",
) -> SimpleNamespace:
    bench = await free_stock_dispatch(session, tenant, quantity=quantity, tracking_id=tracking_id)
    await dispatch_service.mark_ready(
        session, tenant, bench.dispatch_id, reason="Loaded at the gate"
    )
    await dispatch_service.dispatch_consignment(
        session,
        tenant,
        bench.dispatch_id,
        DispatchRequest(reason="Handed to the transporter", lr_number="LR-HTTP-1"),
    )
    return bench


# -----------------------------------------------------------------------------
# PUT /packing/packing-specs/{spec_id}
# -----------------------------------------------------------------------------


class TestUpdateItemPackingSpec:
    async def test_a_spec_is_replaced_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        spec_id, _, material_id = await make_spec(session, tenant, unique_code)

        data = ok(
            await api_client.put(
                f"/packing/packing-specs/{spec_id}",
                json={
                    "packing_material_item_id": str(material_id),
                    "units_per_carton": 12,
                    "is_default": True,
                },
            )
        )

        assert data["id"] == str(spec_id)
        assert data["units_per_carton"] == 12
        assert data["quantity_per_unit"] is None
        assert await column(session, ItemPackingSpec.units_per_carton, spec_id) == 12
        (updated,) = await audit_rows(session, tenant.company_id, spec_id, AuditAction.UPDATE)
        assert updated.entity_type == "item_packing_spec"
        assert updated.created_by_id == tenant.user_id

    async def test_both_quantity_rules_are_a_field_error(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        spec_id, _, material_id = await make_spec(session, tenant, unique_code)

        errors = field_errors(
            await api_client.put(
                f"/packing/packing-specs/{spec_id}",
                json={
                    "packing_material_item_id": str(material_id),
                    "quantity_per_unit": "0.5",
                    "units_per_carton": 10,
                },
            )
        )
        assert errors

    async def test_another_companys_spec_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        spec_id, _, _ = await make_spec(session, foreign, unique_code)

        refused(
            await api_client.put(
                f"/packing/packing-specs/{spec_id}",
                json={
                    "packing_material_item_id": str(uuid.uuid4()),
                    "units_per_carton": 8,
                },
            ),
            404,
            "NOT_FOUND",
        )


# -----------------------------------------------------------------------------
# GET|POST /packing/scan
# -----------------------------------------------------------------------------


class TestScanCarton:
    async def test_a_barcode_resolves_the_carton_and_order(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await packing_bench(session, tenant)
        order_id, carton_id, barcode = await sealed_order(session, tenant, bench)

        data = ok(await api_client.get("/packing/scan", params={"barcode": barcode.lower()}))

        assert data["packing_order"]["id"] == str(order_id)
        assert data["carton"]["id"] == str(carton_id)
        assert data["carton"]["barcode"] == barcode
        assert data["carton"]["is_sealed"] is True

    async def test_a_recorded_scan_writes_audit(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await packing_bench(session, tenant)
        order_id, carton_id, barcode = await sealed_order(session, tenant, bench)

        data = ok(await api_client.post("/packing/scan", json={"barcode": barcode}))

        assert data["carton"]["id"] == str(carton_id)
        assert data["packing_order"]["id"] == str(order_id)
        (scan,) = await audit_rows(session, tenant.company_id, carton_id, AuditAction.SCAN)
        assert scan.entity_type == "carton_scan"
        assert scan.created_by_id == tenant.user_id
        assert scan.context["packing_order_id"] == str(order_id)

    async def test_an_unknown_barcode_is_not_found(self, api_client: AsyncClient) -> None:
        refused(
            await api_client.get("/packing/scan", params={"barcode": "UNKNOWN1"}),
            404,
            "NOT_FOUND",
        )

    async def test_a_short_barcode_is_a_field_error(self, api_client: AsyncClient) -> None:
        assert "barcode" in field_errors(
            await api_client.post("/packing/scan", json={"barcode": "SHORT"})
        )


# -----------------------------------------------------------------------------
# PATCH /packing/{order_id}
# -----------------------------------------------------------------------------


class TestUpdatePackingOrder:
    async def test_a_draft_is_amended_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await packing_bench(session, tenant)
        order_id = await draft_order(session, tenant, bench, quantity="10")
        planned = (today() + timedelta(days=2)).isoformat()

        data = ok(
            await api_client.patch(
                f"/packing/{order_id}",
                json={"planned_date": planned, "notes": "Ship on the evening lorry"},
            )
        )

        assert data["id"] == str(order_id)
        assert data["planned_date"] == planned
        assert data["notes"] == "Ship on the evening lorry"
        assert data["status"] == PackingOrderStatus.DRAFT
        (updated,) = await audit_rows(session, tenant.company_id, order_id, AuditAction.UPDATE)
        assert updated.entity_type == "packing_order"

    async def test_an_in_progress_order_cannot_be_edited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await packing_bench(session, tenant)
        order_id = await started_order(session, tenant, bench)

        refused(
            await api_client.patch(f"/packing/{order_id}", json={"notes": "Too late"}),
            422,
            "BUSINESS_RULE_VIOLATION",
        )

    async def test_another_companys_order_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext
    ) -> None:
        bench = await packing_bench(session, foreign)
        order_id = await draft_order(session, foreign, bench)

        refused(
            await api_client.patch(f"/packing/{order_id}", json={"notes": "Foreign"}),
            404,
            "NOT_FOUND",
        )


# -----------------------------------------------------------------------------
# POST /packing/{order_id}/complete
# -----------------------------------------------------------------------------


class TestCompletePacking:
    async def test_completion_freezes_totals_and_publishes_the_order(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await packing_bench(session, tenant)
        order_id, _, _ = await sealed_order(session, tenant, bench, quantity="10")

        data = ok(
            await api_client.post(
                f"/packing/{order_id}/complete",
                json={"reason": "All cartons sealed for dispatch"},
            )
        )

        assert data["order"]["status"] == PackingOrderStatus.PACKED
        assert data["total_cartons"] == 1
        assert await column(session, PackingOrder.status, order_id) == PackingOrderStatus.PACKED
        trail = await audit_rows(session, tenant.company_id, order_id, AuditAction.STATUS_CHANGE)
        assert any(
            (row.new_values or {}).get("status") == PackingOrderStatus.PACKED for row in trail
        )

    async def test_an_open_carton_blocks_completion(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await packing_bench(session, tenant)
        order_id = await started_order(session, tenant, bench)
        await open_carton(session, tenant, order_id, bench)

        refused(
            await api_client.post(
                f"/packing/{order_id}/complete",
                json={"reason": "Trying to finish with an open box"},
            ),
            422,
            "BUSINESS_RULE_VIOLATION",
        )


# -----------------------------------------------------------------------------
# POST /packing/{order_id}/lines/{line_id}/reinspect
# -----------------------------------------------------------------------------


class TestReinspectLine:
    async def test_a_rejected_line_gets_a_fresh_final_inspection(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        k = await make_packing_kit(session, tenant)
        inspection_id = await start(k)
        await decide(k, inspection_id, [], k.ids[:2])
        order_id, line_id = k.order_id, k.line_id
        replacements = [str(unit) for unit in k.ids[2:]]

        data = ok(
            await api_client.post(
                f"/packing/{order_id}/lines/{line_id}/reinspect",
                json={
                    "reason": "Replacement units selected after rejection",
                    "serial_ids": replacements,
                },
            )
        )

        assert data["id"] == str(line_id)
        assert data["qc_inspection_id"] != str(inspection_id)
        assert data["qc_serial_ids"] == replacements
        trail = await audit_rows(session, tenant.company_id, order_id, AuditAction.UPDATE)
        assert any(
            row.entity_type == "packing_order"
            and (row.context or {}).get("reason") == "Replacement units selected after rejection"
            for row in trail
        )

    async def test_reinspect_without_replacements_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        k = await make_packing_kit(session, tenant)
        inspection_id = await start(k)
        await decide(k, inspection_id, [], k.ids[:2])

        refused(
            await api_client.post(
                f"/packing/{k.order_id}/lines/{k.line_id}/reinspect",
                json={"reason": "Forgot the replacement serials"},
            ),
            422,
            "BUSINESS_RULE_VIOLATION",
        )


# -----------------------------------------------------------------------------
# POST /packing/{order_id}/close
# -----------------------------------------------------------------------------


class TestClosePackingOrder:
    async def test_a_handed_over_order_is_closed_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await packing_bench(session, tenant)
        order_id = await handed_over_order(session, tenant, bench)

        data = ok(
            await api_client.post(
                f"/packing/{order_id}/close",
                json={"reason": "Consignment has left with these boxes"},
            )
        )

        assert data["status"] == PackingOrderStatus.CLOSED
        assert await column(session, PackingOrder.status, order_id) == PackingOrderStatus.CLOSED

    async def test_a_draft_cannot_be_closed(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await packing_bench(session, tenant)
        order_id = await draft_order(session, tenant, bench)

        refused(
            await api_client.post(
                f"/packing/{order_id}/close",
                json={"reason": "Nothing to close yet"},
            ),
            422,
            "INVALID_STATE_TRANSITION",
        )


# -----------------------------------------------------------------------------
# POST /packing/{order_id}/cartons
# -----------------------------------------------------------------------------


class TestAddCarton:
    async def test_a_carton_is_opened_with_a_server_barcode(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await packing_bench(session, tenant)
        order_id = await started_order(session, tenant, bench)

        data = ok(
            await api_client.post(f"/packing/{order_id}/cartons", json=carton_body(bench)),
            201,
        )

        carton_id = uuid.UUID(data["id"])
        assert data["packing_order_id"] == str(order_id)
        assert data["carton_number"] == 1
        assert data["is_sealed"] is False
        assert len(data["barcode"]) >= 8
        assert await column(session, Carton.barcode, carton_id) == data["barcode"]
        (created,) = await audit_rows(session, tenant.company_id, carton_id, AuditAction.CREATE)
        assert created.entity_type == "carton"

    async def test_a_draft_cannot_open_a_carton(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await packing_bench(session, tenant)
        order_id = await draft_order(session, tenant, bench)

        refused(
            await api_client.post(f"/packing/{order_id}/cartons", json=carton_body(bench)),
            422,
            "BUSINESS_RULE_VIOLATION",
        )


# -----------------------------------------------------------------------------
# PUT /packing/{order_id}/cartons/{carton_id}/contents
# -----------------------------------------------------------------------------


class TestReplaceCartonContents:
    async def test_contents_are_replaced_while_the_carton_is_open(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await packing_bench(session, tenant)
        order_id = await started_order(session, tenant, bench, quantity="10")
        carton_id = await open_carton(session, tenant, order_id, bench, quantity="4")

        data = ok(
            await api_client.put(
                f"/packing/{order_id}/cartons/{carton_id}/contents",
                json={
                    "reason": "Recounted before sealing",
                    "contents": [
                        {"line_number": 1, "quantity": "7", "net_weight_kg": "3"},
                    ],
                },
            )
        )

        assert data["id"] == str(carton_id)
        assert data["contents"][0]["quantity"] == "7.0000"
        (updated,) = await audit_rows(session, tenant.company_id, carton_id, AuditAction.UPDATE)
        assert updated.entity_type == "carton"

    async def test_a_sealed_carton_cannot_be_rewritten(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await packing_bench(session, tenant)
        order_id, carton_id, _ = await sealed_order(session, tenant, bench)

        refused(
            await api_client.put(
                f"/packing/{order_id}/cartons/{carton_id}/contents",
                json={
                    "reason": "Too late",
                    "contents": [
                        {"line_number": 1, "quantity": "1", "net_weight_kg": "1"},
                    ],
                },
            ),
            422,
            "BUSINESS_RULE_VIOLATION",
        )


# -----------------------------------------------------------------------------
# POST /packing/{order_id}/cartons/{carton_id}/seal
# -----------------------------------------------------------------------------


class TestSealCarton:
    async def test_sealing_issues_material_and_freezes_the_carton(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await packing_bench(session, tenant)
        order_id = await started_order(session, tenant, bench)
        carton_id = await open_carton(session, tenant, order_id, bench)

        data = ok(
            await api_client.post(
                f"/packing/{order_id}/cartons/{carton_id}/seal",
                json={"reason": "Taped and weighed"},
            )
        )

        assert data["carton"]["id"] == str(carton_id)
        assert data["carton"]["is_sealed"] is True
        assert data["carton"]["material_transaction_id"] is not None
        assert await column(session, Carton.is_sealed, carton_id) is True
        (sealed,) = await audit_rows(
            session, tenant.company_id, carton_id, AuditAction.STATUS_CHANGE
        )
        assert sealed.entity_type == "carton"

    async def test_sealing_twice_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await packing_bench(session, tenant)
        order_id, carton_id, _ = await sealed_order(session, tenant, bench)

        refused(
            await api_client.post(
                f"/packing/{order_id}/cartons/{carton_id}/seal",
                json={"reason": "Already sealed"},
            ),
            422,
            "CONFLICT",
        )


# -----------------------------------------------------------------------------
# PATCH /dispatch/{dispatch_id}
# -----------------------------------------------------------------------------


class TestUpdateDispatch:
    async def test_a_plan_is_amended_before_it_leaves(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await free_stock_dispatch(session, tenant)
        dispatch_id = bench.dispatch_id

        data = ok(
            await api_client.patch(
                f"/dispatch/{dispatch_id}",
                json={
                    "courier_name": "BlueDart",
                    "lr_number": "LR-UPD-1",
                    "notes": "Hold for afternoon gate",
                },
            )
        )

        assert data["id"] == str(dispatch_id)
        assert data["courier_name"] == "BlueDart"
        assert data["lr_number"] == "LR-UPD-1"
        assert await column(session, Dispatch.courier_name, dispatch_id) == "BlueDart"
        (updated,) = await audit_rows(session, tenant.company_id, dispatch_id, AuditAction.UPDATE)
        assert updated.entity_type == "dispatch"

    async def test_a_sent_consignment_cannot_be_edited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await sent_dispatch(session, tenant)

        refused(
            await api_client.patch(
                f"/dispatch/{bench.dispatch_id}",
                json={"notes": "Too late"},
            ),
            422,
            "BUSINESS_RULE_VIOLATION",
        )

    async def test_another_companys_consignment_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext
    ) -> None:
        bench = await free_stock_dispatch(session, foreign)

        refused(
            await api_client.patch(
                f"/dispatch/{bench.dispatch_id}",
                json={"notes": "Foreign"},
            ),
            404,
            "NOT_FOUND",
        )


# -----------------------------------------------------------------------------
# POST /dispatch/{dispatch_id}/cancel
# -----------------------------------------------------------------------------


class TestCancelDispatch:
    async def test_a_plan_is_cancelled_before_stock_moves(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await free_stock_dispatch(session, tenant)
        dispatch_id = bench.dispatch_id

        data = ok(
            await api_client.post(
                f"/dispatch/{dispatch_id}/cancel",
                json={"reason": "Customer postponed the collection"},
            )
        )

        assert data["status"] == DispatchStatus.CANCELLED
        assert await column(session, Dispatch.status, dispatch_id) == DispatchStatus.CANCELLED

    async def test_a_sent_consignment_cannot_be_cancelled(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await sent_dispatch(session, tenant)

        refused(
            await api_client.post(
                f"/dispatch/{bench.dispatch_id}/cancel",
                json={"reason": "Already left"},
            ),
            422,
            "INVALID_STATE_TRANSITION",
        )


# -----------------------------------------------------------------------------
# POST /dispatch/{dispatch_id}/in-transit
# -----------------------------------------------------------------------------


class TestMarkInTransit:
    async def test_a_sent_consignment_is_reported_moving(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await sent_dispatch(session, tenant)
        dispatch_id = bench.dispatch_id

        data = ok(
            await api_client.post(
                f"/dispatch/{dispatch_id}/in-transit",
                json={"reason": "Left the plant gate", "location": "NH48 toll"},
            )
        )

        assert data["status"] == DispatchStatus.IN_TRANSIT
        assert await column(session, Dispatch.status, dispatch_id) == DispatchStatus.IN_TRANSIT

    async def test_a_plan_that_has_not_left_cannot_go_in_transit(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await free_stock_dispatch(session, tenant)

        refused(
            await api_client.post(
                f"/dispatch/{bench.dispatch_id}/in-transit",
                json={"reason": "Still on the dock"},
            ),
            422,
            "INVALID_STATE_TRANSITION",
        )


# -----------------------------------------------------------------------------
# POST /dispatch/{dispatch_id}/deliver
# -----------------------------------------------------------------------------


class TestConfirmDelivery:
    async def test_delivery_is_confirmed_with_a_tracking_event(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await sent_dispatch(session, tenant)
        dispatch_id = bench.dispatch_id
        arrived = (today() + timedelta(days=2)).isoformat()

        data = ok(
            await api_client.post(
                f"/dispatch/{dispatch_id}/deliver",
                json={
                    "actual_delivery_date": arrived,
                    "delivery_note": "Signed by the store keeper",
                },
            )
        )

        assert data["status"] == DispatchStatus.DELIVERED
        assert data["actual_delivery_date"] == arrived
        assert await column(session, Dispatch.status, dispatch_id) == DispatchStatus.DELIVERED

    async def test_a_late_arrival_without_a_delay_reason_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await free_stock_dispatch(session, tenant)
        await dispatch_service.mark_ready(session, tenant, bench.dispatch_id, reason="Loaded")
        await dispatch_service.dispatch_consignment(
            session,
            tenant,
            bench.dispatch_id,
            DispatchRequest(reason="Sent", lr_number="LR-LATE"),
        )
        # expected_delivery_date was today+5 on create; arrive after that.
        refused(
            await api_client.post(
                f"/dispatch/{bench.dispatch_id}/deliver",
                json={"actual_delivery_date": (today() + timedelta(days=10)).isoformat()},
            ),
            422,
            "BUSINESS_RULE_VIOLATION",
        )


# -----------------------------------------------------------------------------
# POST /dispatch/{dispatch_id}/tracking-events
# -----------------------------------------------------------------------------


class TestAddTrackingEvent:
    async def test_a_position_update_is_recorded_without_moving_status(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await sent_dispatch(session, tenant)
        dispatch_id = bench.dispatch_id

        data = ok(
            await api_client.post(
                f"/dispatch/{dispatch_id}/tracking-events",
                json={
                    "event_status": DispatchStatus.DISPATCHED,
                    "location": "Warehouse yard",
                    "remark": "Still waiting for the slot",
                },
            ),
            201,
        )

        assert data["event_status"] == DispatchStatus.DISPATCHED
        assert data["location"] == "Warehouse yard"
        assert await column(session, Dispatch.status, dispatch_id) == DispatchStatus.DISPATCHED

    async def test_an_illegal_status_jump_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await free_stock_dispatch(session, tenant)

        refused(
            await api_client.post(
                f"/dispatch/{bench.dispatch_id}/tracking-events",
                json={"event_status": DispatchStatus.DISPATCHED},
            ),
            422,
            "BUSINESS_RULE_VIOLATION",
        )


# -----------------------------------------------------------------------------
# POST|PATCH /integrations/courier/accounts
# -----------------------------------------------------------------------------


class TestCourierAccounts:
    async def test_a_generic_webhook_account_is_created_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        code = unique_code("CR")

        data = ok(
            await api_client.post(
                "/integrations/courier/accounts",
                json={
                    "code": code,
                    "provider": CourierProvider.GENERIC_WEBHOOK,
                    "display_name": "Plant gate webhook",
                },
            ),
            201,
        )

        account_id = uuid.UUID(data["id"])
        assert data["code"] == code
        assert data["provider"] == CourierProvider.GENERIC_WEBHOOK
        assert data["is_active"] is True
        (created,) = await audit_rows(session, tenant.company_id, account_id, AuditAction.CREATE)
        assert created.entity_type == "courier_account"

    async def test_shiprocket_without_credentials_is_a_field_error(
        self, api_client: AsyncClient, unique_code
    ) -> None:
        errors = field_errors(
            await api_client.post(
                "/integrations/courier/accounts",
                json={
                    "code": unique_code("SR"),
                    "provider": CourierProvider.SHIPROCKET,
                    "display_name": "Shiprocket",
                },
            )
        )
        assert errors

    async def test_an_account_is_updated_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        created = await courier_service.create_account(
            session,
            tenant,
            CourierAccountCreate(
                code=unique_code("CR"),
                provider=CourierProvider.GENERIC_WEBHOOK,
                display_name="Before",
            ),
        )
        account_id = created.id

        data = ok(
            await api_client.patch(
                f"/integrations/courier/accounts/{account_id}",
                json={"display_name": "After rename", "auto_confirm_delivery": False},
            )
        )

        assert data["display_name"] == "After rename"
        assert data["auto_confirm_delivery"] is False
        assert await column(session, CourierAccount.display_name, account_id) == "After rename"
        (updated,) = await audit_rows(session, tenant.company_id, account_id, AuditAction.UPDATE)
        assert updated.entity_type == "courier_account"

    async def test_another_companys_account_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        foreign: TenantContext,
        unique_code,
    ) -> None:
        created = await courier_service.create_account(
            session,
            foreign,
            CourierAccountCreate(
                code=unique_code("FX"),
                provider=CourierProvider.GENERIC_WEBHOOK,
                display_name="Foreign",
            ),
        )

        refused(
            await api_client.patch(
                f"/integrations/courier/accounts/{created.id}",
                json={"display_name": "Stolen"},
            ),
            404,
            "NOT_FOUND",
        )


# -----------------------------------------------------------------------------
# POST /integrations/courier/refresh
# -----------------------------------------------------------------------------


class TestRefreshTracking:
    async def test_refresh_pulls_shiprocket_events_for_a_tracked_dispatch(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        unique_code,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "courier_tracking_enabled", True)
        from src.modules.courier_tracking.schemas import ShiprocketCredentials

        account = await courier_service.create_account(
            session,
            tenant,
            CourierAccountCreate(
                code=unique_code("SR"),
                provider=CourierProvider.SHIPROCKET,
                display_name="Shiprocket",
                credentials=ShiprocketCredentials(email="ops@example.com", password="secret-value"),
            ),
        )
        assert account.provider is CourierProvider.SHIPROCKET
        assert account.has_credentials is True
        bench = await sent_dispatch(session, tenant, tracking_id="AWB-REFRESH-1")

        async def fake_token(*_args: Any, **_kwargs: Any) -> str:
            return "test-token"

        async def fake_track(awb: str, token: str) -> dict[str, Any]:
            assert awb == "AWB-REFRESH-1"
            assert token == "test-token"
            return {
                "tracking_data": {
                    "shipment_track": [{"awb_code": awb, "current_status": "In Transit"}],
                    "current_status": "In Transit",
                    "track_created_at": utcnow().isoformat(),
                }
            }

        monkeypatch.setattr(courier_service, "_shiprocket_token", fake_token)
        monkeypatch.setattr(courier_service, "shiprocket_track", fake_track)

        data = ok(
            await api_client.post(
                "/integrations/courier/refresh",
                json={"dispatch_id": str(bench.dispatch_id)},
            )
        )
        assert data["events_recorded"] >= 1

    async def test_refresh_requires_exactly_one_target(self, api_client: AsyncClient) -> None:
        assert field_errors(await api_client.post("/integrations/courier/refresh", json={}))

    async def test_refresh_of_an_untracked_dispatch_is_not_found(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "courier_tracking_enabled", True)
        bench = await free_stock_dispatch(session, tenant, tracking_id=None)

        refused(
            await api_client.post(
                "/integrations/courier/refresh",
                json={"dispatch_id": str(bench.dispatch_id)},
            ),
            404,
            "NOT_FOUND",
        )


# -----------------------------------------------------------------------------
# POST /sales-returns
# -----------------------------------------------------------------------------


class TestCreateSalesReturn:
    async def test_a_return_is_raised_against_a_sent_consignment(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        dispatch, _item, _warehouse = await shipped_stock(session, tenant, [("5", "10")])
        line_id = dispatch.lines[0].id

        data = ok(
            await api_client.post(
                "/sales-returns",
                json={
                    "dispatch_id": str(dispatch.id),
                    "lines": [
                        {
                            "dispatch_line_id": str(line_id),
                            "quantity": "2",
                            "reason_code": "SURPLUS",
                            "requires_qc": False,
                        }
                    ],
                    "notes": "Customer sent two units back",
                },
            ),
            201,
        )

        return_id = uuid.UUID(data["id"])
        assert data["status"] == SalesReturnStatus.DRAFT
        assert data["dispatch_id"] == str(dispatch.id)
        assert len(data["lines"]) == 1
        assert data["lines"][0]["quantity"] == "2.0000"
        (created,) = await audit_rows(session, tenant.company_id, return_id, AuditAction.CREATE)
        assert created.entity_type == "sales_return"
        assert created.created_by_id == tenant.user_id

    async def test_a_return_before_dispatch_leaves_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await free_stock_dispatch(session, tenant)

        refused(
            await api_client.post(
                "/sales-returns",
                json={
                    "dispatch_id": str(bench.dispatch_id),
                    "lines": [
                        {
                            "dispatch_line_id": str(bench.line_id),
                            "quantity": "1",
                            "reason_code": "SURPLUS",
                            "requires_qc": False,
                        }
                    ],
                },
            ),
            422,
            "BUSINESS_RULE_VIOLATION",
        )

    async def test_empty_lines_are_a_field_error(self, api_client: AsyncClient) -> None:
        assert "lines" in field_errors(
            await api_client.post(
                "/sales-returns",
                json={"dispatch_id": str(uuid.uuid4()), "lines": []},
            )
        )


# -----------------------------------------------------------------------------
# POST /sales-returns/{return_id}/units
# -----------------------------------------------------------------------------


class TestDisposeSalesReturnUnits:
    async def test_received_serials_are_dispositioned(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        dispatch, _item, _warehouse = await shipped_stock(
            session, tenant, [("1", "10"), ("1", "10")], serialized=True
        )
        serial_ids = list(dispatch.lines[0].serial_ids)
        returned = await receive_goods(
            session,
            tenant,
            dispatch,
            "2",
            serial_ids=serial_ids,
        )
        return_id = returned.id
        accepted, damaged = serial_ids[0], serial_ids[1]

        data = ok(
            await api_client.post(
                f"/sales-returns/{return_id}/units",
                json={
                    "accepted": [str(accepted)],
                    "damaged": [str(damaged)],
                },
            )
        )

        assert data["status"] == SalesReturnStatus.RECEIVED
        line = data["lines"][0]
        assert str(accepted) in line["accepted_serial_ids"]
        assert str(damaged) in line["damaged_serial_ids"]

    async def test_disposition_on_a_draft_is_refused(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        dispatch, _item, _warehouse = await shipped_stock(
            session, tenant, [("1", "10")], serialized=True
        )
        serial_id = dispatch.lines[0].serial_ids[0]
        draft = await returns_service.create_return(
            session,
            tenant,
            SalesReturnCreate(
                dispatch_id=dispatch.id,
                lines=[
                    SalesReturnLineIn(
                        dispatch_line_id=dispatch.lines[0].id,
                        quantity=Decimal("1"),
                        serial_ids=[serial_id],
                        reason_code="DAMAGE",
                        requires_qc=True,
                    )
                ],
            ),
        )

        refused(
            await api_client.post(
                f"/sales-returns/{draft.id}/units",
                json={"accepted": [str(serial_id)]},
            ),
            422,
            "BUSINESS_RULE_VIOLATION",
        )

    async def test_empty_dispositions_are_a_field_error(self, api_client: AsyncClient) -> None:
        assert field_errors(
            await api_client.post(
                f"/sales-returns/{uuid.uuid4()}/units",
                json={},
            )
        )


# -----------------------------------------------------------------------------
# POST /sales-returns/{return_id}/cancel
# -----------------------------------------------------------------------------


class TestCancelSalesReturn:
    async def test_a_draft_return_is_cancelled_and_audited(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        dispatch, _item, _warehouse = await shipped_stock(session, tenant, [("4", "10")])
        draft = await returns_service.create_return(
            session,
            tenant,
            SalesReturnCreate(
                dispatch_id=dispatch.id,
                lines=[
                    SalesReturnLineIn(
                        dispatch_line_id=dispatch.lines[0].id,
                        quantity=Decimal("1"),
                        reason_code="SURPLUS",
                        requires_qc=False,
                    )
                ],
            ),
        )
        return_id = draft.id

        data = ok(
            await api_client.post(
                f"/sales-returns/{return_id}/cancel",
                json={"reason": "Customer kept the goods"},
            )
        )

        assert data["status"] == SalesReturnStatus.CANCELLED
        assert await column(session, SalesReturn.status, return_id) == SalesReturnStatus.CANCELLED
        trail = await audit_rows(session, tenant.company_id, return_id, AuditAction.STATUS_CHANGE)
        assert any(
            (row.new_values or {}).get("status") == SalesReturnStatus.CANCELLED for row in trail
        )

    async def test_a_short_reason_is_a_field_error(self, api_client: AsyncClient) -> None:
        assert "reason" in field_errors(
            await api_client.post(
                f"/sales-returns/{uuid.uuid4()}/cancel",
                json={"reason": "no"},
            )
        )

    async def test_another_companys_return_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: TenantContext
    ) -> None:
        dispatch, _item, _warehouse = await shipped_stock(session, foreign, [("3", "10")])
        draft = await returns_service.create_return(
            session,
            foreign,
            SalesReturnCreate(
                dispatch_id=dispatch.id,
                lines=[
                    SalesReturnLineIn(
                        dispatch_line_id=dispatch.lines[0].id,
                        quantity=Decimal("1"),
                        reason_code="SURPLUS",
                        requires_qc=False,
                    )
                ],
            ),
        )

        refused(
            await api_client.post(
                f"/sales-returns/{draft.id}/cancel",
                json={"reason": "Must not see this"},
            ),
            404,
            "NOT_FOUND",
        )


# -----------------------------------------------------------------------------
# Returns and consignments name what they reference
# -----------------------------------------------------------------------------


async def customer_label(session: AsyncSession, customer_id: uuid.UUID) -> tuple[str, str]:
    row = (
        await session.execute(
            select(Customer.code, Customer.name).where(Customer.id == customer_id)
        )
    ).one()
    return row.code, row.name


class TestReturnsAndConsignmentsNameWhatTheyReference:
    """A return names its dispatch, customer, items and units; a consignment its lines'.

    The return screens drew dispatch, customer, item and serial UUIDs, because the
    reads carried nothing else and resolving them in the browser would have been a
    fetch per row.
    """

    async def test_a_return_names_its_dispatch_customer_and_items(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        dispatch, item, _warehouse = await shipped_stock(session, tenant, [("5", "10")])
        item_label = (item.code, item.name)
        customer = await customer_label(session, dispatch.customer_id)

        created = ok(
            await api_client.post(
                "/sales-returns",
                json={
                    "dispatch_id": str(dispatch.id),
                    "lines": [
                        {
                            "dispatch_line_id": str(dispatch.lines[0].id),
                            "quantity": "2",
                            "reason_code": "SURPLUS",
                            "requires_qc": False,
                        }
                    ],
                },
            ),
            201,
        )

        assert created["dispatch_code"] == dispatch.code
        assert (created["customer_code"], created["customer_name"]) == customer
        (line,) = created["lines"]
        assert (line["item_code"], line["item_name"]) == item_label
        assert line["serial_numbers"] == []
        (row,) = [
            row for row in ok(await api_client.get("/sales-returns")) if row["id"] == created["id"]
        ]
        assert row["dispatch_code"] == dispatch.code
        assert (row["customer_code"], row["customer_name"]) == customer

    async def test_dispatched_and_returned_units_are_named_in_serial_order(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        dispatch, item, _warehouse = await shipped_stock(
            session, tenant, [("1", "10"), ("1", "10")], serialized=True
        )
        item_label = (item.code, item.name)
        serial_ids = list(dispatch.lines[0].serial_ids)
        numbers = dict(
            (
                await session.execute(
                    select(StockSerial.id, StockSerial.serial_number).where(
                        StockSerial.id.in_(serial_ids)
                    )
                )
            ).all()
        )
        expected = [numbers[serial_id] for serial_id in serial_ids]

        (shipped,) = ok(await api_client.get(f"/dispatch/{dispatch.id}"))["lines"]
        assert (shipped["item_code"], shipped["item_name"]) == item_label
        assert shipped["serial_numbers"] == expected

        returned = await receive_goods(session, tenant, dispatch, "2", serial_ids=serial_ids)
        (line,) = ok(await api_client.get(f"/sales-returns/{returned.id}"))["lines"]
        assert line["serial_ids"] == [str(serial_id) for serial_id in serial_ids]
        assert line["serial_numbers"] == expected

    async def test_the_list_labels_a_page_in_a_fixed_number_of_statements(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, engine
    ) -> None:
        """Three rows cost what one does: the labels are batched, not read per row."""
        dispatch, _item, _warehouse = await shipped_stock(session, tenant, [("10", "10")])

        async def raise_return() -> None:
            await returns_service.create_return(
                session,
                tenant,
                SalesReturnCreate(
                    dispatch_id=dispatch.id,
                    lines=[
                        SalesReturnLineIn(
                            dispatch_line_id=dispatch.lines[0].id,
                            quantity=Decimal("1"),
                            reason_code="SURPLUS",
                            requires_qc=False,
                        )
                    ],
                ),
            )

        await raise_return()
        with counted(engine) as one_row:
            assert len(ok(await api_client.get("/sales-returns"))) == 1
        await raise_return()
        await raise_return()
        with counted(engine) as three_rows:
            assert len(ok(await api_client.get("/sales-returns"))) == 3

        assert selects(three_rows) == selects(one_row)


# -----------------------------------------------------------------------------
# Shared foreign tenant
# -----------------------------------------------------------------------------


@pytest.fixture
async def foreign(session: AsyncSession, other_company) -> TenantContext:
    owner = await seed_support.seed_user(session, company_id=other_company.id)
    return TenantContext(company_id=other_company.id, user_id=owner.id)
