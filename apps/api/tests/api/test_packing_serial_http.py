"""Packing's own read supplies operational facts without unrelated master rights."""

from dataclasses import replace

import pytest

from src.core.deps import get_current_principal
from src.core.permissions import Permission
from tests.integration.test_packing_final_serials import legacy_sealed_carton, make_packing_kit

pytestmark = pytest.mark.integration


async def test_packing_rights_can_read_flags_start_named_qc_and_cancel_without_item_or_qc_view(
    api_client,
    session,
    tenant,
    principal,
):
    k = await make_packing_kit(session, tenant)
    restricted = replace(
        principal,
        permissions=frozenset(
            {
                str(Permission.PACKING_VIEW),
                str(Permission.PACKING_MANAGE),
            }
        ),
    )
    app = api_client._transport.app
    app.dependency_overrides[get_current_principal] = lambda: restricted
    read = await api_client.get(f"/packing/{k.order_id}")
    assert read.status_code == 200
    line = read.json()["data"]["lines"][0]
    assert line["is_serial_tracked"] is True
    assert line["requires_final_qc"] is True
    assert line["is_batch_tracked"] is False
    assert line["qc_inspection_status"] is None
    assert (await api_client.get(f"/products/items/{k.item_id}")).status_code == 403
    started = await api_client.post(
        f"/packing/{k.order_id}/start",
        json={
            "reason": "Packing operator scans final inspection units",
            "serial_selections": [
                {"line_id": str(k.line_id), "serial_ids": [str(unit) for unit in k.ids[:2]]}
            ],
        },
    )
    assert started.status_code == 200, started.text
    line = started.json()["data"]["lines"][0]
    assert line["qc_inspection_status"] == "PENDING"
    assert line["qc_serial_ids"] == [str(unit) for unit in k.ids[:2]]
    inspection_id = line["qc_inspection_id"]
    assert (await api_client.get(f"/quality/inspections/{inspection_id}")).status_code == 403
    cancelled = await api_client.post(
        f"/packing/{k.order_id}/cancel",
        json={
            "reason": "Customer cancels before the inspection begins",
        },
    )
    assert cancelled.status_code == 200, cancelled.text
    assert (await api_client.get(f"/packing/{k.order_id}")).json()["data"]["lines"][0][
        "qc_inspection_status"
    ] == "WITHDRAWN"


async def test_legacy_recovery_requires_manage_permission_and_an_audit_reason(
    api_client,
    session,
    tenant,
    principal,
):
    k = await make_packing_kit(session, tenant)
    carton_id = await legacy_sealed_carton(k, packed=True)
    path = f"/packing/{k.order_id}/cartons/{carton_id}/recover-legacy"
    app = api_client._transport.app
    readonly = replace(principal, permissions=frozenset({str(Permission.PACKING_VIEW)}))
    app.dependency_overrides[get_current_principal] = lambda: readonly
    assert (
        await api_client.post(path, json={"reason": "Recover reusable legacy material"})
    ).status_code == 403
    manager = replace(principal, permissions=frozenset({str(Permission.PACKING_MANAGE)}))
    app.dependency_overrides[get_current_principal] = lambda: manager
    assert (await api_client.post(path, json={})).status_code == 422
    response = await api_client.post(
        path, json={"reason": "Counted legacy goods and recovered reusable box"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["is_sealed"] is False
    assert response.json()["data"]["material_transaction_id"] is None
