"""Assigned warehouses constrain whole documents, pagination and direct actions."""

import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from src.core.clock import utcnow
from src.core.deps import get_current_principal
from src.main import app
from src.modules.inventory.models import (
    AdjustmentReason,
    PhysicalCount,
    StockAdjustment,
    StockSerial,
    StockTransfer,
)
from src.modules.inventory.schemas import StockSerialCreate
from src.modules.inventory.service import TraceabilityService
from tests import seed_support
from tests.integration.test_inventory_operations import _bin

pytestmark = [pytest.mark.api, pytest.mark.integration]


@pytest.fixture
async def documents(session, tenant):
    allowed = [
        await seed_support.seed_warehouse(
            session, company_id=tenant.company_id, code=f"S-{uuid.uuid4().hex[:8]}"
        )
        for _ in range(2)
    ]
    outside = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=f"S-{uuid.uuid4().hex[:8]}"
    )
    other = await seed_support.seed_company(session)
    foreign = await seed_support.seed_warehouse(
        session, company_id=other.id, code=f"S-{uuid.uuid4().hex[:8]}"
    )
    foreign_destination = await seed_support.seed_warehouse(
        session, company_id=other.id, code=f"S-{uuid.uuid4().hex[:8]}"
    )
    groups = {"transfers": [], "adjustments": [], "physical-counts": []}
    for company_id, source, destination in [
        (tenant.company_id, allowed[0].id, allowed[1].id),
        (tenant.company_id, allowed[0].id, outside.id),
        (tenant.company_id, outside.id, allowed[0].id),
        (other.id, foreign.id, foreign_destination.id),
    ]:
        common = {
            "company_id": company_id,
            "code": f"DOC-{uuid.uuid4().hex[:8]}",
            "reason": "Warehouse scope regression",
        }
        rows = [
            StockTransfer(
                **common,
                from_warehouse_id=source,
                to_warehouse_id=destination,
                transfer_date=utcnow().date(),
            ),
            StockAdjustment(
                **common,
                warehouse_id=destination,
                adjustment_date=utcnow().date(),
                reason_code=AdjustmentReason.CORRECTION,
            ),
            PhysicalCount(**common, warehouse_id=destination, count_date=utcnow().date()),
        ]
        session.add_all(rows)
        await session.flush()
        for key, row in zip(groups, rows, strict=True):
            groups[key].append(row.id)
    return tuple(row.id for row in allowed), groups


@pytest.mark.parametrize("kind", ["transfers", "adjustments", "physical-counts"])
async def test_list_pagination_and_direct_documents_obey_scope(
    api_client, principal, documents, kind
):
    allowed, groups = documents
    app.dependency_overrides[get_current_principal] = lambda: replace(
        principal, allowed_warehouse_ids=allowed
    )
    response = await api_client.get(f"/inventory/{kind}", params={"limit": 1})
    assert response.status_code == 200
    body = response.json()
    # Single-store documents can see the return-to-allowed row; transfers need both endpoints.
    visible = [groups[kind][0]] if kind == "transfers" else [groups[kind][0], groups[kind][2]]
    assert body["pagination"]["total"] == len(visible)
    pages = []
    for page in range(1, len(visible) + 1):
        result = await api_client.get(f"/inventory/{kind}", params={"limit": 1, "page": page})
        pages.extend(row["id"] for row in result.json()["data"])
    assert set(pages) == {str(value) for value in visible}
    assert (await api_client.get(f"/inventory/{kind}/{visible[0]}")).status_code == 200
    hidden = [groups[kind][1], groups[kind][3]]
    if kind == "transfers":
        hidden.append(groups[kind][2])
    for document_id in hidden:
        assert (await api_client.get(f"/inventory/{kind}/{document_id}")).status_code == 404
        assert (
            await api_client.post(
                f"/inventory/{kind}/{document_id}/cancel", json={"reason": "Scope probe"}
            )
        ).status_code == 404
    if kind == "physical-counts":
        assert (await api_client.get(f"/inventory/{kind}/{hidden[0]}/lines")).status_code == 404


@pytest.mark.parametrize("superuser", [False, True])
async def test_unrestricted_and_superuser_see_own_company_only(
    api_client, principal, documents, superuser
):
    allowed, groups = documents
    scope = allowed if superuser else ()
    app.dependency_overrides[get_current_principal] = lambda: replace(
        principal, allowed_warehouse_ids=scope, is_superuser=superuser
    )
    for kind, ids in groups.items():
        result = await api_client.get(f"/inventory/{kind}")
        assert result.status_code == 200
        assert {row["id"] for row in result.json()["data"]} == {str(value) for value in ids[:3]}
        assert (await api_client.get(f"/inventory/{kind}/{ids[3]}")).status_code == 404


# -----------------------------------------------------------------------------
# The serial register: which unit sits in which store is a stock position too
# -----------------------------------------------------------------------------


@pytest.fixture
async def register(session, tenant, unique_code) -> SimpleNamespace:
    """A serialised item: a unit in an assigned store, one in another, one not yet placed."""
    item = await seed_support.seed_item(
        session, company_id=tenant.company_id, code=unique_code("SER"), is_serial_tracked=True
    )
    item_id = item.id
    allowed, outside = [
        await seed_support.seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("S")
        )
        for _ in range(2)
    ]
    outside_bin = await _bin(session, tenant.company_id, outside, unique_code)
    traceability = TraceabilityService(session, tenant)
    units = {}
    for name, warehouse_id in (
        ("allowed", allowed.id),
        ("outside", outside.id),
        ("unplaced", None),
    ):
        unit = await traceability.create_serial(
            StockSerialCreate(
                item_id=item_id, serial_number=unique_code("SN"), warehouse_id=warehouse_id
            )
        )
        units[name] = str(unit.id)
    return SimpleNamespace(
        item_id=str(item_id),
        allowed=allowed.id,
        outside=str(outside.id),
        outside_bin=str(outside_bin.id),
        units=units,
    )


def restricted_to(principal, *warehouse_ids, superuser: bool = False) -> None:
    app.dependency_overrides[get_current_principal] = lambda: replace(
        principal, allowed_warehouse_ids=tuple(warehouse_ids), is_superuser=superuser
    )


async def test_a_restricted_user_reads_only_their_stores_units_and_unplaced_labels(
    api_client, principal, register
):
    restricted_to(principal, register.allowed)
    visible = {register.units["allowed"], register.units["unplaced"]}

    listed = await api_client.get("/inventory/serials", params={"item_id": register.item_id})
    assert listed.status_code == 200
    assert {row["id"] for row in listed.json()["data"]} == visible
    elsewhere = await api_client.get(
        "/inventory/serials", params={"warehouse_id": register.outside}
    )
    assert (elsewhere.status_code, elsewhere.json()["data"]) == (200, [])

    assert (
        await api_client.get(f"/inventory/serials/{register.units['outside']}")
    ).status_code == 404
    for serial_id in visible:
        assert (await api_client.get(f"/inventory/serials/{serial_id}")).status_code == 200

    labels = "/inventory/serials/labels"
    hidden = await api_client.post(labels, json={"serial_ids": list(register.units.values())})
    assert hidden.status_code == 404
    shown = await api_client.post(labels, json={"serial_ids": sorted(visible)})
    assert (shown.status_code, shown.headers["content-type"]) == (200, "application/pdf")


async def test_a_restricted_user_cannot_register_units_in_another_store(
    api_client, session, principal, register, unique_code
):
    restricted_to(principal, register.allowed)

    for position in ({"warehouse_id": register.outside}, {"location_id": register.outside_bin}):
        response = await api_client.post(
            "/inventory/serials",
            json={"item_id": register.item_id, "serial_number": unique_code("SN"), **position},
        )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "SCOPE_DENIED"
    bulk = await api_client.post(
        "/inventory/serials/bulk",
        json={
            "item_id": register.item_id,
            "serial_numbers": [unique_code("SN"), unique_code("SN")],
            "warehouse_id": register.outside,
        },
    )
    assert bulk.status_code == 403, bulk.text
    assert bulk.json()["error"]["code"] == "SCOPE_DENIED"
    registered = await session.scalar(
        select(func.count())
        .select_from(StockSerial)
        .where(StockSerial.item_id == uuid.UUID(register.item_id))
    )
    assert registered == 3

    created = await api_client.post(
        "/inventory/serials",
        json={
            "item_id": register.item_id,
            "serial_number": unique_code("SN"),
            "warehouse_id": str(register.allowed),
        },
    )
    assert created.status_code == 201, created.text


@pytest.mark.parametrize("superuser", [False, True])
async def test_unrestricted_and_superuser_read_and_register_every_store(
    api_client, principal, register, unique_code, superuser
):
    restricted_to(principal, *((register.allowed,) if superuser else ()), superuser=superuser)

    listed = await api_client.get("/inventory/serials", params={"item_id": register.item_id})
    assert {row["id"] for row in listed.json()["data"]} == set(register.units.values())
    assert (
        await api_client.get(f"/inventory/serials/{register.units['outside']}")
    ).status_code == 200
    created = await api_client.post(
        "/inventory/serials",
        json={
            "item_id": register.item_id,
            "serial_number": unique_code("SN"),
            "location_id": register.outside_bin,
        },
    )
    assert created.status_code == 201, created.text
